"""Host-level mutual exclusion for writes to the installed job wrapper.

`/usr/local/bin/log-job-runner.sh` decides which source tree production runs
from, and exactly two operations replace it: the release-boundary cutover, and
routine environment-identity provisioning. Without a shared lock those two race:

  1. provisioning plans, sees the development wrapper, decides it is replaceable;
  2. a cutover installs the release wrapper;
  3. provisioning executes its **stale** plan and writes the development wrapper
     back over it.

Production silently returns to the mutable development tree while every release
pointer still looks healthy — the exact failure the boundary exists to prevent.
Deciding replaceability at plan time is therefore not enough; the decision has
to be re-taken inside this lock, immediately before the write.

Why a separate lock from the release-root management lock: that one protects
pointer/journal consistency inside a release root and is owned by the operator
account, whereas this one protects one absolute path in `/usr/local/bin` and
must be shared with root-run provisioning. Different scope, different lifetime,
different privilege — reusing one for both would silently couple them.

`/run/lock` is the canonical location: mode 1777 so both the operator account
and root can create the file, and cleared on reboot, which is correct for a lock
whose only meaning is "a write is in progress right now". `flock` is released by
the kernel when the holder dies, so a killed installer cannot wedge the host.
"""
from __future__ import annotations

import errno
import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

WRAPPER_INSTALL_LOCK_PATH = Path("/run/lock/log-platform-wrapper-install.lock")
WRAPPER_INSTALL_LOCK_TIMEOUT_SECONDS = 300.0
_POLL_SECONDS = 0.02


class WrapperInstallLockError(RuntimeError):
    """The wrapper-install lock could not be taken. Always fail closed on this."""

    def __init__(self, classification: str, details: dict) -> None:
        self.classification = classification
        self.details = details
        super().__init__(f"{classification}: {details}")


def lock_path() -> Path:
    """The one canonical lock path. Deliberately not configurable at runtime.

    An environment variable here would be a correctness hole, not a
    convenience: provisioning and a cutover are separate processes with separate
    environments, so either could inherit a different value, take a *different*
    lock, and reopen the exact stale-plan race this lock exists to close —
    silently, because both would report having locked successfully.

    Tests that need a private path monkeypatch this function; nothing a
    production caller inherits can change what it returns.
    """
    return WRAPPER_INSTALL_LOCK_PATH


def _open_lock(path: Path) -> int:
    """Open the lock file for `flock`, tolerating a root-created file.

    `flock` needs only an open descriptor, not write permission, so a file
    created earlier by root is still lockable by the operator account. Falling
    back to O_RDONLY keeps the two callers sharing one lock instead of silently
    diverging onto separate files.
    """
    try:
        return os.open(str(path), os.O_CREAT | os.O_RDWR, 0o666)
    except PermissionError:
        try:
            return os.open(str(path), os.O_RDONLY)
        except OSError as exc:
            raise WrapperInstallLockError("WRAPPER_INSTALL_LOCK_UNAVAILABLE", {
                "lock": str(path), "reason": f"{type(exc).__name__}: {exc}",
            }) from exc
    except OSError as exc:
        raise WrapperInstallLockError("WRAPPER_INSTALL_LOCK_UNAVAILABLE", {
            "lock": str(path), "reason": f"{type(exc).__name__}: {exc}",
        }) from exc


@contextmanager
def wrapper_install_lock(*, timeout_seconds: Optional[float] = None):
    """Hold exclusive rights to write the installed job wrapper.

    Every caller that writes `/usr/local/bin/log-job-runner.sh` must hold this,
    and must re-read the wrapper's identity *inside* the block — holding the
    lock says nothing about what the file contained before you took it.
    """
    if timeout_seconds is None:
        timeout_seconds = WRAPPER_INSTALL_LOCK_TIMEOUT_SECONDS
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = _open_lock(path)
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    try:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise WrapperInstallLockError("WRAPPER_INSTALL_LOCK_UNAVAILABLE", {
                        "lock": str(path), "reason": f"{type(exc).__name__}: {exc}",
                    }) from exc
                if time.monotonic() >= deadline:
                    raise WrapperInstallLockError("WRAPPER_INSTALL_LOCK_HELD", {
                        "lock": str(path),
                        "waited_seconds": round(timeout_seconds, 3),
                        "reason": "another wrapper installation or provisioning run holds the lock",
                    }) from exc
                time.sleep(_POLL_SECONDS)
        try:
            yield path
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        os.close(handle)
