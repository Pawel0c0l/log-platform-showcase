#!/usr/bin/env python3
"""The release-boundary cutover, as one deterministic transaction.

The cutover used to be a runbook of shell steps. That was the defect: an
operator reproducing control flow by hand had `preflight || { echo "..."; }`,
which *succeeds* — the brace group's exit status is the echo's — so a failed
gate silently allowed the wrapper installation to proceed. Gates that can be
turned into successes by shell semantics are not gates. The sequence lives here
instead, where each gate either passes or raises, and installation is
unreachable except through every preceding gate.

Ordering that matters, and why:

* **Timers stop before anything is checked.** Checking a service and then
  stopping its timer is a race — a tick starts in between — and stopping a timer
  never terminates a run already underway.
* **Only originally-active timers are restarted.** Restarting all known timers
  would *enable* one an operator had deliberately stopped, inventing scheduler
  state rather than restoring it.
* **Pointers are sequenced M0 -> target inside the quiesced window.** Activating
  the target directly from the current pointer would record the pre-remediation
  release as `previous`, i.e. the first rollback would land on code the review
  rejected. Activating the M0 predecessor first makes `previous` the verified M0
  release, which is a rollback target worth having.
* **The wrapper is installed from an immutable release path**, never through
  `current`, which can move.
* **Everything is verified before a single timer restarts.** A restarted timer
  is production traffic; verification after that point is an audit, not a gate.

Nothing here restarts services or reloads systemd, and `--execute` is required
for any mutation. Every external effect is injected, so the whole transaction is
exercised by tests against fakes and disposable release roots.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ops.cutover_fence import (  # noqa: E402
    STATE_ALLOWED,
    STATE_BLOCKED_UNCERTAIN,
    STATE_IN_PROGRESS,
    STATE_VERIFIED,
    read_fence,
    write_fence,
)  # noqa: E402
from ops.execution_barrier import (  # noqa: E402
    ExecutionBarrierError,
    execution_barrier,
)
from ops.release_boundary import (  # noqa: E402
    INSTALLED_WRAPPER_PATH,
    RELEASE_WRAPPER_RELATIVE,
    WRAPPER_DEVELOPMENT,
    WRAPPER_DEVELOPMENT_HISTORICAL,
    WRAPPER_RELEASE,
    ReleaseBoundaryError,
    activate_release,
    installed_wrapper_variant,
    management_lock,
    pointer_release_id,
    verify_release,
)
from ops.wrapper_install_lock import WrapperInstallLockError, wrapper_install_lock  # noqa: E402

DEFAULT_RELEASE_ROOT = Path("/opt/log-platform-release")
DEFAULT_BACKUP_PATH = Path("/etc/log-platform/log-job-runner.sh.pre-release-boundary.bak")
# Observable transaction states. The point of naming them is the failure rule:
# once REPLACEMENT_ATTEMPTED is reached, the installed wrapper's contents are
# unknown until proven otherwise, and unknown must never restart production.
STATE_INITIAL = "INITIAL"
STATE_TIMERS_CAPTURED = "TIMERS_CAPTURED"
STATE_TIMERS_QUIESCED = "TIMERS_QUIESCED"
STATE_EXECUTION_BARRIER_EXCLUSIVE = "EXECUTION_BARRIER_EXCLUSIVE"
STATE_PRECHECK_GREEN = "PRECHECK_GREEN"
STATE_POINTERS_PREPARED = "POINTERS_PREPARED"
STATE_WRAPPER_REPLACEMENT_ATTEMPTED = "WRAPPER_REPLACEMENT_ATTEMPTED"
STATE_WRAPPER_VERIFIED = "WRAPPER_VERIFIED"
STATE_TIMER_STATE_RESTORED = "TIMER_STATE_RESTORED"
STATE_COMPLETE = "COMPLETE"

# Pointer sub-states, so a half-finished sequence is reported as what it is
# rather than inferred from a boolean that only flips after both activations.
POINTERS_UNTOUCHED = "UNTOUCHED"
POINTERS_ATTEMPTED = "ATTEMPTED"
POINTERS_PREDECESSOR_ACTIVE = "PREDECESSOR_ACTIVATED"
POINTERS_SEQUENCED = "SEQUENCED"

EXPECTED_WRAPPER_UID = 0
EXPECTED_WRAPPER_GID = 0
EXPECTED_WRAPPER_MODE = 0o755


class CutoverError(RuntimeError):
    """A refused or failed cutover step. Carries the state the operator needs."""

    def __init__(self, classification: str, details: Dict[str, object]) -> None:
        self.classification = classification
        self.details = details
        super().__init__(f"{classification}: {json.dumps(details, sort_keys=True, default=str)}")


def _real_systemctl(*args: str):
    try:
        result = subprocess.run(["systemctl", *args], check=False,
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return 125, str(exc)
    return result.returncode, (result.stdout or result.stderr).strip()


def _real_preflight(stage: str, release_root: Path, release_id: str, source_repo: Path):
    from ops import cutover_preflight

    # Two independent requirements, and the reason this needed two attempts to
    # get right is that the preflight used to express both through one argument.
    #
    # The target release MUST be verified here. Both gates run before anything
    # is installed, and a missing, tampered or otherwise unverifiable candidate
    # must fail closed *before* the pointer sequence mutates production state.
    #
    # `current == target` MUST NOT be required here. Both gates also run before
    # `sequence_pointers()`, which is the step that makes the target current, so
    # demanding it is a gate requiring the state a later step creates: on a first
    # cutover `current` is still the previous release, the operator has already
    # stopped all three ingestion timers, and the transaction refuses itself.
    # Quiescence is about running work, not about pointers.
    #
    # Passing no release id silences the second — which M1R4 wrongly enforced —
    # only by also silencing the first, which is how M1R5 came to bless a
    # nonexistent target. The two are now separate parameters.
    #
    # The pointer invariant is not dropped, only asserted where it is true:
    # `sequence_pointers()` asserts the exact pair it just established, and
    # `verify_installed()` re-asserts `current == target` and
    # `previous == predecessor` after the wrapper is in place. The standalone
    # `ops/cutover_preflight.py --release <id>` CLI keeps the stricter default,
    # because for an operator asking "is this release current?" that is a
    # meaningful question.
    report = cutover_preflight.evaluate(release_root, release_id, source_repo,
                                        require_current_target=False)
    return (0 if report["safe_to_replace_wrapper"] else 1), report


def _sudo(*command: str) -> None:
    """Run one privileged command, surfacing why it was refused.

    `sudo` failing for want of a tty is the likely real-world failure (an SSH
    session without a terminal, an expired timestamp), and a bare
    CalledProcessError tells the operator nothing about that.
    """
    try:
        result = subprocess.run(["sudo", "-n", *command], check=False,
                                capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired as exc:
        # Bounded well under the management-lock timeout: a wedged sudo would
        # otherwise hold the execution barrier and the release lock indefinitely,
        # halting ingestion with no upper bound.
        raise CutoverError("CUTOVER_PRIVILEGED_COMMAND_FAILED", {
            "command": " ".join(command), "reason": "timed out after 120s",
        }) from exc
    if result.returncode != 0:
        raise CutoverError("CUTOVER_PRIVILEGED_COMMAND_FAILED", {
            "command": " ".join(command), "returncode": result.returncode,
            "stderr": (result.stderr or "").strip()[:400],
            "hint": "run `sudo -v` first; the transaction uses sudo non-interactively",
        })


def _real_install(source: Path, destination: Path) -> None:
    staging = destination.parent / f".{destination.name}.new"
    _sudo("install", "-o", "root", "-g", "root", "-m", "0755", str(source), str(staging))
    _sudo("mv", "-f", str(staging), str(destination))


def _real_backup(source: Path, destination: Path) -> None:
    _sudo("mkdir", "-p", str(destination.parent))
    _sudo("cp", "-a", str(source), str(destination))


def _real_stat(path: Path) -> Dict[str, object]:
    info = path.stat()
    return {"uid": info.st_uid, "gid": info.st_gid, "mode": info.st_mode & 0o7777,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _declared_release_root(wrapper: Path) -> Optional[str]:
    """The RELEASE_ROOT a wrapper will actually resolve at runtime."""
    try:
        match = re.search(r'^RELEASE_ROOT="([^"]+)"', wrapper.read_text(), re.MULTILINE)
    except (OSError, UnicodeError):
        return None
    return match.group(1) if match else None


@dataclass
class CutoverTransaction:
    """One cutover, gate by gate. Construct with fakes to test the whole flow."""

    release_root: Path
    source_repo: Path
    target_release_id: str
    predecessor_release_id: str
    installed_wrapper: Path = INSTALLED_WRAPPER_PATH
    backup_path: Path = DEFAULT_BACKUP_PATH
    systemctl: Callable[..., tuple] = _real_systemctl
    preflight: Callable[..., tuple] = _real_preflight
    install_wrapper: Callable[[Path, Path], None] = _real_install
    backup_wrapper: Callable[[Path, Path], None] = _real_backup
    stat_wrapper: Callable[[Path], Dict[str, object]] = _real_stat
    wrapper_lock: Callable[[], object] = wrapper_install_lock
    discover: Optional[Callable[[], Dict[str, List[str]]]] = None
    execution_barrier: Callable[..., object] = execution_barrier
    assume_timers_stopped: tuple = ()
    fence_written: bool = False
    steps: List[Dict[str, object]] = field(default_factory=list)
    state: str = STATE_INITIAL
    pointer_state: str = POINTERS_UNTOUCHED
    original_wrapper_sha256: Optional[str] = None

    # -- helpers ----------------------------------------------------------
    def _record(self, name: str, **evidence) -> None:
        self.steps.append({"step": name, **evidence})

    def _consumers(self) -> Dict[str, List[str]]:
        if self.discover is not None:
            return self.discover()
        from ops import cutover_preflight

        return cutover_preflight.discover_wrapper_consumers()

    def _timer_active(self, unit: str) -> bool:
        rc, out = self.systemctl("is-active", unit)
        return out.strip() == "active"

    @property
    def release_path(self) -> Path:
        return self.release_root / "releases" / self.target_release_id

    @property
    def wrapper_source(self) -> Path:
        """The wrapper to install, addressed by immutable release path.

        Deliberately not `<release_root>/current/...`: the pointer moves during
        this very transaction, and installing through it would make the
        installed bytes depend on when the read happened.
        """
        return self.release_path / RELEASE_WRAPPER_RELATIVE

    # -- gates ------------------------------------------------------------
    def verify_candidates(self) -> Dict[str, object]:
        """Both releases must be the right bytes AND able to start.

        This runs before `--execute` is honoured, before the timers stop, before
        the execution barrier and before the release-management lock — so a
        refusal here costs nothing, and it is the only place a bootability
        refusal can be raised without landing inside the pointer sequence. That
        the gate needs no change to the state machine, the barrier or the unwind
        path is precisely why it belongs here and not in `sequence_pointers` or
        in `activate_release`.

        THE PREDECESSOR IS GATED TOO, and that is not symmetry for its own sake:
        it becomes `previous`, i.e. the rollback target. Promoting onto an
        unbootable predecessor would deliberately install the poisoned-fallback
        state — `previous` naming a release that cannot start — that
        `repair-previous` exists to dig a host out of. Installed on purpose, by a
        tool, during a planned maintenance window.

        AN UNKNOWN VERDICT DEPENDS ON WHETHER THIS IS A FIRST RUN OR A RESUMPTION.

        The original rule — refuse on unknown, as `activate` does — rested on "a
        cutover is a planned operation with nothing broken while you retry". That
        is true of a first run and FALSE of the re-run `_unwind` itself
        recommends. When the wrapper state is uncertain, `_unwind` deliberately
        KEEPS THE TIMERS STOPPED rather than restarting production against a
        possibly half-installed wrapper, and the documented recovery is to clear
        the fence and re-run this transaction. In that state production is
        already down, completing the cutover IS the recovery action, and
        refusing it for want of proof gates recovery — the same mistake the
        rollback refusal made this morning.

        The signal is `--assume-timers-stopped`: the operator asserts that the
        wrapper-consumer timers are already inactive, which is exactly the state
        `_unwind` leaves behind and the only reason that flag exists. The fence
        cannot serve as the signal, because the documented path clears it to
        ALLOWED before re-running, making a resumption indistinguishable from a
        first run by fence state alone.

        A DEFINITE False still refuses in both cases. If the release genuinely
        cannot start, promoting it does not recover anything, whether or not
        production is already down.
        """

        bootability: Dict[str, object] = {}
        for label, release_id in (("target", self.target_release_id),
                                  ("predecessor", self.predecessor_release_id)):
            try:
                report = verify_release(release_root=self.release_root, release_id=release_id,
                                        source_repo=self.source_repo)
            except ReleaseBoundaryError as exc:
                raise CutoverError("CUTOVER_RELEASE_VERIFICATION_FAILED", {
                    "role": label, "release_id": release_id,
                    "classification": exc.classification, "details": exc.details,
                }) from exc

            # Deliberately a separate classification from the one above: "the
            # bytes are wrong" and "it cannot start" are different diagnoses and
            # send an operator to different places.
            verdict = report.get("bootability") or {}
            bootability[label] = {"release_id": release_id,
                                  "bootable": report.get("bootable"),
                                  "defects": verdict.get("defects") or [],
                                  "assessment_error": verdict.get("assessment_error")}
            # Resumption: production is already quiesced, so an unproven verdict
            # must not stand between the operator and completing the cutover.
            resuming = bool(self.assume_timers_stopped)
            if report.get("bootable") is None and resuming:
                bootability[label]["proceeded_on_unknown"] = True
                self._record(f"verify_{label}", release_id=release_id, commit=report["commit"],
                             bootable=None, proceeded_on_unknown=True)
                continue

            if report.get("bootable") is not True:
                raise CutoverError("CUTOVER_RELEASE_NOT_BOOTABLE", {
                    "role": label, "release_id": release_id,
                    "bootable": report.get("bootable"),
                    "reason": ("the bootability check could not be completed, so this release "
                               "is not proven able to start"
                               if report.get("bootable") is None else
                               "this release cannot start and would crash-loop the service"),
                    "defects": verdict.get("defects") or [],
                    "assessment_error": verdict.get("assessment_error"),
                    "consequence": ("promoting it would leave `current` naming a release that "
                                    "cannot start" if label == "target" else
                                    "it would become `previous`, i.e. a rollback target that "
                                    "cannot recover the service"),
                    "next": ("re-prepare it with its runtime links and cut over onto that "
                             "instead"
                             if report.get("bootable") is False else
                             "the bootability check could not be completed. If you are "
                             "RESUMING an interrupted cutover — timers already stopped, "
                             "production already down — pass `--assume-timers-stopped` with "
                             "the affected timers, which lets an unproven verdict through "
                             "because completing the cutover is then the recovery action. "
                             "On a first run, investigate the error and re-run."),
                })
            self._record(f"verify_{label}", release_id=release_id, commit=report["commit"],
                         bootable=report.get("bootable"))
        if not self.wrapper_source.is_file():
            raise CutoverError("CUTOVER_WRAPPER_SOURCE_MISSING", {
                "path": str(self.wrapper_source),
                "reason": "the target release does not contain the release wrapper; "
                          "a release cut before the boundary existed cannot be promoted onto it",
            })
        expected = _sha256_file(self.wrapper_source)
        self._record("resolve_wrapper_source", path=str(self.wrapper_source), sha256=expected)
        return {"expected_wrapper_sha256": expected, "bootability": bootability}

    def assert_consumers_use_wrapper(self, services: List[str]) -> None:
        """Every discovered consumer must actually enter through the wrapper.

        The barrier lives in the wrapper, so a unit whose installed ExecStart
        runs `python ops/runner.py` directly takes no barrier at all — it can
        start during the exclusive window and run the development tree while the
        transaction believes the kernel is enforcing quiescence. Discovery lists
        such a unit as a consumer, which makes the claim worse than the gap.
        """
        offenders = []
        for unit in services:
            rc, exec_start = self.systemctl("show", "-p", "ExecStart", "--value", unit)
            if rc != 0:
                offenders.append({"unit": unit, "reason": f"ExecStart unreadable (rc={rc})"})
                continue
            if not exec_start.strip():
                continue  # template with no instance loaded
            if self._reaches_wrapper(exec_start):
                continue
            offenders.append({"unit": unit, "exec_start": exec_start.strip()[:200],
                              "reason": "does not execute through the installed wrapper"})
        if offenders:
            raise CutoverError("CUTOVER_CONSUMER_BYPASSES_WRAPPER", {
                "offenders": offenders,
                "reason": ("these units can start jobs without taking the execution barrier, "
                           "so stopping timers and holding the barrier does not make them "
                           "quiescent"),
                "remediation": ("install the repository unit that routes through "
                                f"{self.installed_wrapper} before cutting over"),
            })
        self._record("consumers_use_wrapper", services=list(services))

    def _reaches_wrapper(self, exec_start: str) -> bool:
        """Whether this ExecStart ends up executing the installed wrapper.

        A substring test on ExecStart alone is wrong in both directions. The
        retention consumer runs `/usr/local/bin/log-retention-purge.sh`, a
        two-line shim that `exec`s the wrapper — it *is* covered by the barrier,
        and flagging it would send an operator to replace a unit that is already
        safe. Conversely a script merely *named* like the wrapper proves nothing.
        So one level of indirection is resolved and the shim is read.
        """
        wrapper = str(self.installed_wrapper)
        if wrapper in exec_start:
            return True
        for token in exec_start.split():
            candidate = token.strip('";')
            if not candidate.startswith("/") or candidate == wrapper:
                continue
            path = Path(candidate)
            if not path.is_file() or path.suffix not in (".sh", ""):
                continue
            try:
                body = path.read_text(errors="replace")
            except OSError:
                continue
            for line in body.splitlines():
                stripped = line.strip()
                if stripped.startswith("exec ") and wrapper in stripped:
                    return True
        return False

    def capture_timer_state(self, timers: List[str]) -> Dict[str, bool]:
        """The scheduler state to restore — persisted across attempts.

        Reading live state on every run is wrong after a failed attempt that
        deliberately left the timers stopped. The documented recovery is to
        re-run; a naive re-capture would then record every timer as inactive,
        "restore" nothing, and return COMPLETE with all Telematics ingestion
        halted — a success report for a silent outage. The first capture is
        therefore written next to the wrapper backup and preferred afterwards.
        """
        live = {unit: self._timer_active(unit) for unit in timers}
        persisted = self._load_captured_timers()
        if persisted is not None:
            merged = {unit: bool(persisted.get(unit, live.get(unit, False))) for unit in timers}
            self._record("capture_timer_state", original=dict(merged),
                         source="persisted from an earlier attempt", live=live)
            return merged
        if self.assume_timers_stopped:
            declared = {unit: unit in self.assume_timers_stopped for unit in timers}
            self._persist_captured_timers(declared)
            self._record("capture_timer_state", original=dict(declared),
                         source="operator-declared via --assume-timers-stopped")
            return declared
        if timers and not any(live.values()):
            raise CutoverError("CUTOVER_NO_ACTIVE_CONSUMER_TIMERS", {
                "timers": live,
                "reason": ("every wrapper-consumer timer is already inactive, so this run has "
                           "no scheduler state to restore and would finish with production "
                           "stopped"),
                "remediation": ("start the timers that should be running and re-run, or pass "
                                "--assume-timers-stopped with the set to restore"),
            })
        self._persist_captured_timers(live)
        self._record("capture_timer_state", original=dict(live), source="live")
        return live

    @property
    def _timer_state_path(self) -> Path:
        return Path(str(self.backup_path) + ".timers.json")

    def _load_captured_timers(self) -> Optional[Dict[str, bool]]:
        try:
            return json.loads(self._timer_state_path.read_text())
        except (OSError, json.JSONDecodeError):
            return None

    def _persist_captured_timers(self, state: Dict[str, bool]) -> None:
        try:
            self._timer_state_path.parent.mkdir(parents=True, exist_ok=True)
            self._timer_state_path.write_text(json.dumps(state, sort_keys=True))
        except OSError:
            # Best effort: losing this degrades recovery guidance, never safety.
            self._record("capture_timer_state_persist_failed",
                         path=str(self._timer_state_path))

    def stop_timers(self, original: Dict[str, bool]) -> None:
        for unit, was_active in original.items():
            if not was_active:
                continue
            rc, out = self.systemctl("stop", unit)
            if rc != 0:
                raise CutoverError("CUTOVER_TIMER_STOP_FAILED", {"unit": unit, "output": out})
        self._record("stop_timers", stopped=[u for u, a in original.items() if a])

    def gate_preflight(self, stage: str) -> None:
        rc, report = self.preflight(stage, self.release_root, self.target_release_id, self.source_repo)
        if rc != 0:
            raise CutoverError("CUTOVER_PREFLIGHT_FAILED", {
                "stage": stage, "returncode": rc,
                "blockers": (report or {}).get("blockers"),
            })
        self._record("preflight", stage=stage, returncode=rc)

    def sequence_pointers(self) -> None:
        """M0 -> target, so `previous` ends up as the M0 release.

        Activating the target straight from the existing pointer would record
        the pre-remediation release as the rollback target. Both activations run
        inside one release-management critical section, so no other operator can
        interleave a promotion between them, and nothing reads these pointers
        until the wrapper is installed.

        The sub-state advances *before* each activation. Marking movement only
        after the whole sequence returns would let a failure between the two
        report "pointers untouched" while `current` had in fact already moved to
        M0 — evidence that is worse than none, because it is confidently wrong.
        """
        self.pointer_state = POINTERS_ATTEMPTED
        try:
            # The caller already holds the release-management lock for the whole
            # span; `activate_release` re-enters it on this thread.
            activate_release(release_root=self.release_root,
                             release_id=self.predecessor_release_id,
                             source_repo=self.source_repo)
            self.pointer_state = POINTERS_PREDECESSOR_ACTIVE
            activate_release(release_root=self.release_root,
                             release_id=self.target_release_id,
                             source_repo=self.source_repo)
            self.pointer_state = POINTERS_SEQUENCED
        except ReleaseBoundaryError as exc:
            raise CutoverError("CUTOVER_POINTER_SEQUENCE_FAILED", {
                "classification": exc.classification, "details": exc.details,
                "pointer_state": self.pointer_state,
                "observed": self.observed_pointers(),
            }) from exc
        observed = self.observed_pointers()
        if (observed.get("current") != self.target_release_id
                or observed.get("previous") != self.predecessor_release_id):
            raise CutoverError("CUTOVER_POINTER_SEQUENCE_FAILED", {
                "pointer_state": self.pointer_state, "observed": observed,
                "expected_current": self.target_release_id,
                "expected_previous": self.predecessor_release_id,
            })
        self._record("sequence_pointers", **observed)
        self.state = STATE_POINTERS_PREPARED

    def observed_pointers(self) -> Dict[str, object]:
        """Read the pointers as they actually are — never inferred from a flag."""
        try:
            return {"current": pointer_release_id(self.release_root / "current"),
                    "previous": pointer_release_id(self.release_root / "previous")}
        except ReleaseBoundaryError as exc:
            return {"current": "unreadable", "previous": "unreadable",
                    "error": exc.classification}

    def install(self, expected_sha256: str) -> None:
        """Back up and replace the wrapper, under the shared host lock.

        The lock is shared with identity provisioning, and the installed
        wrapper's identity is re-read *inside* it: holding a lock says nothing
        about what the file contained before it was taken.

        The `with` sits inside the `try` deliberately. A @contextmanager call
        returns the manager without running anything, so an acquisition failure
        surfaces at `__enter__`; catching only around the call left this
        classification unreachable and turned a real lock timeout into an opaque
        generic failure.
        """
        try:
            with self.wrapper_lock():
                variant = installed_wrapper_variant(repo_root=self.source_repo,
                                                    installed_wrapper=self.installed_wrapper)
                # Either development variant is fine: both mean "no release
                # boundary is installed here". A wrapper from an earlier commit
                # is expected — any change to it alters its bytes.
                # The same exact-bootstrap decision preflight makes, repeated
                # here: the point of an in-lock re-check is that the pre-state
                # may have changed since the gate.
                if variant != WRAPPER_DEVELOPMENT:
                    raise CutoverError("CUTOVER_INSTALLED_WRAPPER_UNEXPECTED", {
                        "variant": variant, "installed_wrapper": str(self.installed_wrapper),
                        "reason": "expected a development wrapper immediately before replacement",
                    })
                try:
                    self.original_wrapper_sha256 = _sha256_file(self.installed_wrapper)
                except OSError:
                    self.original_wrapper_sha256 = None
                self.backup_wrapper(self.installed_wrapper, self.backup_path)
                self._record("backup_wrapper", path=str(self.backup_path),
                             replaced_variant=variant,
                             original_sha256=self.original_wrapper_sha256)
                # Enter the attempted state BEFORE the replacement can happen.
                self.state = STATE_WRAPPER_REPLACEMENT_ATTEMPTED
                self.install_wrapper(self.wrapper_source, self.installed_wrapper)
                self._record("install_wrapper", source=str(self.wrapper_source),
                             destination=str(self.installed_wrapper),
                             expected_sha256=expected_sha256)
        except WrapperInstallLockError as exc:
            raise CutoverError("CUTOVER_WRAPPER_LOCK_UNAVAILABLE", {
                "classification": exc.classification, "details": exc.details,
            }) from exc

    def wrapper_conclusively_unchanged(self) -> bool:
        """True only if the installed wrapper is provably the original bytes.

        This is the single condition that downgrades "replacement may have
        happened" back to "it did not". Anything else — an unreadable file, no
        recorded original, a different digest — stays uncertain, and uncertain
        keeps production stopped.
        """
        if self.original_wrapper_sha256 is None:
            return False
        try:
            return _sha256_file(self.installed_wrapper) == self.original_wrapper_sha256
        except OSError:
            return False

    def verify_installed(self, expected_sha256: str) -> Dict[str, object]:
        """Every assertion that must hold before a timer may restart."""
        failures: List[str] = []
        try:
            info = self.stat_wrapper(self.installed_wrapper)
        except OSError as exc:
            raise CutoverError("CUTOVER_POST_INSTALL_VERIFICATION_FAILED", {
                "failures": [f"installed wrapper unreadable: {exc}"],
            }) from exc

        if info.get("sha256") != expected_sha256:
            failures.append(f"wrapper sha256 {info.get('sha256')} != expected {expected_sha256}")
        if info.get("uid") != EXPECTED_WRAPPER_UID:
            failures.append(f"wrapper owner uid {info.get('uid')} != {EXPECTED_WRAPPER_UID}")
        if info.get("gid") != EXPECTED_WRAPPER_GID:
            failures.append(f"wrapper group gid {info.get('gid')} != {EXPECTED_WRAPPER_GID}")
        if info.get("mode") != EXPECTED_WRAPPER_MODE:
            failures.append(f"wrapper mode {oct(int(info.get('mode') or 0))} != 0755")

        # Classify against the promoted release's own wrapper. Using the
        # development tree would compare the installed bytes with a working copy
        # that is dirty by design: promoting a release whose wrapper differs
        # from the current checkout would then report `release_historical` and
        # fail a byte-correct installation — after the install, with production
        # halted. The byte identity that matters was already asserted above.
        variant = installed_wrapper_variant(repo_root=self.release_path,
                                            installed_wrapper=self.installed_wrapper)
        if variant != WRAPPER_RELEASE:
            failures.append(f"installed wrapper does not match the promoted release's "
                            f"wrapper (classified {variant})")

        # M4: the installed wrapper must resolve the release root this
        # transaction verified. A rehearsal against another root would otherwise
        # install a wrapper pointing at the canonical one and report success.
        declared_root = _declared_release_root(self.installed_wrapper)
        if declared_root is None:
            failures.append("installed wrapper declares no RELEASE_ROOT")
        elif Path(declared_root).resolve() != Path(self.release_root).resolve():
            failures.append(f"installed wrapper resolves {declared_root}, "
                            f"not the verified release root {self.release_root}")

        try:
            current = pointer_release_id(self.release_root / "current")
            previous = pointer_release_id(self.release_root / "previous")
        except ReleaseBoundaryError as exc:
            current = previous = None
            failures.append(f"release pointers unreadable: {exc.classification}")
        if current != self.target_release_id:
            failures.append(f"current is {current}, expected {self.target_release_id}")
        if previous != self.predecessor_release_id:
            failures.append(f"previous is {previous}, expected {self.predecessor_release_id}")

        try:
            verify_release(release_root=self.release_root, release_id=self.target_release_id,
                           source_repo=self.source_repo)
        except ReleaseBoundaryError as exc:
            failures.append(f"target release no longer verifies: {exc.classification}")

        if failures:
            raise CutoverError("CUTOVER_POST_INSTALL_VERIFICATION_FAILED", {
                "failures": failures,
                "timers": "left stopped deliberately; production is not running",
                "rollback": f"sudo install -o root -g root -m 0755 {self.backup_path} "
                            f"{self.installed_wrapper}.new && sudo mv -f "
                            f"{self.installed_wrapper}.new {self.installed_wrapper}, then restart "
                            f"only the timers listed in capture_timer_state",
            })
        self._record("verify_installed", sha256=expected_sha256, variant=variant,
                     current=current, previous=previous)
        return {"installed_wrapper_sha256": expected_sha256, "current": current, "previous": previous}

    def restore_timers(self, original: Dict[str, bool]) -> Dict[str, object]:
        """Start exactly the timers that were running, and confirm they are.

        A zero exit from `systemctl start` is a request, not a state. Re-reading
        activity is what makes "the scheduler is restored" an observation rather
        than an assumption — the difference matters most in the unwind, where an
        operator reads the evidence at 03:00 and stops looking.
        """
        restored, failed = [], []
        for unit, was_active in original.items():
            if not was_active:
                continue  # never enable a timer the operator had stopped
            rc, out = self.systemctl("start", unit)
            if rc != 0 or not self._timer_active(unit):
                failed.append(unit)
            else:
                restored.append(unit)
        outcome = {"restored": restored, "failed": failed,
                   "left_stopped": [u for u, a in original.items() if not a]}
        self._record("restore_timers", **outcome)
        if failed:
            raise CutoverError("CUTOVER_TIMER_RESTORE_FAILED", {
                **outcome,
                "reason": "timers that were running before the cutover are not running now",
            })
        return outcome

    # -- transaction ------------------------------------------------------
    def run(self, *, execute: bool = False) -> Dict[str, object]:
        consumers = self._consumers()
        timers = consumers["timers"]
        if not consumers.get("complete", True):
            raise CutoverError("CUTOVER_CONSUMER_DISCOVERY_INCOMPLETE", {
                "failures": consumers.get("discovery_failures"),
                "reason": "the set of units that can launch work through the wrapper is unknown, "
                          "so quiescence cannot be established",
            })
        self.assert_consumers_use_wrapper(consumers["services"])
        candidate = self.verify_candidates()
        if not execute:
            return {"executed": False, "would_promote": self.target_release_id,
                    "would_set_previous": self.predecessor_release_id,
                    "wrapper_source": str(self.wrapper_source),
                    "expected_wrapper_sha256": candidate["expected_wrapper_sha256"],
                    "bootability": candidate["bootability"],
                    "wrapper_consumer_timers": timers,
                    "consumer_discovery_complete": consumers.get("complete", True),
                    "state": self.state, "pointer_state": self.pointer_state,
                    "steps": self.steps,
                    "next": "re-run the identical command with --execute"}

        original = self.capture_timer_state(timers)
        self.state = STATE_TIMERS_CAPTURED
        # Initialized here, not inside the barrier block: a failure at the
        # post-timer-stop gate happens before the barrier is ever acquired.
        unwound = None
        try:
            self.stop_timers(original)
            self.state = STATE_TIMERS_QUIESCED
            self.gate_preflight("post-timer-stop")

            # Exclusive execution barrier. Acquiring it waits out any job already
            # running through the wrapper, and from here no new one can start —
            # which is what makes the final gate meaningful. It stays held across
            # the pointer sequence, the replacement and the verification, so a
            # manual `systemctl start` during the window blocks and then resolves
            # the *new* release rather than the old tree.
            # Acquisition happens at __enter__, so it must be inside the try
            # or CUTOVER_EXECUTION_BARRIER_UNAVAILABLE is unreachable.
            try:
                barrier = self.execution_barrier(exclusive=True)
                barrier_path = barrier.__enter__()
            except ExecutionBarrierError as exc:
                raise CutoverError("CUTOVER_EXECUTION_BARRIER_UNAVAILABLE", {
                    "classification": exc.classification, "details": exc.details,
                }) from exc
            try:
                self.state = STATE_EXECUTION_BARRIER_EXCLUSIVE
                self._record("execution_barrier", exclusive=True, path=str(barrier_path))
                self.gate_preflight("pre-replacement")
                self.state = STATE_PRECHECK_GREEN

                # R4: one release-management critical section spanning the
                # pointer sequence, the installation and the final verification.
                # Releasing it after the pointers were sequenced would let a
                # concurrent activation move `current` between the verification
                # and the moment production resumes, so the cutover would report
                # a target it is no longer delivering.
                try:
                    management = management_lock(self.release_root)
                    management.__enter__()
                except ReleaseBoundaryError as exc:
                    # Routine contention with `manage_release.py` deserves its own
                    # classification rather than a generic unexpected failure.
                    raise CutoverError("CUTOVER_RELEASE_MANAGEMENT_LOCKED", {
                        "classification": exc.classification, "details": exc.details,
                    }) from exc
                try:
                    write_fence(self.release_root, STATE_IN_PROGRESS,
                                target_release=self.target_release_id,
                                previous_release=self.predecessor_release_id)
                    self.fence_written = True
                    self._record("fence", state=STATE_IN_PROGRESS)
                    self.sequence_pointers()
                    self.install(candidate["expected_wrapper_sha256"])
                    verified = self.verify_installed(candidate["expected_wrapper_sha256"])
                    self.state = STATE_WRAPPER_VERIFIED
                    # Only now may production execute again.
                    write_fence(self.release_root, STATE_VERIFIED,
                                target_release=self.target_release_id,
                                previous_release=self.predecessor_release_id,
                                wrapper_sha256=candidate["expected_wrapper_sha256"])
                    self._record("fence", state=STATE_VERIFIED)
                finally:
                    management.__exit__(None, None, None)
            except BaseException:
                # Inside the barrier deliberately: `finally` would run before the
                # outer handler, releasing every queued job before the fence was
                # reconciled. The fence decision must precede the release.
                unwound = self._unwind(original)
                raise
            finally:
                barrier.__exit__(None, None, None)
        except BaseException as exc:
            # Deliberately broader than CutoverError. `sudo` failing for want of
            # a tty raises CalledProcessError, and an ENOSPC during a pointer
            # swap raises OSError; either escaping here would leave all three
            # ingestion timers stopped with nothing but a traceback — the exact
            # silent-halt this program exists to prevent.
            if unwound is None:
                unwound = self._unwind(original)
            if isinstance(exc, CutoverError):
                exc.details.setdefault("unwind", unwound)
                exc.details.setdefault("state", self.state)
                raise
            raise CutoverError("CUTOVER_UNEXPECTED_FAILURE", {
                "error": f"{type(exc).__name__}: {exc}",
                "state": self.state,
                "unwind": unwound,
                "steps": self.steps,
            }) from exc
        restore = self.restore_timers(original)
        self.state = STATE_TIMER_STATE_RESTORED
        self.state = STATE_COMPLETE
        return {"executed": True, "release_id": self.target_release_id,
                "previous_release_id": self.predecessor_release_id,
                **verified, "original_timer_state": original,
                "timer_restore": restore, "state": self.state,
                "pointer_state": self.pointer_state, "steps": self.steps}

    def _unwind(self, original: Dict[str, bool]) -> Dict[str, object]:
        """Return to the safest reachable state and describe it truthfully.

        The decision hinges on one question — could the installed wrapper have
        changed? Before the replacement phase the answer is no, and the safe
        state is the one we started from. From WRAPPER_REPLACEMENT_ATTEMPTED
        onwards the answer is "not without checking", and the only thing that
        downgrades it is proving the bytes are still the original ones. Anything
        else keeps the timers stopped: restarting production against a wrapper
        whose contents are unknown is worse than an outage that is visible.
        """
        outcome: Dict[str, object] = {"state": self.state,
                                      "pointer_state": self.pointer_state}
        if self.pointer_state != POINTERS_UNTOUCHED:
            outcome["pointers"] = self.observed_pointers()
            outcome["note_pointers"] = (
                "release pointers were touched; they are inert until a release wrapper "
                "is installed. Re-running the transaction re-sequences them idempotently, "
                "or restore explicitly with `manage_release.py activate`.")

        if self.state == STATE_WRAPPER_VERIFIED:
            # The wrapper was proven correct before this failure. Reporting it as
            # uncertain would hand the operator "restore the development wrapper"
            # for a cutover that actually succeeded. Fall through so the fence is
            # still reconciled to VERIFIED before anything is said about timers.
            outcome["wrapper_state"] = "VERIFIED"

        if self.state == STATE_WRAPPER_REPLACEMENT_ATTEMPTED:
            # Written before the caller releases the execution barrier, so a job
            # queued behind it finds the fence closed rather than a free lock.
            try:
                write_fence(self.release_root, STATE_BLOCKED_UNCERTAIN,
                            reason="wrapper replacement attempted but not verified",
                            installed_wrapper=str(self.installed_wrapper),
                            original_wrapper_sha256=str(self.original_wrapper_sha256))
                outcome["fence"] = STATE_BLOCKED_UNCERTAIN
            except Exception as exc:  # noqa: BLE001 - reported, never hidden
                outcome["fence_write_error"] = f"{type(exc).__name__}: {exc}"
            unchanged = self.wrapper_conclusively_unchanged()
            outcome["wrapper_state"] = ("CONCLUSIVELY_UNCHANGED" if unchanged
                                        else "WRAPPER_STATE_MAY_HAVE_CHANGED")
            outcome["installed_wrapper"] = str(self.installed_wrapper)
            outcome["original_wrapper_sha256"] = self.original_wrapper_sha256
            try:
                outcome["observed_wrapper_sha256"] = _sha256_file(self.installed_wrapper)
            except OSError as exc:
                outcome["observed_wrapper_sha256"] = f"unreadable: {exc}"
            if not unchanged:
                outcome["timer_state_restored"] = False
                outcome["note"] = (
                    "TIMERS LEFT STOPPED. The installed wrapper may already have been "
                    "replaced and has not been verified, so production must not resume.")
                outcome["recovery"] = [
                    f"inspect: sha256sum {self.installed_wrapper}",
                    f"restore the development wrapper: sudo flock "
                    f"/run/lock/log-platform-wrapper-install.lock -c 'install -o root -g root "
                    f"-m 0755 {self.backup_path} {self.installed_wrapper}.new && mv -f "
                    f"{self.installed_wrapper}.new {self.installed_wrapper}'",
                    "inspect the fence: ops/manage_cutover_fence.py recovery",
                    "the fence is BLOCKED_UNCERTAIN, so a re-run will refuse until it is "
                    "cleared: ops/manage_cutover_fence.py allow --execute --reason '<why>'",
                    "then either complete the cutover by re-running this transaction, or "
                    "restore the development wrapper as above",
                    "then start only: " + ", ".join(u for u, a in original.items() if a),
                ]
                self._record("unwind", **outcome)
                return outcome
            outcome["note"] = ("wrapper proven byte-identical to the original, so the "
                               "replacement did not take effect; restoring timers")

        # Reconcile the fence this transaction installed. `IN_PROGRESS` blocks
        # every job, so restarting the timers without clearing it would halt all
        # ingestion while reporting the scheduler restored — the fence inverted
        # into the failure it exists to prevent.
        if self.fence_written:
            try:
                if self.state == STATE_WRAPPER_VERIFIED:
                    write_fence(self.release_root, STATE_VERIFIED,
                                target_release=self.target_release_id,
                                previous_release=self.predecessor_release_id,
                                reason="verified before the failure")
                else:
                    write_fence(self.release_root, STATE_ALLOWED,
                                reason="cutover aborted before the wrapper was replaced")
                outcome["fence"] = read_fence(self.release_root).get("state")
            except Exception as exc:  # noqa: BLE001 - reported, never hidden
                outcome["fence_write_error"] = f"{type(exc).__name__}: {exc}"

        fence_now = read_fence(self.release_root)
        outcome["fence_state"] = fence_now.get("state")
        if not fence_now.get("execution_permitted"):
            # Starting timers now would produce a storm of unit failures: every
            # tick would exit 5 against the fence. Say so instead.
            outcome["timer_state_restored"] = False
            outcome["note"] = (f"TIMERS LEFT STOPPED: the cutover fence is "
                               f"{fence_now.get('state')}, so every job would refuse to run. "
                               f"Resolve with `ops/manage_cutover_fence.py recovery`, then "
                               f"`allow --execute --reason '<why>'`, then start: "
                               + ", ".join(u for u, a in original.items() if a))
            self._record("unwind", **outcome)
            return outcome

        try:
            restore = self.restore_timers(original)
            outcome["timer_state_restored"] = True
            outcome["timer_restore"] = restore
        except BaseException as exc:  # noqa: BLE001 - reported, never hidden
            outcome["timer_state_restored"] = False
            outcome["timer_restore_error"] = f"{type(exc).__name__}: {exc}"
            outcome["note"] = ("TIMERS ARE NOT RUNNING — restart them manually: "
                               + ", ".join(u for u, a in original.items() if a))
        self._record("unwind", **outcome)
        return outcome


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--release", required=True, help="release id to promote (the cutover target)")
    parser.add_argument("--previous", required=True,
                        help="release id to leave as the rollback predecessor (the M0 release)")
    parser.add_argument("--release-root", default=str(DEFAULT_RELEASE_ROOT))
    parser.add_argument("--source-repo", default=str(REPO_ROOT))
    parser.add_argument("--backup", default=str(DEFAULT_BACKUP_PATH))
    parser.add_argument("--installed-wrapper", default=str(INSTALLED_WRAPPER_PATH),
                        help="wrapper destination; must be given for a non-default release root")
    parser.add_argument("--assume-timers-stopped", default=None,
                        help="comma-separated timers to restore when all are already inactive")
    parser.add_argument("--execute", action="store_true",
                        help="actually stop timers, move pointers and replace the wrapper")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if (args.execute and Path(args.release_root) != DEFAULT_RELEASE_ROOT
            and Path(args.installed_wrapper) == INSTALLED_WRAPPER_PATH):
        # Otherwise a "rehearsal" against a temp release root would physically
        # install that root's wrapper over the production one.
        print(json.dumps({"classification": "CUTOVER_REHEARSAL_WOULD_TOUCH_PRODUCTION",
                          "release_root": args.release_root,
                          "installed_wrapper": args.installed_wrapper,
                          "reason": "a non-default release root with the production wrapper "
                                    "destination would install a rehearsal wrapper over "
                                    "production; pass --installed-wrapper as well"},
                         indent=2, sort_keys=True), file=sys.stderr)
        return 1
    transaction = CutoverTransaction(
        release_root=Path(args.release_root), source_repo=Path(args.source_repo),
        installed_wrapper=Path(args.installed_wrapper),
        target_release_id=args.release, predecessor_release_id=args.previous,
        backup_path=Path(args.backup),
        assume_timers_stopped=(tuple(t.strip() for t in args.assume_timers_stopped.split(","))
                               if args.assume_timers_stopped else ()),
    )
    try:
        report = transaction.run(execute=args.execute)
    except CutoverError as exc:
        print(json.dumps({"classification": exc.classification, "details": exc.details,
                          "steps": transaction.steps}, indent=2, sort_keys=True, default=str),
              file=sys.stderr)
        return 1
    except BaseException as exc:  # noqa: BLE001 - the steps are the operator's only record
        print(json.dumps({"classification": "CUTOVER_UNEXPECTED_FAILURE",
                          "error": f"{type(exc).__name__}: {exc}",
                          "steps": transaction.steps}, indent=2, sort_keys=True, default=str),
              file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
