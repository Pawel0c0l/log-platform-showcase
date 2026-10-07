#!/usr/bin/env python3
"""`OPS-20260827-01` — the release tooling can tell intent from execution.

THE DEFECT THIS CLOSES. Activation moves `current`; it restarts nothing. The
release-bound services resolve the pointer at start and hold it until somebody
restarts them, so between an activation and that restart the pointer names one
release and production executes another. Every field `status` reported before
this described files and symlinks, so it reported that state as healthy —
`production_executes_release_root: true`, `remedy: null` — while production
served the previous release. The host was in exactly that state from
2026-08-25 to 2026-08-27 and no tool said so.

THREE STATES, NOT TWO. The running release must be able to be UNKNOWN: systemd
may be absent, the unit stopped, `/proc` unreadable. Unknown must collapse into
neither answer — reporting agreement would restore the false all-clear, and
reporting drift would send an operator to restart production over a systemctl
that timed out. This is the rule `unknown_bootability` already follows.

NOTHING HERE TOUCHES THE REAL HOST. The observation readers are injected, and
the CLI cases build a throwaway release root under a temporary directory. No
test reads the production release root, and none can write to it.

Run:

    cd /opt/log-platform-worktrees/ops-release-status
    PYTHONPATH="$PWD" .venv/bin/python ops/tests_manual/test_release_running_release_observation.py
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
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ops.release_boundary import (  # noqa: E402
    RELEASE_BOUND_SERVICES,
    RUNNING_RELEASE_DIVERGENT,
    RUNNING_RELEASE_OBSERVED,
    RUNNING_RELEASE_UNKNOWN,
    observe_running_release,
    pointer_matches_running,
    release_id_from_path,
)

CLI = REPO_ROOT / "ops" / "manage_release.py"
CHECKS = 0
FAILURES: list[str] = []

# Two well-formed release ids, used only as labels for directories these tests
# create under a temporary root. They are the pair from the 2026-08-25/27 drift
# because a real example reads better than `aaaaaaaaaaaa`, but nothing here
# consults the host: no assertion depends on which release production runs, and
# none would change if it changed.
CURRENT = "92c53ece27da"   # stands for whatever the pointer names
RUNNING = "023e6452fd5b"   # stands for whatever the processes execute


def check(label: str, condition: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if condition:
        print(f"PASS: {label}")
    else:
        FAILURES.append(label)
        print(f"FAIL: {label}\n      {detail}")


def _root_with(tmp: Path, *release_ids: str) -> Path:
    root = tmp / "release-root"
    for rid in release_ids:
        (root / "releases" / rid).mkdir(parents=True)
    return root


def _readers(mapping: dict, cwds: dict):
    """Injected observation: service -> pid, pid -> cwd."""

    return (lambda service: mapping.get(service),
            lambda pid: cwds.get(pid))


# ===========================================================================
print("== 1. DIFFER — the state the host was actually in ==")
# ===========================================================================
with tempfile.TemporaryDirectory() as tmp:
    root = _root_with(Path(tmp), CURRENT, RUNNING)
    running_dir = root / "releases" / RUNNING
    pid_reader, cwd_reader = _readers(
        {"log-platform-api.service": 2234134, "database-export-worker.service": 2234160},
        {2234134: running_dir, 2234160: running_dir},
    )
    observed = observe_running_release(
        release_root=root, main_pid_reader=pid_reader, cwd_reader=cwd_reader)

    check("the running release is observed from the process, not the symlink",
          observed["verdict"] == RUNNING_RELEASE_OBSERVED
          and observed["release_id"] == RUNNING, json.dumps(observed)[:300])
    check("both release-bound services are reported individually",
          {s["service"] for s in observed["services"]} == set(RELEASE_BOUND_SERVICES),
          str(observed["services"]))
    # THE DRIFT-DETECTION CHECK. Remove the comparison and this fails.
    check("pointer and running release are reported as DIFFERING",
          pointer_matches_running(CURRENT, observed) is False,
          repr(pointer_matches_running(CURRENT, observed)))
    check("...and differing is not merely falsy — it is exactly False",
          pointer_matches_running(CURRENT, observed) is not None)

# ===========================================================================
print()
print("== 2. AGREE — a restarted host must not have a drift manufactured for it ==")
# ===========================================================================
with tempfile.TemporaryDirectory() as tmp:
    root = _root_with(Path(tmp), CURRENT)
    current_dir = root / "releases" / CURRENT
    pid_reader, cwd_reader = _readers(
        {"log-platform-api.service": 11, "database-export-worker.service": 12},
        {11: current_dir, 12: current_dir},
    )
    observed = observe_running_release(
        release_root=root, main_pid_reader=pid_reader, cwd_reader=cwd_reader)
    check("agreement is reported as agreement",
          pointer_matches_running(CURRENT, observed) is True,
          repr(pointer_matches_running(CURRENT, observed)))
    check("...with no remedy implied — the observed id equals the pointer",
          observed["release_id"] == CURRENT)

    # A deeper cwd (the launcher may cd below the release root) still resolves.
    deep = current_dir / "api"
    deep.mkdir()
    _, deep_cwd = _readers({}, {11: deep, 12: deep})
    observed_deep = observe_running_release(
        release_root=root, main_pid_reader=pid_reader, cwd_reader=deep_cwd)
    check("a cwd BELOW the release directory still identifies the release",
          pointer_matches_running(CURRENT, observed_deep) is True,
          json.dumps(observed_deep)[:250])

# ===========================================================================
print()
print("== 3. UNKNOWN — reachable, and never collapsed into either answer ==")
# ===========================================================================
with tempfile.TemporaryDirectory() as tmp:
    root = _root_with(Path(tmp), CURRENT)

    cases = {
        # systemd absent, or the unit not known: no pid at all.
        "no systemd / unit unknown": (lambda s: None, lambda p: None),
        # The unit is stopped. systemd reports MainPID=0, which must not read
        # as a pid.
        "unit stopped (MainPID=0)": (lambda s: 0, lambda p: None),
        # /proc unreadable — another user's process, or a hardened host.
        "process cwd unreadable": (lambda s: 4242, lambda p: None),
        # The process runs from somewhere outside the release layout.
        "cwd outside the release layout": (lambda s: 4242, lambda p: Path("/var/tmp")),
        # The reader itself blows up. An observation must never raise.
        "reader raises": (lambda s: (_ for _ in ()).throw(RuntimeError("boom")),
                          lambda p: None),
    }
    for label, (pid_reader, cwd_reader) in cases.items():
        observed = observe_running_release(
            release_root=root, main_pid_reader=pid_reader, cwd_reader=cwd_reader)
        verdict = pointer_matches_running(CURRENT, observed)
        check(f"UNKNOWN is reached: {label}",
              observed["verdict"] == RUNNING_RELEASE_UNKNOWN and observed["release_id"] is None,
              json.dumps(observed)[:250])
        check(f"...and reports None, never True or False: {label}",
              verdict is None, repr(verdict))

    check("an unknown observation names why, for the operator reading it",
          bool(observe_running_release(
              release_root=root, main_pid_reader=lambda s: None,
              cwd_reader=lambda p: None).get("reason")))

# ===========================================================================
print()
print("== 4. DIVERGENT — two services on different releases is observed, not unknown ==")
# ===========================================================================
with tempfile.TemporaryDirectory() as tmp:
    root = _root_with(Path(tmp), CURRENT, RUNNING)
    pid_reader, cwd_reader = _readers(
        {"log-platform-api.service": 21, "database-export-worker.service": 22},
        {21: root / "releases" / CURRENT, 22: root / "releases" / RUNNING},
    )
    observed = observe_running_release(
        release_root=root, main_pid_reader=pid_reader, cwd_reader=cwd_reader)
    check("services executing different releases is its own verdict",
          observed["verdict"] == RUNNING_RELEASE_DIVERGENT, json.dumps(observed)[:250])
    check("...and is NOT unknown — we know something is wrong",
          observed["verdict"] != RUNNING_RELEASE_UNKNOWN)
    check("...and never reads as agreement",
          pointer_matches_running(CURRENT, observed) is False,
          repr(pointer_matches_running(CURRENT, observed)))

# ===========================================================================
print()
print("== 5. Path -> release id, including the ways it must refuse ==")
# ===========================================================================
with tempfile.TemporaryDirectory() as tmp:
    root = _root_with(Path(tmp), CURRENT)
    check("a release path resolves to its id",
          release_id_from_path(root, root / "releases" / CURRENT) == CURRENT)
    check("a path outside the layout resolves to nothing",
          release_id_from_path(root, Path("/var/tmp")) is None)
    check("the releases directory itself is not a release",
          release_id_from_path(root, root / "releases") is None)
    (root / "releases" / "not-a-release-id").mkdir()
    check("a directory whose name is not a release id is refused",
          release_id_from_path(root, root / "releases" / "not-a-release-id") is None)

# ===========================================================================
print()
print("== 6. THE CLI: status renders all three states ==")
# ===========================================================================


def _status_payload(release_root: Path, observation) -> dict:
    """`cmd_status` in-process, with the observation injected.

    In-process so the observation can be replaced; the point is the CLI's
    rendering of each verdict, which is where the false all-clear was printed.
    """

    import io
    import contextlib
    import ops.manage_release as mr

    original = mr.observe_running_release
    mr.observe_running_release = lambda **_kwargs: observation
    try:
        args = mr.build_parser().parse_args(
            ["--release-root", str(release_root), "status"])
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            args.func(args)
        return json.loads(buffer.getvalue())
    finally:
        mr.observe_running_release = original


with tempfile.TemporaryDirectory() as tmp:
    root = _root_with(Path(tmp), CURRENT, RUNNING)
    os.symlink(f"releases/{CURRENT}", root / "current")

    differ = _status_payload(root, {
        "release_id": RUNNING, "verdict": RUNNING_RELEASE_OBSERVED,
        "reason": None, "observation_error": None, "services": []})
    check("status reports the running release as its own field",
          differ["running_release_id"] == RUNNING, json.dumps(differ)[:300])
    check("status reports the pointer separately",
          differ["current_release_id"] == CURRENT)
    check("status says plainly that they differ",
          differ["pointer_matches_running_release"] is False)
    # CRITERION 1 / 4: this is the assertion that fails if drift detection is
    # removed. `remedy: null` was the field that positively asserted health.
    check("status returns a NON-NULL remedy naming the drift and the action",
          differ["remedy"] and RUNNING in differ["remedy"] and CURRENT in differ["remedy"]
          and "systemctl restart" in differ["remedy"], repr(differ["remedy"]))
    check("the misreadable field is gone, replaced by one that says 'wrapper'",
          "production_executes_release_root" not in differ
          and "wrapper_executes_release_root" in differ, sorted(differ))
    check("...and the configured path is labelled as configuration",
          "configured_source_path" in differ and "production_source_path" not in differ,
          sorted(differ))

    agree = _status_payload(root, {
        "release_id": CURRENT, "verdict": RUNNING_RELEASE_OBSERVED,
        "reason": None, "observation_error": None, "services": []})
    check("status on an agreeing host reports agreement",
          agree["pointer_matches_running_release"] is True
          and agree["running_release_id"] == CURRENT, json.dumps(agree)[:250])
    check("...and manufactures no drift remedy",
          agree["remedy"] is None, repr(agree["remedy"]))

    unknown = _status_payload(root, {
        "release_id": None, "verdict": RUNNING_RELEASE_UNKNOWN,
        "reason": "no_main_pid", "observation_error": None, "services": []})
    check("status renders the unknown case as unknown",
          unknown["pointer_matches_running_release"] is None
          and unknown["running_release_id"] is None, json.dumps(unknown)[:250])
    check("...and does not invent a drift remedy from an unknown",
          unknown["remedy"] is None, repr(unknown["remedy"]))

# ===========================================================================
print()
print("== 7. THE CLI end-to-end: unknown is reachable through the REAL readers ==")
# ===========================================================================
with tempfile.TemporaryDirectory() as tmp:
    root = _root_with(Path(tmp), CURRENT)
    os.symlink(f"releases/{CURRENT}", root / "current")
    proc = subprocess.run(
        [sys.executable, str(CLI), "--release-root", str(root), "status"],
        capture_output=True, text=True)
    payload = json.loads(proc.stdout)
    # The real services (if any run on this machine) execute out of the REAL
    # release root, which is not this temporary one, so the honest answer here
    # is "not observed" — reached with no mocking at all.
    check("a real run against a foreign release root reports UNKNOWN, not agreement",
          payload["pointer_matches_running_release"] is None
          and payload["running_release_id"] is None,
          json.dumps({k: payload.get(k) for k in
                      ("running_release_id", "pointer_matches_running_release")}))
    check("...and says why it could not observe",
          bool(payload["running_release"].get("reason")),
          json.dumps(payload["running_release"])[:300])
    check("...and exits 0 — status is an observation, not a gate",
          proc.returncode == 0, f"exit={proc.returncode}")


# ===========================================================================
print()
print("== 8. `activate` cannot report success into a not-live state ==")
# ===========================================================================
def _git(repo: Path, *args: str) -> str:
    repo.mkdir(parents=True, exist_ok=True)
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True).stdout


def _make_repo(root: Path) -> Path:
    repo = root / "devrepo"
    (repo / "ops").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Release Liveness Test")
    (repo / "ops" / "runner.py").write_text("VALUE = 'committed'\n")
    _git(repo, "add", "ops/runner.py")
    _git(repo, "commit", "-q", "-m", "baseline")
    (repo / ".env").write_text("EXAMPLE_SETTING=1\n")
    interpreter = repo / ".venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("#!/bin/sh\nexit 0\n")
    interpreter.chmod(0o755)
    return repo


def _force_rmtree(path: Path) -> None:
    def _onerror(func, target, _exc):
        try:
            os.chmod(target, stat.S_IWUSR | stat.S_IRUSR | stat.S_IXUSR)
            func(target)
        except OSError:
            pass
    shutil.rmtree(path, onerror=_onerror)


with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    repo = _make_repo(root)
    rr = root / "release"
    head = _git(repo, "rev-parse", "HEAD").strip()

    def cli(*args: str) -> tuple[int, dict]:
        proc = subprocess.run(
            [sys.executable, str(CLI), "--source-repo", str(repo),
             "--release-root", str(rr), *args],
            capture_output=True, text=True)
        for stream in (proc.stdout, proc.stderr):
            try:
                return proc.returncode, json.loads(stream)
            except json.JSONDecodeError:
                continue
        return proc.returncode, {"_stdout": proc.stdout, "_stderr": proc.stderr[-500:]}

    rc, prep = cli("prepare", "--commit", head)
    rid = prep.get("release_id")
    check("a well-formed release still prepares and verifies",
          rid is not None and prep.get("verified") is True, json.dumps(prep)[:250])

    # The real `--execute` path runs a schema preflight against the platform
    # database, which a throwaway release root has no access to, so it refuses
    # before reaching the pointer swap. That refusal is a DIFFERENT, existing
    # gate; asserting liveness behaviour through it would be asserting nothing.
    rc, act = cli("activate", "--release", rid, "--execute")
    check("`activate --execute` on an unreachable platform DB still refuses first",
          rc != 0 and act.get("executed") is False,
          f"exit={rc} payload={json.dumps(act)[:220]}")

    # So the post-activation reporting is exercised where it lives: the pointer
    # swap is injected, and what the command DOES with the liveness observation
    # is what these checks are about.
    def _activate_payload(observation):
        import io
        import contextlib
        import ops.manage_release as mr

        original_activate = mr.activate_release
        original_observe = mr.observe_running_release
        mr.activate_release = lambda **_k: {
            "release_id": rid, "commit": head, "changed": True,
            "previous_release_id": "0123456789ab"}
        mr.observe_running_release = lambda **_k: observation
        try:
            args = mr.build_parser().parse_args(
                ["--source-repo", str(repo), "--release-root", str(rr),
                 "activate", "--release", rid, "--execute"])
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = args.func(args)
            return code, json.loads(buffer.getvalue())
        finally:
            mr.activate_release = original_activate
            mr.observe_running_release = original_observe

    code, payload = _activate_payload({
        "release_id": "0123456789ab", "verdict": RUNNING_RELEASE_OBSERVED,
        "reason": None, "observation_error": None, "services": []})
    check("activation into a NOT-LIVE state exits non-zero",
          code != 0, f"exit={code}")
    check("...and is classified ACTIVATED_NOT_YET_LIVE",
          payload.get("classification") == "ACTIVATED_NOT_YET_LIVE",
          repr(payload.get("classification")))
    check("...and says `live` is False, not merely absent",
          payload.get("live") is False, repr(payload.get("live")))
    check("...and names the release still running",
          payload.get("running_release_id") == "0123456789ab")
    check("...and hands over the exact restart command",
          "systemctl restart" in (payload.get("restart_command") or "")
          and "systemctl restart" in (payload.get("next") or ""),
          repr(payload.get("restart_command")))
    check("...and still reports the activation as executed — the pointer did move",
          payload.get("executed") is True)
    check("...and offers the abandon path too",
          "rollback" in (payload.get("next") or ""), repr(payload.get("next"))[:200])

    code, payload = _activate_payload({
        "release_id": rid, "verdict": RUNNING_RELEASE_OBSERVED,
        "reason": None, "observation_error": None, "services": []})
    check("an activation that IS live exits 0 and says so",
          code == 0 and payload.get("classification") == "ACTIVE_AND_LIVE"
          and payload.get("live") is True, f"exit={code} {json.dumps(payload)[:220]}")

    code, payload = _activate_payload({
        "release_id": None, "verdict": RUNNING_RELEASE_UNKNOWN,
        "reason": "no_main_pid", "observation_error": None, "services": []})
    check("an UNPROVEN activation is never reported as live",
          code != 0 and payload.get("live") is None
          and payload.get("classification") == "ACTIVATION_LIVENESS_UNKNOWN",
          f"exit={code} {json.dumps(payload)[:220]}")
    check("...and tells the operator not to assume it",
          "not proven live" in (payload.get("next") or ""), repr(payload.get("next"))[:200])

    _force_rmtree(rr)

print()
if FAILURES:
    print(f"FAILED ({len(FAILURES)} of {CHECKS}): " + "; ".join(FAILURES))
    raise SystemExit(1)
print(f"OK - running-release observation checks passed ({CHECKS} checks)")
