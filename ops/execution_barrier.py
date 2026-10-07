"""Execution-quiescence barrier between running jobs and a cutover.

Stopping the timers is not quiescence. After the final preflight passes, a job
can still be started by hand — `systemctl start log-job@dispatcher.service`, or
`/usr/local/bin/log-job-runner.sh` invoked directly — resolve the *development*
wrapper, and keep running mutable source straight through the wrapper
replacement. No amount of runbook prose prevents that; the guarantee has to be
executable.

So every job takes a **shared** lock on `/run/lock/log-platform-execution.lock`
before it resolves which tree to run, and holds it for its whole lifetime. The
cutover takes the **exclusive** side across the final gate, the pointer
sequence, the replacement and the verification. That gives three properties for
free, from the kernel rather than from discipline:

* jobs never contend with each other — shared locks stack, so ordinary
  production is unchanged when no cutover is running;
* a cutover cannot begin its critical section until every job already running
  has exited, because an exclusive request waits behind the shared holders;
* once the cutover holds it, a new job blocks *before* resolving a wrapper, and
  when it is released the job resolves the **new** release — which is exactly
  the race that made "stop the timers" insufficient.

The lock is released by the kernel when the holder dies, so a killed job or a
killed cutover cannot wedge the host.

Lock ordering, to keep it deadlock-free — always in this direction:

    execution barrier  ->  release-management lock  ->  wrapper-install lock

The cutover is the only participant that takes more than one. Jobs take only
the barrier; identity provisioning takes only the wrapper-install lock; release
management takes only its own. No cycle exists because nothing acquires the
barrier while already holding either of the others.
"""
from __future__ import annotations

import errno
import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

EXECUTION_BARRIER_PATH = Path("/run/lock/log-platform-execution.lock")

# A cutover waits behind jobs already running. The dispatcher's unit allows 4h,
# so a bounded wait that is generous but finite keeps a stuck job from turning
# into an unbounded hang; the operator gets a classified refusal instead.
EXCLUSIVE_WAIT_SECONDS = 900.0
_POLL_SECONDS = 0.02


class ExecutionBarrierError(RuntimeError):
    """The barrier could not be taken. Always fail closed on this."""

    def __init__(self, classification: str, details: dict) -> None:
        self.classification = classification
        self.details = details
        super().__init__(f"{classification}: {details}")


def barrier_path() -> Path:
    """Canonical path. A seam for tests to monkeypatch — never an env lookup.

    Reading this from the environment would let a cutover and a job inherit
    different values and take *different* locks, which is precisely the failure
    the barrier exists to prevent.
    """
    return EXECUTION_BARRIER_PATH


def ensure_barrier_file(path: Optional[Path] = None) -> Path:
    """Create the lock file world-writable if it does not exist.

    Jobs run as the service account and a cutover may run privileged, so
    whichever creates it first must leave it usable by the other. `flock` needs
    only an open descriptor, so a read-only open is a valid fallback, but a
    file that cannot be opened at all would take the barrier out of the picture
    silently.
    """
    path = Path(path or barrier_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        try:
            handle = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o666)
            os.close(handle)
            try:
                os.chmod(str(path), 0o666)
            except OSError:
                pass
        except FileExistsError:
            pass
        except OSError as exc:
            raise ExecutionBarrierError("EXECUTION_BARRIER_UNAVAILABLE", {
                "barrier": str(path), "reason": f"{type(exc).__name__}: {exc}",
            }) from exc
    return path


def _open_barrier(path: Path) -> int:
    try:
        return os.open(str(path), os.O_RDWR)
    except PermissionError:
        try:
            return os.open(str(path), os.O_RDONLY)
        except OSError as exc:
            raise ExecutionBarrierError("EXECUTION_BARRIER_UNAVAILABLE", {
                "barrier": str(path), "reason": f"{type(exc).__name__}: {exc}",
            }) from exc
    except OSError as exc:
        raise ExecutionBarrierError("EXECUTION_BARRIER_UNAVAILABLE", {
            "barrier": str(path), "reason": f"{type(exc).__name__}: {exc}",
        }) from exc


@contextmanager
def execution_barrier(*, exclusive: bool, timeout_seconds: float = EXCLUSIVE_WAIT_SECONDS,
                      path: Optional[Path] = None):
    """Hold the barrier: shared for a job, exclusive for a cutover."""
    target = ensure_barrier_file(path)
    handle = _open_barrier(target)
    mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    try:
        while True:
            try:
                fcntl.flock(handle, mode | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise ExecutionBarrierError("EXECUTION_BARRIER_UNAVAILABLE", {
                        "barrier": str(target), "reason": f"{type(exc).__name__}: {exc}",
                    }) from exc
                if time.monotonic() >= deadline:
                    raise ExecutionBarrierError("EXECUTION_BARRIER_BUSY", {
                        "barrier": str(target),
                        "exclusive": exclusive,
                        "waited_seconds": round(timeout_seconds, 3),
                        "reason": ("a job is still executing through the wrapper"
                                   if exclusive else
                                   "a cutover holds the barrier exclusively"),
                    }) from exc
                time.sleep(_POLL_SECONDS)
        try:
            yield target
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        os.close(handle)
