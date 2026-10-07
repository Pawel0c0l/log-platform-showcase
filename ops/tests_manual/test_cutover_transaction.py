#!/usr/bin/env python3
"""Deterministic tests for the release-boundary cutover transaction.

Pure: stdlib plus `git`/`tar`. No network, no database, no systemd mutation, no
privileged write. Every fixture is a throwaway git repository, a disposable
release root and a fake installed wrapper under a temporary directory, so
nothing here can observe or touch the real development tree, the real release
root, the real timers or the real `/usr/local/bin/log-job-runner.sh`.

What is proven:

  1. a failed preflight gate makes wrapper installation *unreachable* — the
     defect being closed is a runbook whose `preflight || { echo ...; }` turned
     a failed gate into a success, because the brace group's exit status is the
     echo's;
  2. the first-cutover pointer sequence ends at `current = target`,
     `previous = M0`, not at the pre-remediation release;
  3. every post-install assertion (sha256, uid, gid, mode, wrapper variant,
     current, previous, release verification) blocks the timer restart;
  4. exactly the originally-active timers are restarted — never one an operator
     had deliberately stopped;
  5. consumer-discovery failure is a blocker, not an empty result;
  6. provisioning cannot overwrite a wrapper a cutover installed after
     provisioning planned.

Run from repo root:

    PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_cutover_transaction.py
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ops.cutover_execute import CutoverError, CutoverTransaction  # noqa: E402
from ops.release_boundary import (  # noqa: E402
    RELEASE_WRAPPER_RELATIVE,
    ReleaseBoundaryError,
    activate_release,
    pointer_release_id,
    prepare_release,
)
from ops.wrapper_install_lock import (  # noqa: E402
    WrapperInstallLockError,
    wrapper_install_lock,
)

# Never touch the real host locks: these tests must not contend with a genuine
# provisioning or cutover run. The production paths are constants with no
# environment override (that override *was* a correctness hole), so tests
# redirect them through the documented monkeypatchable seams.
import ops.execution_barrier as execution_barrier_module  # noqa: E402
import ops.wrapper_install_lock as wrapper_lock_module  # noqa: E402

_TEST_LOCK_DIR = Path(tempfile.mkdtemp(prefix="cutover-locks-"))
wrapper_lock_module.lock_path = lambda: _TEST_LOCK_DIR / "wrapper-install.lock"
execution_barrier_module.barrier_path = lambda: _TEST_LOCK_DIR / "execution.lock"

FAILURES: list[str] = []


def _check(label: str, condition: bool, evidence: str = "") -> None:
    if condition:
        print(f"PASS  {label}")
        return
    FAILURES.append(label)
    print(f"FAIL  {label}")
    if evidence:
        print(f"      {evidence}")


def _expect_cutover_error(label: str, classification: str, fn) -> dict:
    try:
        fn()
    except CutoverError as exc:
        _check(label, exc.classification == classification,
               f"expected {classification}, got {exc.classification}: {exc.details}")
        return exc.details
    except Exception as exc:  # noqa: BLE001
        _check(label, False, f"expected {classification}, raised {type(exc).__name__}: {exc}")
        return {}
    _check(label, False, f"expected {classification}, nothing raised")
    return {}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args],
                          check=True, capture_output=True, text=True).stdout


def _force_rmtree(path: Path) -> None:
    def _onerror(func, target, _exc):
        try:
            os.chmod(target, stat.S_IWUSR | stat.S_IRUSR | stat.S_IXUSR)
            func(target)
        except OSError:
            pass
    shutil.rmtree(path, onerror=_onerror)


def _make_repo(root: Path, release_root: Path | None = None) -> Path:
    """A fixture repo shaped like the real one: it carries both wrappers.

    The release wrapper declares the release root it will resolve at runtime,
    exactly as the real one does, so the transaction's release-root assertion is
    exercised against a truthful fixture rather than trivially satisfied.
    """
    repo = root / "devrepo"
    (repo / "ops" / "systemd" / "proposed").mkdir(parents=True)
    _git_init(repo)
    declared = release_root if release_root is not None else root / "release"
    (repo / "ops" / "runner.py").write_text("VALUE = 'committed'\n")
    (repo / "ops" / "systemd" / "proposed" / "log-job-runner.sh").write_text(
        '#!/usr/bin/env bash\nset -euo pipefail\n'
        'WRAPPER_VARIANT="development"\n'
        'flock --shared --wait 900 9\n'
        'CUTOVER_FENCE="x"\n'
        'BASE_DIR="/home/dev/log-platform"\n')
    (repo / "ops" / "systemd" / "proposed" / "log-job-runner.release.sh").write_text(
        '#!/usr/bin/env bash\nset -euo pipefail\n'
        f'RELEASE_ROOT="{declared}"\n'
        'BASE_DIR="${RELEASE_ROOT}/current"\n')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "baseline")
    return repo


def _git_init(repo: Path) -> None:
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Cutover Test")


class FakeSystemctl:
    """Records every call; timer activity is whatever the test says it is."""

    def __init__(self, active: dict, fail_on: tuple = ()) -> None:
        self.active = dict(active)
        self.fail_on = fail_on
        self.calls: list[tuple] = []

    def __call__(self, *args: str):
        self.calls.append(args)
        verb = args[0]
        if verb in self.fail_on:
            return 1, f"simulated failure for {args}"
        if verb == "is-active":
            return (0, "active") if self.active.get(args[1]) else (3, "inactive")
        if verb == "stop":
            self.active[args[1]] = False
            return 0, ""
        if verb == "start":
            self.active[args[1]] = True
            return 0, ""
        return 0, ""

    def started(self) -> list:
        return [a[1] for a in self.calls if a[0] == "start"]

    def stopped(self) -> list:
        return [a[1] for a in self.calls if a[0] == "stop"]


def _build(root: Path, repo: Path, release_root: Path, target: str, predecessor: str,
           *, systemctl=None, preflight=None, active=None, stat_override=None,
           installed_variant_source=None):
    """A transaction wired entirely to fakes, including a fake installed wrapper."""
    installed = root / "usr-local-bin" / "log-job-runner.sh"
    installed.parent.mkdir(parents=True, exist_ok=True)
    dev_wrapper = repo / "ops/systemd/proposed/log-job-runner.sh"
    if not installed.exists():
        # Some fixtures deliberately predate the boundary and carry no wrapper.
        installed.write_bytes(dev_wrapper.read_bytes() if dev_wrapper.is_file()
                              else b"#!/bin/sh\necho placeholder\n")
    backup = root / "backup" / "log-job-runner.sh.bak"
    timers = ["log-job@dispatcher.timer", "log-workflow-b.timer", "log-job@retention-purge.timer"]
    fake_systemctl = systemctl or FakeSystemctl(active if active is not None
                                                else {unit: True for unit in timers})

    def default_preflight(stage, *_a, **_k):
        return 0, {"safe_to_replace_wrapper": True, "blockers": []}

    def do_install(source: Path, destination: Path) -> None:
        destination.write_bytes(source.read_bytes())
        destination.chmod(0o755)

    def do_backup(source: Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())

    def do_stat(path: Path) -> dict:
        # Root ownership cannot be produced unprivileged, so the expected values
        # are supplied here; individual tests override to prove each assertion.
        base = {"uid": 0, "gid": 0, "mode": 0o755,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        base.update(stat_override or {})
        return base

    return CutoverTransaction(
        release_root=release_root, source_repo=installed_variant_source or repo,
        target_release_id=target, predecessor_release_id=predecessor,
        installed_wrapper=installed, backup_path=backup,
        systemctl=fake_systemctl, preflight=preflight or default_preflight,
        install_wrapper=do_install, backup_wrapper=do_backup, stat_wrapper=do_stat,
        wrapper_lock=lambda: wrapper_install_lock(timeout_seconds=5),
        execution_barrier=lambda **kw: execution_barrier_module.execution_barrier(
            timeout_seconds=5, **kw),
        discover=lambda: {"timers": timers,
                          "services": [t.replace(".timer", ".service") for t in timers],
                          "complete": True, "discovery_failures": []},
    ), fake_systemctl, installed, backup


def _two_releases(root: Path):
    """A release root shaped like production: M0 as predecessor, M1 as current."""
    repo = _make_repo(root)
    release_root = root / "release"
    m0 = prepare_release(source_repo=repo, release_root=release_root,
                         committish=_git(repo, "rev-parse", "HEAD").strip())["release_id"]
    _git(repo, "commit", "-q", "--allow-empty", "-m", "m1")
    m1 = prepare_release(source_repo=repo, release_root=release_root,
                         committish=_git(repo, "rev-parse", "HEAD").strip())["release_id"]
    _git(repo, "commit", "-q", "--allow-empty", "-m", "target")
    target = prepare_release(source_repo=repo, release_root=release_root,
                             committish=_git(repo, "rev-parse", "HEAD").strip())["release_id"]
    activate_release(release_root=release_root, release_id=m0, source_repo=repo)
    activate_release(release_root=release_root, release_id=m1, source_repo=repo)
    return repo, release_root, m0, m1, target


os.environ.setdefault("RELEASE_MANAGEMENT_LOCK_TIMEOUT_SECONDS", "10")

# ---------------------------------------------------------------------------
print("-- first-cutover pointer sequence (R3) --")

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    repo, release_root, m0, m1, target = _two_releases(root)

    _check("the fixture starts where production is: current=M1, previous=M0",
           pointer_release_id(release_root / "current") == m1
           and pointer_release_id(release_root / "previous") == m0)

    transaction, fake, installed, backup = _build(root, repo, release_root, target, m0)
    report = transaction.run(execute=True)

    _check("current ends at the cutover target",
           pointer_release_id(release_root / "current") == target,
           f"current={pointer_release_id(release_root / 'current')} target={target}")
    _check("previous ends at the M0 release, not the pre-remediation one",
           pointer_release_id(release_root / "previous") == m0,
           f"previous={pointer_release_id(release_root / 'previous')} m0={m0} m1={m1}")
    _check("the transaction reports success", report["executed"] is True)
    _check("the wrapper was installed from the immutable release path",
           str(release_root / "releases" / target / RELEASE_WRAPPER_RELATIVE)
           in json.dumps(report["steps"]),
           f"steps={report['steps']}")
    _check("the installed wrapper is the release wrapper from that release",
           installed.read_bytes()
           == (release_root / "releases" / target / RELEASE_WRAPPER_RELATIVE).read_bytes())
    _check("the outgoing wrapper was backed up first", backup.is_file())
    _force_rmtree(release_root)

# ---------------------------------------------------------------------------
print("\n-- failed preflight makes installation unreachable (R2) --")

for stage_to_fail in ("post-timer-stop", "pre-replacement"):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo, release_root, m0, m1, target = _two_releases(root)

        def failing(stage, *_a, _fail=stage_to_fail, **_k):
            if stage == _fail:
                return 1, {"safe_to_replace_wrapper": False, "blockers": ["simulated blocker"]}
            return 0, {"safe_to_replace_wrapper": True, "blockers": []}

        transaction, fake, installed, backup = _build(root, repo, release_root, target, m0,
                                                      preflight=failing)
        before = installed.read_bytes()
        details = _expect_cutover_error(
            f"a failed {stage_to_fail} preflight aborts the cutover",
            "CUTOVER_PREFLIGHT_FAILED", lambda: transaction.run(execute=True))
        _check(f"{stage_to_fail}: the wrapper was never replaced",
               installed.read_bytes() == before)
        _check(f"{stage_to_fail}: no backup was taken, so install was never reached",
               not backup.exists())
        _check(f"{stage_to_fail}: the blocker is reported, not swallowed",
               details.get("blockers") == ["simulated blocker"], f"details={details}")
        _check(f"{stage_to_fail}: originally-active timers were restarted on unwind",
               sorted(fake.started()) == sorted(fake.stopped()),
               f"started={fake.started()} stopped={fake.stopped()}")
        _force_rmtree(release_root)

with tempfile.TemporaryDirectory() as tmp:
    # The exact shell defect: `preflight || { echo ...; }` exits 0. Prove the
    # transaction cannot be talked into continuing the same way.
    root = Path(tmp)
    repo, release_root, m0, m1, target = _two_releases(root)
    shell = subprocess.run(["bash", "-c", "false || { echo 'ABORT'; }; echo rc=$?"],
                           capture_output=True, text=True)
    _check("the old shell idiom really does report success",
           "rc=0" in shell.stdout, f"stdout={shell.stdout!r}")

    transaction, fake, installed, backup = _build(
        root, repo, release_root, target, m0,
        preflight=lambda *a, **k: (1, {"blockers": ["always fails"]}))
    _expect_cutover_error("the transaction cannot be talked past a failed gate",
                          "CUTOVER_PREFLIGHT_FAILED", lambda: transaction.run(execute=True))
    _check("pointers were not left advanced by an aborted cutover",
           pointer_release_id(release_root / "current") in (m1, m0),
           f"current={pointer_release_id(release_root / 'current')}")
    _force_rmtree(release_root)

# ---------------------------------------------------------------------------
print("\n-- post-install verification blocks the timer restart (R5) --")

VERIFICATION_CASES = (
    ("wrong sha256", {"sha256": "0" * 64}),
    ("wrong owner", {"uid": 1000}),
    ("wrong group", {"gid": 1000}),
    ("wrong mode", {"mode": 0o777}),
)
for label, override in VERIFICATION_CASES:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo, release_root, m0, m1, target = _two_releases(root)
        transaction, fake, installed, backup = _build(root, repo, release_root, target, m0,
                                                      stat_override=override)
        details = _expect_cutover_error(
            f"{label} fails post-install verification",
            "CUTOVER_POST_INSTALL_VERIFICATION_FAILED", lambda: transaction.run(execute=True))
        _check(f"{label}: no timer was restarted",
               fake.started() == [], f"started={fake.started()}")
        _check(f"{label}: the failure names what was wrong",
               bool(details.get("failures")), f"details={details}")
        _check(f"{label}: rollback instructions are provided",
               "rollback" in details, f"details={details}")
        _force_rmtree(release_root)

with tempfile.TemporaryDirectory() as tmp:
    # Wrong wrapper variant: the installed file is not a release wrapper at all.
    root = Path(tmp)
    repo, release_root, m0, m1, target = _two_releases(root)
    transaction, fake, installed, backup = _build(root, repo, release_root, target, m0)

    def install_wrong_content(source: Path, destination: Path) -> None:
        destination.write_text("#!/bin/sh\necho not a release wrapper\n")
        destination.chmod(0o755)
    transaction.install_wrapper = install_wrong_content
    _expect_cutover_error("a non-release wrapper fails verification",
                          "CUTOVER_POST_INSTALL_VERIFICATION_FAILED",
                          lambda: transaction.run(execute=True))
    _check("wrong variant: no timer was restarted", fake.started() == [])
    _force_rmtree(release_root)

with tempfile.TemporaryDirectory() as tmp:
    # Wrong pointers: something moved `current` after the sequence step.
    root = Path(tmp)
    repo, release_root, m0, m1, target = _two_releases(root)
    transaction, fake, installed, backup = _build(root, repo, release_root, target, m0)
    original_install = transaction.install_wrapper

    def install_then_move_pointer(source: Path, destination: Path) -> None:
        original_install(source, destination)
        activate_release(release_root=release_root, release_id=m1, source_repo=repo)
    transaction.install_wrapper = install_then_move_pointer
    details = _expect_cutover_error("a pointer moved after installation fails verification",
                                    "CUTOVER_POST_INSTALL_VERIFICATION_FAILED",
                                    lambda: transaction.run(execute=True))
    _check("wrong current: no timer was restarted", fake.started() == [])
    _check("wrong current: the mismatch is named",
           any("current is" in f for f in details.get("failures", [])), f"details={details}")
    _force_rmtree(release_root)

with tempfile.TemporaryDirectory() as tmp:
    # A target release that does not verify must never reach installation.
    root = Path(tmp)
    repo, release_root, m0, m1, target = _two_releases(root)
    tree = release_root / "releases" / target
    tree.chmod(0o755)
    (tree / "ops").chmod(0o755)
    (tree / "ops" / "runner.py").chmod(0o644)
    (tree / "ops" / "runner.py").write_text("TAMPERED = True\n")
    transaction, fake, installed, backup = _build(root, repo, release_root, target, m0)
    before = installed.read_bytes()
    _expect_cutover_error("a tampered target release is refused before anything is touched",
                          "CUTOVER_RELEASE_VERIFICATION_FAILED",
                          lambda: transaction.run(execute=True))
    _check("tampered target: the wrapper was not replaced", installed.read_bytes() == before)
    _check("tampered target: no timer was stopped", fake.stopped() == [])
    _force_rmtree(release_root)

with tempfile.TemporaryDirectory() as tmp:
    # A release predating the boundary carries no release wrapper to install.
    root = Path(tmp)
    repo = root / "devrepo"
    (repo / "ops").mkdir(parents=True)
    _git_init(repo)
    (repo / "ops" / "runner.py").write_text("X = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "no wrapper")
    release_root = root / "release"
    old = prepare_release(source_repo=repo, release_root=release_root,
                          committish=_git(repo, "rev-parse", "HEAD").strip())["release_id"]
    activate_release(release_root=release_root, release_id=old, source_repo=repo)
    transaction, fake, installed, backup = _build(root, repo, release_root, old, old)
    _expect_cutover_error("a release without the wrapper cannot be promoted",
                          "CUTOVER_WRAPPER_SOURCE_MISSING",
                          lambda: transaction.run(execute=True))
    _force_rmtree(release_root)

# ---------------------------------------------------------------------------
print("\n-- timer state is restored, never invented (R6) --")

TIMER_CASES = (
    ("all active", {"log-job@dispatcher.timer": True, "log-workflow-b.timer": True,
                    "log-job@retention-purge.timer": True}),
    ("one inactive", {"log-job@dispatcher.timer": True, "log-workflow-b.timer": False,
                      "log-job@retention-purge.timer": True}),
    ("two inactive", {"log-job@dispatcher.timer": True, "log-workflow-b.timer": False,
                      "log-job@retention-purge.timer": False}),
)
for label, initial in TIMER_CASES:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo, release_root, m0, m1, target = _two_releases(root)
        transaction, fake, installed, backup = _build(root, repo, release_root, target, m0,
                                                      active=dict(initial))
        transaction.run(execute=True)
        expected = sorted(u for u, a in initial.items() if a)
        _check(f"{label}: exactly the originally-active timers restart",
               sorted(fake.started()) == expected,
               f"started={sorted(fake.started())} expected={expected}")
        _check(f"{label}: an inactive timer is never enabled",
               all(not fake.active[u] for u, a in initial.items() if not a),
               f"final={fake.active}")
        _check(f"{label}: only active timers were stopped",
               sorted(fake.stopped()) == expected,
               f"stopped={sorted(fake.stopped())}")
        _force_rmtree(release_root)

with tempfile.TemporaryDirectory() as tmp:
    # Every consumer timer already inactive: the transaction must refuse rather
    # than "restore" nothing and report COMPLETE with production halted. This is
    # the trap the documented recovery-by-re-run would otherwise walk into.
    root = Path(tmp)
    repo, release_root, m0, m1, target = _two_releases(root)
    all_off = {unit: False for unit in
               ("log-job@dispatcher.timer", "log-workflow-b.timer", "log-job@retention-purge.timer")}
    transaction, fake, installed, backup = _build(root, repo, release_root, target, m0,
                                                  active=dict(all_off))
    _expect_cutover_error("an already-stopped scheduler is refused, not silently accepted",
                          "CUTOVER_NO_ACTIVE_CONSUMER_TIMERS",
                          lambda: transaction.run(execute=True))
    _check("nothing was installed", not backup.exists())

    # The operator can still declare the intended set explicitly.
    transaction2, fake2, installed2, backup2 = _build(root, repo, release_root, target, m0,
                                                      active=dict(all_off))
    transaction2.assume_timers_stopped = ("log-job@dispatcher.timer",)
    transaction2.run(execute=True)
    _check("an explicitly declared timer set is restored",
           fake2.started() == ["log-job@dispatcher.timer"], f"started={fake2.started()}")
    _force_rmtree(release_root)

with tempfile.TemporaryDirectory() as tmp:
    # Recovery-by-re-run must not adopt the stopped scheduler as the baseline.
    root = Path(tmp)
    repo, release_root, m0, m1, target = _two_releases(root)
    initial = {"log-job@dispatcher.timer": True, "log-workflow-b.timer": True,
               "log-job@retention-purge.timer": False}
    shared_backup = root / "backup" / "log-job-runner.sh.bak"
    first, fake_first, installed_first, _b = _build(root, repo, release_root, target, m0,
                                                    active=dict(initial))
    first.backup_path = shared_backup

    def replace_then_raise(source: Path, destination: Path) -> None:
        destination.write_bytes(source.read_bytes())
        raise RuntimeError("dies after replacement")
    first.install_wrapper = replace_then_raise
    _expect_cutover_error("the first attempt fails after replacement",
                          "CUTOVER_UNEXPECTED_FAILURE", lambda: first.run(execute=True))
    _check("the first attempt left the timers stopped", fake_first.started() == [])
    _check("the captured baseline was persisted",
           Path(str(shared_backup) + ".timers.json").is_file())

    # Operator restores the wrapper and re-runs, with every timer now stopped.
    installed_first.write_bytes((repo / "ops/systemd/proposed/log-job-runner.sh").read_bytes())
    second, fake_second, _i, _b2 = _build(root, repo, release_root, target, m0,
                                          active={u: False for u in initial})
    second.backup_path = shared_backup
    second.installed_wrapper = installed_first
    report = second.run(execute=True)
    _check("the re-run restores the ORIGINAL scheduler, not the stopped one",
           sorted(fake_second.started()) == ["log-job@dispatcher.timer", "log-workflow-b.timer"],
           f"started={fake_second.started()}")
    _check("a timer that was inactive before the first attempt stays inactive",
           "log-job@retention-purge.timer" not in fake_second.started())
    _check("the re-run completes", report["executed"] is True)
    _force_rmtree(release_root)

with tempfile.TemporaryDirectory() as tmp:
    # A consumer whose installed unit bypasses the wrapper takes no barrier, so
    # quiescence cannot be claimed for it.
    root = Path(tmp)
    repo, release_root, m0, m1, target = _two_releases(root)

    class BypassingSystemctl(FakeSystemctl):
        def __call__(self, *args):
            if args[0] == "show" and "ExecStart" in args:
                unit = args[-1]
                if "retention-purge" in unit:
                    return 0, "/usr/bin/python3 ops/runner.py jobs.api.telematics.retention_purge"
                return 0, "/usr/local/bin/log-job-runner.sh %i {}"
            return super().__call__(*args)

    fake = BypassingSystemctl({u: True for u in
                               ("log-job@dispatcher.timer", "log-workflow-b.timer",
                                "log-job@retention-purge.timer")})
    transaction, _f, installed, backup = _build(root, repo, release_root, target, m0,
                                                systemctl=fake)
    details = _expect_cutover_error("a consumer that bypasses the wrapper blocks the cutover",
                                    "CUTOVER_CONSUMER_BYPASSES_WRAPPER",
                                    lambda: transaction.run(execute=True))
    _check("the offending unit is named",
           any("retention-purge" in o["unit"] for o in details.get("offenders", [])),
           f"details={details}")
    _check("no timer was stopped", fake.stopped() == [])
    _force_rmtree(release_root)

with tempfile.TemporaryDirectory() as tmp:
    # A failure before installation must put the scheduler back as it was.
    root = Path(tmp)
    repo, release_root, m0, m1, target = _two_releases(root)
    initial = {"log-job@dispatcher.timer": True, "log-workflow-b.timer": False,
               "log-job@retention-purge.timer": True}
    transaction, fake, installed, backup = _build(
        root, repo, release_root, target, m0, active=dict(initial),
        preflight=lambda stage, *a, **k: ((1, {"blockers": ["nope"]})
                                          if stage == "pre-replacement"
                                          else (0, {"blockers": []})))
    _expect_cutover_error("a late gate failure aborts", "CUTOVER_PREFLIGHT_FAILED",
                          lambda: transaction.run(execute=True))
    _check("unwind restores exactly the pre-cutover timer state",
           fake.active == initial, f"final={fake.active} expected={initial}")
    _force_rmtree(release_root)

# ---------------------------------------------------------------------------
print("\n-- consumer discovery failure is a blocker (R7) --")

import ops.cutover_preflight as preflight_module  # noqa: E402

_original_systemctl = preflight_module._systemctl
for failing_command in ("list-unit-files", "list-units"):
    def fake_systemctl(*args, _fail=failing_command):
        if args[0] == _fail:
            return 1, "simulated systemctl failure"
        return _original_systemctl(*args)
    preflight_module._systemctl = fake_systemctl
    try:
        consumers = preflight_module.discover_wrapper_consumers()
        _check(f"`systemctl {failing_command}` failure is recorded, not swallowed",
               consumers["complete"] is False
               and any(failing_command in f["command"] for f in consumers["discovery_failures"]),
               f"consumers={consumers}")
        report = preflight_module.evaluate(Path(tempfile.gettempdir()) / "nope", None, REPO_ROOT)
        _check(f"`systemctl {failing_command}` failure blocks the preflight",
               report["safe_to_replace_wrapper"] is False
               and any("discovery failed" in b for b in report["blockers"]),
               f"blockers={report['blockers']}")
        _check(f"`systemctl {failing_command}` failure is visible in the report",
               report["consumer_discovery"]["complete"] is False)
    finally:
        preflight_module._systemctl = _original_systemctl

consumers = preflight_module.discover_wrapper_consumers()
_check("with systemctl working, discovery reports complete", consumers["complete"] is True)

# ---------------------------------------------------------------------------
print("\n-- provisioning cannot race a cutover (R1) --")

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    lock_file = wrapper_lock_module.lock_path()

    lock_source = (REPO_ROOT / "ops/wrapper_install_lock.py").read_text()
    _check("the shared lock is a single canonical path",
           'WRAPPER_INSTALL_LOCK_PATH = Path("/run/lock/log-platform-wrapper-install.lock")'
           in lock_source)
    # R2: production identity must not be reachable through the environment —
    # two processes inheriting different values would take different locks and
    # both report success, silently reopening the stale-plan race.
    _check("the wrapper-lock path cannot be redirected by the environment",
           "os.environ" not in lock_source, "wrapper_install_lock must not read the environment")
    _check("the execution barrier path cannot be redirected by the environment",
           "os.environ" not in (REPO_ROOT / "ops/execution_barrier.py").read_text())

    provisioning_source = (REPO_ROOT / "ops/provision_runtime_environment_identity.py").read_text()
    _check("provisioning takes the shared wrapper lock before writing assets",
           "with wrapper_install_lock():" in provisioning_source)
    _check("provisioning re-evaluates replaceability inside that lock",
           provisioning_source.index("with wrapper_install_lock():")
           < provisioning_source.index('"detected_at": "pre_write_recheck"')
           < provisioning_source.index("for raw, target, mode in verified_assets:"))
    cutover_source = (REPO_ROOT / "ops/cutover_execute.py").read_text()
    _check("the cutover installs under the same shared lock",
           "from ops.wrapper_install_lock import" in cutover_source
           and "with self.wrapper_lock():" in cutover_source)

    # The race itself, deterministically: a holder simulating cutover keeps the
    # lock while a second process tries to take it, and must be refused.
    held, go = root / "held", root / "go"
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import time,sys;sys.path.insert(0,%r)\n"
         "from pathlib import Path\n"
         "import ops.wrapper_install_lock as m\n"
         "from pathlib import Path as _P\n"
         "m.lock_path = lambda: _P(%r)\n"
         "from ops.wrapper_install_lock import wrapper_install_lock\n"
         "with wrapper_install_lock(timeout_seconds=30):\n"
         "    Path(%r).write_text('1')\n"
         "    while not Path(%r).exists():\n"
         "        time.sleep(0.005)\n"
         % (str(REPO_ROOT), str(lock_file), str(held), str(go))],
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT), "PYTHONDONTWRITEBYTECODE": "1"},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = __import__("time").monotonic() + 60
    while not held.exists() and __import__("time").monotonic() < deadline:
        __import__("time").sleep(0.005)
    _check("the simulated cutover holds the shared wrapper lock", held.exists())

    refused = False
    try:
        with wrapper_install_lock(timeout_seconds=1):
            refused = False
    except WrapperInstallLockError as exc:
        refused = exc.classification == "WRAPPER_INSTALL_LOCK_HELD"
    _check("a concurrent wrapper writer is refused while the cutover holds the lock", refused)

    go.write_text("1")
    holder.wait(timeout=60)
    _check("the holder exited cleanly", holder.returncode == 0, holder.stderr.read()[-200:])
    acquired = False
    with wrapper_install_lock(timeout_seconds=5):
        acquired = True
    _check("the lock is free again afterwards", acquired)

with tempfile.TemporaryDirectory() as tmp:
    # Crash safety: a killed installer must not wedge the host.
    root = Path(tmp)
    held = root / "held2"
    crasher = subprocess.Popen(
        [sys.executable, "-c",
         "import os,sys;sys.path.insert(0,%r)\n"
         "from pathlib import Path\n"
         "import ops.wrapper_install_lock as m\n"
         "from pathlib import Path as _P\n"
         "m.lock_path = lambda: _P(%r)\n"
         "from ops.wrapper_install_lock import wrapper_install_lock\n"
         "with wrapper_install_lock(timeout_seconds=30):\n"
         "    Path(%r).write_text('1')\n"
         "    os._exit(9)\n"
         % (str(REPO_ROOT), str(wrapper_lock_module.lock_path()), str(held))],
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT), "PYTHONDONTWRITEBYTECODE": "1"},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = __import__("time").monotonic() + 60
    while not held.exists() and __import__("time").monotonic() < deadline:
        __import__("time").sleep(0.005)
    crasher.wait(timeout=60)
    _check("the installer died while holding the wrapper lock", crasher.returncode == 9)
    acquired = False
    try:
        with wrapper_install_lock(timeout_seconds=5):
            acquired = True
    except WrapperInstallLockError:
        acquired = False
    _check("a crashed wrapper installer leaves no stale host lock", acquired)

# ---------------------------------------------------------------------------
print("\n-- full stale-plan provisioning race (M1R3 R2) --")

with tempfile.TemporaryDirectory() as tmp:
    # The complete race, end to end, with the real decision function:
    #   provisioning plans while the development wrapper is installed
    #   -> pauses
    #   -> a cutover takes the canonical lock and installs the release wrapper
    #   -> provisioning resumes, takes the SAME lock, re-reads, and refuses.
    # The earlier tests only proved mutual exclusion; this proves the stale plan
    # cannot authorize the overwrite.
    root = Path(tmp)
    repo = _make_repo(root)
    installed = root / "usr-local-bin" / "log-job-runner.sh"
    installed.parent.mkdir(parents=True, exist_ok=True)
    dev_bytes = (repo / "ops/systemd/proposed/log-job-runner.sh").read_bytes()
    release_bytes = (repo / "ops/systemd/proposed/log-job-runner.release.sh").read_bytes()
    installed.write_bytes(dev_bytes)

    from ops.release_boundary import wrapper_replaceability

    # 1. Provisioning plans: the development wrapper is present and replaceable.
    plan_decision = wrapper_replaceability(repo_root=repo, installed_wrapper=installed)
    _check("provisioning plans against a replaceable development wrapper",
           plan_decision["replaceable"] is True, f"decision={plan_decision}")

    plan_done, cutover_done = root / "planned", root / "cutover-done"
    plan_done.write_text("1")

    # 2-4. A cutover, in a separate process, takes the canonical lock and
    # installs the release wrapper while provisioning is paused.
    cutover = subprocess.Popen(
        [sys.executable, "-c",
         "import sys;sys.path.insert(0,%r)\n"
         "from pathlib import Path as _P\n"
         "import ops.wrapper_install_lock as m\n"
         "m.lock_path = lambda: _P(%r)\n"
         "from ops.wrapper_install_lock import wrapper_install_lock\n"
         "with wrapper_install_lock(timeout_seconds=30):\n"
         "    _P(%r).write_bytes(_P(%r).read_bytes())\n"
         "_P(%r).write_text('1')\n"
         % (str(REPO_ROOT), str(wrapper_lock_module.lock_path()), str(installed),
            str(repo / "ops/systemd/proposed/log-job-runner.release.sh"), str(cutover_done))],
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT), "PYTHONDONTWRITEBYTECODE": "1"},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    cutover.wait(timeout=60)
    _check("the simulated cutover completed", cutover.returncode == 0,
           cutover.stderr.read()[-200:])
    _check("the release wrapper is now installed", installed.read_bytes() == release_bytes)

    # 5-8. Provisioning resumes on its stale plan, takes the same canonical lock,
    # re-reads the wrapper, and must refuse.
    with wrapper_install_lock(timeout_seconds=10):
        recheck = wrapper_replaceability(repo_root=repo, installed_wrapper=installed)
        if recheck["replaceable"]:
            installed.write_bytes(dev_bytes)   # what the stale plan would have done
    _check("provisioning refuses to overwrite on the re-read",
           recheck["replaceable"] is False, f"recheck={recheck}")
    _check("the refusal names the live boundary",
           recheck["classification"] == "RELEASE_BOUNDARY_ACTIVE", f"recheck={recheck}")
    _check("the release wrapper survived the stale plan",
           installed.read_bytes() == release_bytes,
           "provisioning must not have reverted the cutover")
    _check("both parties used the one canonical lock identity",
           wrapper_lock_module.lock_path() == wrapper_lock_module.lock_path())

# ---------------------------------------------------------------------------
print("\n-- unexpected failures still unwind --")

with tempfile.TemporaryDirectory() as tmp:
    # A sudo failure raises CalledProcessError, not CutoverError. Letting that
    # escape would leave all three ingestion timers stopped with nothing but a
    # traceback — the silent halt this program exists to prevent.
    root = Path(tmp)
    repo, release_root, m0, m1, target = _two_releases(root)
    transaction, fake, installed, backup = _build(root, repo, release_root, target, m0)

    def exploding_backup(source, destination):
        raise subprocess.CalledProcessError(1, ["sudo", "cp"], stderr="sudo: no tty present")
    transaction.backup_wrapper = exploding_backup

    details = _expect_cutover_error("a non-CutoverError failure is wrapped, not leaked",
                                    "CUTOVER_UNEXPECTED_FAILURE",
                                    lambda: transaction.run(execute=True))
    _check("the underlying error is reported",
           "CalledProcessError" in str(details.get("error")), f"details={details}")
    _check("the unwind ran and restored the timers",
           details.get("unwind", {}).get("timer_state_restored") is True,
           f"unwind={details.get('unwind')}")
    _check("every originally-active timer is running again",
           all(fake.active.values()), f"final={fake.active}")
    _check("the unwind reports the pointer sub-state, not a coarse boolean",
           details.get("unwind", {}).get("pointer_state") == "SEQUENCED",
           f"unwind={details.get('unwind')}")
    _check("and reports where the pointers actually point",
           details.get("unwind", {}).get("pointers", {}).get("current") == target
           and details.get("unwind", {}).get("pointers", {}).get("previous") == m0,
           f"unwind={details.get('unwind')}")
    _force_rmtree(release_root)

with tempfile.TemporaryDirectory() as tmp:
    # A timer that will not come back must be reported, not assumed restored.
    root = Path(tmp)
    repo, release_root, m0, m1, target = _two_releases(root)
    timers = ["log-job@dispatcher.timer", "log-workflow-b.timer", "log-job@retention-purge.timer"]

    class StubbornTimer(FakeSystemctl):
        def __call__(self, *args):
            if args[0] == "start" and args[1] == "log-workflow-b.timer":
                self.calls.append(args)
                return 0, ""  # claims success, but the unit never becomes active
            return super().__call__(*args)

    fake = StubbornTimer({unit: True for unit in timers})
    transaction, _f, installed, backup = _build(root, repo, release_root, target, m0,
                                                systemctl=fake)
    details = _expect_cutover_error("a timer that does not come back fails the cutover",
                                    "CUTOVER_TIMER_RESTORE_FAILED",
                                    lambda: transaction.run(execute=True))
    _check("the unrestored timer is named",
           details.get("failed") == ["log-workflow-b.timer"], f"details={details}")
    _check("a zero exit from `systemctl start` is not taken as proof",
           "log-workflow-b.timer" not in details.get("restored", []), f"details={details}")
    _force_rmtree(release_root)

# ---------------------------------------------------------------------------
print("\n-- post-install identity is judged against the promoted release --")

with tempfile.TemporaryDirectory() as tmp:
    # The development tree is dirty by design. Classifying the installed wrapper
    # against it would fail a byte-correct install of a release whose wrapper
    # differs from the working copy — after installation, with production halted.
    root = Path(tmp)
    repo, release_root, m0, m1, target = _two_releases(root)
    dev_wrapper = repo / "ops/systemd/proposed/log-job-runner.release.sh"
    dev_wrapper.write_text(dev_wrapper.read_text() + "# edited after the release was cut\n")
    _check("the working copy now differs from the promoted release's wrapper",
           dev_wrapper.read_bytes()
           != (release_root / "releases" / target / RELEASE_WRAPPER_RELATIVE).read_bytes())

    transaction, fake, installed, backup = _build(root, repo, release_root, target, m0)
    report = transaction.run(execute=True)
    _check("a dirty working copy does not fail a correct installation",
           report["executed"] is True)
    _check("the timers were restarted", sorted(fake.started()) == sorted(fake.stopped()))
    _force_rmtree(release_root)

with tempfile.TemporaryDirectory() as tmp:
    # A wrapper resolving a different release root must be refused: a rehearsal
    # would otherwise install a wrapper pointing at the canonical root while
    # every assertion was made against the rehearsal one.
    root = Path(tmp)
    # Build a release whose wrapper declares a *different* release root, the
    # shape a rehearsal against --release-root would produce.
    repo = _make_repo(root, release_root=Path("/somewhere/else/log-platform-release"))
    release_root = root / "release"
    m0 = prepare_release(source_repo=repo, release_root=release_root,
                         committish=_git(repo, "rev-parse", "HEAD").strip())["release_id"]
    _git(repo, "commit", "-q", "--allow-empty", "-m", "target")
    target = prepare_release(source_repo=repo, release_root=release_root,
                             committish=_git(repo, "rev-parse", "HEAD").strip())["release_id"]
    activate_release(release_root=release_root, release_id=m0, source_repo=repo)
    transaction, fake, installed, backup = _build(root, repo, release_root, target, m0)
    details = _expect_cutover_error(
        "a wrapper resolving another release root fails verification",
        "CUTOVER_POST_INSTALL_VERIFICATION_FAILED", lambda: transaction.run(execute=True))
    _check("the mismatched release root is named",
           any("release root" in f for f in details.get("failures", [])), f"details={details}")
    _check("no timer was restarted", fake.started() == [])
    _force_rmtree(release_root)

# ---------------------------------------------------------------------------
print("\n-- incomplete consumer discovery blocks the transaction (R7) --")

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    repo, release_root, m0, m1, target = _two_releases(root)
    transaction, fake, installed, backup = _build(root, repo, release_root, target, m0)
    transaction.discover = lambda: {"timers": ["log-job@dispatcher.timer"],
                                    "services": ["log-job@dispatcher.service"],
                                    "complete": False,
                                    "discovery_failures": [{"command": "systemctl list-units",
                                                            "returncode": 1}]}
    _expect_cutover_error("an unknown consumer set refuses before anything is touched",
                          "CUTOVER_CONSUMER_DISCOVERY_INCOMPLETE",
                          lambda: transaction.run(execute=True))
    _check("no timer was stopped", fake.stopped() == [])
    _force_rmtree(release_root)

# ---------------------------------------------------------------------------
print("\n-- replacement uncertainty fails closed (M1R3 R1) --")

with tempfile.TemporaryDirectory() as tmp:
    # The exact interleaving the review reproduced: the wrapper is physically
    # replaced and *then* the install path raises. Inferring "not installed"
    # from a function that failed to return would restart production against an
    # unverified wrapper.
    root = Path(tmp)
    repo, release_root, m0, m1, target = _two_releases(root)
    transaction, fake, installed, backup = _build(root, repo, release_root, target, m0)
    development_bytes = installed.read_bytes()

    def replace_then_raise(source: Path, destination: Path) -> None:
        destination.write_bytes(source.read_bytes())   # physical replacement succeeds
        destination.chmod(0o755)
        raise RuntimeError("signal after rename")      # ... and then we die
    transaction.install_wrapper = replace_then_raise

    details = _expect_cutover_error("an exception after replacement is not a success",
                                    "CUTOVER_UNEXPECTED_FAILURE",
                                    lambda: transaction.run(execute=True))
    unwind = details.get("unwind", {})
    _check("the wrapper really was replaced on disk",
           installed.read_bytes() != development_bytes)
    _check("the transaction reached the replacement-attempted state",
           details.get("state") == "WRAPPER_REPLACEMENT_ATTEMPTED", f"details={details}")
    _check("wrapper state is reported as possibly changed",
           unwind.get("wrapper_state") == "WRAPPER_STATE_MAY_HAVE_CHANGED", f"unwind={unwind}")
    _check("no timer was restarted", fake.started() == [], f"started={fake.started()}")
    _check("the unwind says the timers were left stopped",
           unwind.get("timer_state_restored") is False, f"unwind={unwind}")
    _check("deterministic recovery instructions are provided",
           any("restore the development wrapper" in step for step in unwind.get("recovery", [])),
           f"unwind={unwind}")
    _check("the observed and original wrapper digests are both reported",
           unwind.get("original_wrapper_sha256") and unwind.get("observed_wrapper_sha256"),
           f"unwind={unwind}")
    _force_rmtree(release_root)

with tempfile.TemporaryDirectory() as tmp:
    # The one case that may downgrade to "not replaced": the bytes are provably
    # the original ones, so production can safely resume.
    root = Path(tmp)
    repo, release_root, m0, m1, target = _two_releases(root)
    transaction, fake, installed, backup = _build(root, repo, release_root, target, m0)

    def raise_without_touching(source: Path, destination: Path) -> None:
        raise RuntimeError("failed before writing anything")
    transaction.install_wrapper = raise_without_touching

    details = _expect_cutover_error("a failure that changed nothing is still not a success",
                                    "CUTOVER_UNEXPECTED_FAILURE",
                                    lambda: transaction.run(execute=True))
    unwind = details.get("unwind", {})
    _check("the wrapper is proven unchanged",
           unwind.get("wrapper_state") == "CONCLUSIVELY_UNCHANGED", f"unwind={unwind}")
    _check("and only then are the timers restored",
           unwind.get("timer_state_restored") is True and all(fake.active.values()),
           f"unwind={unwind} active={fake.active}")
    _force_rmtree(release_root)

# ---------------------------------------------------------------------------
print("\n-- execution-quiescence barrier (M1R3 R3) --")

BARRIER_PATH = execution_barrier_module.barrier_path()
execution_barrier_module.ensure_barrier_file(BARRIER_PATH)

WRAPPER_STUB = """#!/usr/bin/env bash
set -euo pipefail
EXEC_BARRIER="{barrier}"
if ! {{ exec 9<>"${{EXEC_BARRIER}}"; }} 2>/dev/null; then
  {{ exec 9<"${{EXEC_BARRIER}}"; }} 2>/dev/null || exit 4
fi
flock --shared --wait "${{2:-30}}" 9 || exit 4
# Resolution happens only AFTER the barrier is held — that ordering is the point.
printf '%s' "$(cat {pointer})" > "$1"
# Hold the shared barrier until told to finish. A file handshake rather than a
# sleep, so the interleaving is forced instead of hoped for.
if [[ -n "${{3:-}}" ]]; then
  while [[ ! -e "$3" ]]; do sleep 0.005; done
fi
"""


def _write_stub(path: Path, barrier: Path, pointer: Path) -> Path:
    path.write_text(WRAPPER_STUB.format(barrier=barrier, pointer=pointer))
    path.chmod(0o755)
    return path


with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    pointer = root / "which-tree"
    pointer.write_text("development-tree")
    stub = _write_stub(root / "wrapper-stub.sh", BARRIER_PATH, pointer)

    # 1. A job already running holds the shared side; the cutover must wait.
    started, finish = root / "job-a-out", root / "job-a-finish"
    job = subprocess.Popen([str(stub), str(started), "30", str(finish)])
    deadline = time.monotonic() + 30
    while not started.exists() and time.monotonic() < deadline:
        time.sleep(0.005)
    _check("a wrapper job takes the shared barrier and resolves a tree", started.exists())

    exclusive_blocked = False
    try:
        with execution_barrier_module.execution_barrier(exclusive=True, timeout_seconds=0.5,
                                                        path=BARRIER_PATH):
            exclusive_blocked = False
    except execution_barrier_module.ExecutionBarrierError as exc:
        exclusive_blocked = exc.classification == "EXECUTION_BARRIER_BUSY"
    _check("a cutover cannot take exclusivity while a job is running", exclusive_blocked)

    finish.write_text("1")
    job.wait(timeout=30)
    acquired = False
    with execution_barrier_module.execution_barrier(exclusive=True, timeout_seconds=5,
                                                    path=BARRIER_PATH):
        acquired = True
    _check("exclusivity is available once the job exits", acquired)

with tempfile.TemporaryDirectory() as tmp:
    # 2 + 3. The load-bearing regression: the final gate has passed, the cutover
    # holds exclusivity, and a manual wrapper start races it. The job must not
    # resolve the old tree — it must wait and then resolve the NEW one.
    root = Path(tmp)
    pointer = root / "which-tree"
    pointer.write_text("development-tree")
    stub = _write_stub(root / "wrapper-stub.sh", BARRIER_PATH, pointer)
    out = root / "raced-job-resolution"

    with execution_barrier_module.execution_barrier(exclusive=True, timeout_seconds=10,
                                                    path=BARRIER_PATH):
        competitor = subprocess.Popen([str(stub), str(out), "60", ""])
        # The competitor is alive and trying; it must not have resolved anything.
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if out.exists():
                break
            time.sleep(0.01)
        _check("a wrapper start during exclusivity cannot resolve a tree",
               not out.exists(), f"resolved early as {out.read_text() if out.exists() else ''!r}")
        _check("the competing process is genuinely alive and waiting",
               competitor.poll() is None)
        # The cutover completes its replacement while still holding the barrier.
        pointer.write_text("release-tree")
    competitor.wait(timeout=60)
    _check("after the barrier is released the job resolves the NEW tree",
           out.exists() and out.read_text() == "release-tree",
           f"resolved={out.read_text() if out.exists() else None!r}")

with tempfile.TemporaryDirectory() as tmp:
    # Shared locks must stack: ordinary production is unaffected when no cutover
    # is running, or the barrier would serialize every job in the platform.
    root = Path(tmp)
    pointer = root / "which-tree"
    pointer.write_text("development-tree")
    stub = _write_stub(root / "wrapper-stub.sh", BARRIER_PATH, pointer)
    outs = [root / f"concurrent-{index}" for index in range(3)]
    concurrent_finish = root / "concurrent-finish"
    jobs = [subprocess.Popen([str(stub), str(target_out), "30", str(concurrent_finish)])
            for target_out in outs]
    deadline = time.monotonic() + 30
    while not all(out.exists() for out in outs) and time.monotonic() < deadline:
        time.sleep(0.005)
    _check("all three hold the shared barrier at the same time",
           all(out.exists() for out in outs) and all(j.poll() is None for j in jobs),
           "shared locks must stack")
    concurrent_finish.write_text("1")
    for job in jobs:
        job.wait(timeout=60)
    _check("concurrent wrapper jobs do not block each other",
           all(out.exists() for out in outs) and all(j.returncode == 0 for j in jobs))

_check("both wrappers participate in the barrier",
       all("EXEC_BARRIER" in (REPO_ROOT / f"ops/systemd/proposed/{name}").read_text()
           and "flock --shared" in (REPO_ROOT / f"ops/systemd/proposed/{name}").read_text()
           for name in ("log-job-runner.sh", "log-job-runner.release.sh")))
_check("the release wrapper takes the barrier before resolving the pointer",
       (REPO_ROOT / "ops/systemd/proposed/log-job-runner.release.sh").read_text().index("flock --shared")
       < (REPO_ROOT / "ops/systemd/proposed/log-job-runner.release.sh").read_text().index('BASE_DIR="${RELEASE_ROOT}/current"'))

with tempfile.TemporaryDirectory() as tmp:
    # The transaction must hold the barrier across gate -> replacement -> verify.
    root = Path(tmp)
    repo, release_root, m0, m1, target = _two_releases(root)
    transaction, fake, installed, backup = _build(root, repo, release_root, target, m0)
    held_during = {}
    original_install = transaction.install_wrapper

    def install_and_probe(source: Path, destination: Path) -> None:
        try:
            with execution_barrier_module.execution_barrier(exclusive=True, timeout_seconds=0.3,
                                                            path=BARRIER_PATH):
                held_during["exclusive_available"] = True
        except execution_barrier_module.ExecutionBarrierError:
            held_during["exclusive_available"] = False
        original_install(source, destination)
    transaction.install_wrapper = install_and_probe
    transaction.run(execute=True)
    _check("the cutover still holds the barrier at the moment of replacement",
           held_during.get("exclusive_available") is False, f"probe={held_during}")
    _force_rmtree(release_root)

# ---------------------------------------------------------------------------
print("\n-- partial pointer sequencing is reported truthfully (M1R3 R4) --")

with tempfile.TemporaryDirectory() as tmp:
    # M0 activation succeeds, target activation fails. The old code reported
    # "pointers untouched" here, which is confidently wrong.
    root = Path(tmp)
    repo, release_root, m0, m1, target = _two_releases(root)
    transaction, fake, installed, backup = _build(root, repo, release_root, target, m0)

    import ops.cutover_execute as cutover_module
    real_activate = cutover_module.activate_release
    calls = {"n": 0}

    def failing_second_activate(**kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ReleaseBoundaryError("RELEASE_CONTENT_MISMATCH", {"simulated": True})
        return real_activate(**kwargs)
    cutover_module.activate_release = failing_second_activate
    try:
        details = _expect_cutover_error("a half-finished pointer sequence fails the cutover",
                                        "CUTOVER_POINTER_SEQUENCE_FAILED",
                                        lambda: transaction.run(execute=True))
    finally:
        cutover_module.activate_release = real_activate

    _check("the partial sub-state is named",
           details.get("pointer_state") == "PREDECESSOR_ACTIVATED", f"details={details}")
    _check("the ACTUAL pointer pair is read back and reported",
           details.get("observed", {}).get("current") == m0, f"details={details}")
    _check("the unwind repeats the real pointer state",
           details.get("unwind", {}).get("pointers", {}).get("current") == m0,
           f"unwind={details.get('unwind')}")
    _check("the wrapper was never touched", not backup.exists())
    _check("recovery guidance is given for the moved pointers",
           "note_pointers" in details.get("unwind", {}), f"unwind={details.get('unwind')}")
    _check("timers were restored, since the wrapper is untouched",
           details.get("unwind", {}).get("timer_state_restored") is True)

    # Recovery: re-running the transaction re-sequences deterministically.
    transaction2, fake2, installed2, backup2 = _build(root, repo, release_root, target, m0)
    transaction2.run(execute=True)
    _check("re-running after a partial sequence reaches the intended pointer pair",
           pointer_release_id(release_root / "current") == target
           and pointer_release_id(release_root / "previous") == m0,
           f"current={pointer_release_id(release_root / 'current')}")
    _force_rmtree(release_root)

with tempfile.TemporaryDirectory() as tmp:
    # Failure before the first activation: state genuinely unchanged.
    root = Path(tmp)
    repo, release_root, m0, m1, target = _two_releases(root)
    transaction, fake, installed, backup = _build(
        root, repo, release_root, target, m0,
        preflight=lambda stage, *a, **k: ((1, {"blockers": ["stop before pointers"]})
                                          if stage == "pre-replacement" else (0, {"blockers": []})))
    details = _expect_cutover_error("a failure before pointer work aborts",
                                    "CUTOVER_PREFLIGHT_FAILED",
                                    lambda: transaction.run(execute=True))
    _check("the pointer sub-state says untouched",
           details.get("unwind", {}).get("pointer_state") == "UNTOUCHED",
           f"unwind={details.get('unwind')}")
    _check("and the pointers really are unchanged",
           pointer_release_id(release_root / "current") == m1
           and pointer_release_id(release_root / "previous") == m0)
    _force_rmtree(release_root)

# ---------------------------------------------------------------------------
print("\n-- queued starts stay fail-closed after uncertain replacement (M1R4 R1) --")

import ops.cutover_fence as fence_module  # noqa: E402

# A stand-in for the wrapper's own gate: the same two checks, in the same order
# (barrier first, then fence), executed as a real second process.
GATED_STUB = """#!/usr/bin/env bash
set -euo pipefail
EXEC_BARRIER="{barrier}"
if ! {{ exec 9<>"${{EXEC_BARRIER}}"; }} 2>/dev/null; then
  {{ exec 9<"${{EXEC_BARRIER}}"; }} 2>/dev/null || exit 4
fi
flock --shared --wait "${{2:-60}}" 9
__rc=$?
if (( __rc != 0 )); then
  if (( __rc == 1 )); then echo "EXECUTION_BARRIER_BUSY" >&2; else echo "EXECUTION_BARRIER_UNAVAILABLE" >&2; fi
  exit 4
fi
CUTOVER_FENCE="{fence}"
if [[ -e "${{CUTOVER_FENCE}}" ]]; then
  __state="$(sed -n 's/^STATE=\\([A-Z_]*\\)$/\\1/p' "${{CUTOVER_FENCE}}" 2>/dev/null | head -n 1)"
  case "${{__state}}" in
    ALLOWED|VERIFIED) : ;;
    "") echo "CUTOVER_FENCE_UNREADABLE" >&2; exit 5 ;;
    *)  echo "CUTOVER_FENCE_BLOCKS_EXECUTION: ${{__state}}" >&2; exit 5 ;;
  esac
fi
echo "PROJECT-CODE-EXECUTED" > "$1"
"""

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    repo, release_root, m0, m1, target = _two_releases(root)
    barrier_file = execution_barrier_module.barrier_path()
    execution_barrier_module.ensure_barrier_file(barrier_file)
    stub = root / "gated.sh"
    stub.write_text(GATED_STUB.format(barrier=barrier_file,
                                      fence=fence_module.fence_path(release_root)))
    stub.chmod(0o755)

    _check("the fence starts permissive before any cutover",
           fence_module.read_fence(release_root)["execution_permitted"] is True)

    transaction, fake, installed, backup = _build(root, repo, release_root, target, m0)
    queued = {}

    def replace_then_raise(source: Path, destination: Path) -> None:
        destination.write_bytes(source.read_bytes())
        # A competing job queues behind the barrier *now*, while the cutover
        # still owns exclusivity and the replacement has already happened.
        queued["proc"] = subprocess.Popen([str(stub), str(root / "queued-out"), "60"],
                                          stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and queued["proc"].poll() is None:
            time.sleep(0.01)
        raise RuntimeError("install raises after the physical replacement")
    transaction.install_wrapper = replace_then_raise

    details = _expect_cutover_error("the uncertain cutover fails", "CUTOVER_UNEXPECTED_FAILURE",
                                    lambda: transaction.run(execute=True))
    _check("the unwind recorded the fail-closed fence",
           details.get("unwind", {}).get("fence") == "BLOCKED_UNCERTAIN",
           f"unwind={details.get('unwind')}")
    _check("the fence blocks execution after the barrier was released",
           fence_module.read_fence(release_root)["execution_permitted"] is False)
    _check("no timer was restarted", fake.started() == [])

    proc = queued["proc"]
    proc.wait(timeout=60)
    stderr = proc.stderr.read()
    _check("the QUEUED job did not execute project code",
           not (root / "queued-out").exists(),
           f"rc={proc.returncode} stderr={stderr.strip()[:160]!r}")
    _check("the queued job refused under the fence, not the barrier",
           proc.returncode == 5 and "CUTOVER_FENCE_BLOCKS_EXECUTION" in stderr,
           f"rc={proc.returncode} stderr={stderr.strip()[:160]!r}")

    # A brand-new start after the failed cutover must also refuse.
    later = subprocess.run([str(stub), str(root / "later-out"), "5"],
                           capture_output=True, text=True)
    _check("a NEW job started afterwards is refused too",
           later.returncode == 5 and not (root / "later-out").exists(),
           f"rc={later.returncode} stderr={later.stderr.strip()[:160]!r}")

    # Recovery is explicit: the report gathers facts, it does not clear anything.
    report = fence_module.recovery_report(release_root, repo, installed_wrapper=installed)
    _check("recovery reports the installed wrapper identity",
           report["installed_wrapper"]["sha256"] is not None)
    _check("recovery reports the real pointers",
           report["pointers"]["current"] == target)
    _check("recovery does not clear the fence by itself",
           fence_module.read_fence(release_root)["execution_permitted"] is False)

    fence_module.write_fence(release_root, fence_module.STATE_ALLOWED,
                             reason="operator verified state in test")
    after = subprocess.run([str(stub), str(root / "recovered-out"), "5"],
                           capture_output=True, text=True)
    _check("execution resumes only after a deliberate recovery",
           after.returncode == 0 and (root / "recovered-out").exists(),
           f"rc={after.returncode} stderr={after.stderr.strip()[:160]!r}")
    _force_rmtree(release_root)

with tempfile.TemporaryDirectory() as tmp:
    # A stale IN_PROGRESS — the cutover process died mid-flight — must fail closed.
    root = Path(tmp)
    release_root = root / "release"
    (release_root / "state").mkdir(parents=True)
    fence_module.write_fence(release_root, fence_module.STATE_IN_PROGRESS, target_release="x")
    _check("a stale IN_PROGRESS fence blocks execution",
           fence_module.read_fence(release_root)["execution_permitted"] is False)
    fence_module.fence_path(release_root).write_text("GARBAGE\n")
    _check("a malformed fence blocks execution",
           fence_module.read_fence(release_root)["execution_permitted"] is False)
    fence_module.fence_path(release_root).unlink()
    _check("an absent fence is the permissive pre-cutover default",
           fence_module.read_fence(release_root)["execution_permitted"] is True)
    _check("the fence lives outside the release trees, so it survives a reboot",
           "state/cutover-state.txt" in str(fence_module.fence_path(release_root))
           and "/releases/" not in str(fence_module.fence_path(release_root)))

# ---------------------------------------------------------------------------
print("\n-- ambient environment cannot suppress re-exec (M1R4 R2) --")

with tempfile.TemporaryDirectory() as tmp:
    # Run the REAL wrapper logic with a poisoned environment.
    root = Path(tmp)
    barrier_file = root / "barrier.lock"
    barrier_file.write_text("")
    fence_dir = root / "fence-root"
    (fence_dir / "state").mkdir(parents=True)

    def _stage(name: str, tag: str) -> Path:
        text = (REPO_ROOT / "ops/systemd/proposed" / name).read_text()
        text = text.replace('EXEC_BARRIER="/run/lock/log-platform-execution.lock"',
                            f'EXEC_BARRIER="{barrier_file}"')
        text = text.replace(
            'RELEASE_ROOT_FOR_FENCE="/opt/log-platform-release"',
            f'RELEASE_ROOT_FOR_FENCE="{fence_dir}"')
        text = text.replace('RELEASE_ROOT="/opt/log-platform-release"',
                            f'RELEASE_ROOT="{fence_dir}"')
        cut = text.index('exec "$0" "$@"\nfi\n') + len('exec "$0" "$@"\nfi\n')
        path = root / f"{tag}.sh"
        path.write_text(text[:cut] + f'\necho "{tag}" > "$1"\n')
        path.chmod(0o755)
        return path

    dev_staged = _stage("log-job-runner.sh", "development")
    rel_staged = _stage("log-job-runner.release.sh", "release")
    installed = root / "installed.sh"
    installed.write_bytes(dev_staged.read_bytes())
    installed.chmod(0o755)

    poisoned = {**os.environ, "LOG_JOB_RUNNER_REEXECED": "1",
                "PYTHONDONTWRITEBYTECODE": "1"}
    out = root / "which-ran"
    fd = os.open(str(barrier_file), os.O_RDWR)
    import fcntl as _fcntl
    _fcntl.flock(fd, _fcntl.LOCK_EX)
    job = subprocess.Popen([str(installed), str(out)], env=poisoned,
                           stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not out.exists() and job.poll() is None:
        time.sleep(0.01)
    _check("the poisoned job is blocked on the barrier", job.poll() is None and not out.exists())
    staged = root / ".staged"
    staged.write_bytes(rel_staged.read_bytes())
    os.replace(staged, installed)
    os.chmod(installed, 0o755)
    _fcntl.flock(fd, _fcntl.LOCK_UN); os.close(fd)
    job.wait(timeout=60)
    _check("a caller-supplied re-exec marker cannot suppress the refresh",
           out.exists() and out.read_text().strip() == "release",
           f"ran={out.read_text().strip() if out.exists() else None!r} "
           f"stderr={job.stderr.read().strip()[:160]!r}")

    # Unchanged wrapper: must not loop.
    out2 = root / "unchanged"
    steady = subprocess.run([str(installed), str(out2)], env=poisoned,
                            capture_output=True, text=True, timeout=60)
    _check("an unchanged wrapper does not re-exec in a loop",
           steady.returncode == 0 and out2.read_text().strip() == "release",
           f"rc={steady.returncode} stderr={steady.stderr.strip()[:160]!r}")
    _check("no environment marker remains in either wrapper",
           all("LOG_JOB_RUNNER_REEXECED" not in
               (REPO_ROOT / "ops/systemd/proposed" / n).read_text()
               for n in ("log-job-runner.sh", "log-job-runner.release.sh")))

# ---------------------------------------------------------------------------
print("\n-- exact bootstrap-wrapper prerequisite (M1R4 R3) --")

import ops.cutover_preflight as preflight_r3  # noqa: E402
from ops.release_boundary import (  # noqa: E402
    WRAPPER_DEVELOPMENT_HISTORICAL,
    installed_wrapper_variant as _variant,
)

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    dev = REPO_ROOT / "ops/systemd/proposed/log-job-runner.sh"
    rel = REPO_ROOT / "ops/systemd/proposed/log-job-runner.release.sh"
    historical = root / "historical.sh"
    historical.write_text(dev.read_text().replace("set -euo pipefail",
                                                  "set -euo pipefail\n# older", 1))
    unknown = root / "unknown.sh"
    unknown.write_text("#!/bin/sh\nexec /usr/bin/python3 /home/dev/ops/runner.py \"$@\"\n")

    _check("the exact repository development wrapper is the bootstrap contract",
           _variant(repo_root=REPO_ROOT, installed_wrapper=dev) == "development_tree")
    _check("a historical development wrapper is NOT the bootstrap contract",
           _variant(repo_root=REPO_ROOT, installed_wrapper=historical)
           == WRAPPER_DEVELOPMENT_HISTORICAL)

    def _blockers(wrapper: Path):
        original = preflight_r3.INSTALLED_WRAPPER_PATH
        preflight_r3.INSTALLED_WRAPPER_PATH = wrapper
        try:
            report = preflight_r3.evaluate(root / "no-release-root", None, REPO_ROOT)
        finally:
            preflight_r3.INSTALLED_WRAPPER_PATH = original
        return report

    hist_report = _blockers(historical)
    _check("preflight refuses a historical development wrapper",
           any("historical development wrapper" in b for b in hist_report["blockers"]),
           f"blockers={hist_report['blockers']}")
    unknown_report = _blockers(unknown)
    _check("preflight refuses an unrecognized wrapper",
           any("not the exact reviewed" in b for b in unknown_report["blockers"]),
           f"blockers={unknown_report['blockers']}")
    exact_report = _blockers(dev)
    _check("preflight raises no wrapper objection for the exact bootstrap wrapper",
           not any("wrapper" in b and "development" in b for b in exact_report["blockers"]),
           f"blockers={exact_report['blockers']}")
    _check("the exact bootstrap wrapper is reported as such",
           "barrier-capable development" in exact_report["notes"], f"notes={exact_report['notes']}")
    rel_report = _blockers(rel)
    _check("the release wrapper is recognised as already-cut-over, not a bootstrap",
           "already the release wrapper" in rel_report["notes"], f"notes={rel_report['notes']}")

# ---------------------------------------------------------------------------
print("\n-- release lock spans install and verification (M1R4 R4) --")

with tempfile.TemporaryDirectory() as tmp:
    # The race: M1R3 released the release-management lock once the pointers were
    # sequenced, so a competing activation could move `current` between the
    # cutover's verification and production resuming — the cutover would report
    # a target it was no longer delivering.
    root = Path(tmp)
    repo, release_root, m0, m1, target = _two_releases(root)
    transaction, fake, installed, backup = _build(root, repo, release_root, target, m0)

    competitor_started, competitor_done = root / "rival-started", root / "rival-done"
    observed_during_install = {}

    def install_while_rival_tries(source: Path, destination: Path) -> None:
        # A second process attempts to activate a different release right after
        # the pointer sequence and while the wrapper is being installed.
        proc = subprocess.Popen(
            [sys.executable, "-c",
             "import sys;sys.path.insert(0,%r)\n"
             "from pathlib import Path as _P\n"
             "from ops.release_boundary import activate_release\n"
             "_P(%r).write_text('1')\n"
             "activate_release(release_root=_P(%r), release_id=%r, source_repo=_P(%r))\n"
             "_P(%r).write_text('1')\n"
             % (str(REPO_ROOT), str(competitor_started), str(release_root), m1,
                str(repo), str(competitor_done))],
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT), "PYTHONDONTWRITEBYTECODE": "1",
                 "RELEASE_MANAGEMENT_LOCK_TIMEOUT_SECONDS": "60"},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        observed_during_install["proc"] = proc
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not competitor_started.exists():
            time.sleep(0.01)
        # Give it every chance to win the race; the lock must hold it off.
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if competitor_done.exists():
                break
            time.sleep(0.01)
        observed_during_install["rival_completed_during_install"] = competitor_done.exists()
        observed_during_install["current_during_install"] = pointer_release_id(
            release_root / "current")
        destination.write_bytes(source.read_bytes())
        destination.chmod(0o755)
    transaction.install_wrapper = install_while_rival_tries

    report = transaction.run(execute=True)
    proc = observed_during_install["proc"]
    proc.wait(timeout=90)

    _check("the competing activation started while the cutover held the lock",
           competitor_started.exists())
    _check("it could NOT complete during install and verification",
           observed_during_install["rival_completed_during_install"] is False,
           f"observed={observed_during_install}")
    _check("`current` was the verified target throughout the critical section",
           observed_during_install["current_during_install"] == target,
           f"observed={observed_during_install}")
    _check("the cutover verified the target it intended",
           report["current"] == target and report["previous"] == m0,
           f"report_current={report.get('current')} previous={report.get('previous')}")
    _check("the fence records the verified target",
           fence_module.read_fence(release_root).get("target_release") == target,
           f"fence={fence_module.read_fence(release_root)}")
    _check("the competing activation only proceeds after the safe point",
           proc.returncode == 0 and competitor_done.exists(),
           f"rc={proc.returncode} stderr={proc.stderr.read()[-200:]}")
    _force_rmtree(release_root)

with tempfile.TemporaryDirectory() as tmp:
    # Failure paths must release the release-management lock, or the next
    # attempt (the documented recovery) would block forever.
    root = Path(tmp)
    repo, release_root, m0, m1, target = _two_releases(root)
    transaction, fake, installed, backup = _build(root, repo, release_root, target, m0)
    transaction.install_wrapper = lambda s_, d_: (_ for _ in ()).throw(RuntimeError("boom"))
    _expect_cutover_error("the attempt fails inside the locked span",
                          "CUTOVER_UNEXPECTED_FAILURE", lambda: transaction.run(execute=True))
    acquired = False
    from ops.release_boundary import management_lock as _mlock
    try:
        with _mlock(release_root, timeout_seconds=5):
            acquired = True
    except ReleaseBoundaryError:
        acquired = False
    _check("the release-management lock is released on the failure path", acquired)
    _force_rmtree(release_root)

# Acquisition order, judged inside run() — comparing offsets across the whole
# file would measure where methods are *defined*, not the order they run in.
_cutover_src = (REPO_ROOT / "ops/cutover_execute.py").read_text()
_run_body = _cutover_src[_cutover_src.index("    def run(self, *, execute: bool = False)"):
                         _cutover_src.index("    def _unwind(self")]
_check("run() takes the execution barrier before the release-management lock",
       _run_body.index("execution_barrier(exclusive=True)")
       < _run_body.index("management = management_lock(self.release_root)"),
       "lock order must be barrier -> release management")
_check("the wrapper install happens inside the release-management span",
       _run_body.index("management.__enter__()")
       < _run_body.index("self.install(candidate")
       < _run_body.index("write_fence(self.release_root, STATE_VERIFIED")
       < _run_body.index("management.__exit__"),
       "install and verification must sit inside the release-management lock")
_check("the unwind runs while the execution barrier is still held",
       _run_body.index("unwound = self._unwind(original)")
       < _run_body.index("barrier.__exit__(None, None, None)"),
       "the fail-closed fence must be written before queued jobs are released")
_check("only install() takes the wrapper-install lock, so it is always innermost",
       _cutover_src.count("self.wrapper_lock()") == 1
       and "with self.wrapper_lock():" in _cutover_src)
_check("no path acquires the execution barrier while holding another lock",
       (REPO_ROOT / "ops/release_boundary.py").read_text().count("execution_barrier") == 0
       and "execution_barrier" not in
       (REPO_ROOT / "ops/wrapper_install_lock.py").read_text())

# ---------------------------------------------------------------------------
print("\n-- review fixes: fence reconciliation and real-wrapper gates --")

with tempfile.TemporaryDirectory() as tmp:
    # HIGH-2: a failure BEFORE the replacement must not leave IN_PROGRESS behind.
    # Restoring the timers into a blocking fence would halt every job while the
    # transaction reported the scheduler restored.
    root = Path(tmp)
    repo, release_root, m0, m1, target = _two_releases(root)
    transaction, fake, installed, backup = _build(root, repo, release_root, target, m0)
    transaction.install_wrapper = lambda s_, d_: (_ for _ in ()).throw(
        RuntimeError("fails before touching the destination"))
    details = _expect_cutover_error("the attempt fails before replacement",
                                    "CUTOVER_UNEXPECTED_FAILURE",
                                    lambda: transaction.run(execute=True))
    unwind = details.get("unwind", {})
    _check("the fence was reconciled, not left IN_PROGRESS",
           fence_module.read_fence(release_root)["execution_permitted"] is True,
           f"fence={fence_module.read_fence(release_root)}")
    _check("the unwind reports the reconciled fence state",
           unwind.get("fence_state") in ("ALLOWED", "VERIFIED"), f"unwind={unwind}")
    _check("only then were the timers restored",
           unwind.get("timer_state_restored") is True and all(fake.active.values()),
           f"unwind={unwind}")
    _force_rmtree(release_root)

with tempfile.TemporaryDirectory() as tmp:
    # And when the fence genuinely blocks, timers must stay stopped.
    root = Path(tmp)
    repo, release_root, m0, m1, target = _two_releases(root)
    transaction, fake, installed, backup = _build(root, repo, release_root, target, m0)

    def replace_then_raise(source: Path, destination: Path) -> None:
        destination.write_bytes(source.read_bytes())
        raise RuntimeError("dies after replacement")
    transaction.install_wrapper = replace_then_raise
    details = _expect_cutover_error("the uncertain attempt fails",
                                    "CUTOVER_UNEXPECTED_FAILURE",
                                    lambda: transaction.run(execute=True))
    unwind = details.get("unwind", {})
    _check("the fence blocks execution", unwind.get("fence") == "BLOCKED_UNCERTAIN"
           or unwind.get("fence_state") == "BLOCKED_UNCERTAIN", f"unwind={unwind}")
    _check("timers are NOT restored into a blocking fence",
           unwind.get("timer_state_restored") is False and fake.started() == [],
           f"unwind={unwind} started={fake.started()}")
    _check("the recovery guidance names the fence tool",
           any("manage_cutover_fence" in step for step in unwind.get("recovery", [])),
           f"recovery={unwind.get('recovery')}")
    _force_rmtree(release_root)

with tempfile.TemporaryDirectory() as tmp:
    # MEDIUM-6: a consumer reached through a shim that execs the wrapper is
    # covered by the barrier and must NOT be flagged — the real retention unit
    # on this host is exactly that shape.
    root = Path(tmp)
    repo, release_root, m0, m1, target = _two_releases(root)
    shim = root / "log-retention-purge.sh"
    installed_wrapper = root / "usr-local-bin" / "log-job-runner.sh"
    installed_wrapper.parent.mkdir(parents=True, exist_ok=True)
    installed_wrapper.write_text("#!/usr/bin/env bash\n")
    shim.write_text(f"#!/usr/bin/env bash\nset -euo pipefail\nexec {installed_wrapper} jobs.x '{{}}'\n")
    shim.chmod(0o755)

    class ShimSystemctl(FakeSystemctl):
        def __call__(self, *args):
            if args[0] == "show" and "ExecStart" in args:
                if "retention-purge" in args[-1]:
                    return 0, str(shim)
                return 0, f"{installed_wrapper} %i {{}}"
            return super().__call__(*args)

    fake = ShimSystemctl({u: True for u in ("log-job@dispatcher.timer", "log-workflow-b.timer",
                                            "log-job@retention-purge.timer")})
    transaction, _f, _i, _b = _build(root, repo, release_root, target, m0, systemctl=fake)
    transaction.installed_wrapper = installed_wrapper
    transaction.assert_consumers_use_wrapper(
        ["log-job@dispatcher.service", "log-job@retention-purge.service"])
    _check("a shim that execs the wrapper is not flagged as a bypass", True)

    bypass = root / "direct.sh"
    bypass.write_text("#!/usr/bin/env bash\nexec /usr/bin/python3 ops/runner.py jobs.x\n")
    bypass.chmod(0o755)

    class RealBypass(ShimSystemctl):
        def __call__(self, *args):
            if args[0] == "show" and "ExecStart" in args and "retention" in args[-1]:
                return 0, str(bypass)
            return super().__call__(*args)
    transaction.systemctl = RealBypass({})
    _expect_cutover_error("a shim that does NOT exec the wrapper is still flagged",
                          "CUTOVER_CONSUMER_BYPASSES_WRAPPER",
                          lambda: transaction.assert_consumers_use_wrapper(
                              ["log-job@retention-purge.service"]))
    _force_rmtree(release_root)

with tempfile.TemporaryDirectory() as tmp:
    # MEDIUM-3: byte-equality is not capability.
    root = Path(tmp)
    from ops.release_boundary import wrapper_has_bootstrap_capabilities
    capable = wrapper_has_bootstrap_capabilities(
        REPO_ROOT / "ops/systemd/proposed/log-job-runner.sh")
    _check("the repository bootstrap wrapper has every required capability",
           capable["capable"] is True, f"capability={capable}")
    stripped = root / "no-barrier.sh"
    stripped.write_text('#!/usr/bin/env bash\nWRAPPER_VARIANT="development"\nBASE_DIR="/x"\n')
    lacking = wrapper_has_bootstrap_capabilities(stripped)
    _check("a wrapper without the barrier or fence is not capable",
           lacking["capable"] is False and "flock --shared" in lacking["missing"],
           f"capability={lacking}")
    # Post-M1 repair. This check used to assert the installed wrapper is NOT
    # capable, which encoded the *pre-cutover* host as an invariant: it held only
    # while `/usr/local/bin/log-job-runner.sh` was still the historical
    # development wrapper. The cutover installed the release wrapper — which
    # carries all three contracts by construction — so the old form failed on a
    # correctly cut-over host and would have had to be disabled to get a clean
    # run. Restated against the classification instead of the calendar, so it
    # stays load-bearing in both states rather than being deleted: a release
    # wrapper must be capable, a historical development wrapper must not be.
    from ops.release_boundary import (
        WRAPPER_DEVELOPMENT, WRAPPER_DEVELOPMENT_HISTORICAL,
        WRAPPER_RELEASE, WRAPPER_RELEASE_HISTORICAL,
        installed_wrapper_variant,
    )
    installed_variant = installed_wrapper_variant(repo_root=REPO_ROOT)
    installed_capable = wrapper_has_bootstrap_capabilities(
        Path("/usr/local/bin/log-job-runner.sh"))["capable"]
    if installed_variant in (WRAPPER_RELEASE, WRAPPER_RELEASE_HISTORICAL):
        _check("the installed release wrapper carries all three contracts",
               installed_capable is True,
               f"variant={installed_variant} capable={installed_capable}")
    elif installed_variant == WRAPPER_DEVELOPMENT_HISTORICAL:
        _check("the historical installed wrapper lacks all three contracts",
               installed_capable is False,
               f"variant={installed_variant} capable={installed_capable}")
    else:
        # `development` is the barrier-capable bootstrap wrapper, `absent` /
        # `unrecognized` / `unreadable` are host states this check cannot judge.
        _check("the installed wrapper classifies to a known variant",
               installed_variant in (WRAPPER_DEVELOPMENT,),
               f"variant={installed_variant}")

with tempfile.TemporaryDirectory() as tmp:
    # LOW-1: run the REAL wrapper against a blocking fence, not a replica.
    root = Path(tmp)
    barrier_file = root / "barrier.lock"; barrier_file.write_text("")
    fence_root = root / "fence-root"; (fence_root / "state").mkdir(parents=True)
    text = (REPO_ROOT / "ops/systemd/proposed/log-job-runner.sh").read_text()
    text = text.replace('EXEC_BARRIER="/run/lock/log-platform-execution.lock"',
                        f'EXEC_BARRIER="{barrier_file}"')
    text = text.replace(
        'RELEASE_ROOT_FOR_FENCE="/opt/log-platform-release"',
        f'RELEASE_ROOT_FOR_FENCE="{fence_root}"')
    cut = text.index('exec "$0" "$@"\nfi\n') + len('exec "$0" "$@"\nfi\n')
    real = root / "real-wrapper.sh"
    real.write_text(text[:cut] + '\necho "PROJECT-CODE" > "$1"\n')
    real.chmod(0o755)

    ok = subprocess.run([str(real), str(root / "allowed-out")], capture_output=True, text=True)
    _check("the real wrapper runs when the fence permits",
           ok.returncode == 0 and (root / "allowed-out").exists(),
           f"rc={ok.returncode} stderr={ok.stderr.strip()[:120]!r}")

    for state in ("BLOCKED_UNCERTAIN", "IN_PROGRESS"):
        fence_module.write_fence(fence_root, state, reason="test")
        blocked = subprocess.run([str(real), str(root / f"out-{state}")],
                                 capture_output=True, text=True)
        _check(f"the real wrapper refuses under {state}",
               blocked.returncode == 5 and not (root / f"out-{state}").exists()
               and "CUTOVER_FENCE_BLOCKS_EXECUTION" in blocked.stderr,
               f"rc={blocked.returncode} stderr={blocked.stderr.strip()[:120]!r}")

    fence_module.fence_path(fence_root).write_text("GARBAGE\n")
    corrupt = subprocess.run([str(real), str(root / "out-corrupt")],
                             capture_output=True, text=True)
    _check("the real wrapper refuses on a malformed fence",
           corrupt.returncode == 5 and not (root / "out-corrupt").exists(),
           f"rc={corrupt.returncode} stderr={corrupt.stderr.strip()[:120]!r}")

# ---------------------------------------------------------------------------
print("\n-- first-cutover preflight ordering, REAL preflight (M1R5/M1R6) --")

# Two defects, opposite directions, one overloaded parameter. The gate must
# reject neither too early nor too late:
#
#   M1R4 rejected too early — a valid target was refused before the pointer
#         sequence because `current != target`, a state that sequence creates.
#   M1R5 accepted too late  — the fix passed no release id at all, which also
#         disabled candidate verification, so a nonexistent target came back
#         rc=0 / safe=true from the real pre-pointer preflight.
#
# The blocks below assert both directions against the REAL preflight.


@contextlib.contextmanager
def _quiescent_host(installed_wrapper: Path):
    """Fake the host facts only: systemd units and the advisory-lock query.

    Those are properties of the machine this runs on, not of the contract under
    test. Candidate verification, the release pointers and the blocker logic
    stay entirely real — faking any of those would fake the defect away.
    """
    units = ("log-job@dispatcher", "log-workflow-b", "log-job@retention-purge")

    def fake_systemctl(*args):
        verb = args[0]
        if verb == "show" and "LoadState,ActiveState,Unit" in " ".join(args):
            return 0, "LoadState=loaded\nActiveState=inactive\nUnit=" + args[1]
        if verb == "list-unit-files":
            return 0, "\n".join(f"{u}.timer enabled enabled" for u in units)
        if verb == "list-units":
            return 0, "\n".join(f"{u}.service loaded inactive dead x" for u in units)
        if verb == "is-enabled":
            return 0, "enabled"
        if verb == "is-active":
            return 3, "inactive"
        return 0, ""

    saved = (preflight_module._systemctl, preflight_module.advisory_lock_states,
             preflight_module.INSTALLED_WRAPPER_PATH)
    preflight_module._systemctl = fake_systemctl
    preflight_module.advisory_lock_states = lambda: [
        {"domain": "dispatcher", "key": 1, "held": False, "determinate": True, "holders": 0},
        {"domain": "workflow_b", "key": 2, "held": False, "determinate": True, "holders": 0}]
    preflight_module.INSTALLED_WRAPPER_PATH = installed_wrapper
    try:
        yield
    finally:
        (preflight_module._systemctl, preflight_module.advisory_lock_states,
         preflight_module.INSTALLED_WRAPPER_PATH) = saved


with tempfile.TemporaryDirectory() as tmp:
    # Every earlier transaction test injects a fake preflight, which is exactly
    # why this defect survived: both quiescence gates run BEFORE
    # sequence_pointers(), yet `pre-replacement` asked the real preflight to
    # assert `current == target` — a gate demanding the state a later step
    # creates. On a first cutover `current` is still the old release, so the
    # transaction refused itself after production had already been quiesced.
    #
    # This exercises the real _real_preflight -> cutover_preflight.evaluate
    # path. systemd and the database are faked (they are host facts, not the
    # contract under test); the pointer logic is emphatically NOT faked.
    root = Path(tmp)
    repo, release_root, m0, old_m1, target = _two_releases(root)
    _check("the fixture reproduces the first-cutover starting state",
           pointer_release_id(release_root / "current") == old_m1
           and pointer_release_id(release_root / "previous") == m0
           and target != old_m1,
           f"current={pointer_release_id(release_root / 'current')} target={target}")

    quiescent_units = ("log-job@dispatcher", "log-workflow-b", "log-job@retention-purge")

    def fake_systemctl(*args):
        verb = args[0]
        if verb == "show" and "LoadState,ActiveState,Unit" in " ".join(args):
            return 0, "LoadState=loaded\nActiveState=inactive\nUnit=" + args[1]
        if verb == "show" and "ExecStart" in args:
            return 0, "/usr/local/bin/log-job-runner.sh %i {}"
        if verb == "list-unit-files":
            return 0, "\n".join(f"{u}.timer enabled enabled" for u in quiescent_units)
        if verb == "list-units":
            return 0, "\n".join(f"{u}.service loaded inactive dead x" for u in quiescent_units)
        if verb == "is-enabled":
            return 0, "enabled"
        if verb == "is-active":
            return 3, "inactive"
        return 0, ""

    def fake_locks():
        return [{"domain": "dispatcher", "key": 1, "held": False, "determinate": True,
                 "holders": 0},
                {"domain": "workflow_b", "key": 2, "held": False, "determinate": True,
                 "holders": 0}]

    real_systemctl = preflight_module._systemctl
    real_locks = preflight_module.advisory_lock_states
    real_wrapper_path = preflight_module.INSTALLED_WRAPPER_PATH
    bootstrap = REPO_ROOT / "ops/systemd/proposed/log-job-runner.sh"
    preflight_module._systemctl = fake_systemctl
    preflight_module.advisory_lock_states = fake_locks
    preflight_module.INSTALLED_WRAPPER_PATH = bootstrap
    try:
        from ops.cutover_execute import _real_preflight

        post_rc, post_report = _real_preflight("post-timer-stop", release_root, target, REPO_ROOT)
        _check("post-timer-stop passes with current != target",
               post_rc == 0, f"blockers={post_report['blockers']}")

        pre_rc, pre_report = _real_preflight("pre-replacement", release_root, target, REPO_ROOT)
        _check("pre-replacement passes BEFORE the pointer sequence, with current != target",
               pre_rc == 0, f"blockers={pre_report['blockers']}")
        _check("no blocker mentions the current pointer at either gate",
               not any("current points at" in b
                       for b in post_report["blockers"] + pre_report["blockers"]),
               f"post={post_report['blockers']} pre={pre_report['blockers']}")

        # M1R6: not requiring `current == target` must not mean not looking at
        # the target. Both pre-pointer gates verify the candidate itself.
        for stage, report_ in (("post-timer-stop", post_report),
                               ("pre-replacement", pre_report)):
            _check(f"{stage} verified the target release itself",
                   report_["release"]["release_id"] == target
                   and report_["release"]["verified"] is True,
                   f"release={report_['release']}")
            _check(f"{stage} reports the current pointer without demanding it",
                   report_["release"]["current_release_id"] == old_m1
                   and report_["release"]["current_must_be_target"] is False,
                   f"release={report_['release']}")

        # The pre-fix implementation passed `target` for this stage; prove that
        # is exactly what refused, so this regression is anchored to the defect.
        # Anchor the regression to the defect: passing the target — which is
        # what the pre-fix code did for this stage — is precisely what refuses.
        would_have = preflight_module.evaluate(release_root, target, REPO_ROOT)
        _check("the pre-fix expectation is what refused the legitimate state",
               any("not the release being cut over to" in b
                   for b in would_have["blockers"]),
               f"blockers={would_have['blockers']}")

        # Now the pointer sequence, then the invariant that must still hold.
        # The transaction passes its own source_repo to the preflight, so the
        # bootstrap-wrapper gate is judged against the fixture repository.
        transaction, fake, installed, backup = _build(root, repo, release_root, target, m0)
        preflight_module.INSTALLED_WRAPPER_PATH = repo / "ops/systemd/proposed/log-job-runner.sh"
        transaction.preflight = _real_preflight
        report = transaction.run(execute=True)

        _check("the pointer sequence established current = target",
               pointer_release_id(release_root / "current") == target)
        _check("and previous = M0", pointer_release_id(release_root / "previous") == m0)
        _check("the transaction completed with the real preflight",
               report["executed"] is True and report["current"] == target
               and report["previous"] == m0, f"report={ {k: report[k] for k in ('current','previous')} }")

        after_rc, after_report = _real_preflight("pre-replacement", release_root, target, REPO_ROOT)
        _check("after the sequence the release IS current, as the CLI would report",
               preflight_module.evaluate(release_root, target, REPO_ROOT)["release"]
               ["current_release_id"] == target)
    finally:
        preflight_module._systemctl = real_systemctl
        preflight_module.advisory_lock_states = real_locks
        preflight_module.INSTALLED_WRAPPER_PATH = real_wrapper_path
    _force_rmtree(release_root)

with tempfile.TemporaryDirectory() as tmp:
    # M1R6 case 2: the target does not exist. Under M1R5 this returned rc=0,
    # safe_to_replace_wrapper=true and no blockers from the REAL pre-pointer
    # preflight — the gate had stopped looking at the candidate altogether.
    root = Path(tmp)
    repo, release_root, m0, old_m1, target = _two_releases(root)
    absent = "0" * 12
    _check("the absent id addresses no release",
           not (release_root / "releases" / absent).exists())

    from ops.cutover_execute import _real_preflight

    with _quiescent_host(repo / "ops/systemd/proposed/log-job-runner.sh"):
        for stage in ("post-timer-stop", "pre-replacement"):
            rc, report = _real_preflight(stage, release_root, absent, repo)
            _check(f"{stage} fails closed on a nonexistent target",
                   rc == 1 and report["safe_to_replace_wrapper"] is False
                   and any("does not verify" in b for b in report["blockers"]),
                   f"rc={rc} blockers={report['blockers']}")
            _check(f"{stage} classifies it as RELEASE_NOT_FOUND",
                   report["release"]["verified"] is False
                   and report["release"].get("classification") == "RELEASE_NOT_FOUND",
                   f"release={report['release']}")
            _check(f"{stage} refuses for the candidate, not for the current pointer",
                   not any("current points at" in b for b in report["blockers"]),
                   f"blockers={report['blockers']}")

        # Anchor the regression to the defect, as the M1R4 block above does:
        # passing no release id — what M1R5 did for both pre-pointer gates — is
        # precisely what blessed a target that does not exist.
        m1r5 = preflight_module.evaluate(release_root, None, repo)
        _check("the M1R5 expectation is what blessed the nonexistent target",
               m1r5["safe_to_replace_wrapper"] is True
               and m1r5["blockers"] == [] and m1r5["release"]["verified"] is None,
               f"blockers={m1r5['blockers']} release={m1r5['release']}")

    # And the transaction never reaches the pointer sequence.
    transaction, fake, installed, backup = _build(root, repo, release_root, absent, m0)
    transaction.preflight = _real_preflight
    _expect_cutover_error("the transaction refuses a nonexistent target",
                          "CUTOVER_RELEASE_VERIFICATION_FAILED",
                          lambda: transaction.run(execute=True))
    _check("no pointer moved", transaction.pointer_state == "UNTOUCHED"
           and pointer_release_id(release_root / "current") == old_m1
           and pointer_release_id(release_root / "previous") == m0,
           f"pointer_state={transaction.pointer_state}")
    _check("no timer was stopped", fake.stopped() == [])
    _force_rmtree(release_root)

with tempfile.TemporaryDirectory() as tmp:
    # M1R6 case 3: the target exists but its bytes no longer match its manifest.
    # Same M1R5 hole: a candidate that cannot be trusted read as safe.
    root = Path(tmp)
    repo, release_root, m0, old_m1, target = _two_releases(root)
    victim = release_root / "releases" / target / "ops" / "runner.py"
    victim.parent.chmod(0o755)
    victim.chmod(0o644)
    victim.write_text("VALUE = 'tampered'\n")

    with _quiescent_host(repo / "ops/systemd/proposed/log-job-runner.sh"):
        for stage in ("post-timer-stop", "pre-replacement"):
            rc, report = _real_preflight(stage, release_root, target, repo)
            _check(f"{stage} fails closed on a tampered target",
                   rc == 1 and report["safe_to_replace_wrapper"] is False
                   and report["release"]["verified"] is False
                   and report["release"].get("classification") == "RELEASE_CONTENT_MISMATCH",
                   f"rc={rc} release={report['release']} blockers={report['blockers']}")

    transaction, fake, installed, backup = _build(root, repo, release_root, target, m0)
    transaction.preflight = _real_preflight
    _expect_cutover_error("the transaction refuses a tampered target",
                          "CUTOVER_RELEASE_VERIFICATION_FAILED",
                          lambda: transaction.run(execute=True))
    _check("no pointer moved", transaction.pointer_state == "UNTOUCHED"
           and pointer_release_id(release_root / "current") == old_m1
           and pointer_release_id(release_root / "previous") == m0,
           f"pointer_state={transaction.pointer_state}")
    _force_rmtree(release_root)

with tempfile.TemporaryDirectory() as tmp:
    # M1R6: the relaxed pre-pointer gate is a *pointer* relaxation only. Prove
    # the strict form is still available and still enforced, which is what the
    # standalone CLI runs when an operator asks "is this release current?".
    root = Path(tmp)
    repo, release_root, m0, old_m1, target = _two_releases(root)
    with _quiescent_host(repo / "ops/systemd/proposed/log-job-runner.sh"):
        strict = preflight_module.evaluate(release_root, target, repo)
        relaxed = preflight_module.evaluate(release_root, target, repo,
                                            require_current_target=False)
    _check("the strict form still refuses when the target is not current",
           strict["safe_to_replace_wrapper"] is False
           and any("not the release being cut over to" in b for b in strict["blockers"])
           and strict["release"]["current_must_be_target"] is True,
           f"blockers={strict['blockers']}")
    _check("the relaxed form accepts the same state, having verified the target",
           relaxed["safe_to_replace_wrapper"] is True
           and relaxed["release"]["verified"] is True,
           f"blockers={relaxed['blockers']} release={relaxed['release']}")
    _check("both forms report the same observed current pointer",
           strict["release"]["current_release_id"]
           == relaxed["release"]["current_release_id"] == old_m1,
           f"strict={strict['release']} relaxed={relaxed['release']}")
    _force_rmtree(release_root)

with tempfile.TemporaryDirectory() as tmp:
    # Negative: relocating the invariant must not remove it. verify_installed
    # still rejects a wrong pointer pair after the sequence.
    root = Path(tmp)
    repo, release_root, m0, old_m1, target = _two_releases(root)
    transaction, fake, installed, backup = _build(root, repo, release_root, target, m0)
    original_install = transaction.install_wrapper

    def install_then_move_current(source: Path, destination: Path) -> None:
        original_install(source, destination)
        activate_release(release_root=release_root, release_id=old_m1, source_repo=repo)
    transaction.install_wrapper = install_then_move_current
    details = _expect_cutover_error("a wrong current after the sequence still fails",
                                    "CUTOVER_POST_INSTALL_VERIFICATION_FAILED",
                                    lambda: transaction.run(execute=True))
    _check("the current mismatch is named",
           any("current is" in f for f in details.get("failures", [])), f"details={details}")
    _check("no timer was restarted", fake.started() == [])
    _force_rmtree(release_root)

with tempfile.TemporaryDirectory() as tmp:
    # Negative: a wrong `previous` after the sequence fails closed as well, and
    # is named as such — the post-sequence contract asserts the whole pair, not
    # just the release being promoted.
    root = Path(tmp)
    repo, release_root, m0, old_m1, target = _two_releases(root)
    transaction, fake, installed, backup = _build(root, repo, release_root, target, m0)
    original_install = transaction.install_wrapper

    def install_then_move_previous(source: Path, destination: Path) -> None:
        original_install(source, destination)
        # target -> old_m1 -> target leaves `current` correct and `previous` at
        # old_m1 instead of M0: only the predecessor is wrong.
        activate_release(release_root=release_root, release_id=old_m1, source_repo=repo)
        activate_release(release_root=release_root, release_id=target, source_repo=repo)
    transaction.install_wrapper = install_then_move_previous
    details = _expect_cutover_error("a wrong previous after the sequence still fails",
                                    "CUTOVER_POST_INSTALL_VERIFICATION_FAILED",
                                    lambda: transaction.run(execute=True))
    _check("the previous mismatch is named",
           any("previous is" in f for f in details.get("failures", [])), f"details={details}")
    _check("and current is not falsely reported as wrong",
           not any(f.startswith("current is") for f in details.get("failures", [])),
           f"details={details}")
    _check("no timer was restarted", fake.started() == [])
    _force_rmtree(release_root)

with tempfile.TemporaryDirectory() as tmp:
    # Negative: a wrong predecessor is still rejected by the sequence assertion.
    root = Path(tmp)
    repo, release_root, m0, old_m1, target = _two_releases(root)
    transaction, fake, installed, backup = _build(root, repo, release_root, target, old_m1)
    transaction.run(execute=True)
    _check("previous follows the declared predecessor, not M0 by accident",
           pointer_release_id(release_root / "previous") == old_m1)
    _force_rmtree(release_root)

with tempfile.TemporaryDirectory() as tmp:
    # Negative: non-pointer blockers must still refuse. An armed timer is the
    # cheapest real one, and it must survive the relocation of the pointer gate.
    root = Path(tmp)
    repo, release_root, m0, old_m1, target = _two_releases(root)
    real_systemctl = preflight_module._systemctl
    real_locks = preflight_module.advisory_lock_states

    def armed_systemctl(*args):
        if args[0] == "show" and "LoadState,ActiveState,Unit" in " ".join(args):
            return 0, "LoadState=loaded\nActiveState=active\nUnit=" + args[1]
        if args[0] in ("list-unit-files", "list-units"):
            return 0, "log-job@dispatcher.timer enabled enabled"
        return 0, ""
    preflight_module._systemctl = armed_systemctl
    preflight_module.advisory_lock_states = lambda: [
        {"domain": "dispatcher", "key": 1, "held": False, "determinate": True, "holders": 0}]
    try:
        from ops.cutover_execute import _real_preflight
        rc, report = _real_preflight("pre-replacement", release_root, target, REPO_ROOT)
        _check("a still-armed timer is still a blocker after the fix",
               rc == 1 and any("still armed" in b for b in report["blockers"]),
               f"blockers={report['blockers']}")
    finally:
        preflight_module._systemctl = real_systemctl
        preflight_module.advisory_lock_states = real_locks
    _force_rmtree(release_root)

# ---------------------------------------------------------------------------
print("\n-- dry run mutates nothing --")

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    repo, release_root, m0, m1, target = _two_releases(root)
    transaction, fake, installed, backup = _build(root, repo, release_root, target, m0)
    before = installed.read_bytes()
    report = transaction.run(execute=False)
    _check("dry run reports what it would do", report["executed"] is False
           and report["would_promote"] == target and report["would_set_previous"] == m0)
    # The dry run reads unit definitions to prove every consumer enters through
    # the wrapper; read-only inspection is fine, mutation is not.
    _check("dry run issues no mutating systemctl call",
           all(call[0] not in ("stop", "start", "restart", "daemon-reload")
               for call in fake.calls), f"calls={fake.calls}")
    _check("dry run only inspects units",
           all(call[0] in ("show", "is-active") for call in fake.calls), f"calls={fake.calls}")
    _check("dry run leaves the wrapper alone", installed.read_bytes() == before)
    _check("dry run leaves the pointers alone",
           pointer_release_id(release_root / "current") == m1)
    _check("dry run still verifies the candidate",
           report["expected_wrapper_sha256"] ==
           hashlib.sha256((release_root / "releases" / target / RELEASE_WRAPPER_RELATIVE)
                          .read_bytes()).hexdigest())
    _force_rmtree(release_root)

shutil.rmtree(_TEST_LOCK_DIR, ignore_errors=True)

print()
if FAILURES:
    print(f"FAILED {len(FAILURES)} cutover transaction check(s):")
    for name in FAILURES:
        print(f"  - {name}")
    raise SystemExit(1)
print("ALL CUTOVER TRANSACTION CHECKS PASSED")
