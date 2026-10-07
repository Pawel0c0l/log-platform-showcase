"""Durable fail-closed gate on production job execution.

The execution barrier makes a cutover exclusive, but it lives in a file
descriptor: the moment the cutover process releases it — including during an
unwind — every queued job is free to run. That is wrong in exactly one case,
and it is the case that matters. If the wrapper was physically replaced and the
install then raised, the transaction does not know whether the installed wrapper
is safe; releasing the barrier at that point lets a job that has been waiting
patiently start executing project code through a wrapper nobody has verified.

So the barrier answers "may I start *right now*", and this fence answers "is
production execution permitted at all". The barrier is held only for the
critical section; the fence outlives the process that wrote it.

States:

``ALLOWED``
    Normal operation. No cutover has recorded anything, or a recovery
    explicitly re-enabled execution.
``IN_PROGRESS``
    A cutover owns the critical section. A wrapper seeing this refuses — and
    keeps refusing if the cutover dies here, because a half-finished cutover is
    not a state to start jobs in. Clearing it requires explicit recovery.
``BLOCKED_UNCERTAIN``
    The wrapper may have been replaced and could not be verified. The strongest
    refusal: production stays stopped until an operator establishes the real
    state and runs a recovery.
``VERIFIED``
    A cutover completed and verified. Execution permitted.

Where it lives, and why. `<release_root>/state/cutover-state.txt`: canonical,
outside both the immutable release tree and the development tree (so no source
edit can forge it), owned by the account that runs cutovers, readable by the
wrappers, and **persistent across reboot** — deliberately not `/run`, because a
reboot must not silently convert `BLOCKED_UNCERTAIN` into "fine now". An absent
file means no cutover has ever recorded state and is read as `ALLOWED`; an
unreadable or unparsable one is read as blocked, because a fence that fails open
is not a fence.

The format is `KEY=VALUE` lines rather than JSON so the wrappers can read it
with `sed` — they must gate execution *before* running any Python, so they
cannot use a Python parser to decide whether Python may run. Writes go through a
temporary file plus `rename`, so a reader never sees half a state.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

FENCE_RELATIVE = "state/cutover-state.txt"

STATE_ALLOWED = "ALLOWED"
STATE_IN_PROGRESS = "IN_PROGRESS"
STATE_BLOCKED_UNCERTAIN = "BLOCKED_UNCERTAIN"
STATE_VERIFIED = "VERIFIED"

# The only two states in which a wrapper may execute project code.
EXECUTION_PERMITTED = frozenset({STATE_ALLOWED, STATE_VERIFIED})
KNOWN_STATES = frozenset({STATE_ALLOWED, STATE_IN_PROGRESS,
                          STATE_BLOCKED_UNCERTAIN, STATE_VERIFIED})

_STATE_RE = re.compile(r"^STATE=([A-Z_]+)$", re.MULTILINE)
_VALUE_RE = re.compile(r"^([A-Z_]+)=(.*)$", re.MULTILINE)


class CutoverFenceError(RuntimeError):
    def __init__(self, classification: str, details: Dict[str, object]) -> None:
        self.classification = classification
        self.details = details
        super().__init__(f"{classification}: {details}")


def fence_path(release_root: Path) -> Path:
    return Path(release_root) / FENCE_RELATIVE


def read_fence(release_root: Path) -> Dict[str, object]:
    """Current fence state, with the fail-closed defaults applied.

    Absent means "never written", which is the pre-cutover normal and therefore
    permitted. Everything else that cannot be understood is refused.
    """
    path = fence_path(release_root)
    if not path.exists():
        return {"state": STATE_ALLOWED, "execution_permitted": True,
                "source": "absent (no cutover has recorded state)", "path": str(path)}
    try:
        text = path.read_text()
    except OSError as exc:
        return {"state": "UNREADABLE", "execution_permitted": False,
                "source": f"unreadable: {exc}", "path": str(path)}
    match = _STATE_RE.search(text)
    if not match:
        return {"state": "MALFORMED", "execution_permitted": False,
                "source": "no STATE= line", "path": str(path)}
    state = match.group(1)
    fields = {key: value for key, value in _VALUE_RE.findall(text)}
    return {"state": state,
            "execution_permitted": state in EXECUTION_PERMITTED,
            "known_state": state in KNOWN_STATES,
            "path": str(path), **{k.lower(): v for k, v in fields.items() if k != "STATE"}}


def write_fence(release_root: Path, state: str, **fields) -> Path:
    """Record a state atomically. Callers must be the authorized cutover path."""
    if state not in KNOWN_STATES:
        raise CutoverFenceError("CUTOVER_FENCE_INVALID_STATE", {"state": state})
    path = fence_path(release_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"STATE={state}",
             f"UPDATED_AT={datetime.now(timezone.utc).isoformat()}",
             f"UPDATED_BY={_username()}@{os.uname().nodename}"]
    for key, value in fields.items():
        clean = str(value).replace("\n", " ")
        lines.append(f"{key.upper()}={clean}")
    staging = path.parent / f".{path.name}.{os.getpid()}.tmp"
    staging.write_text("\n".join(lines) + "\n")
    os.replace(staging, path)
    return path


def require_execution_permitted(release_root: Path) -> Dict[str, object]:
    """Raise unless the fence permits production execution."""
    fence = read_fence(release_root)
    if not fence["execution_permitted"]:
        raise CutoverFenceError("CUTOVER_FENCE_BLOCKS_EXECUTION", dict(fence))
    return fence


def _username() -> str:
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_name
    except Exception:  # pragma: no cover - provenance only
        return str(os.getuid())


def recovery_report(release_root: Path, source_repo: Path,
                    installed_wrapper: Optional[Path] = None) -> Dict[str, object]:
    """Everything an operator must establish before clearing an unsafe fence.

    Deliberately a report, not an action. Clearing the fence because the cutover
    process is no longer running would defeat its purpose — the whole point is
    that the *outcome* was never established. So this gathers the four facts the
    recovery contract requires and leaves the decision to a human.
    """
    from ops.release_boundary import (
        INSTALLED_WRAPPER_PATH,
        ReleaseBoundaryError,
        installed_wrapper_variant,
        pointer_release_id,
        verify_release,
    )

    wrapper = installed_wrapper or INSTALLED_WRAPPER_PATH
    report: Dict[str, object] = {"fence": read_fence(release_root)}
    report["installed_wrapper"] = {
        "path": str(wrapper),
        "variant": installed_wrapper_variant(repo_root=source_repo, installed_wrapper=wrapper),
        "sha256": _sha256(wrapper),
    }
    try:
        current = pointer_release_id(Path(release_root) / "current")
        previous = pointer_release_id(Path(release_root) / "previous")
    except ReleaseBoundaryError as exc:
        current = previous = f"unreadable: {exc.classification}"
    report["pointers"] = {"current": current, "previous": previous}
    if isinstance(current, str) and len(current) == 12:
        try:
            verify_release(release_root=Path(release_root), release_id=current,
                           source_repo=Path(source_repo))
            report["current_release_verifies"] = True
        except ReleaseBoundaryError as exc:
            report["current_release_verifies"] = False
            report["current_release_error"] = exc.classification
    report["next"] = ("establish that the installed wrapper, the pointers and the release "
                      "are the intended ones, then clear the fence explicitly with "
                      "`ops/manage_cutover_fence.py allow --execute --reason '<why>'`")
    return report


def _sha256(path: Path) -> Optional[str]:
    import hashlib

    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None
