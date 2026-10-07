"""Immutable release boundary between the development tree and production runtime.

Production has historically executed `ops/runner.py` straight out of the
development working tree, so an uncommitted edit became production behaviour at
the next timer tick with no promotion, no review point and no rollback target
(`docs/20` §1.9). This module is the mechanism that removes that property.

Three things are kept strictly apart and must never be used as evidence for one
another:

``DEVELOPMENT REPOSITORY``
    `/opt/log-platform` — mutable, frequently dirty,
    the only place work happens. Never a release payload.

``RELEASE TREE``
    `<release_root>/releases/<release_id>` — the byte content of one Git commit,
    extracted with `git archive`, sealed read-only. It contains no `.git`, so it
    cannot be re-pointed by a stray `git checkout`, and nothing in the
    development tree can alter it afterwards.

``ACTIVE RUNTIME``
    whatever `BASE_DIR` the installed `/usr/local/bin/log-job-runner.sh` names.
    A release becomes active only when that wrapper resolves to it — preparing,
    verifying and even activating the `current` pointer changes nothing until
    the wrapper is repointed, which is a separate authorized operation.

Design notes that are load-bearing rather than stylistic:

* **`git archive`, not a worktree.** `docs/20` §5 proposed a worktree checked
  out to a tag. A worktree keeps a `.git` link into the development repository,
  stays writable, and — decisively — promotion by `git checkout` mutates the
  live tree in place, so a dispatcher tick landing mid-checkout would execute a
  half-updated tree. Extraction into a per-release directory plus an atomic
  pointer swap makes a partially constructed release unobservable.
* **Verification is never cached, and does not need the source repository.**
  Provenance metadata carries the full `path -> (mode, blob sha)` manifest of
  the commit, and `verify_release` recomputes Git blob hashes over the actual
  bytes every time. A stored "verified" flag would be exactly the kind of stale
  trust anchor this boundary exists to remove — but a stored *manifest* is the
  opposite, because every entry in it is re-derived from the release bytes on
  each check. Keeping the manifest local matters operationally: a rollback
  target must stay verifiable years later, when its commit may have been made
  unreachable by a rebase, a deleted branch or `git gc`. When the commit is
  still reachable the manifest is additionally cross-checked against
  `git ls-tree`, so a doctored manifest is caught as long as Git can answer.
* **Mutable runtime resources are linked, never copied.** `.env` holds secrets
  and `.venv` is a build artifact; neither belongs to the commit. They are
  declared symlinks whose targets are recorded in metadata and checked by
  verification, so their ownership is explicit instead of accidental.

Every failure mode raises `ReleaseBoundaryError` with a stable classification
string. Nothing here degrades to a best-effort path.
"""
from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, Optional, Tuple

SCHEMA_VERSION = 1
TOOL_NAME = "release_boundary"

RELEASES_DIRNAME = "releases"
META_DIRNAME = "meta"
CURRENT_POINTER = "current"
PREVIOUS_POINTER = "previous"
ACTIVATION_LOG = "activations.log"
MANAGEMENT_LOCK = ".manage.lock"

# How long a mutating operation waits for the management lock before refusing.
# Long enough to sit behind a normal prepare (a `git archive` of this repository
# plus verification), short enough that a stuck operator command surfaces rather
# than hanging a terminal indefinitely.
MANAGEMENT_LOCK_TIMEOUT_SECONDS = 120.0
MANAGEMENT_LOCK_TIMEOUT_ENV = "RELEASE_MANAGEMENT_LOCK_TIMEOUT_SECONDS"
_LOCK_POLL_SECONDS = 0.05


def _default_lock_timeout() -> float:
    """Lock wait budget, overridable so tests need no real contention delays."""
    raw = os.environ.get(MANAGEMENT_LOCK_TIMEOUT_ENV)
    if raw is None:
        return MANAGEMENT_LOCK_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return MANAGEMENT_LOCK_TIMEOUT_SECONDS
    return value if value >= 0 else MANAGEMENT_LOCK_TIMEOUT_SECONDS

# Re-entrancy bookkeeping, per release root **and per thread**.
#
# `flock` is per open file description, so a second `open()` inside the same
# process would block against itself — hence depth counting for legitimate
# nesting. But keying that state by release root alone is a correctness bug:
# thread B entering while thread A holds the lock would see the process-global
# "held" marker, believe itself a nested owner, skip `flock` entirely and run a
# mutation concurrently with A. Ownership is therefore keyed by
# (release root, thread ident), and a per-root RLock makes different threads in
# one process queue exactly as different processes do.
_LOCK_STATE_GUARD = threading.Lock()
_HELD_LOCKS: Dict[Tuple[str, int], list] = {}
_ROOT_RLOCKS: Dict[str, threading.RLock] = {}


def _root_rlock(key: str) -> threading.RLock:
    with _LOCK_STATE_GUARD:
        lock = _ROOT_RLOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _ROOT_RLOCKS[key] = lock
        return lock

# Runtime resources that live outside the commit. The names are the paths the
# release tree exposes; the targets are supplied by the operator and recorded in
# provenance, because `docs/20` §5.5 requires this to be decided, not defaulted.
RUNTIME_LINK_ENV = ".env"
RUNTIME_LINK_VENV = ".venv"
DEFAULT_RUNTIME_LINK_NAMES = (RUNTIME_LINK_ENV, RUNTIME_LINK_VENV)

_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_RELEASE_ID_RE = re.compile(r"^[0-9a-f]{12}$")
_STAGING_PREFIX = ".staging-"

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class ReleaseBoundaryError(RuntimeError):
    """A refused operation. Carries a stable classification and bounded details."""

    def __init__(self, classification: str, details: Dict[str, object]) -> None:
        self.classification = classification
        self.details = details
        super().__init__(f"{classification}: {json.dumps(details, sort_keys=True, default=str)}")


# --------------------------------------------------------------------------
# Git primitives
# --------------------------------------------------------------------------

def _git(repo: Path, *args: str, runner: Runner = subprocess.run, binary: bool = False):
    result = runner(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=not binary,
    )
    if result.returncode != 0:
        stderr = result.stderr if isinstance(result.stderr, str) else result.stderr.decode("utf-8", "replace")
        raise ReleaseBoundaryError("RELEASE_GIT_COMMAND_FAILED", {
            "repository": str(repo),
            "args": list(args),
            "returncode": result.returncode,
            "stderr": stderr.strip()[:500],
        })
    return result.stdout


def resolve_commit(repo: Path, committish: str, *, runner: Runner = subprocess.run) -> str:
    """Resolve `committish` to a full 40-hex commit SHA, or refuse.

    A ref that resolves to a tag or tree is refused rather than silently
    peeled: a release must name the commit it ships.
    """
    repo = Path(repo)
    if not committish or not committish.strip():
        raise ReleaseBoundaryError("RELEASE_COMMIT_UNRESOLVED", {"requested": committish})
    try:
        raw = _git(repo, "rev-parse", "--verify", "--end-of-options", f"{committish}^{{commit}}", runner=runner)
    except ReleaseBoundaryError as exc:
        raise ReleaseBoundaryError("RELEASE_COMMIT_UNRESOLVED", {
            "requested": committish,
            "repository": str(repo),
            "git_error": exc.details.get("stderr"),
        }) from exc
    sha = raw.strip()
    if not _FULL_SHA_RE.match(sha):
        raise ReleaseBoundaryError("RELEASE_COMMIT_UNRESOLVED", {"requested": committish, "resolved": sha})
    object_type = _git(repo, "cat-file", "-t", sha, runner=runner).strip()
    if object_type != "commit":
        raise ReleaseBoundaryError("RELEASE_SOURCE_NOT_A_COMMIT", {
            "requested": committish, "resolved": sha, "object_type": object_type,
        })
    return sha


def commit_tree_entries(
    repo: Path, commit: str, *, runner: Runner = subprocess.run,
) -> Dict[str, Tuple[str, str]]:
    """Map ``path -> (git mode, blob sha)`` for every file in `commit`.

    `-z` keeps paths that contain spaces or non-ASCII bytes intact; the repo has
    both. A non-blob entry (submodule, symlink) is refused rather than skipped,
    because the extraction and verification paths below only model plain files.
    """
    raw = _git(repo, "ls-tree", "-r", "-z", "--full-tree", commit, runner=runner)
    entries: Dict[str, Tuple[str, str]] = {}
    for record in raw.split("\0"):
        if not record:
            continue
        meta, _, path = record.partition("\t")
        mode, object_type, object_sha = meta.split()
        if object_type != "blob" or mode not in ("100644", "100755"):
            raise ReleaseBoundaryError("RELEASE_UNSUPPORTED_TREE_ENTRY", {
                "commit": commit, "path": path, "mode": mode, "object_type": object_type,
            })
        entries[path] = (mode, object_sha)
    if not entries:
        raise ReleaseBoundaryError("RELEASE_EMPTY_COMMIT_TREE", {"commit": commit})
    return entries


def blob_sha1(data: bytes) -> str:
    """Git's blob hash for `data` — the same identity `git ls-tree` reports."""
    header = f"blob {len(data)}\0".encode()
    return hashlib.sha1(header + data).hexdigest()


def source_tree_digest(entries: Dict[str, Tuple[str, str]]) -> str:
    """One stable digest over the whole source surface (mode + path + content).

    Two materializations of the same commit produce the same digest; any
    difference in content, path set or executable bit changes it.
    """
    payload = "\n".join(f"{mode} {sha} {path}" for path, (mode, sha) in sorted(entries.items()))
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def worktree_is_dirty(repo: Path, *, runner: Runner = subprocess.run) -> bool:
    """Whether the development tree has any tracked or untracked change.

    Recorded as provenance only. It is deliberately **not** a gate: the release
    payload comes from the commit object, so a dirty tree cannot contaminate it.
    """
    raw = _git(repo, "status", "--porcelain=v1", "--untracked-files=all", runner=runner)
    return bool(raw.strip())


# --------------------------------------------------------------------------
# Release identity and layout
# --------------------------------------------------------------------------

def release_id_for(commit: str) -> str:
    """Content-addressed release id: the first 12 hex characters of the commit.

    Deliberately not timestamped. Preparing the same commit twice must converge
    on the same release rather than accumulate near-duplicates, which is what
    makes preparation idempotent and re-preparation a verification instead of a
    rebuild.
    """
    if not _FULL_SHA_RE.match(commit):
        raise ReleaseBoundaryError("RELEASE_METADATA_INVALID", {"commit": commit})
    return commit[:12]


@dataclass(frozen=True)
class ReleaseLayout:
    root: Path

    @property
    def releases_dir(self) -> Path:
        return self.root / RELEASES_DIRNAME

    @property
    def meta_dir(self) -> Path:
        return self.root / META_DIRNAME

    @property
    def current(self) -> Path:
        return self.root / CURRENT_POINTER

    @property
    def previous(self) -> Path:
        return self.root / PREVIOUS_POINTER

    @property
    def activation_log(self) -> Path:
        return self.root / ACTIVATION_LOG

    def release_path(self, release_id: str) -> Path:
        return self.releases_dir / release_id

    def meta_path(self, release_id: str) -> Path:
        return self.meta_dir / f"{release_id}.json"


@contextmanager
def management_lock(
    release_root: Path,
    *,
    timeout_seconds: Optional[float] = None,
):
    """Serialize every mutating operation on one release root.

    Individually atomic renames are not enough. Activation is a *sequence* —
    read `current`, write `previous`, write `current`, append the journal — and
    two operators running it concurrently can both read the same outgoing
    release, so one of them records a predecessor that was never current. The
    resulting `previous` points at the wrong release and rollback silently goes
    to the wrong code. The same applies to `remove`, whose "is this release
    referenced?" check would otherwise be a stale observation by the time the
    directory is deleted.

    `flock` on a lock file inside the release root is the right primitive here:
    it is advisory but every writer goes through this function, it is scoped to
    exactly one release root, and — decisively — the kernel drops it when the
    holding process dies. A killed or crashed operator command therefore cannot
    leave a lock that blocks all future management, which a lock-file-with-PID
    scheme would.

    Read-only operations (`verify`, `status`, `list`) deliberately do not take
    it: they never mutate, and making observation contend with promotion would
    only tempt operators to skip verification.
    """
    if timeout_seconds is None:
        timeout_seconds = _default_lock_timeout()
    root = Path(release_root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    # Resolve the *directory*, never the lock file: resolving a path that does
    # not exist yet yields a different key than resolving it once created, so a
    # nested acquisition would miss the re-entrancy table and block on its own
    # flock. The directory always exists by this point.
    lock_path = root.resolve() / MANAGEMENT_LOCK
    key = str(lock_path)
    owner = (key, threading.get_ident())

    # Serialize threads of this process first. An RLock is re-entrant for the
    # owning thread, so legitimate nesting still passes straight through, while
    # another thread waits here instead of racing past the flock.
    rlock = _root_rlock(key)
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    if not rlock.acquire(timeout=max(0.0, timeout_seconds)):
        raise ReleaseBoundaryError("RELEASE_MANAGEMENT_LOCKED", {
            "release_root": str(root), "lock": key,
            "waited_seconds": round(timeout_seconds, 3),
            "reason": "another thread in this process holds the release-management lock",
        })
    try:
        with _LOCK_STATE_GUARD:
            held = _HELD_LOCKS.get(owner)
            if held:
                held[0] += 1
                reentrant = True
            else:
                reentrant = False
        if reentrant:
            try:
                yield
            finally:
                with _LOCK_STATE_GUARD:
                    entry = _HELD_LOCKS.get(owner)
                    if entry:
                        entry[0] -= 1
                        if entry[0] <= 0:
                            _HELD_LOCKS.pop(owner, None)
            return

        try:
            handle = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        except PermissionError:
            # A lock file created by a privileged run is still lockable: flock
            # needs an open descriptor, not write permission. Falling back keeps
            # both callers on one lock instead of failing with a bare OSError.
            try:
                handle = os.open(str(lock_path), os.O_RDONLY)
            except OSError as exc:
                raise ReleaseBoundaryError("RELEASE_MANAGEMENT_LOCKED", {
                    "release_root": str(root), "lock": key,
                    "reason": f"lock file is not openable: {type(exc).__name__}: {exc}",
                }) from exc
        try:
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN):
                        raise
                    if time.monotonic() >= deadline:
                        raise ReleaseBoundaryError("RELEASE_MANAGEMENT_LOCKED", {
                            "release_root": str(root),
                            "lock": key,
                            "waited_seconds": round(timeout_seconds, 3),
                            "reason": "another release-management operation holds the lock",
                        }) from exc
                    time.sleep(_LOCK_POLL_SECONDS)
            # Confirm the lock we hold is still the lock file at that path. If it
            # was unlinked or replaced while we waited, our fd guards a detached
            # inode and another process could enter the critical section too.
            try:
                on_disk = os.stat(str(lock_path))
                held_stat = os.fstat(handle)
                if (on_disk.st_dev, on_disk.st_ino) != (held_stat.st_dev, held_stat.st_ino):
                    raise ReleaseBoundaryError("RELEASE_MANAGEMENT_LOCKED", {
                        "release_root": str(root), "lock": key,
                        "reason": "lock file was replaced while the lock was being acquired",
                    })
            except FileNotFoundError as exc:
                raise ReleaseBoundaryError("RELEASE_MANAGEMENT_LOCKED", {
                    "release_root": str(root), "lock": key,
                    "reason": "lock file disappeared while the lock was being acquired",
                }) from exc

            with _LOCK_STATE_GUARD:
                _HELD_LOCKS[owner] = [1, handle]
            try:
                yield
            finally:
                with _LOCK_STATE_GUARD:
                    _HELD_LOCKS.pop(owner, None)
                fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            os.close(handle)
    finally:
        rlock.release()


def _validated_layout(release_root: Path, source_repo: Optional[Path] = None) -> ReleaseLayout:
    root = Path(release_root).expanduser()
    if not root.is_absolute():
        raise ReleaseBoundaryError("RELEASE_ROOT_INVALID", {"release_root": str(root), "reason": "not_absolute"})
    if source_repo is not None:
        repo = Path(source_repo).expanduser().resolve()
        probe = root.resolve() if root.exists() else root
        if probe == repo:
            raise ReleaseBoundaryError("RELEASE_ROOT_INVALID", {
                "release_root": str(root),
                "reason": "release_root_is_the_development_repository",
            })
    return ReleaseLayout(root)


# --------------------------------------------------------------------------
# Materialization
# --------------------------------------------------------------------------

def _seal_tree(path: Path) -> None:
    """Drop every write bit from the extracted files **and directories**.

    Sealing directories is not cosmetic. Running the release is itself a write
    attempt: CPython creates `__pycache__` next to every module it imports, so a
    tree with writable directories acquires files its commit does not contain on
    the very first execution and fails verification from then on. Read-only
    directories make that impossible — CPython degrades silently to not caching
    — and the release wrapper additionally redirects bytecode to a mutable
    `PYTHONPYCACHEPREFIX` so nothing is lost by it.

    An unprivileged owner can always chmod this back, so it is a guard against
    accidental and automatic writes, not a claim of kernel-level immutability.
    The enforced guarantee is `verify_release`, mandatory before activation.

    Files are sealed before directories: removing write permission from a
    directory does not prevent chmod of the entries already inside it, but the
    reverse order would need the parent writable for nothing.
    """
    entries = sorted(path.rglob("*"), reverse=True)
    for entry in entries:
        if entry.is_symlink() or entry.is_dir():
            continue
        entry.chmod(stat.S_IMODE(entry.stat().st_mode) & ~0o222)
    for entry in entries:
        if entry.is_symlink() or not entry.is_dir():
            continue
        entry.chmod(stat.S_IMODE(entry.stat().st_mode) & ~0o222)
    path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o222)


def _reject_colliding_link_names(entries: Dict[str, Tuple[str, str]], link_names: Iterable[str]) -> None:
    """Refuse a runtime link that would shadow committed content.

    Checked against the commit tree *before* extraction. A link named after a
    committed directory would otherwise mask that whole subtree from
    verification's view — the one way a declared link could hide missing source
    rather than expose it.
    """
    committed_dirs = {parent for path in entries for parent in Path(path).parents if parent != Path(".")}
    committed_dir_names = {p.as_posix() for p in committed_dirs}
    for name in link_names:
        if "/" in name or name in ("", ".", ".."):
            raise ReleaseBoundaryError("RELEASE_RUNTIME_LINK_MISMATCH", {
                "name": name, "reason": "invalid_link_name",
            })
        if name in entries or name in committed_dir_names:
            raise ReleaseBoundaryError("RELEASE_RUNTIME_LINK_MISMATCH", {
                "name": name, "reason": "commit_already_provides_this_path",
            })


def _link_runtime_resources(tree: Path, runtime_links: Dict[str, Path]) -> Dict[str, str]:
    resolved: Dict[str, str] = {}
    for name, target in runtime_links.items():
        if "/" in name or name in ("", ".", ".."):
            raise ReleaseBoundaryError("RELEASE_RUNTIME_LINK_MISMATCH", {"name": name, "reason": "invalid_link_name"})
        target_path = Path(target).expanduser()
        if not target_path.is_absolute():
            raise ReleaseBoundaryError("RELEASE_RUNTIME_RESOURCE_MISSING", {
                "name": name, "target": str(target_path), "reason": "not_absolute",
            })
        if not target_path.exists():
            raise ReleaseBoundaryError("RELEASE_RUNTIME_RESOURCE_MISSING", {
                "name": name, "target": str(target_path), "reason": "does_not_exist",
            })
        link = tree / name
        if link.exists() or link.is_symlink():
            raise ReleaseBoundaryError("RELEASE_RUNTIME_LINK_MISMATCH", {
                "name": name, "reason": "commit_already_provides_this_path",
            })
        link.symlink_to(target_path)
        resolved[name] = str(target_path)
    return resolved


def prepare_release(
    *,
    source_repo: Path,
    release_root: Path,
    committish: str,
    runtime_links: Optional[Dict[str, Path]] = None,
    runner: Runner = subprocess.run,
    now: Optional[datetime] = None,
) -> Dict[str, object]:
    """Materialize the commit named by `committish` as an immutable release.

    Idempotent by construction: if the release directory already exists it is
    verified against the same commit and reused when identical, and refused when
    it is not. An existing release is never overwritten in place.

    The payload is produced by `git archive` from the commit object. The
    development working tree is never read, which is what makes dirty-tree
    isolation structural rather than a checked condition.
    """
    source_repo = Path(source_repo).expanduser().resolve()
    layout = _validated_layout(release_root, source_repo)
    with management_lock(layout.root):
        return _prepare_release_locked(
            source_repo=source_repo, layout=layout, committish=committish,
            runtime_links=runtime_links, runner=runner, now=now,
        )


def _prepare_release_locked(
    *,
    source_repo: Path,
    layout: "ReleaseLayout",
    committish: str,
    runtime_links: Optional[Dict[str, Path]],
    runner: Runner,
    now: Optional[datetime],
) -> Dict[str, object]:
    """Body of `prepare_release`, executed under the management lock."""
    commit = resolve_commit(source_repo, committish, runner=runner)
    release_id = release_id_for(commit)
    entries = commit_tree_entries(source_repo, commit, runner=runner)
    digest = source_tree_digest(entries)
    links = {name: Path(target) for name, target in (runtime_links or {}).items()}
    _reject_colliding_link_names(entries, links)

    target = layout.release_path(release_id)
    if target.exists():
        report = verify_release(release_root=layout.root, release_id=release_id,
                                source_repo=source_repo, runner=runner)
        if report["commit"] != commit or report["source_tree_digest"] != digest:
            raise ReleaseBoundaryError("RELEASE_ALREADY_EXISTS_MISMATCH", {
                "release_id": release_id,
                "existing_commit": report["commit"],
                "requested_commit": commit,
            })
        declared = report["metadata"].get("runtime_links", {})
        requested = {name: str(Path(t).expanduser()) for name, t in links.items()}
        if requested and requested != declared:
            raise ReleaseBoundaryError("RELEASE_ALREADY_EXISTS_MISMATCH", {
                "release_id": release_id,
                "existing_runtime_links": declared,
                "requested_runtime_links": requested,
            })
        return {"release_id": release_id, "commit": commit, "reused": True,
                "release_path": str(target), "source_tree_digest": digest}

    layout.releases_dir.mkdir(parents=True, exist_ok=True)
    layout.meta_dir.mkdir(parents=True, exist_ok=True)

    # Build under a staging name and rename into place, so a concurrent reader
    # (or an interrupted run) can never observe a partially extracted release.
    staging = layout.releases_dir / f"{_STAGING_PREFIX}{os.getpid()}-{secrets.token_hex(4)}"
    staging.mkdir(mode=0o755)
    try:
        archive = _git(source_repo, "archive", "--format=tar", commit, runner=runner, binary=True)
        extract = subprocess.run(
            ["tar", "-x", "-f", "-", "-C", str(staging)],
            input=archive, capture_output=True, check=False,
        )
        if extract.returncode != 0:
            raise ReleaseBoundaryError("RELEASE_INCOMPLETE_MATERIALIZATION", {
                "release_id": release_id,
                "stderr": extract.stderr.decode("utf-8", "replace")[:500],
            })
        observed = _scan_tree(staging, set(links))
        mismatches = _compare(entries, observed)
        if mismatches:
            raise ReleaseBoundaryError("RELEASE_INCOMPLETE_MATERIALIZATION", {
                "release_id": release_id, "commit": commit, "mismatches": mismatches[:20],
            })
        resolved_links = _link_runtime_resources(staging, links)
        _seal_tree(staging)

        created = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        metadata = {
            "schema_version": SCHEMA_VERSION,
            "tool": TOOL_NAME,
            "release_id": release_id,
            "commit": commit,
            "requested_committish": committish,
            "commit_committed_at": _git(source_repo, "log", "-1", "--format=%cI", commit, runner=runner).strip(),
            "commit_subject": _git(source_repo, "log", "-1", "--format=%s", commit, runner=runner).strip(),
            "source_repository_root": str(source_repo),
            "source_repository_origin": _origin_url(source_repo, runner=runner),
            "source_worktree_dirty_at_preparation": worktree_is_dirty(source_repo, runner=runner),
            "source_tree_digest": digest,
            "file_count": len(entries),
            "runtime_links": resolved_links,
            "created_at": created.isoformat(),
            "created_by": f"{_username()}@{os.uname().nodename}",
            # Full expectation, so verification never depends on the source
            # repository still being able to resolve the commit.
            "manifest": {path: [mode, sha] for path, (mode, sha) in sorted(entries.items())},
        }
        # Provenance is written *before* the tree is renamed into place. The two
        # writes cannot be made one atomic operation, so the order is chosen for
        # how the survivable states behave: metadata without a tree makes
        # `prepare` rebuild cleanly on the next run, whereas a tree without
        # metadata would be permanently unverifiable and would need a manual
        # `remove` to clear.
        layout.meta_path(release_id).write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
        os.rename(staging, target)
    except BaseException:
        _remove_staging(staging)
        raise

    return {"release_id": release_id, "commit": commit, "reused": False,
            "release_path": str(target), "source_tree_digest": digest}


def _origin_url(repo: Path, *, runner: Runner = subprocess.run) -> Optional[str]:
    try:
        return _git(repo, "config", "--get", "remote.origin.url", runner=runner).strip() or None
    except ReleaseBoundaryError:
        return None


def _username() -> str:
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_name
    except Exception:  # pragma: no cover - identity is provenance, never a gate
        return str(os.getuid())


def _remove_staging(staging: Path) -> None:
    """Discard a failed materialization. Sealing may already have run, so write
    permission is restored on the way down before anything is unlinked."""
    if not staging.exists():
        return
    try:
        staging.chmod(0o755)
    except OSError:
        pass
    for entry in sorted(staging.rglob("*"), reverse=True):
        try:
            if entry.is_dir() and not entry.is_symlink():
                entry.chmod(0o755)
        except OSError:
            pass
    for entry in sorted(staging.rglob("*"), reverse=True):
        try:
            if entry.is_symlink() or entry.is_file():
                entry.unlink()
            elif entry.is_dir():
                entry.rmdir()
        except OSError:
            pass
    try:
        staging.rmdir()
    except OSError:
        pass


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------

def _scan_tree(tree: Path, ignore_names: Iterable[str] = ()) -> Dict[str, Tuple[str, str]]:
    ignored = set(ignore_names)
    observed: Dict[str, Tuple[str, str]] = {}
    for path in tree.rglob("*"):
        relative = path.relative_to(tree).as_posix()
        if relative in ignored or relative.split("/", 1)[0] in ignored:
            continue
        if path.is_symlink():
            observed[relative] = ("symlink", os.readlink(path))
            continue
        if path.is_dir():
            continue
        mode = "100755" if path.stat().st_mode & stat.S_IXUSR else "100644"
        observed[relative] = (mode, blob_sha1(path.read_bytes()))
    return observed


def _compare(expected: Dict[str, Tuple[str, str]], observed: Dict[str, Tuple[str, str]]) -> list:
    mismatches = []
    for path in sorted(set(expected) | set(observed)):
        want = expected.get(path)
        got = observed.get(path)
        if want is None:
            mismatches.append({"path": path, "reason": "unexpected_file_in_release"})
        elif got is None:
            mismatches.append({"path": path, "reason": "missing_from_release"})
        elif want != got:
            reason = "mode_mismatch" if want[1] == got[1] else "content_mismatch"
            mismatches.append({"path": path, "reason": reason,
                               "expected": f"{want[0]} {want[1]}", "observed": f"{got[0]} {got[1]}"})
    return mismatches


def _manifest_entries(metadata: Dict[str, object], release_id: str) -> Dict[str, Tuple[str, str]]:
    """Rebuild the expected file map from the release's own provenance."""
    manifest = metadata.get("manifest")
    if not isinstance(manifest, dict) or not manifest:
        raise ReleaseBoundaryError("RELEASE_METADATA_INVALID", {
            "release_id": release_id, "reason": "missing_manifest",
            "remediation": "this release predates manifest provenance; remove and re-prepare it",
        })
    entries: Dict[str, Tuple[str, str]] = {}
    for path, value in manifest.items():
        if not isinstance(value, list) or len(value) != 2:
            raise ReleaseBoundaryError("RELEASE_METADATA_INVALID", {
                "release_id": release_id, "reason": "malformed_manifest_entry", "path": path,
            })
        mode, sha = value
        if mode not in ("100644", "100755") or not re.fullmatch(r"[0-9a-f]{40}", str(sha)):
            raise ReleaseBoundaryError("RELEASE_METADATA_INVALID", {
                "release_id": release_id, "reason": "malformed_manifest_entry", "path": path,
            })
        entries[path] = (mode, sha)
    return entries


def _cross_check_against_git(
    source_repo: Path,
    commit: str,
    entries: Dict[str, Tuple[str, str]],
    release_id: str,
    *,
    runner: Runner = subprocess.run,
) -> Tuple[str, bool]:
    """Confirm the stored manifest still matches Git, when Git can still answer.

    A release must stay verifiable after its commit becomes unreachable, so an
    absent commit is reported rather than fatal — the manifest alone is still a
    complete expectation. But while the commit *is* reachable, disagreeing with
    it is fatal: that is the case where the manifest has been doctored.
    """
    try:
        resolved = resolve_commit(source_repo, commit, runner=runner)
    except ReleaseBoundaryError:
        return commit, False
    git_entries = commit_tree_entries(source_repo, resolved, runner=runner)
    if git_entries != entries:
        differing = sorted(set(git_entries) ^ set(entries)) or [
            path for path in sorted(entries) if entries[path] != git_entries.get(path)
        ]
        raise ReleaseBoundaryError("RELEASE_METADATA_INVALID", {
            "release_id": release_id, "commit": resolved,
            "reason": "manifest_disagrees_with_source_repository",
            "paths": differing[:20],
        })
    return resolved, True


#: `_readlink_or_absent` returns this when the link is genuinely gone.
LINK_ABSENT = object()


def _readlink_or_absent(path: Path):
    """`os.readlink`, resolving the TOCTOU race into a DEFINITE answer.

    Every caller here checks `is_symlink()` (or `exists()`) and then reads the
    link. Between those two syscalls the link can be removed — by a concurrent
    `repair-previous`, an activation swap, or an operator — and `readlink` then
    raises. On 2026-08-20 that surfaced as a bare traceback out of `rollback`.

    A vanished link is NOT an unknowable condition, which is what makes this
    fixable without deciding whether verification may fail open: re-checking
    answers the question outright. If the path is no longer a symlink, the link
    is absent, and absent is a defect the existing vocabulary already reports.

    It deliberately does NOT retry. Retrying would let a flapping link settle
    into a false pass, and the point is to reach a definite verdict, not a
    convenient one. A genuinely absent link is still reported as absent.

    If the path IS still a symlink and reading it still fails, the fault is
    something other than the race — a filesystem or permissions problem this
    function cannot characterise. That case RE-RAISES unchanged, on purpose:
    turning it into a verdict would be deciding whether verification may proceed
    without an answer, which is a separate semantics question and not this one.
    """

    try:
        return os.readlink(path)
    except OSError:
        if not path.is_symlink():
            return LINK_ABSENT
        raise


# ---------------------------------------------------------------------------
# What is ACTUALLY EXECUTING, as opposed to what the pointers intend
# ---------------------------------------------------------------------------
#
# The pointer and the running process are different facts and they drift apart
# in one specific, silent way: activation moves `current`, and until somebody
# restarts the services the old release keeps serving. Every field the release
# tooling had before this described files and symlinks, so a host in that state
# reported a clean picture of a release that had never executed — which is
# exactly what happened between 2026-08-25 and 2026-08-27.
#
# WHY /proc/<pid>/cwd IS THE RIGHT WITNESS. The launcher resolves `current` at
# process start and cd's into the release it resolved to. The kernel stores a
# cwd as the directory itself, not as the path walked to reach it, so a later
# pointer move cannot rewrite it: the running process keeps reporting the
# release it actually started under. Reading the symlink again would only ever
# tell us what we already know from the pointer, and would report agreement in
# precisely the case we need to detect.

#: The long-running services bound to the release boundary since 2026-08-20.
#: Both execute the active release through `/usr/local/bin/log-ops-runner.sh`,
#: so both are witnesses and a drift can in principle affect one and not the
#: other -- which is why they are observed individually rather than sampled.
RELEASE_BOUND_SERVICES: Tuple[str, ...] = (
    "log-platform-api.service",
    "database-export-worker.service",
)

RUNNING_RELEASE_OBSERVED = "observed"
RUNNING_RELEASE_UNKNOWN = "unknown"
#: Observed, but the witnesses do not agree with each other. Still an
#: observation, never an unknown: we know something is wrong.
RUNNING_RELEASE_DIVERGENT = "divergent"


def unknown_running_release(reason: str, detail: Optional[str] = None,
                            services: Optional[list] = None) -> Dict[str, object]:
    """The verdict when the running release could not be observed.

    `release_id` is None and the verdict is UNKNOWN, never "no drift". The same
    rule `unknown_bootability` follows, for the same reason: a host where
    systemd is absent, `/proc` is unreadable or the unit is stopped has not told
    us that the pointer matches, and a caller must be able to tell "they differ"
    from "I could not find out". Collapsing unknown into either answer is how a
    monitor learns to report a state it never verified.
    """

    return {
        "release_id": None,
        "verdict": RUNNING_RELEASE_UNKNOWN,
        "reason": reason,
        "observation_error": detail,
        "services": services or [],
    }


def _systemd_main_pid(service: str) -> Optional[int]:
    """The service's MainPID via systemd, or None when it cannot be had.

    Returns None rather than raising for every failure mode -- no systemctl on
    the host, the unit not known, the unit stopped (systemd reports MainPID=0),
    a timeout. Each of those is an UNKNOWN, and none of them is evidence about
    which release is running.
    """

    try:
        proc = subprocess.run(
            ["systemctl", "show", service, "--property=MainPID", "--value"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    raw = (proc.stdout or "").strip()
    if not raw.isdigit():
        return None
    pid = int(raw)
    return pid or None  # systemd reports 0 for a unit with no main process


def _process_cwd(pid: int) -> Optional[Path]:
    """Where the process actually is, or None if it cannot be read."""

    try:
        return Path(os.readlink(f"/proc/{pid}/cwd"))
    except OSError:
        return None


def release_id_from_path(release_root: Path, path: Path) -> Optional[str]:
    """The release a path lies inside, or None if it lies outside the layout.

    Resolves both sides: the observed cwd is already a real path, and the
    release root may itself be reached through a symlink, so comparing the two
    unresolved would report a false "outside the layout" on an ordinary host.
    """

    try:
        releases = Path(release_root).resolve() / RELEASES_DIRNAME
        relative = Path(path).resolve().relative_to(releases)
    except (OSError, ValueError):
        return None
    if not relative.parts:
        return None
    candidate = relative.parts[0]
    return candidate if _RELEASE_ID_RE.match(candidate) else None


def observe_running_release(
    *,
    release_root: Path,
    services: Iterable[str] = RELEASE_BOUND_SERVICES,
    main_pid_reader: Optional[Callable[[str], Optional[int]]] = None,
    cwd_reader: Optional[Callable[[int], Optional[Path]]] = None,
) -> Dict[str, object]:
    """Which release the release-bound services are executing right now.

    NEVER RAISES. This is an observation, and a host that cannot be observed
    must produce an UNKNOWN verdict rather than an exception: the callers
    include `status`, which an operator reaches for during an incident, and a
    traceback there would be the 2026-08-20 lesson repeated in a new place.

    The readers are injectable so the three verdicts can be exercised
    deterministically. Nothing in the test path may consult the real host.
    """

    read_pid = main_pid_reader or _systemd_main_pid
    read_cwd = cwd_reader or _process_cwd

    observations: list = []
    for service in services:
        entry: Dict[str, object] = {"service": service, "main_pid": None,
                                    "release_id": None, "verdict": RUNNING_RELEASE_UNKNOWN,
                                    "reason": None}
        try:
            pid = read_pid(service)
            if not pid:
                entry["reason"] = "no_main_pid"
            else:
                entry["main_pid"] = pid
                cwd = read_cwd(pid)
                if cwd is None:
                    entry["reason"] = "process_cwd_unreadable"
                else:
                    entry["cwd"] = str(cwd)
                    release_id = release_id_from_path(Path(release_root), cwd)
                    if release_id is None:
                        entry["reason"] = "cwd_outside_release_layout"
                    else:
                        entry["release_id"] = release_id
                        entry["verdict"] = RUNNING_RELEASE_OBSERVED
        except Exception as exc:  # noqa: BLE001 - an observation must not raise
            entry["reason"] = "observation_failed"
            entry["observation_error"] = f"{type(exc).__name__}: {exc}"
        observations.append(entry)

    seen = {e["release_id"] for e in observations if e["verdict"] == RUNNING_RELEASE_OBSERVED}
    if not seen:
        reasons = sorted({str(e.get("reason")) for e in observations}) or ["no_services_checked"]
        return unknown_running_release(
            reason=reasons[0] if len(reasons) == 1 else "no_service_could_be_observed",
            detail="; ".join(f"{e['service']}: {e.get('reason')}" for e in observations) or None,
            services=observations,
        )
    if len(seen) > 1:
        # Two release-bound services executing different releases is itself a
        # finding, and an observed one. It must not read as unknown.
        return {"release_id": None, "verdict": RUNNING_RELEASE_DIVERGENT,
                "reason": "release_bound_services_disagree",
                "observation_error": None,
                "release_ids": sorted(str(s) for s in seen),
                "services": observations}
    return {"release_id": seen.pop(), "verdict": RUNNING_RELEASE_OBSERVED,
            "reason": None, "observation_error": None, "services": observations}


def pointer_matches_running(current_release_id: Optional[str],
                            running: Dict[str, object]) -> Optional[bool]:
    """True / False / None -- and None is a real answer, not a missing one.

    None means the running release could not be observed. It must never be
    rendered as agreement, which would restore the exact false all-clear this
    whole mechanism exists to remove, and never as disagreement, which would
    send an operator to restart production over a systemctl that timed out.
    """

    verdict = running.get("verdict")
    if verdict == RUNNING_RELEASE_UNKNOWN:
        return None
    if verdict == RUNNING_RELEASE_DIVERGENT:
        return False
    if current_release_id is None:
        return None
    return running.get("release_id") == current_release_id


BOOTABILITY_UNKNOWN = "unknown"


def unknown_bootability(release_id: str, error: BaseException) -> Dict[str, object]:
    """The verdict when the checker itself failed. Distinct from "not bootable".

    `bootable` is None, never False. A checker that could not run is not
    evidence that the release is bad, and the two must never collapse into one
    falsy value: the whole asymmetry below depends on a caller being able to
    tell "I know this is broken" from "I could not find out".
    """

    return {
        "bootable": None,
        "verdict": BOOTABILITY_UNKNOWN,
        "release_id": release_id,
        "assessment_error": f"{type(error).__name__}: {error}",
        "defects": [],
    }


def assess_release_bootability(
    *,
    release_root: Path,
    release_id: str,
) -> Dict[str, object]:
    """Would the launcher be able to start this release? Read-only.

    THE VERDICT IS A FUNCTION OF THE FILESYSTEM, because the launcher is.
    `ops/systemd/proposed/log-job-runner.release.sh` resolves `BASE_DIR` and then
    asserts exactly two things before it execs anything:

        [[ -x "${BASE_DIR}/.venv/bin/python" ]]
        [[ -e "${BASE_DIR}/.env" ]]

    It never opens `meta/<id>.json`. This function models those two predicates
    and nothing else.

    WHY THAT IS SPELLED OUT SO EMPHATICALLY. The first version of this check read
    the METADATA — it required each runtime link to be *declared* in
    `meta/<id>.json`, and reported `not_declared` as a defect. An independent
    review on 2026-08-20 built the obvious counterexample: a release whose `.env`
    and `.venv` are present and working, but undeclared. Both wrapper
    preconditions true; verdict False. `rollback` then refused a fallback the
    launcher would have started. The check was answering a different question
    from the one its name asks, and on the emergency path that blocked recovery.

    Metadata state is still reported, as NOTES that never affect `bootable`:
    whether the file is missing, unreadable, or simply does not declare a name.
    Those matter for provenance and for `verify_release`, which legitimately
    cares what was declared — they say nothing about whether the thing will run.

    `-e` and `-x` follow symlinks, so a `.env` that is a regular file or a
    `.venv` that is a real directory passes here exactly as it does for the
    wrapper. Releases imported or hand-built by other tooling are precisely the
    population most likely to be a rollback target once things have gone wrong,
    and the old symlink requirement refused them.

    UNKNOWN is reserved for genuinely not finding out — the filesystem refusing
    to answer. Metadata problems never produce UNKNOWN, because the launcher does
    not consult metadata, so being unable to read it does not prevent a definite
    answer about whether the release can start.

    Known limitation, unchanged and documented in docs/07_operations.md:
    `os.access(..., X_OK)` answers for the CALLING process, not the service user.
    They are the same identity on this host. Run release commands as that user.
    """

    layout = _validated_layout(release_root)
    tree = layout.release_path(release_id)

    if not tree.is_dir():
        return {"bootable": False, "release_id": release_id,
                "required_runtime_links": list(DEFAULT_RUNTIME_LINK_NAMES),
                "declared_runtime_links": [], "notes": [],
                "defects": [{"name": None, "reason": "release_not_found", "path": str(tree)}]}

    # -- the launcher's own preconditions, in its own order --------------------
    interpreter = tree / RUNTIME_LINK_VENV / "bin" / "python"
    env_file = tree / RUNTIME_LINK_ENV
    defects: list[Dict[str, object]] = []
    try:
        if not os.access(interpreter, os.X_OK):
            defects.append({
                "name": f"{RUNTIME_LINK_VENV}/bin/python", "reason": "interpreter_not_executable",
                "path": str(interpreter),
                "detail": "the wrapper execs this path and would exit 3 with "
                          "RELEASE_RUNTIME_RESOURCE_MISSING",
            })
        if not env_file.exists():
            defects.append({
                "name": RUNTIME_LINK_ENV, "reason": "env_missing", "path": str(env_file),
                "detail": "the wrapper requires this path to exist and would exit 3 with "
                          "RELEASE_RUNTIME_RESOURCE_MISSING",
            })
    except OSError as exc:
        # The filesystem would not answer. That is not evidence of a bad release.
        return unknown_bootability(release_id, exc)

    # -- metadata: reported, never load-bearing -------------------------------
    notes: list[Dict[str, object]] = []
    declared: Dict[str, object] = {}
    meta_path = layout.meta_path(release_id)
    if not meta_path.is_file():
        notes.append({"reason": "metadata_missing", "path": str(meta_path),
                      "detail": "provenance is absent; this says nothing about whether the "
                                "release can start, and is not the same as a release having "
                                "been prepared without runtime links"})
    else:
        try:
            declared = json.loads(meta_path.read_text()).get("runtime_links") or {}
        except (OSError, json.JSONDecodeError) as exc:
            notes.append({"reason": "metadata_unreadable", "path": str(meta_path),
                          "error": f"{type(exc).__name__}: {exc}"})
        else:
            for name in DEFAULT_RUNTIME_LINK_NAMES:
                if name not in declared:
                    notes.append({"name": name, "reason": "not_declared",
                                  "detail": "metadata declares no runtime link under this "
                                            "name; the path itself is what decides"})

    return {
        "bootable": not defects,
        "release_id": release_id,
        "required_runtime_links": list(DEFAULT_RUNTIME_LINK_NAMES),
        "declared_runtime_links": sorted(declared),
        "defects": defects,
        "notes": notes,
    }


#: Where the fault lies, when a release is definitely not bootable.
FAULT_SHARED = "SHARED_RUNTIME_RESOURCE_FAULT"
FAULT_RELEASE_SPECIFIC = "RELEASE_SPECIFIC_FAULT"
FAULT_INDETERMINATE = "INDETERMINATE"
FAULT_NOT_APPLICABLE = "NOT_APPLICABLE"


def diagnose_runtime_fault(
    *,
    release_root: Path,
    release_id: str,
    compare_with: Optional[str],
) -> Dict[str, object]:
    """Is this release broken, or is what every release SHARES broken?

    Every release on this host declares the same two runtime-link targets, both
    inside the mutable development tree — deliberately, per docs/07_operations.md,
    so a code rollback does not roll back dependencies. One interrupted
    `pip install` therefore makes EVERY release fail the launcher's preconditions
    at the same instant, and a verdict alone cannot tell that apart from a single
    broken release.

    The comparison is on RESOLVED PATHS, not on matching defect reasons: two
    releases independently broken the same way would match on reason, and a wrong
    "shared" conclusion sends an operator to repair something healthy during an
    incident.

    FOUR OUTCOMES, and the distinction between the last two is load-bearing:

      SHARED            both releases fail on the same resolved path
      RELEASE_SPECIFIC  this release fails, the comparison release does NOT —
                        positive evidence that the fault is this release's own
      INDETERMINATE     could not establish either: nothing to compare against,
                        or the comparison release's own state is unknown
      NOT_APPLICABLE    this release is not definitely unbootable

    INDETERMINATE IS NOT RELEASE_SPECIFIC. Callers that act on a fault being
    release-specific — `rollback` refuses on it — must require the positive
    conclusion, because absence of a shared verdict is not evidence of a
    release-specific one. Collapsing the two would resurrect the original defect:
    refusing recovery on a release that is fine.
    """

    layout = _validated_layout(release_root)

    def failing_paths(rid: str) -> Optional[Dict[str, str]]:
        """Resolved target per failing link, or None when the state is unknown."""
        report = assess_release_bootability(release_root=release_root, release_id=rid)
        if report.get("bootable") is None:
            return None
        if report.get("bootable") is True:
            return {}
        tree = layout.release_path(rid)
        out: Dict[str, str] = {}
        for defect in report.get("defects") or []:
            name = RUNTIME_LINK_VENV if "venv" in str(defect.get("name") or "") else RUNTIME_LINK_ENV
            try:
                out[str(defect.get("reason"))] = os.path.realpath(tree / name)
            except OSError:
                continue
        return out

    mine = failing_paths(release_id)
    if not mine:
        return {"conclusion": FAULT_NOT_APPLICABLE, "release_id": release_id}

    if not compare_with or compare_with == release_id:
        return {"conclusion": FAULT_INDETERMINATE, "release_id": release_id,
                "detail": "no distinct release to compare against, so it cannot be "
                          "established whether the fault is shared"}

    theirs = failing_paths(compare_with)
    if theirs is None:
        return {"conclusion": FAULT_INDETERMINATE, "release_id": release_id,
                "compared_with": compare_with,
                "detail": "the comparison release's own bootability is unknown, so it "
                          "cannot be established whether the fault is shared"}

    shared = {reason: path for reason, path in mine.items() if theirs.get(reason) == path}
    if shared:
        return {
            "conclusion": FAULT_SHARED, "release_id": release_id,
            "compared_with": compare_with, "shared_paths": sorted(set(shared.values())),
            "detail": ("both releases fail on the same resolved path, so the fault is in the "
                       "resource they share, not in either release"),
            "next": ("repair the shared path above — a venv rebuild, an interrupted "
                     "`pip install` and a recreated `.env` are the usual causes. No pointer "
                     "move will help: `repair-previous` and choosing a different release "
                     "both land on the same resource."),
        }
    return {
        "conclusion": FAULT_RELEASE_SPECIFIC, "release_id": release_id,
        "compared_with": compare_with,
        "failing_paths": sorted(set(mine.values())),
        "detail": ("this release fails on paths the currently serving release does not, so "
                   "the fault is its own"),
    }


def diagnose_shared_runtime_fault(
    *,
    release_root: Path,
    release_id: str,
    compare_with: Optional[str],
) -> Optional[Dict[str, object]]:
    """The SHARED conclusion only, or None. Kept for callers that want just that."""

    report = diagnose_runtime_fault(release_root=release_root, release_id=release_id,
                                    compare_with=compare_with)
    return report if report["conclusion"] == FAULT_SHARED else None


def verify_release(
    *,
    release_root: Path,
    release_id: str,
    source_repo: Path,
    runner: Runner = subprocess.run,
) -> Dict[str, object]:
    """Recompute, from the actual bytes, that a release is exactly its commit.

    Never trusts stored state: metadata supplies the claim, `git ls-tree` the
    expectation, and the on-disk content the evidence. Raises on any divergence,
    including a file the commit does not contain.
    """
    layout = _validated_layout(release_root)
    source_repo = Path(source_repo).expanduser().resolve()
    if not _RELEASE_ID_RE.match(release_id or ""):
        raise ReleaseBoundaryError("RELEASE_METADATA_INVALID", {"release_id": release_id, "reason": "malformed_id"})

    tree = layout.release_path(release_id)
    meta_path = layout.meta_path(release_id)
    if not tree.is_dir():
        raise ReleaseBoundaryError("RELEASE_NOT_FOUND", {"release_id": release_id, "path": str(tree)})
    if not meta_path.is_file():
        raise ReleaseBoundaryError("RELEASE_METADATA_INVALID", {"release_id": release_id, "reason": "missing_metadata"})
    try:
        metadata = json.loads(meta_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseBoundaryError("RELEASE_METADATA_INVALID", {
            "release_id": release_id, "reason": "unreadable_metadata", "error": str(exc),
        }) from exc

    commit = metadata.get("commit")
    if not isinstance(commit, str) or not _FULL_SHA_RE.match(commit):
        raise ReleaseBoundaryError("RELEASE_METADATA_INVALID", {"release_id": release_id, "commit": commit})
    if release_id_for(commit) != release_id:
        raise ReleaseBoundaryError("RELEASE_METADATA_INVALID", {
            "release_id": release_id, "commit": commit, "reason": "id_does_not_address_commit",
        })

    # The metadata's own claim about WHICH release it describes.
    #
    # WHY THIS IS NOT REDUNDANT WITH THE TWO CHECKS ABOVE.
    #     Those bind the requested id to the commit. They say nothing about the
    #     `release_id` field `prepare_release` writes, so independent review
    #     rewrote it to `000000000000` in an otherwise genuine release and
    #     verification still passed — leaving a verified release whose own
    #     provenance names a different one. Anything downstream that reads
    #     identity from metadata rather than from the directory (an audit, a
    #     ledger entry, an operator reading `meta/*.json`) would then be reading
    #     a different release than the one that was actually verified, which is
    #     exactly the authoritative-materialization identity G6 relies on.
    #
    #     `release_id` is mandatory: `prepare_release` always writes it, and the
    #     directory id is `release_id_for(commit)` by construction, so one
    #     equality binds all three representations — directory, commit-derived
    #     id, and the metadata's own claim. Optional provenance is deliberately
    #     left alone; only the identity field is load-bearing here.
    declared_release_id = metadata.get("release_id")
    if declared_release_id != release_id:
        raise ReleaseBoundaryError("RELEASE_METADATA_INVALID", {
            "release_id": release_id, "declared_release_id": declared_release_id,
            "commit": commit, "reason": "metadata_release_id_mismatch",
        })

    entries = _manifest_entries(metadata, release_id)
    resolved, git_available = _cross_check_against_git(
        source_repo, commit, entries, release_id, runner=runner,
    )
    declared_links = metadata.get("runtime_links") or {}
    observed = _scan_tree(tree, set(declared_links))
    mismatches = _compare(entries, observed)
    if mismatches:
        raise ReleaseBoundaryError("RELEASE_CONTENT_MISMATCH", {
            "release_id": release_id, "commit": resolved,
            "mismatch_count": len(mismatches), "mismatches": mismatches[:20],
        })

    for name, target in declared_links.items():
        link = tree / name
        if not link.is_symlink():
            raise ReleaseBoundaryError("RELEASE_RUNTIME_LINK_MISMATCH", {
                "release_id": release_id, "name": name, "reason": "not_a_symlink",
            })
        actual = _readlink_or_absent(link)
        if actual is LINK_ABSENT:
            # Same finding as the `not_a_symlink` branch above; the link simply
            # went away between the two checks. Reported, never tolerated.
            raise ReleaseBoundaryError("RELEASE_RUNTIME_LINK_MISMATCH", {
                "release_id": release_id, "name": name, "reason": "not_a_symlink",
                "observed_as": "removed_during_verification",
            })
        if actual != target:
            raise ReleaseBoundaryError("RELEASE_RUNTIME_LINK_MISMATCH", {
                "release_id": release_id, "name": name, "declared": target, "observed": actual,
            })
        if not Path(actual).exists():
            raise ReleaseBoundaryError("RELEASE_RUNTIME_RESOURCE_MISSING", {
                "release_id": release_id, "name": name, "target": actual, "reason": "dangling_link",
            })

    digest = source_tree_digest(entries)
    if metadata.get("source_tree_digest") != digest:
        raise ReleaseBoundaryError("RELEASE_METADATA_INVALID", {
            "release_id": release_id, "declared_digest": metadata.get("source_tree_digest"),
            "recomputed_digest": digest,
        })
    # `verified` has always meant "these are the commit's bytes", and widening
    # it would break every caller that prepares a synthetic link-less release in
    # a temporary root. Instead the answer to the *other* question travels
    # alongside it, so nobody can read a passing verification as proof the
    # release can start: when it cannot, this says so and names why.
    # A failing checker must not break verification. `verify_release` is called
    # by activate, by rollback and by repair-previous, so letting an exception
    # out of here would take down the entire recovery path along with the thing
    # it was only ever meant to annotate.
    try:
        bootability = assess_release_bootability(release_root=release_root, release_id=release_id)
    except Exception as exc:  # noqa: BLE001 - deliberately broad; see unknown_bootability
        bootability = unknown_bootability(release_id, exc)
    return {
        "release_id": release_id,
        "commit": resolved,
        "release_path": str(tree),
        "source_tree_digest": digest,
        "file_count": len(entries),
        "runtime_links": dict(declared_links),
        "verified_against_source_repository": git_available,
        "bootable": bootability["bootable"],
        "bootability": bootability,
        "metadata": metadata,
    }


# --------------------------------------------------------------------------
# Activation and rollback
# --------------------------------------------------------------------------

def _pointer_target(pointer: Path) -> Optional[str]:
    if not pointer.is_symlink():
        if pointer.exists():
            raise ReleaseBoundaryError("RELEASE_POINTER_INVALID", {
                "pointer": str(pointer), "reason": "exists_but_is_not_a_symlink",
            })
        return None
    target = _readlink_or_absent(pointer)
    # The pointer was unlinked between the two syscalls. "Unset" is the same
    # definite answer the branch above returns for a pointer that is not a
    # symlink, so the race converges on the state the caller already handles.
    return None if target is LINK_ABSENT else target


def pointer_release_id(pointer: Path) -> Optional[str]:
    """The release id a pointer resolves to, or None when it is unset."""
    target = _pointer_target(Path(pointer))
    if target is None:
        return None
    name = Path(target).name
    if not _RELEASE_ID_RE.match(name):
        raise ReleaseBoundaryError("RELEASE_POINTER_INVALID", {
            "pointer": str(pointer), "target": target, "reason": "target_is_not_a_release",
        })
    return name


def _swap_pointer(pointer: Path, relative_target: str) -> None:
    """Repoint a symlink atomically.

    `ln -sfn` unlinks before it links, so a reader can see the pointer missing.
    Creating a uniquely named symlink and renaming it over the pointer is a
    single `rename(2)`: readers observe either the old release or the new one.
    """
    staging = pointer.parent / f".{pointer.name}.swap-{os.getpid()}-{secrets.token_hex(4)}"
    staging.symlink_to(relative_target)
    try:
        os.rename(staging, pointer)
    except BaseException:
        if staging.is_symlink():
            staging.unlink()
        raise


def _append_activation(layout: ReleaseLayout, record: Dict[str, object]) -> None:
    with layout.activation_log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


@contextmanager
def _null_fence():
    """No client-business requirement, so there is no fleet to hold still."""
    yield None


def _fleet_fence(fingerprint: str, declared_capabilities: frozenset = frozenset()):
    """The real fence. Separated so a test can substitute one deterministically.

    `declared_capabilities` is what the release being activated declares. The
    fence re-reads every client's narrowing schema state against it under the
    transition lock, so the capability decision that authorizes the swap is
    taken inside the protected interval rather than before it.
    """
    from ops.release_schema_preflight import activation_fence
    return activation_fence(
        expected_fingerprint=fingerprint,
        declared_capabilities=declared_capabilities,
    )


def activate_release(
    *,
    release_root: Path,
    release_id: str,
    source_repo: Path,
    runner: Runner = subprocess.run,
    now: Optional[datetime] = None,
    schema_preflight: Optional[Callable[..., object]] = None,
) -> Dict[str, object]:
    """Point `current` at a verified release, preserving the outgoing one.

    Verification runs first and a failure aborts before any pointer moves, so an
    unverifiable release can never become current. The previously active release
    is recorded in `previous` and its directory is left untouched, which is what
    makes rollback a pointer move rather than a rebuild.

    Since M4 a release must additionally prove its **schema prerequisites** —
    that the migrations its code depends on are recorded applied and physically
    present, in the platform database and in every affected client business
    database. Verified bytes say the release is what it claims to be; they say
    nothing about whether the databases it will talk to can run it.
    `schema_preflight` is injectable so the suites can exercise the gate without
    a live fleet; production passes `None` and gets the real one.
    """
    layout = _validated_layout(release_root)
    with management_lock(layout.root):
        return _activate_release_locked(layout=layout, release_id=release_id,
                                        source_repo=source_repo, runner=runner, now=now,
                                        schema_preflight=schema_preflight)


def _activate_release_locked(
    *,
    layout: "ReleaseLayout",
    release_id: str,
    source_repo: Path,
    runner: Runner,
    now: Optional[datetime],
    schema_preflight: Optional[Callable[..., object]] = None,
    tolerate_missing_runtime_resource: bool = False,
) -> Dict[str, object]:
    """Body of `activate_release`. The caller must hold the management lock.

    Verification runs inside the critical section on purpose: a release verified
    before the lock could be removed or rebuilt by another operator before the
    pointer swap, which is exactly the stale-observation class the lock exists
    to close. The schema prerequisite gate runs in the same critical section for
    the same reason, and is re-checked immediately before the swap.
    """
    # `tolerate_missing_runtime_resource` exists for ONE caller: `rollback`.
    #
    # Verification refuses a release whose runtime links dangle. That is right
    # for a promotion and wrong for a recovery, because those links point at the
    # `.env` and `.venv` that EVERY release shares — so a venv rebuild makes
    # verification refuse every release at once, and every command that could
    # move a pointer stops working. There is no path out of that state: rollback,
    # activate and repair-previous all verify first. The "a shared fault lets
    # rollback proceed" behaviour built earlier was unreachable for exactly the
    # most common shape of the shared fault.
    #
    # Deliberately NARROW. Only RELEASE_RUNTIME_RESOURCE_MISSING passes — the
    # environment is absent. RELEASE_RUNTIME_LINK_MISMATCH still refuses: a link
    # pointing somewhere other than its declared target is the release's own
    # structure being wrong, not the environment's. Content and commit mismatch
    # still refuse absolutely; "the shared environment is missing" and "this
    # release's bytes are wrong" are different facts and get opposite treatment.
    tolerated: Optional[Dict[str, object]] = None
    try:
        report = verify_release(release_root=layout.root, release_id=release_id,
                                source_repo=source_repo, runner=runner)
    except ReleaseBoundaryError as exc:
        if not (tolerate_missing_runtime_resource
                and exc.classification == "RELEASE_RUNTIME_RESOURCE_MISSING"):
            raise
        tolerated = {"classification": exc.classification, "details": exc.details}
        # The commit is still needed downstream, and it is metadata rather than
        # a claim about the runtime resources.
        meta = json.loads(layout.meta_path(release_id).read_text())
        report = {"commit": meta["commit"], "bootable": False, "bootability": {}}
    outgoing = pointer_release_id(layout.current)
    if outgoing == release_id:
        return {"release_id": release_id, "commit": report["commit"],
                "changed": False, "previous_release_id": pointer_release_id(layout.previous)}

    # Schema prerequisites of the RELEASE BEING ACTIVATED, read from its own
    # tree. A release that predates the mechanism declares nothing and passes,
    # which keeps rollback to any historical release possible.
    preflight = schema_preflight
    if preflight is None:
        from ops.release_schema_preflight import verify_schema_prerequisites
        preflight = verify_schema_prerequisites
    schema_report = preflight(
        release_tree=layout.release_path(release_id),
        release_id=release_id,
    )
    fingerprint = getattr(schema_report, "fleet_fingerprint", None)
    declared_capabilities = frozenset(
        getattr(schema_report, "declared_capabilities", None) or ()
    )

    # The pointer swap happens INSIDE the fleet fence, not after it.
    #
    # `activation_fence` opens a platform transaction, takes a SHARE lock on the
    # authoritative fleet table, re-enumerates under that lock and confirms the
    # fleet is still exactly the one the prerequisites were verified against —
    # and then holds the lock while the body below runs. Any session attempting
    # to enable, disable, add or re-point a client must take ROW EXCLUSIVE on
    # that table and therefore blocks until this transaction ends, which is after
    # the pointer has moved. That includes raw SQL, which is the case an advisory
    # lock could not cover and the previous implementation got wrong.
    #
    # The same fence additionally holds SCHEMA_TRANSITION_LOCK_KEY and re-reads
    # every client's narrowing schema state under it, so the capability decision
    # that authorizes this swap cannot have gone stale: a CONTRACT closure is
    # either already committed and observed here (and a legacy release is
    # refused before the pointer moves), or blocked on that key until after it.
    #
    # There is deliberately no unlock-and-reconnect step between the final check
    # and the swap: closing that gap is the entire point.
    fence = (_fleet_fence(fingerprint, declared_capabilities)
             if fingerprint else _null_fence())
    with fence:
        if outgoing is not None:
            _swap_pointer(layout.previous, f"{RELEASES_DIRNAME}/{outgoing}")
        _swap_pointer(layout.current, f"{RELEASES_DIRNAME}/{release_id}")

    record = {
        "action": "activate",
        "at": (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(),
        "release_id": release_id,
        "commit": report["commit"],
        "previous_release_id": outgoing,
        "by": f"{_username()}@{os.uname().nodename}",
        "schema_preflight": (
            schema_report.as_dict() if hasattr(schema_report, "as_dict")
            else None
        ),
    }
    _append_activation(layout, record)
    return {"release_id": release_id, "commit": report["commit"],
            "changed": True, "previous_release_id": outgoing,
            "tolerated_verification_failure": tolerated,
            "schema_preflight": (
                schema_report.as_dict() if hasattr(schema_report, "as_dict")
                else None
            )}


def rollback_release(
    *,
    release_root: Path,
    source_repo: Path,
    runner: Runner = subprocess.run,
    now: Optional[datetime] = None,
    schema_preflight: Optional[Callable[..., object]] = None,
    tolerate_missing_runtime_resource: bool = False,
) -> Dict[str, object]:
    """Return `current` to whatever `previous` records, verifying it first.

    Rollback is ordinary activation of the recorded predecessor, so the release
    being rolled back from becomes the new `previous` and a rollback is itself
    reversible.
    """
    layout = _validated_layout(release_root)
    with management_lock(layout.root):
        return _rollback_release_locked(
            layout=layout, source_repo=source_repo, runner=runner, now=now,
            schema_preflight=schema_preflight,
            tolerate_missing_runtime_resource=tolerate_missing_runtime_resource)


def _rollback_release_locked(
    *,
    layout: "ReleaseLayout",
    source_repo: Path,
    runner: Runner,
    now: Optional[datetime],
    schema_preflight: Optional[Callable[..., object]] = None,
    tolerate_missing_runtime_resource: bool = False,
) -> Dict[str, object]:
    """Body of `rollback_release`. The caller must hold the management lock."""
    target = pointer_release_id(layout.previous)
    if target is None:
        raise ReleaseBoundaryError("RELEASE_POINTER_INVALID", {
            "pointer": str(layout.previous), "reason": "no_previous_release_recorded",
        })
    # Both pointers naming the same release means there is nothing to roll back
    # to — the state an activation interrupted between its two swaps leaves
    # behind. Reporting a successful rollback there would journal a recovery
    # that did not happen, during an incident.
    if pointer_release_id(layout.current) == target:
        raise ReleaseBoundaryError("RELEASE_POINTER_INVALID", {
            "pointer": str(layout.previous), "release_id": target,
            "reason": "previous_and_current_name_the_same_release",
        })
    result = _activate_release_locked(
        layout=layout, release_id=target, source_repo=source_repo, runner=runner, now=now,
        schema_preflight=schema_preflight,
        tolerate_missing_runtime_resource=tolerate_missing_runtime_resource)
    if result.get("tolerated_verification_failure"):
        _append_activation(layout, {
            "action": "rollback_tolerated_missing_runtime_resource",
            "at": (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(),
            "release_id": target,
            "tolerated": result["tolerated_verification_failure"],
            "by": f"{_username()}@{os.uname().nodename}",
        })
    _append_activation(layout, {
        "action": "rollback",
        "at": (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(),
        "release_id": target,
        "commit": result["commit"],
        "by": f"{_username()}@{os.uname().nodename}",
    })
    return result


# --------------------------------------------------------------------------
# Which wrapper is installed
# --------------------------------------------------------------------------

DEV_WRAPPER_RELATIVE = "ops/systemd/proposed/log-job-runner.sh"
RELEASE_WRAPPER_RELATIVE = "ops/systemd/proposed/log-job-runner.release.sh"
INSTALLED_WRAPPER_PATH = Path("/usr/local/bin/log-job-runner.sh")

WRAPPER_ABSENT = "absent"
WRAPPER_DEVELOPMENT = "development_tree"
WRAPPER_DEVELOPMENT_HISTORICAL = "development_historical"
WRAPPER_RELEASE = "release"
WRAPPER_RELEASE_HISTORICAL = "release_historical"
WRAPPER_UNRECOGNIZED = "unrecognized"
WRAPPER_UNREADABLE = "unreadable"

# The only two states routine identity provisioning may overwrite. Everything
# else is either a live boundary or an unknown, and both must stop the operator.
WRAPPER_REPLACEABLE_BY_PROVISIONING = frozenset({WRAPPER_ABSENT, WRAPPER_DEVELOPMENT})

# Structural signature of *any* release wrapper, current or historical: it
# derives BASE_DIR from a release root pointer rather than naming a source tree.
_RELEASE_WRAPPER_ROOT_RE = re.compile(r'^RELEASE_ROOT="([^"]+)"', re.MULTILINE)
_RELEASE_WRAPPER_BASE_RE = re.compile(r'^BASE_DIR="\$\{RELEASE_ROOT\}/current"', re.MULTILINE)

# Structural signature of a development wrapper: BASE_DIR is a literal source
# path and there is no release root at all. Needed for the same reason as
# `release_historical` — any commit that edits the development wrapper changes
# its bytes, so the wrapper installed on the host stops matching the repository
# copy. Without this, a routine change (adding the execution barrier, say) makes
# the live wrapper "unrecognized", which would block the cutover's own
# pre-replacement check and every future provisioning run.
_DEV_WRAPPER_BASE_RE = re.compile(r'^BASE_DIR="(/[^"$]+)"', re.MULTILINE)


def _sha256_file(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def installed_wrapper_variant(
    *, repo_root: Path, installed_wrapper: Path = INSTALLED_WRAPPER_PATH,
) -> str:
    """Classify the installed job wrapper.

    Returns one of `absent`, `development_tree`, `release`,
    `release_historical`, `unrecognized`, `unreadable`.

    Identity is decided by SHA-256 against the two wrappers this repository
    maintains, never by parsing `BASE_DIR` — the release wrapper's assignment
    references a shell variable, so textual comparison reads back
    `${RELEASE_ROOT}/current` and would misreport a completed cutover as a
    failed one.

    `release_historical` exists because hash equality is too brittle to carry
    the whole safety decision. Any later commit that touches the release wrapper
    changes its bytes, so a perfectly valid *older* release wrapper installed on
    the host would otherwise classify as `unrecognized` — and an operator told
    "unrecognized" is an operator who reinstalls the development wrapper and
    silently destroys a live boundary. A wrapper that structurally derives
    `BASE_DIR` from a release-root pointer is therefore reported as a release
    wrapper that is merely out of date, which is a materially different fact.

    Provisioning and readiness both depend on this: they hold the development
    wrapper as the expected content of `/usr/local/bin/log-job-runner.sh`.
    """
    repo_root = Path(repo_root)
    if not installed_wrapper.is_file():
        return WRAPPER_ABSENT
    digest = _sha256_file(installed_wrapper)
    if digest is None:
        return WRAPPER_UNREADABLE
    if digest == _sha256_file(repo_root / RELEASE_WRAPPER_RELATIVE):
        return WRAPPER_RELEASE
    if digest == _sha256_file(repo_root / DEV_WRAPPER_RELATIVE):
        return WRAPPER_DEVELOPMENT
    try:
        text = installed_wrapper.read_text()
    except (OSError, UnicodeError):
        return WRAPPER_UNREADABLE
    if _RELEASE_WRAPPER_ROOT_RE.search(text) and _RELEASE_WRAPPER_BASE_RE.search(text):
        return WRAPPER_RELEASE_HISTORICAL
    if not _RELEASE_WRAPPER_ROOT_RE.search(text) and _DEV_WRAPPER_BASE_RE.search(text):
        return WRAPPER_DEVELOPMENT_HISTORICAL
    return WRAPPER_UNRECOGNIZED


BOOTSTRAP_CAPABILITY_MARKERS = (
    "flock --shared",            # participates in the execution-quiescence barrier
    "CUTOVER_FENCE",             # participates in the durable fail-closed fence
    "WRAPPER_VARIANT",           # supports the stale-wrapper re-exec comparison
)


def wrapper_has_bootstrap_capabilities(wrapper: Path) -> Dict[str, object]:
    """Whether a wrapper actually implements the pre-cutover contracts.

    Byte-equality with a repository file proves only that two files match; it
    says nothing about capability, and it is defeatable by checking out an older
    copy of that file. These markers assert the properties the cutover depends
    on, so the gate proves the wrapper can be gated rather than that it looks
    familiar.
    """
    try:
        text = Path(wrapper).read_text(errors="replace")
    except OSError as exc:
        return {"capable": False, "reason": f"unreadable: {exc}", "missing": list(BOOTSTRAP_CAPABILITY_MARKERS)}
    missing = [marker for marker in BOOTSTRAP_CAPABILITY_MARKERS if marker not in text]
    return {"capable": not missing, "missing": missing}


def wrapper_replaceability(
    *, repo_root: Path, installed_wrapper: Path = INSTALLED_WRAPPER_PATH,
) -> Dict[str, object]:
    """Whether routine identity provisioning may overwrite the installed wrapper.

    Fail-closed by construction: only an absent wrapper or one that is
    conclusively the development wrapper may be replaced. A live release
    wrapper, an older release wrapper, an unrecognized script and an unreadable
    file are all refused, because each of them means the operator's mental model
    and the host disagree — and the failure mode of guessing is a production
    boundary that disappears without anyone noticing.

    Returns a decision rather than raising, so the caller can raise in its own
    error vocabulary.
    """
    variant = installed_wrapper_variant(repo_root=repo_root, installed_wrapper=installed_wrapper)
    if variant in WRAPPER_REPLACEABLE_BY_PROVISIONING:
        return {"variant": variant, "replaceable": True, "classification": None, "reason": None}
    classification = {
        WRAPPER_RELEASE: "RELEASE_BOUNDARY_ACTIVE",
        WRAPPER_RELEASE_HISTORICAL: "RELEASE_BOUNDARY_ACTIVE",
        WRAPPER_DEVELOPMENT_HISTORICAL: "INSTALLED_WRAPPER_UNRECOGNIZED",
        WRAPPER_UNRECOGNIZED: "INSTALLED_WRAPPER_UNRECOGNIZED",
        WRAPPER_UNREADABLE: "INSTALLED_WRAPPER_INDETERMINATE",
    }[variant]
    reason = {
        WRAPPER_DEVELOPMENT_HISTORICAL:
            "installed job wrapper is a development wrapper from an earlier commit; it is not "
            "conclusively the expected one, so it is not overwritten without an explicit decision",
        WRAPPER_RELEASE:
            "installed job wrapper is the release wrapper; provisioning would revert the cutover",
        WRAPPER_RELEASE_HISTORICAL:
            "installed job wrapper is a release wrapper from an earlier commit; provisioning "
            "would revert the cutover. Promote a release instead of re-provisioning.",
        WRAPPER_UNRECOGNIZED:
            "installed job wrapper matches neither maintained wrapper; it may be a boundary "
            "variant or a local modification, and overwriting it could remove a live boundary",
        WRAPPER_UNREADABLE:
            "installed job wrapper could not be read, so its identity is indeterminate",
    }[variant]
    return {"variant": variant, "replaceable": False,
            "classification": classification, "reason": reason}


def expected_wrapper_source_relative(
    *, repo_root: Path, installed_wrapper: Path = INSTALLED_WRAPPER_PATH,
) -> str:
    """The repo wrapper that installed content should be compared against.

    Once a release wrapper is live — current or historical — the development
    wrapper is no longer the correct expectation, and reporting it as a mismatch
    would drive an operator straight into re-provisioning the boundary away. A
    historical release wrapper still compares unequal against the current one,
    which is the truthful result: the boundary is present but out of date, and
    the fix is to promote a release, not to reinstall the development wrapper.
    """
    if installed_wrapper_variant(repo_root=repo_root, installed_wrapper=installed_wrapper) in (
        WRAPPER_RELEASE, WRAPPER_RELEASE_HISTORICAL,
    ):
        return RELEASE_WRAPPER_RELATIVE
    return DEV_WRAPPER_RELATIVE


def processes_using_release(tree: Path) -> list:
    """PIDs whose working directory or mapped files live inside `tree`.

    Best-effort by nature — /proc entries for other users are unreadable and a
    process can start right after the scan — so it is a guard against the
    likely accident (deleting a release a long job is still running from), not
    a proof of absence. It is used only to refuse, never to permit.
    """
    resolved = str(tree.resolve())
    found = []
    proc = Path("/proc")
    if not proc.is_dir():
        return found
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cwd = os.readlink(str(entry / "cwd"))
        except OSError:
            cwd = ""
        if cwd == resolved or cwd.startswith(resolved + "/"):
            found.append(int(entry.name))
            continue
        try:
            maps = (entry / "maps").read_text()
        except OSError:
            continue
        if resolved + "/" in maps:
            found.append(int(entry.name))
    return sorted(found)


def repoint_previous(
    *,
    release_root: Path,
    release_id: str,
    source_repo: Path,
    runner: Runner = subprocess.run,
    now: Optional[datetime] = None,
) -> Dict[str, object]:
    """Aim `previous` at a different release. The supported way out of a poisoned fallback.

    WHY THIS EXISTS. Rollback is an activation, so it swaps the pointers: after
    fleeing a release that will not start, `previous` names the release you just
    fled. The fallback is now the thing you escaped from, and the state is stuck
    — `remove_release` refuses a pointer-referenced release, and
    `prepare_release` refuses to converge a release whose runtime links differ,
    so a correct rebuild of the same commit cannot replace it. Production sat in
    exactly that state for about forty minutes on 2026-08-20, and the only exit
    was activating a different good release, which is not a thing anyone wants to
    reason about mid-incident.

    WHAT IT DELIBERATELY WILL NOT DO. It never writes `current`. Whatever is
    serving production keeps serving production; this moves the *fallback* only,
    so the worst outcome of a mistake here is a rollback target that is not the
    one you meant, never a change to what is running. It refuses a target that
    equals `current`, because that is the state that makes rollback
    unperformable. It verifies the target against its commit first, and reports
    the target's bootability, so this cannot be used to quietly install a second
    unbootable fallback.
    """
    layout = _validated_layout(release_root)
    with management_lock(layout.root):
        if not _RELEASE_ID_RE.match(release_id or ""):
            raise ReleaseBoundaryError("RELEASE_METADATA_INVALID", {
                "release_id": release_id, "reason": "malformed_id"})
        if not layout.release_path(release_id).is_dir():
            raise ReleaseBoundaryError("RELEASE_NOT_FOUND", {
                "release_id": release_id, "path": str(layout.release_path(release_id))})

        current = pointer_release_id(layout.current)
        if current == release_id:
            raise ReleaseBoundaryError("RELEASE_POINTER_INVALID", {
                "release_id": release_id, "reason": "target_is_current",
                "detail": "previous may not name the running release; rollback would have "
                          "nothing to roll back to",
            })

        # Verified inside the lock, for the same reason activation is: a release
        # verified before the lock could be rebuilt or removed before use.
        report = verify_release(release_root=layout.root, release_id=release_id,
                                source_repo=source_repo, runner=runner)
        outgoing = pointer_release_id(layout.previous)
        _swap_pointer(layout.previous, f"{RELEASES_DIRNAME}/{release_id}")
        _append_activation(layout, {
            "action": "repoint_previous",
            "at": (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(),
            "release_id": release_id,
            "commit": report["commit"],
            "replaced_previous_release_id": outgoing,
            "current_release_id": current,
            "by": f"{_username()}@{os.uname().nodename}",
        })
        return {
            "release_id": release_id,
            "commit": report["commit"],
            "replaced_previous_release_id": outgoing,
            "current_release_id": current,
            "previous_release_id": release_id,
            "bootable": report["bootable"],
            "bootability": report["bootability"],
        }


def remove_release(*, release_root: Path, release_id: str) -> Dict[str, object]:
    """Delete a release that no pointer names.

    Sealed trees cannot be removed by a plain `rm -rf`, and an operator
    improvising `chmod -R u+w` on a release root is exactly the accident this
    boundary exists to prevent. Refusing to touch `current` or `previous` keeps
    the rollback target intact by construction rather than by operator care.

    Pointer checks alone are not enough. The wrapper resolves `current` once and
    runs from the resolved path, so a job started before two later promotions is
    still executing a release that no pointer names — for up to the unit's
    multi-hour timeout. Deleting it kills that run at its next import, which
    surfaces as an unexplained ImportError mid-window rather than as a boundary
    event, so processes are checked too.
    """
    layout = _validated_layout(release_root)
    with management_lock(layout.root):
        return _remove_release_locked(layout=layout, release_id=release_id)


def _remove_release_locked(*, layout: "ReleaseLayout", release_id: str) -> Dict[str, object]:
    """Body of `remove_release`. The caller must hold the management lock.

    The pointer check and the deletion must be one critical section: otherwise a
    concurrent activation can make this release current between the two, and the
    running release is deleted out from under production.
    """
    if not _RELEASE_ID_RE.match(release_id or ""):
        raise ReleaseBoundaryError("RELEASE_METADATA_INVALID", {"release_id": release_id, "reason": "malformed_id"})
    for pointer in (layout.current, layout.previous):
        if pointer_release_id(pointer) == release_id:
            raise ReleaseBoundaryError("RELEASE_POINTER_INVALID", {
                "release_id": release_id, "pointer": pointer.name,
                "reason": "release_is_referenced_by_a_pointer",
            })
    tree = layout.release_path(release_id)
    if not tree.is_dir():
        raise ReleaseBoundaryError("RELEASE_NOT_FOUND", {"release_id": release_id, "path": str(tree)})
    users = processes_using_release(tree)
    if users:
        raise ReleaseBoundaryError("RELEASE_IN_USE", {
            "release_id": release_id, "path": str(tree), "pids": users[:20],
            "reason": "a running process is executing from this release",
        })
    _remove_staging(tree)
    if tree.exists():
        raise ReleaseBoundaryError("RELEASE_INCOMPLETE_MATERIALIZATION", {
            "release_id": release_id, "reason": "removal_incomplete", "path": str(tree),
        })
    meta_path = layout.meta_path(release_id)
    if meta_path.is_file():
        meta_path.unlink()
    # PYTHONPYCACHEPREFIX mirrors the release's absolute path under state/, so
    # the cache outlives the release unless it is dropped here.
    cache = layout.root / "state" / "pycache" / str(tree).lstrip("/")
    if cache.is_dir():
        _remove_staging(cache)
    return {"release_id": release_id, "removed": True}


def list_releases(release_root: Path) -> list:
    layout = _validated_layout(release_root)
    if not layout.releases_dir.is_dir():
        return []
    found = []
    for entry in sorted(layout.releases_dir.iterdir()):
        if not entry.is_dir() or entry.name.startswith(_STAGING_PREFIX):
            continue
        meta_path = layout.meta_path(entry.name)
        metadata = {}
        if meta_path.is_file():
            try:
                metadata = json.loads(meta_path.read_text())
            except (OSError, json.JSONDecodeError):
                metadata = {"error": "unreadable_metadata"}
        found.append({"release_id": entry.name, "commit": metadata.get("commit"),
                      "created_at": metadata.get("created_at"),
                      "commit_subject": metadata.get("commit_subject")})
    return found


def release_status(release_root: Path) -> Dict[str, object]:
    """Pointer state only — never a verification result.

    `verify` answers whether a release is intact; this answers which release the
    pointers name. Keeping them separate stops a green status line from being
    mistaken for proof of content integrity.
    """
    layout = _validated_layout(release_root)
    return {
        "release_root": str(layout.root),
        "exists": layout.root.exists(),
        "current_release_id": pointer_release_id(layout.current) if layout.root.exists() else None,
        "previous_release_id": pointer_release_id(layout.previous) if layout.root.exists() else None,
        "releases": list_releases(layout.root),
    }
