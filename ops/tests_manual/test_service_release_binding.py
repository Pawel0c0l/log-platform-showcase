#!/usr/bin/env python3
"""The two long-running services must execute the ACTIVE RELEASE, not the checkout.

    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" \\
        .venv/bin/python ops/tests_manual/test_service_release_binding.py

Sibling of `test_release_runtime_isolation.py`, which proved the same property
for the oneshot alerting sidecars. The gap this file closes is the one the Portal
V1 rollout exposed: `log-platform-api.service` and `database-export-worker.service`
resolved their Python source from
`/opt/log-platform`, so `current` could move to a new
release and those two processes would still serve whatever the development
worktree happened to contain at their next restart. The serving bytes matching
the deployed release was a coincidence of timing, not a property of the system.

Everything here is hermetic. Disposable release roots are built under a temporary
directory with `git archive`, and the real production pointer at
`/opt/log-platform-release` is never read or written.
No unit is installed, no daemon is reloaded and no service is restarted.

Two kinds of evidence are collected, because either alone is weak:

*   **resolution** — a probe module reports `importlib.util.find_spec` origins,
    which locates a file without executing its body. This answers "which tree
    would an import land in?" for `api.main` and `ops.database_export_worker`.
*   **real execution** — the units' own ExecStart argv is read out of the unit
    files and run for real (`uvicorn --version`, `ops.database_export_worker
    --help`). Both exit 0 only if the entire production import graph loaded out
    of the release, which a path assertion cannot show.

Why the A/B releases carry a marker module rather than differing by commit: the
property under test is "a newly started process takes its code from whatever
`current` names", and a marker whose CONTENT differs per release proves content
selection, not merely that two paths are spelled differently.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PROPOSED = REPO_ROOT / "ops/systemd/proposed"
LAUNCHER = PROPOSED / "log-ops-runner.sh"
PRODUCTION_RELEASE_ROOT = "/opt/log-platform-release"
DEV_TREE = Path("/opt/log-platform")
VENV = DEV_TREE / ".venv"
DEV_ENV = DEV_TREE / ".env"

# Twelve hex characters, because the launcher refuses anything that is not
# release-shaped and that refusal is one of the things under test.
RELEASE_A = "aaaaaaaaaaaa"
RELEASE_B = "bbbbbbbbbbbb"

# The units this file exists for, and the release-relative paths each one
# declares as its prerequisite. Read from the unit files rather than restated,
# so a unit that stops declaring them fails here instead of drifting silently.
API_UNIT = PROPOSED / "log-platform-api.service"
WORKER_UNIT = PROPOSED / "database-export-worker.service"
CLEANUP_UNIT = PROPOSED / "database-export-cleanup.service"

# Modules whose provenance decides whether the hardening is real.
PROBED_MODULES = ("api.main", "ops.database_export_worker", "api.platform_prune")

PROBE_MODULE = "ops._service_release_binding_probe"
MARKER_MODULE = "ops._service_release_marker"
PROBE_SOURCE = '''
"""Disposable provenance probe, written into a throwaway release tree."""
import importlib.util
import json
import os
import sys

import ops._service_release_marker as marker

out = {
    "cwd": os.getcwd(),
    "sys_path0": sys.path[0],
    "pythonpath": os.environ.get("PYTHONPATH"),
    "marker": marker.RELEASE_MARKER,
    "marker_origin": marker.__file__,
    "modules": {},
}
for name in %(modules)r:
    spec = importlib.util.find_spec(name)
    out["modules"][name] = spec.origin if spec else None
print(json.dumps(out))
''' % {"modules": list(PROBED_MODULES)}


# --------------------------------------------------------------- scaffolding

def git(*args: str) -> bytes:
    return subprocess.run(["git", "-C", str(REPO_ROOT), *args],
                          capture_output=True, check=True).stdout


def unit_directives(path: Path) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    section = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            out.append((section, key.strip(), value.strip()))
    return out


def unit_value(path: Path, key: str, prefix: str = "") -> str:
    found = [v for sec, k, v in unit_directives(path)
             if sec == "Service" and k == key and v.startswith(prefix)]
    assert len(found) == 1, f"{path.name}: expected one {key}{'=' + prefix if prefix else ''}, got {found}"
    return found[0]


def unit_exec_args(path: Path) -> list[str]:
    """Everything the unit passes to the launcher, launcher path excluded."""
    return unit_value(path, "ExecStart").split()[1:]


def unit_requirements(path: Path) -> str:
    return unit_value(path, "Environment", "OPS_RUNNER_REQUIRE_RELEASE_FILE=").partition("=")[2]


def materialize(commit: str, target: Path, marker: str) -> None:
    """Extract one commit into `target`, the way prepare_release does."""
    target.mkdir(parents=True)
    archive = git("archive", "--format=tar", commit)
    extract = subprocess.run(["tar", "-x", "-f", "-", "-C", str(target)],
                             input=archive, capture_output=True, check=False)
    assert extract.returncode == 0, extract.stderr.decode("utf-8", "replace")[:400]
    # The runtime resources, linked exactly as ops/release_boundary.py links
    # them: shared, not copied, and not release content. `.env` matters here —
    # the API declares it as a prerequisite because api/platform_prune.py reads
    # it in-process.
    (target / ".venv").symlink_to(VENV)
    (target / ".env").symlink_to(DEV_ENV)
    # Additive: one module that exists in neither real release and changes how
    # nothing else resolves.
    (target / "ops" / f"{MARKER_MODULE.split('.')[-1]}.py").write_text(
        f'RELEASE_MARKER = {marker!r}\n', encoding="utf-8")
    (target / "ops" / f"{PROBE_MODULE.split('.')[-1]}.py").write_text(
        PROBE_SOURCE, encoding="utf-8")


def seal(tree: Path) -> None:
    """Drop write bits, as ops/release_boundary.py does to a real release.

    Sealed on purpose rather than for realism theatre: a read-only tree is what
    forces PYTHONPYCACHEPREFIX to be correct, so a launcher that forgot it would
    fail here instead of silently recompiling on every restart.
    """
    for entry in sorted(tree.rglob("*"), reverse=True):
        if entry.is_symlink():
            continue
        entry.chmod(entry.stat().st_mode & ~0o222)
    tree.chmod(tree.stat().st_mode & ~0o222)


def unseal(root: Path) -> None:
    for entry in root.rglob("*"):
        if entry.is_symlink():
            continue
        try:
            entry.chmod(entry.stat().st_mode | stat.S_IWUSR)
        except OSError:
            pass
    root.chmod(root.stat().st_mode | stat.S_IWUSR)


def build_release_root(tmp: Path) -> tuple[Path, Path]:
    """A disposable release root plus a launcher copy pinned to it."""
    root = tmp / "log-platform-release"
    (root / "releases").mkdir(parents=True)
    for release_id in (RELEASE_A, RELEASE_B):
        tree = root / "releases" / release_id
        materialize("HEAD", tree, release_id)
        seal(tree)

    launcher = tmp / "log-ops-runner.sh"
    text = LAUNCHER.read_text(encoding="utf-8")
    assert f'RELEASE_ROOT="{PRODUCTION_RELEASE_ROOT}"' in text, "launcher lost its pinned root"
    launcher.write_text(
        text.replace(f'RELEASE_ROOT="{PRODUCTION_RELEASE_ROOT}"', f'RELEASE_ROOT="{root}"'),
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    return root, launcher


def point_current_at(root: Path, release_id: str) -> None:
    """Atomic pointer swap, the same rename ops/release_boundary.py performs."""
    staging = root / ".current.staging"
    if staging.is_symlink() or staging.exists():
        staging.unlink()
    staging.symlink_to(Path("releases") / release_id)
    os.replace(staging, root / "current")


def run_launcher(launcher: Path, args: list[str], *,
                 requirements: str | None = None,
                 timeout: int = 180) -> subprocess.CompletedProcess:
    environment = dict(os.environ)
    # Reproduce the hostile condition the launcher exists to survive:
    # /etc/log-platform/runtime.env exports a PYTHONPATH naming the development
    # tree, and EnvironmentFile= beats Environment=. If the launcher merely
    # prepended, `ops` being a namespace package would let modules leak in.
    environment["PYTHONPATH"] = str(DEV_TREE)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment.pop("OPS_RUNNER_REQUIRE_RELEASE_FILE", None)
    if requirements is not None:
        environment["OPS_RUNNER_REQUIRE_RELEASE_FILE"] = requirements
    return subprocess.run([str(launcher), *args], capture_output=True, text=True,
                          env=environment, timeout=timeout)


def probe(launcher: Path, *, requirements: str | None = None) -> dict:
    completed = run_launcher(launcher, [PROBE_MODULE], requirements=requirements)
    assert completed.returncode == 0, (
        f"launcher exited {completed.returncode}: {completed.stderr[-600:]}")
    return json.loads(completed.stdout)


def assert_serves(report: dict, release_dir: Path, marker: str) -> None:
    for name in PROBED_MODULES:
        origin = report["modules"].get(name)
        assert origin, f"{name} did not resolve at all"
        resolved = Path(origin).resolve()
        assert resolved.is_relative_to(release_dir), f"{name} resolved to {resolved}"
        assert not resolved.is_relative_to(DEV_TREE), f"{name} leaked out of the release: {resolved}"
    assert report["marker"] == marker, f"served {report['marker']}, expected {marker}"
    assert Path(report["cwd"]).resolve() == release_dir, report["cwd"]
    assert Path(report["pythonpath"]).resolve() == release_dir, report["pythonpath"]


def launched_release(stderr: str) -> str:
    """The release id the launcher reported on its own stderr line."""
    for line in stderr.splitlines():
        if line.startswith("log-ops-runner release="):
            return line.split("release=", 1)[1].split()[0]
    raise AssertionError(f"launcher printed no release identity:\n{stderr[-600:]}")


# --------------------------------------------------------------------- tests

def test_units_no_longer_name_the_development_tree_as_code_root(_: Path, __: Path) -> None:
    """The defect, stated as the assertion that would have caught it.

    Before this slice both units carried `WorkingDirectory=<dev tree>`,
    `Environment=PYTHONPATH=<dev tree>` and an ExecStart naming the checkout's
    own interpreter. Any one of the three was sufficient to make the release
    pointer irrelevant to what the process executed.
    """
    for unit in (API_UNIT, WORKER_UNIT, CLEANUP_UNIT):
        assert unit.exists(), f"{unit.name} is not repository-managed"
        for section, key, value in unit_directives(unit):
            if section != "Service":
                continue
            if key in {"ExecStart", "WorkingDirectory"} or (
                    key == "Environment" and value.startswith("PYTHONPATH=")):
                payload = value.partition("=")[2] if key == "Environment" else value
                for token in payload.split():
                    assert token != str(DEV_TREE) and not token.startswith(f"{DEV_TREE}/"), (
                        f"{unit.name}: {key} still names the development tree ({token})")
        assert unit_value(unit, "ExecStart").startswith("/usr/local/bin/log-ops-runner.sh "), (
            f"{unit.name}: ExecStart does not go through the release launcher")
    print("PASS: no long-running unit names the development tree as its code root")


def test_activation_selects_release_a(root: Path, launcher: Path) -> None:
    point_current_at(root, RELEASE_A)
    assert_serves(probe(launcher), (root / "releases" / RELEASE_A).resolve(), RELEASE_A)
    print("PASS: current -> A, a newly started process serves A")


def test_activation_to_release_b_changes_served_code(root: Path, launcher: Path) -> None:
    """Acceptance: activation followed by a restart loads the new release.

    No unit is edited between the two halves of this test — only `current`
    moves, which is the whole contract.
    """
    point_current_at(root, RELEASE_A)
    before = probe(launcher)
    point_current_at(root, RELEASE_B)
    after = probe(launcher)
    assert_serves(before, (root / "releases" / RELEASE_A).resolve(), RELEASE_A)
    assert_serves(after, (root / "releases" / RELEASE_B).resolve(), RELEASE_B)
    assert before["marker"] != after["marker"]
    print("PASS: A -> B activation changes what a newly started process executes")


def test_rollback_restores_release_a(root: Path, launcher: Path) -> None:
    """Acceptance: rollback + restart restores the previous release's code."""
    sequence = [RELEASE_A, RELEASE_B, RELEASE_A]
    served = []
    for release_id in sequence:
        point_current_at(root, release_id)
        report = probe(launcher)
        assert_serves(report, (root / "releases" / release_id).resolve(), release_id)
        served.append(report["marker"])
    assert served == sequence, served
    print("PASS: B -> A rollback restores A, unit unchanged")


def test_editing_the_development_tree_cannot_change_what_runs(root: Path, launcher: Path) -> None:
    """Invariant 4: the mutable worktree is not a production code source.

    Approximated safely rather than by editing the real repository: the launcher
    is run with PYTHONPATH already naming the development tree — the exact
    condition /etc/log-platform/runtime.env creates — and every probed module
    must still resolve inside the release. A launcher that prepended instead of
    setting, or that inherited a working directory, would fail here.
    """
    point_current_at(root, RELEASE_B)
    report = probe(launcher)
    assert_serves(report, (root / "releases" / RELEASE_B).resolve(), RELEASE_B)
    assert Path(report["sys_path0"]).resolve() == (root / "releases" / RELEASE_B).resolve(), (
        report["sys_path0"])
    # The marker module exists in the release and nowhere in the checkout, so a
    # leak in the other direction would be equally visible.
    assert not (DEV_TREE / "ops" / f"{MARKER_MODULE.split('.')[-1]}.py").exists()
    print("PASS: a development tree on PYTHONPATH cannot supply the served modules")


def test_real_unit_commands_start_from_the_release(root: Path, launcher: Path) -> None:
    """The units' own ExecStart argv, executed for real against each release.

    Resolution assertions show which file an import WOULD find. These show the
    whole production import graph actually loading out of the release: uvicorn is
    reached through the release's runtime symlink, and the worker's `--help` path
    imports `api.main` before argparse ever sees the flag.
    """
    api_args = unit_exec_args(API_UNIT)
    assert api_args[0] == "uvicorn" and "api.main:app" in api_args, api_args
    worker_args = unit_exec_args(WORKER_UNIT)
    assert worker_args[0] == "ops.database_export_worker", worker_args

    for release_id in (RELEASE_A, RELEASE_B):
        point_current_at(root, release_id)

        api = run_launcher(launcher, ["uvicorn", "--version"],
                           requirements=unit_requirements(API_UNIT))
        assert api.returncode == 0, f"API entrypoint failed: {api.stderr[-600:]}"
        assert launched_release(api.stderr) == release_id, api.stderr[-400:]

        worker = run_launcher(launcher, [worker_args[0], "--help"],
                              requirements=unit_requirements(WORKER_UNIT))
        assert worker.returncode == 0, f"worker entrypoint failed: {worker.stderr[-600:]}"
        assert launched_release(worker.stderr) == release_id, worker.stderr[-400:]
        assert "--cleanup-only" in worker.stdout, worker.stdout[:300]
    print("PASS: the units' real ExecStart argv runs out of whichever release is active")


def test_missing_or_invalid_release_fails_closed(root: Path, launcher: Path) -> None:
    """Fail closed, and never fall back to the development tree.

    The development-tree case is the important one: without the release-shaped
    path assertion, `ln -sfn <dev tree> current` satisfies every other guard and
    silently undoes this whole slice — which is exactly the state production was
    in before it.
    """
    current = root / "current"

    point_current_at(root, RELEASE_A)
    assert probe(launcher)["marker"] == RELEASE_A, "sanity: a healthy pointer works"

    current.unlink()
    missing = run_launcher(launcher, [PROBE_MODULE])
    assert missing.returncode == 90, missing.returncode
    assert "RELEASE_POINTER_INVALID" in missing.stderr
    assert str(DEV_TREE) not in missing.stdout

    current.symlink_to(DEV_TREE)
    dev = run_launcher(launcher, [PROBE_MODULE])
    assert dev.returncode == 90, f"a development tree was accepted as a release: {dev.returncode}"
    assert "is not a release under" in dev.stderr
    current.unlink()

    point_current_at(root, "ffffffffffff")
    dangling = run_launcher(launcher, [PROBE_MODULE])
    assert dangling.returncode == 90, dangling.returncode
    print("PASS: a missing, dangling or development-tree pointer is refused, not followed")


def test_a_release_that_cannot_serve_the_unit_is_refused(root: Path, launcher: Path) -> None:
    """Part D: a valid pointer to an incomplete release must also fail closed.

    `Restart=on-failure` is what makes the distinct exit code matter. Without the
    check the API restarts every 5s with exit 1 — indistinguishable from an
    ordinary crash — against a release that structurally cannot serve it.
    """
    point_current_at(root, RELEASE_A)

    absent = run_launcher(launcher, [PROBE_MODULE], requirements="api/main.py:ops/not_here.py")
    assert absent.returncode == 92, absent.returncode
    assert "RELEASE_ENTRYPOINT_MISSING" in absent.stderr

    # A requirement satisfiable from outside the release would defeat the
    # boundary it reinforces, so both escape shapes are refused outright.
    for bad in ("/etc/passwd", "../../etc/passwd"):
        escaped = run_launcher(launcher, [PROBE_MODULE], requirements=bad)
        assert escaped.returncode == 2, (bad, escaped.returncode)
        assert "OPS_RUNNER_REQUIREMENT_INVALID" in escaped.stderr, bad

    # And the real declarations must be satisfiable by a real release.
    for unit in (API_UNIT, WORKER_UNIT, CLEANUP_UNIT):
        ok = run_launcher(launcher, [PROBE_MODULE], requirements=unit_requirements(unit))
        assert ok.returncode == 0, f"{unit.name}: {ok.stderr[-400:]}"
    print("PASS: an incomplete release is refused with a distinct, non-restarting failure")


def test_runtime_resource_contract_is_shared_not_release_versioned(root: Path, launcher: Path) -> None:
    """Part H: CODE is release-bound; the runtime environment deliberately is not.

    `.env` and `.venv` stay symlinks into the host-managed checkout, so a code
    rollback moves Python source and nothing else. That is the existing contract
    and this slice does not change it — but it must be explicit, because it is
    also the boundary of what a rollback can undo.
    """
    point_current_at(root, RELEASE_A)
    tree = root / "releases" / RELEASE_A
    for name, target in ((".env", DEV_ENV), (".venv", VENV)):
        link = tree / name
        assert link.is_symlink(), f"{name} must remain a runtime link, not release content"
        assert Path(os.readlink(link)) == target, (name, os.readlink(link))

    # A dangling runtime link is a real failure mode: load_dotenv() on an absent
    # path is a silent no-op, so the API would start and resolve the wrong
    # environment identity. `-e` follows symlinks, which is what catches it.
    report = probe(launcher, requirements=unit_requirements(API_UNIT))
    assert report["marker"] == RELEASE_A
    broken = root / "releases" / RELEASE_B
    broken.chmod(broken.stat().st_mode | stat.S_IWUSR)
    (broken / ".env").unlink()
    (broken / ".env").symlink_to(broken / "no-such-env")
    point_current_at(root, RELEASE_B)
    dangled = run_launcher(launcher, [PROBE_MODULE], requirements=unit_requirements(API_UNIT))
    assert dangled.returncode == 92, dangled.returncode
    assert "RELEASE_ENTRYPOINT_MISSING" in dangled.stderr
    # Restore, so ordering between tests cannot matter.
    (broken / ".env").unlink()
    (broken / ".env").symlink_to(DEV_ENV)
    broken.chmod(broken.stat().st_mode & ~0o222)
    print("PASS: .env/.venv remain shared runtime links, and a dangling one fails closed")


def main() -> int:
    assert VENV.is_dir(), f"missing dependency environment: {VENV}"
    assert DEV_ENV.exists(), f"missing runtime environment file: {DEV_ENV}"
    tmp = Path(tempfile.mkdtemp(prefix="service-release-binding-"))
    try:
        root, launcher = build_release_root(tmp)
        test_units_no_longer_name_the_development_tree_as_code_root(root, launcher)
        test_activation_selects_release_a(root, launcher)
        test_activation_to_release_b_changes_served_code(root, launcher)
        test_rollback_restores_release_a(root, launcher)
        test_editing_the_development_tree_cannot_change_what_runs(root, launcher)
        test_real_unit_commands_start_from_the_release(root, launcher)
        test_missing_or_invalid_release_fails_closed(root, launcher)
        test_a_release_that_cannot_serve_the_unit_is_refused(root, launcher)
        test_runtime_resource_contract_is_shared_not_release_versioned(root, launcher)
    finally:
        unseal(tmp)
        shutil.rmtree(tmp, ignore_errors=True)
    print("OK - long-running services are bound to the active release")
    return 0


if __name__ == "__main__":
    sys.exit(main())
