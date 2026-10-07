#!/usr/bin/env python3
"""B1-R: prove the alerting sidecars follow the release pointer, not a checkout.

    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" \
        .venv/bin/python ops/tests_manual/test_release_runtime_isolation.py

Everything here is hermetic. Disposable release roots are built under a
temporary directory with `git archive`, and the real production pointer at
/opt/log-platform-release is never read or written.

The launcher pins its release root as a literal, on purpose: an environment
variable that could repoint it would be a bypass of the exact boundary it
exists to enforce. So these tests run a *copy* of the script with that one
literal rewritten, and separately assert that the shipped script still names the
production path — which is what `test_systemd_unit_contract.py` checks.

Resolution is observed with `importlib.util.find_spec`, which locates a module
without executing its body. Importing `ops.execution_watchdog` for real would
open a database connection path and pull in the dispatcher; the question here is
only *which file* an import would resolve to.
"""
from __future__ import annotations

import ast
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = REPO_ROOT / "ops/systemd/proposed/log-ops-runner.sh"
PRODUCTION_RELEASE_ROOT = "/opt/log-platform-release"
DEV_TREE = Path("/opt/log-platform")
VENV = DEV_TREE / ".venv"

BASELINE_COMMIT = "b682df90c95853f6e04ad567f8e016e11f1e5047"
CANDIDATE_COMMIT = "1bec254d67ac03a7bbeb9cd5b450181439f04482"
BASELINE_ID = BASELINE_COMMIT[:12]
CANDIDATE_ID = CANDIDATE_COMMIT[:12]

# Modules whose provenance decides whether B1 is actually deployed.
PROBED_MODULES = (
    "ops.suspected_bug_email_worker",
    "ops.execution_watchdog",
    "ops.operational_alert",
    "api.suspected_bug",
)

# The B1 watchdog subject. Present in the candidate's expectation file, absent
# from the baseline's — which is what makes "the expectations followed the
# release" an observable fact rather than an assertion about intent.
B1_HEARTBEAT_SUBJECT = "alerting_email_worker"

PROBE_MODULE = "ops._b1r_release_probe"
PROBE_SOURCE = '''
"""Disposable provenance probe, written into a throwaway release tree."""
import importlib.util
import json
import os
import sys

out = {"cwd": os.getcwd(), "sys_path0": sys.path[0],
       "pythonpath": os.environ.get("PYTHONPATH"), "modules": {}}
for name in %(modules)r:
    spec = importlib.util.find_spec(name)
    out["modules"][name] = spec.origin if spec else None
print(json.dumps(out))
''' % {"modules": list(PROBED_MODULES)}


# --------------------------------------------------------------- scaffolding

def git(*args: str) -> bytes:
    return subprocess.run(["git", "-C", str(REPO_ROOT), *args],
                          capture_output=True, check=True).stdout


def materialize(commit: str, target: Path) -> None:
    """Extract one commit into `target`, the way prepare_release does."""
    target.mkdir(parents=True)
    archive = git("archive", "--format=tar", commit)
    extract = subprocess.run(["tar", "-x", "-f", "-", "-C", str(target)],
                             input=archive, capture_output=True, check=False)
    assert extract.returncode == 0, extract.stderr.decode("utf-8", "replace")[:400]
    # The dependency environment, linked exactly as the real release layout
    # links it. Deliberately no `.env`: the sidecars must not need one.
    (target / ".venv").symlink_to(VENV)


def seal(tree: Path) -> None:
    """Drop write bits, as ops/release_boundary.py does to a real release.

    Sealed on purpose rather than for realism theatre: a read-only tree is what
    forces PYTHONPYCACHEPREFIX to be correct, so a launcher that forgot it would
    fail here instead of silently recompiling on every production fire.
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
    for commit, release_id in ((BASELINE_COMMIT, BASELINE_ID),
                               (CANDIDATE_COMMIT, CANDIDATE_ID)):
        tree = root / "releases" / release_id
        materialize(commit, tree)
        # The probe is additive: it adds one module to a throwaway copy and
        # changes nothing about how any ops.* module resolves.
        (tree / "ops" / "_b1r_release_probe.py").write_text(PROBE_SOURCE, encoding="utf-8")
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


def run_probe(launcher: Path, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    environment = dict(os.environ)
    # Reproduce the hostile condition the launcher exists to survive:
    # /etc/log-platform/runtime.env exports a PYTHONPATH naming the development
    # tree, and EnvironmentFile= beats Environment=. If the launcher merely
    # prepended, `ops` being a namespace package would let modules leak in from
    # here.
    environment["PYTHONPATH"] = str(DEV_TREE)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment.update(env or {})
    return subprocess.run([str(launcher), PROBE_MODULE], capture_output=True,
                          text=True, env=environment, timeout=120)


def probe(launcher: Path) -> dict:
    completed = run_probe(launcher)
    assert completed.returncode == 0, (
        f"launcher exited {completed.returncode}: {completed.stderr[-600:]}"
    )
    return json.loads(completed.stdout)


def assert_resolves_to(report: dict, release_dir: Path) -> None:
    for name in PROBED_MODULES:
        origin = report["modules"].get(name)
        assert origin, f"{name} did not resolve at all"
        resolved = Path(origin).resolve()
        assert resolved.is_relative_to(release_dir), f"{name} resolved to {resolved}"
        assert not resolved.is_relative_to(DEV_TREE), f"{name} leaked out of the release: {resolved}"
    assert Path(report["cwd"]).resolve() == release_dir, report["cwd"]
    assert Path(report["pythonpath"]).resolve() == release_dir, report["pythonpath"]


# --------------------------------------------------------------------- tests

def test_baseline_release_resolves_baseline_code(root: Path, launcher: Path) -> None:
    """Rollback stays viable: the launcher must run a pre-B1 release unchanged."""
    point_current_at(root, BASELINE_ID)
    report = probe(launcher)
    assert_resolves_to(report, (root / "releases" / BASELINE_ID).resolve())
    worker = Path(report["modules"]["ops.suspected_bug_email_worker"]).read_text(encoding="utf-8")
    assert "HEARTBEAT_COMPONENT" not in worker, "baseline worker should predate the heartbeat"
    print("PASS: baseline release resolves baseline code")


def test_candidate_release_resolves_b1_code(root: Path, launcher: Path) -> None:
    """One release activation delivers every B1 module the sidecars import."""
    point_current_at(root, CANDIDATE_ID)
    report = probe(launcher)
    release_dir = (root / "releases" / CANDIDATE_ID).resolve()
    assert_resolves_to(report, release_dir)

    worker = Path(report["modules"]["ops.suspected_bug_email_worker"]).read_text(encoding="utf-8")
    assert 'HEARTBEAT_COMPONENT = "alerting.email_worker"' in worker
    assert "EXIT_DEAD_LETTER = 3" in worker
    watchdog = Path(report["modules"]["ops.execution_watchdog"]).read_text(encoding="utf-8")
    assert "evaluate_alert_delivery_subject" in watchdog
    alert = Path(report["modules"]["ops.operational_alert"]).read_text(encoding="utf-8")
    assert "INCIDENT_ALERT_DELIVERY_FAILED" in alert
    api = Path(report["modules"]["api.suspected_bug"]).read_text(encoding="utf-8")
    assert "CONFIGURATION_SUPPRESSION_REASONS" in api
    print("PASS: the B1 release resolves B1 worker, watchdog, alert and api code")


def test_watchdog_expectations_follow_the_release(root: Path, launcher: Path) -> None:
    """Acceptance B: activating a release changes the expectation set.

    The watchdog computes DEFAULT_EXPECTATIONS_PATH from REPO_ROOT, which is
    derived from its own __file__. Three facts are asserted together: the module
    still derives the path that way, the module resolves inside the release, and
    the file that formula then selects is the release's own — carrying the B1
    subject for the candidate and not for the baseline.
    """
    for release_id, expect_subject in ((BASELINE_ID, False), (CANDIDATE_ID, True)):
        point_current_at(root, release_id)
        report = probe(launcher)
        module = Path(report["modules"]["ops.execution_watchdog"])
        source = module.read_text(encoding="utf-8")
        assert 'REPO_ROOT = Path(__file__).resolve().parents[1]' in source, (
            "watchdog no longer derives REPO_ROOT from its own __file__"
        )
        assert 'DEFAULT_EXPECTATIONS_PATH = REPO_ROOT / "ops" / "watchdog_expectations.json"' in source, (
            "watchdog no longer derives its expectations path from REPO_ROOT"
        )
        selected = module.resolve().parents[1] / "ops" / "watchdog_expectations.json"
        assert selected.is_relative_to((root / "releases" / release_id).resolve()), selected
        expectations = json.loads(selected.read_text(encoding="utf-8"))
        subjects = {item.get("subject") for item in expectations.get("heartbeats") or []}
        assert (B1_HEARTBEAT_SUBJECT in subjects) is expect_subject, (
            f"{release_id}: {B1_HEARTBEAT_SUBJECT} present={B1_HEARTBEAT_SUBJECT in subjects}"
        )
        has_alert_delivery = isinstance(expectations.get("alert_delivery"), dict)
        assert has_alert_delivery is expect_subject, f"{release_id}: alert_delivery block"
    print("PASS: the expectation set is whatever the active release ships")


def test_pointer_rollback_restores_baseline_without_editing_the_unit(
    root: Path, launcher: Path
) -> None:
    """Acceptance F: baseline -> B1 -> baseline, same command every time.

    The unit's ExecStart is a constant; only `current` moves. A oneshot resolves
    the pointer at invocation, so the fire after a rollback runs baseline code
    with no unit edit and no daemon-reload.
    """
    sequence = [(BASELINE_ID, False), (CANDIDATE_ID, True), (BASELINE_ID, False)]
    seen = []
    for release_id, expect_b1 in sequence:
        point_current_at(root, release_id)
        report = probe(launcher)
        assert_resolves_to(report, (root / "releases" / release_id).resolve())
        worker = Path(report["modules"]["ops.suspected_bug_email_worker"]).read_text(encoding="utf-8")
        assert ("HEARTBEAT_COMPONENT" in worker) is expect_b1, release_id
        seen.append(release_id)
    assert seen == [BASELINE_ID, CANDIDATE_ID, BASELINE_ID]
    print("PASS: rollback resolves baseline code again, unit unchanged")


def test_launcher_refuses_anything_that_is_not_a_release(root: Path, launcher: Path) -> None:
    """Fail closed, and never fall back to the development tree.

    The development-tree case is the important one: without the release-shaped
    path assertion, `ln -sfn <dev tree> current` satisfies every other guard and
    silently undoes this whole slice.
    """
    current = root / "current"

    point_current_at(root, CANDIDATE_ID)
    assert probe(launcher)["modules"]["ops.execution_watchdog"], "sanity: healthy pointer works"

    current.unlink()
    missing = run_probe(launcher)
    assert missing.returncode == 90, missing.returncode
    assert "RELEASE_POINTER_INVALID" in missing.stderr

    current.symlink_to(DEV_TREE)
    dev = run_probe(launcher)
    assert dev.returncode == 90, f"a development tree was accepted as a release: {dev.returncode}"
    assert "is not a release under" in dev.stderr
    current.unlink()

    dangling = root / "releases" / "ffffffffffff"
    point_current_at(root, dangling.name)
    broken = run_probe(launcher)
    assert broken.returncode == 90, broken.returncode

    point_current_at(root, CANDIDATE_ID)
    bad_module = subprocess.run([str(launcher), "-c"], capture_output=True, text=True, timeout=60)
    assert bad_module.returncode == 2, bad_module.returncode
    assert "OPS_RUNNER_MODULE_INVALID" in bad_module.stderr
    print("PASS: a missing, dangling, development-tree or malformed target is refused")


def test_failure_adapter_api_surface_is_stable_across_releases(root: Path) -> None:
    """Acceptance C: the host-bound handler cannot drift from release code.

    ops/systemd_failure_adapter.py stays on the host deliberately — a broken
    release pointer must not disable the reporter that would announce it. The
    price of that choice is a version boundary, so it is bounded here: every
    name the adapter imports from repository code, and every keyword it passes
    to report_operational_failure, must exist in BOTH releases.

    Parsed with `ast`, never imported: the point is to compare two release trees
    without executing either.
    """
    adapter = ast.parse((DEV_TREE / "ops/systemd_failure_adapter.py").read_text(encoding="utf-8"))

    imported: dict[str, set[str]] = {}
    for node in ast.walk(adapter):
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in {"ops", "api", "jobs"}:
            imported.setdefault(node.module, set()).update(alias.name for alias in node.names)
    assert imported == {"ops.operational_alert": {
        "INCIDENT_UNIT_FAILURE", "is_self_alerting", "report_operational_failure", "utcnow",
    }}, f"the adapter's dependency surface changed: {imported}"

    passed_keywords = {
        keyword.arg
        for node in ast.walk(adapter)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "report_operational_failure"
        for keyword in node.keywords
        if keyword.arg
    }
    assert passed_keywords, "adapter no longer calls report_operational_failure"

    for release_id in (BASELINE_ID, CANDIDATE_ID):
        module = root / "releases" / release_id / "ops" / "operational_alert.py"
        tree = ast.parse(module.read_text(encoding="utf-8"))
        top_level: dict[str, ast.AST] = {}
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                top_level[node.name] = node
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        top_level[target.id] = node

        for name in imported["ops.operational_alert"]:
            assert name in top_level, f"{release_id}: ops.operational_alert lost {name}"

        reporter = top_level["report_operational_failure"]
        assert isinstance(reporter, ast.FunctionDef), release_id
        accepted = {arg.arg for arg in reporter.args.args + reporter.args.kwonlyargs}
        if reporter.args.kwarg is None:
            missing = passed_keywords - accepted
            assert not missing, f"{release_id}: report_operational_failure rejects {missing}"
    print("PASS: the host-bound adapter's surface exists in both releases")


def main() -> int:
    assert VENV.is_dir(), f"missing dependency environment: {VENV}"
    tmp = Path(tempfile.mkdtemp(prefix="b1r-release-isolation-"))
    try:
        root, launcher = build_release_root(tmp)
        test_baseline_release_resolves_baseline_code(root, launcher)
        test_candidate_release_resolves_b1_code(root, launcher)
        test_watchdog_expectations_follow_the_release(root, launcher)
        test_pointer_rollback_restores_baseline_without_editing_the_unit(root, launcher)
        test_launcher_refuses_anything_that_is_not_a_release(root, launcher)
        test_failure_adapter_api_surface_is_stable_across_releases(root)
    finally:
        unseal(tmp)
        shutil.rmtree(tmp, ignore_errors=True)
    print("OK - release runtime isolation tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
