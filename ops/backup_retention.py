#!/usr/bin/env python3
"""Deterministic retention for platform backup sets (P0-4).

`ops/backup.sh` creates verified backup sets but has never deleted one: it only
prints `WARNING: BACKUP_RETENTION_DAYS is not applied automatically`. In
production that left 236 GB across 141 files growing by ~7 GB per night against
118 GB of free space — a dated, predictable disk-full outage that would take down
PostgreSQL, MinIO, backup creation and every scheduled job at once.

This module enforces the retention policy that was already documented, and
nothing more. It does not touch backup creation, manifest generation or
restore selection.

**What counts as a VERIFIED set.** Not "three correctly named files". That
assumption was wrong and dangerous: `ops/backup.sh` publishes the final names at
lines 153-155 and only *then* runs `verify_pair`, so a process killed in that
window leaves a structurally perfect but never-verified triple. Corruption after
publication is invisible to a filename check entirely.

Validity is therefore decided by `ops/backup_manifest.verify_manifest()` — the
same function `ops/backup.sh verify` uses. There is exactly one definition of
"restorable": manifest contract, timestamp agreement, safe filenames, archive
presence, 0600 modes, recorded sizes and **sha256 checksums**. A second, weaker
definition living here is precisely the defect this module was found to have.

**Safety floor.** Retention keeps at least `keep_minimum` sets that pass that
contract *and* agree with the newest anchor on environment/database/platform
identity. Anchors are verified newest-first and re-verified immediately before
deletion. If fewer than `keep_minimum` verified anchors can be proven, retention
**fails closed and deletes nothing** — a full disk is recoverable, a missing last
good backup is not.

**Locking.** Retention takes the same `.backup.lock` (`flock`) that
`ops/backup.sh` takes, held across discover → verify → plan → delete. Without it
retention could verify a set that a concurrent backup is still publishing, or
count an in-flight set toward its floor. Restore is documented command-by-command
rather than scripted; `docs/09_disaster_recovery.md` records that a restore must
be wrapped in the same lock.

**WHERE THE WINDOW COMES FROM.** Not from here. This module used to own
`DEFAULT_RETENTION_DAYS = 14` and let `BACKUP_RETENTION_DAYS`, a `--retention-days`
flag or a unit override replace it, independently of
`ops/retention_registry.py` — which computes every store's enforcement lead from
a backup SHADOW derived from that same number. Two independent copies of one
policy is how the shadow silently stops bounding anything. The window is now
`PLATFORM_BACKUP_SET.retention_days` from the registry, and an operational
override is still accepted but VALIDATED: shorter is fine, longer fails closed
through `ops.retention_registry.validate_backup_retention_days()`.

**WHAT COUNTS AS A SET, AND HOW LONG EACH KIND LIVES.** Three recognised shapes,
one horizon:

  * `manifest_set` — the current three-file format. Verifiable, and the only
    kind that may serve as a survivor anchor.
  * `legacy_pair` — `postgres_<ts>.sql.gz` + `minio_<ts>.tar.gz` with no
    manifest: exactly what `ops/backup.sh` produced before manifests existed.
    Deterministically recognised, never an anchor, and expiring on the ordinary
    window. Before that rule it was `invalid_remnant_retained`, which in
    production meant 11 sets — the oldest from February 2026 — surviving
    indefinitely while the registry claimed 14 days, and the end-to-end
    hard-retention proof for everything in the backup set was therefore untrue.
  * `incomplete_remnant` — recognised platform members that form neither: a lone
    half, or a manifest whose companion archive is gone. Never an anchor, never
    restorable, and expiring on the same window.

`.partial`/`.failed` members keep their own, stricter treatment: they mark a
run's own in-flight or explicitly-failed state, and expiring one still needs
`--purge-invalid`.

Fail-closed on ambiguity is a property of DISCOVERY, not of age: `discover_sets`
recognises only the exact `postgres_<ts>.sql.gz` / `minio_<ts>.tar.gz` /
`backup_<ts>.manifest.json` grammar with a parseable timestamp. Client database
dumps, schema snapshots and every other file in `backups/` are invisible to this
module and can never be planned for deletion.

Dry-run is the default. Deletion requires an explicit `--execute`.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ops.backup_manifest import (  # noqa: E402
    ManifestVerificationError,
    verify_manifest,
)
from ops.retention_registry import (  # noqa: E402
    PLATFORM_BACKUP_SET,
    BackupRetentionPolicyConflict,
    validate_backup_retention_days,
)

DEFAULT_BACKUP_DIR = REPO_ROOT / "backups"

#: The central policy, READ rather than declared. Kept under the old name so
#: existing callers and tests keep working, but it is no longer an authority: it
#: resolves through `ops/retention_registry.py`, which is also where the backup
#: shadow every hard-retention enforcement lead depends on is computed from.
DEFAULT_RETENTION_DAYS = PLATFORM_BACKUP_SET.retention_days
DEFAULT_KEEP_MINIMUM = 3

#: The three shapes `discover_sets` can recognise. See the module docstring.
KIND_MANIFEST_SET = "manifest_set"
KIND_LEGACY_PAIR = "legacy_pair"
KIND_INCOMPLETE_REMNANT = "incomplete_remnant"

# The same advisory lock `ops/backup.sh` takes (`exec 9>"$LOCK_FILE"; flock -n 9`).
# Identical path + flock() on both sides is what makes the exclusion real.
LOCK_FILENAME = ".backup.lock"

# Identity fields an anchor must match. These are pinned to the CURRENT platform,
# never inferred from the backup directory.
COHERENCE_FIELDS = ("environment", "database", "platform_uuid")


@dataclass(frozen=True)
class AuthoritativeIdentity:
    """Who this platform actually is, right now.

    Deriving the reference identity from "whichever accepted backup is newest"
    was exploitable: drop coherent staging backups into the directory and they
    become the baseline, after which the real production backups read as foreign
    and expire. Trust has to come from the running platform, not from files an
    attacker or a mis-scripted copy can add.

    The source is the one the rest of the repository already treats as
    authoritative — `jobs.common.environment_identity.attest_platform_identity()`
    reading `ops_control.environment_identity`, which is also what
    `ops/backup.sh verify` queries for its identity pins. No second source of
    truth is introduced here.
    """

    environment: str
    database: str
    platform_uuid: str

    def as_expected(self) -> dict[str, str | None]:
        return {
            "environment": self.environment,
            "database": self.database,
            "platform_uuid": self.platform_uuid,
            # Deliberately NOT pinned. `repository_commit` records the commit the
            # backup was taken at, so pinning it to current HEAD would reject every
            # backup older than the last commit — `ops/backup.sh verify` only pins
            # it because it runs immediately after creation. It is provenance, not
            # identity.
            "repository_commit": None,
        }

    def as_dict(self) -> dict[str, str]:
        return {
            "environment": self.environment,
            "database": self.database,
            "platform_uuid": self.platform_uuid,
        }


def load_authoritative_identity(conn=None) -> AuthoritativeIdentity:
    """Attest the running platform. Raises rather than guessing."""
    from jobs.common.environment_identity import (
        attest_platform_identity,
        load_runtime_identity,
    )

    runtime = load_runtime_identity()
    owns_conn = conn is None
    if conn is None:
        from api.suspected_bug import platform_db_conn

        conn = platform_db_conn()
    try:
        attested = attest_platform_identity(conn, runtime)
    finally:
        if owns_conn:
            try:
                conn.close()
            except Exception:
                pass
    return AuthoritativeIdentity(
        environment=attested.environment,
        database=attested.database_name,
        platform_uuid=attested.database_identity_id,
    )

TIMESTAMP_RE = re.compile(r"^\d{8}_\d{6}$")

# Components of one backup set, keyed by role.
_MEMBER_PATTERNS = {
    "postgres": "postgres_{ts}.sql.gz",
    "minio": "minio_{ts}.tar.gz",
    "manifest": "backup_{ts}.manifest.json",
}
_SET_RE = re.compile(
    r"^(?:postgres_(?P<a>\d{8}_\d{6})\.sql\.gz"
    r"|minio_(?P<b>\d{8}_\d{6})\.tar\.gz"
    r"|backup_(?P<c>\d{8}_\d{6})\.manifest\.json)"
    r"(?P<suffix>\.partial|\.failed)?$"
)


@dataclass(frozen=True)
class BackupSet:
    timestamp: str
    created_at: datetime
    members: dict[str, Path]
    invalid_members: dict[str, Path]
    total_bytes: int
    # Populated only by `verify_set()`. `None` means "not yet verified", which is
    # never the same as "valid" — nothing may treat an unverified set as an anchor.
    verified: bool | None = None
    invalid_reason: str | None = None
    identity: dict[str, Any] | None = None

    @property
    def complete(self) -> bool:
        return set(self.members) == set(_MEMBER_PATTERNS)

    @property
    def structurally_eligible(self) -> bool:
        """Worth spending a checksum on: complete, no `.partial`/`.failed` member.

        A necessary condition for validity, never a sufficient one. Deliberately
        not named `known_good`: that name is what invited the original defect.
        """
        return self.complete and not self.invalid_members

    @property
    def has_suffixed_members(self) -> bool:
        """A `.partial`/`.failed` member: a run's own in-flight or failed marker."""
        return bool(self.invalid_members)

    @property
    def kind(self) -> str:
        """WHICH recognised backup shape this is. Purely structural, never a guess.

        Decided from filenames alone, because that is all a shape is: whether
        the contents are restorable is `verify_set()`'s question, and conflating
        the two is the defect this module was originally found to have. What the
        kind decides is LIFECYCLE — see `plan_retention` — and anchor
        eligibility, which only `manifest_set` can ever have.
        """
        if self.has_suffixed_members:
            return KIND_INCOMPLETE_REMNANT
        roles = set(self.members)
        if roles == set(_MEMBER_PATTERNS):
            return KIND_MANIFEST_SET
        if roles == {"postgres", "minio"}:
            # The exact output shape of `ops/backup.sh` before it wrote
            # manifests: both archives, same timestamp, nothing suffixed. A
            # complete backup of its era, and recognisable as one.
            return KIND_LEGACY_PAIR
        return KIND_INCOMPLETE_REMNANT

    @property
    def can_anchor(self) -> bool:
        """Only a manifest set may ever hold the survivor floor.

        A legacy pair has no recorded checksums, sizes or identity pins, so
        "restorable" cannot be PROVEN for it — and an unprovable anchor is
        exactly what `keep_minimum` exists to refuse. Recognising the shape
        gives it a bounded lifetime; it does not give it authority.
        """
        return self.kind == KIND_MANIFEST_SET

    def as_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "created_at": self.created_at.isoformat(),
            "kind": self.kind,
            "can_anchor": self.can_anchor,
            "structurally_eligible": self.structurally_eligible,
            "verified": self.verified,
            "invalid_reason": self.invalid_reason,
            "complete": self.complete,
            "total_bytes": self.total_bytes,
            "members": sorted(path.name for path in self.members.values()),
            "invalid_members": sorted(path.name for path in self.invalid_members.values()),
        }


@contextlib.contextmanager
def backup_lock(backup_dir: Path, *, timeout_note: str = "") -> Iterator[None]:
    """Hold the same advisory lock `ops/backup.sh` uses, or refuse to proceed.

    Non-blocking on purpose. If a backup is running, the correct action is to skip
    this retention window entirely — the next fire is 24 hours away and the disk
    cost of one skipped window is trivial next to planning deletions against a
    directory that is being written.
    """
    lock_path = backup_dir / LOCK_FILENAME
    handle = open(lock_path, "a+")  # noqa: SIM115 - lifetime is the context manager
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError(
                f"another platform backup or retention run holds {lock_path}{timeout_note}"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def verify_set(
    item: BackupSet,
    *,
    identity: AuthoritativeIdentity | None = None,
    check_archives: bool = True,
) -> BackupSet:
    """Decide validity with the authoritative contract. Never raises.

    `identity` pins environment/database/platform to the current platform. It is
    optional only so a caller can ask the narrower question "is this set
    internally self-consistent?"; retention always passes it.
    """
    if not item.structurally_eligible:
        return replace(
            item, verified=False,
            invalid_reason="incomplete_or_suffixed_members",
        )
    try:
        payload = verify_manifest(
            item.members["manifest"], timestamp=item.timestamp,
            expected=identity.as_expected() if identity else None,
            check_archives=check_archives,
        )
    except ManifestVerificationError as exc:
        return replace(item, verified=False, invalid_reason=str(exc))
    except Exception as exc:  # defensive: an unexpected error is not a pass
        return replace(item, verified=False, invalid_reason=f"{type(exc).__name__}: {exc}")
    return replace(
        item, verified=True, invalid_reason=None,
        identity={field: payload.get(field) for field in COHERENCE_FIELDS},
    )


def parse_timestamp(stamp: str) -> datetime | None:
    if not TIMESTAMP_RE.fullmatch(stamp or ""):
        return None
    try:
        return datetime.strptime(stamp, "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def discover_sets(backup_dir: Path) -> list[BackupSet]:
    """Group backup files by timestamp. Unrecognised files are ignored, never deleted."""
    grouped: dict[str, dict[str, Path]] = {}
    invalid: dict[str, dict[str, Path]] = {}
    if not backup_dir.is_dir():
        return []
    for entry in sorted(backup_dir.iterdir()):
        if not entry.is_file() or entry.is_symlink():
            continue
        match = _SET_RE.match(entry.name)
        if not match:
            continue
        stamp = match.group("a") or match.group("b") or match.group("c")
        role = (
            "postgres" if entry.name.startswith("postgres_")
            else "minio" if entry.name.startswith("minio_")
            else "manifest"
        )
        target = invalid if match.group("suffix") else grouped
        target.setdefault(stamp, {})[role] = entry

    sets: list[BackupSet] = []
    for stamp in sorted(set(grouped) | set(invalid)):
        created_at = parse_timestamp(stamp)
        if created_at is None:
            continue
        members = grouped.get(stamp, {})
        bad = invalid.get(stamp, {})
        total = 0
        for path in list(members.values()) + list(bad.values()):
            try:
                total += path.stat().st_size
            except OSError:
                pass
        sets.append(
            BackupSet(
                timestamp=stamp, created_at=created_at, members=dict(members),
                invalid_members=dict(bad), total_bytes=total,
            )
        )
    return sorted(sets, key=lambda item: item.timestamp)


class RetentionUnsafe(RuntimeError):
    """The verified survivor floor could not be proven. Delete nothing."""


def establish_anchors(
    sets: Sequence[BackupSet], *, keep_minimum: int, identity: AuthoritativeIdentity
) -> tuple[list[BackupSet], list[BackupSet]]:
    """Verify newest-first until `keep_minimum` anchors of the CURRENT platform are proven.

    Newest-first matters twice. It is the cheap order — verification reads and
    hashes whole multi-GB archives, and stopping as soon as the floor is proven
    keeps a nightly run bounded instead of hashing the entire directory. It is
    also the safe order: the anchors are the sets an operator would actually
    restore from.

    Every anchor is pinned against `identity`, which comes from the running
    platform. The newest backup in the directory has no say in what this platform
    is, so foreign or staging sets are rejected however recent they are.
    """
    verified: list[BackupSet] = []
    examined: list[BackupSet] = []

    for item in sorted(sets, key=lambda entry: entry.timestamp, reverse=True):
        if len(verified) >= keep_minimum:
            break
        if not item.can_anchor:
            # A legacy pair reaches here too. It is a recognised shape with a
            # bounded lifetime, not a provable restore point: there is no
            # manifest to check its checksums, sizes or platform identity
            # against, so it can never hold the survivor floor.
            examined.append(replace(
                item, verified=False,
                invalid_reason=(
                    "legacy_pair_has_no_manifest_to_verify"
                    if item.kind == KIND_LEGACY_PAIR
                    else "incomplete_or_suffixed_members"
                ),
            ))
            continue
        checked = verify_set(item, identity=identity)
        examined.append(checked)
        if checked.verified:
            verified.append(checked)
    return verified, examined


def plan_retention(
    sets: Sequence[BackupSet],
    *,
    retention_days: int,
    keep_minimum: int,
    now: datetime,
    identity: AuthoritativeIdentity,
    purge_invalid: bool = False,
) -> dict[str, Any]:
    """Decide what to delete. Pure: no filesystem mutation, fully deterministic.

    Raises `RetentionUnsafe` when fewer than `keep_minimum` verified anchors can
    be proven. Failing closed is the whole point: unchecked backup growth ends in
    a disk-full outage, which is recoverable; deleting the last restorable backup
    is not.

    `retention_days` is validated against the central policy here as well as at
    the entry point, so no caller — including a test or another module — can
    plan a window longer than `PLATFORM_BACKUP_SET.retention_days`.
    """
    if keep_minimum < 1:
        raise ValueError("keep_minimum must be >= 1")
    retention_days = validate_backup_retention_days(
        retention_days, source="plan_retention(retention_days=...)"
    )

    cutoff = now - timedelta(days=retention_days)
    anchors, examined = establish_anchors(
        sets, keep_minimum=keep_minimum, identity=identity
    )
    anchor_stamps = {item.timestamp for item in anchors}
    verdicts = {item.timestamp: item for item in examined}

    if len(anchors) < keep_minimum:
        raise RetentionUnsafe(
            f"only {len(anchors)} verified backup sets could be proven, "
            f"keep_minimum is {keep_minimum}; refusing to delete anything. "
            f"Verified: {sorted(anchor_stamps)}. "
            f"Rejected: {sorted((item.timestamp, item.invalid_reason) for item in examined if not item.verified)}"
        )

    delete: list[BackupSet] = []
    keep: list[dict[str, Any]] = []
    for item in sets:
        verdict = verdicts.get(item.timestamp, item)
        if item.timestamp in anchor_stamps:
            keep.append({**verdict.as_dict(), "reason": "verified_safety_floor"})
            continue
        if item.created_at >= cutoff:
            keep.append({**verdict.as_dict(), "reason": "within_retention"})
            continue
        if item.has_suffixed_members:
            # `.partial`/`.failed` remnants: never anchors, and still never
            # silently removed. The suffix is a run's own marker for work that
            # was in flight or explicitly failed, and an operator may be
            # triaging it — so this one shape keeps its opt-in gate.
            if purge_invalid:
                delete.append(item)
            else:
                keep.append({**verdict.as_dict(), "reason": "invalid_remnant_retained"})
            continue
        # Expired, unsuffixed, and not an anchor: a manifest set, a recognised
        # legacy pair, or an incomplete remnant. None of them can endanger the
        # floor, which is made of independently verified manifest sets that were
        # proven before this loop and are re-proven immediately before deletion.
        #
        # THE LEGACY PAIR IS THE CORRECTION. It used to land in the branch above
        # purely because it had no manifest, so it was retained forever while
        # this module reported a 14-day policy.
        delete.append(item)

    reclaimed = sum(item.total_bytes for item in delete)
    by_kind: dict[str, dict[str, int]] = {}
    for item in delete:
        bucket = by_kind.setdefault(item.kind, {"sets": 0, "bytes": 0})
        bucket["sets"] += 1
        bucket["bytes"] += item.total_bytes
    return {
        "cutoff": cutoff.isoformat(),
        "retention_days": retention_days,
        "retention_days_source": (
            f"ops.retention_registry.PLATFORM_BACKUP_SET.retention_days="
            f"{PLATFORM_BACKUP_SET.retention_days}"
        ),
        "keep_minimum": keep_minimum,
        "purge_invalid": bool(purge_invalid),
        "delete_by_kind": {key: by_kind[key] for key in sorted(by_kind)},
        "verified_anchor_count": len(anchors),
        "protected_floor": sorted(anchor_stamps),
        "authoritative_identity": identity.as_dict(),
        "rejected_candidates": [
            {"timestamp": item.timestamp, "reason": item.invalid_reason}
            for item in examined if not item.verified
        ],
        "delete": [item.as_dict() for item in delete],
        "keep": keep,
        "reclaimable_bytes": reclaimed,
        "_delete_sets": delete,
        "_anchor_stamps": sorted(anchor_stamps),
    }


def reverify_anchors(
    backup_dir: Path, plan: dict[str, Any], identity: AuthoritativeIdentity
) -> tuple[list[str], list[dict[str, Any]]]:
    """Prove the survivor floor is still intact *immediately before* deleting.

    The plan was computed from an earlier directory listing. This re-reads the
    anchors from disk under the same held lock and re-applies the **full**
    contract — identity pins, exact member identity, sizes, checksums and archive
    readability — so the last gate before an irreversible `unlink()` is evidence
    rather than an earlier belief. Returns the anchors that still verify plus a
    per-anchor result for the operator.
    """
    present = {item.timestamp: item for item in discover_sets(backup_dir)}
    still_valid: list[str] = []
    results: list[dict[str, Any]] = []
    for stamp in plan.get("_anchor_stamps", []):
        item = present.get(stamp)
        if item is None:
            results.append({"timestamp": stamp, "verified": False, "reason": "set_disappeared"})
            continue
        checked = verify_set(item, identity=identity)
        results.append(
            {"timestamp": stamp, "verified": bool(checked.verified),
             "reason": checked.invalid_reason}
        )
        if checked.verified:
            still_valid.append(stamp)
    return still_valid, results


def apply_retention(
    plan: dict[str, Any], *, execute: bool, backup_dir: Path | None = None,
    identity: AuthoritativeIdentity | None = None,
) -> dict[str, Any]:
    """Delete the planned sets. Idempotent: an already-absent file is not an error.

    A dry run produces exactly the candidate list an execute run would delete, so
    the plan an operator reviews is the plan that runs.
    """
    deleted: list[str] = []
    errors: list[dict[str, str]] = []
    freed = 0

    revalidation: list[dict[str, Any]] = []
    if execute and backup_dir is not None:
        if identity is None:
            raise RetentionUnsafe(
                "refusing to delete without an authoritative platform identity to "
                "re-pin the survivor floor against"
            )
        survivors, revalidation = reverify_anchors(backup_dir, plan, identity)
        required = int(plan.get("keep_minimum", DEFAULT_KEEP_MINIMUM))
        if len(survivors) < required:
            raise RetentionUnsafe(
                f"survivor floor re-verification failed just before deletion: "
                f"{len(survivors)} of {required} anchors still verify ({survivors}). "
                f"Details: {revalidation}. Nothing was deleted."
            )

    for item in plan.get("_delete_sets", []):
        for path in list(item.members.values()) + list(item.invalid_members.values()):
            if not execute:
                deleted.append(path.name)
                continue
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            try:
                path.unlink(missing_ok=True)
                deleted.append(path.name)
                freed += size
            except OSError as exc:
                errors.append({"file": path.name, "error": f"{type(exc).__name__}: {exc}"})
    return {
        "deleted_files": sorted(deleted), "freed_bytes": freed, "errors": errors,
        "pre_delete_revalidation": revalidation,
    }


def run(
    *,
    backup_dir: Path | None = None,
    retention_days: int | None = None,
    keep_minimum: int | None = None,
    execute: bool = False,
    purge_invalid: bool = False,
    now: datetime | None = None,
    alert_on_failure: bool = True,
    identity: AuthoritativeIdentity | None = None,
) -> dict[str, Any]:
    """Plan and optionally apply retention. Returns a structured result.

    `identity` is injectable for tests only; in production it is attested from the
    running platform and never inferred from the backup directory.
    """
    backup_dir = backup_dir or Path(os.getenv("BACKUP_DIR") or DEFAULT_BACKUP_DIR)
    keep_minimum = int(
        keep_minimum
        if keep_minimum is not None
        else os.getenv("BACKUP_RETENTION_KEEP_MINIMUM") or DEFAULT_KEEP_MINIMUM
    )
    now = now or datetime.now(timezone.utc)

    result: dict[str, Any] = {
        "schema": "log-platform-backup-retention/v1",
        "backup_dir": str(backup_dir),
        "dry_run": not execute,
        "executed_at": now.isoformat(),
        "central_policy": PLATFORM_BACKUP_SET.as_dict(),
    }
    try:
        # WHERE THE WINDOW COMES FROM, resolved before anything else so a
        # contradiction is reported instead of applied. The registry's number is
        # the default; a caller flag or `BACKUP_RETENTION_DAYS` may only
        # SHORTEN it, and `validate_backup_retention_days` fails closed on a
        # longer one rather than letting configuration establish a lifetime the
        # hard-retention proof does not know about.
        if retention_days is not None:
            source = "--retention-days"
            requested: Any = retention_days
        elif os.getenv("BACKUP_RETENTION_DAYS"):
            source = "BACKUP_RETENTION_DAYS"
            requested = os.environ["BACKUP_RETENTION_DAYS"]
        else:
            source = "ops.retention_registry.PLATFORM_BACKUP_SET"
            requested = PLATFORM_BACKUP_SET.retention_days
        retention_days = validate_backup_retention_days(requested, source=source)
        result["retention_days"] = retention_days
        result["retention_days_source"] = source
        # A configured backup root that is not a directory means the disk is not
        # mounted, the path is wrong, or something removed it. Reporting a
        # successful empty retention there would hide a real backup outage.
        if not backup_dir.is_dir():
            raise RetentionUnsafe(
                f"backup directory {backup_dir} does not exist or is not a directory; "
                f"refusing to report a successful retention run"
            )

        # Everything from discovery to the final unlink happens under the same
        # lock `ops/backup.sh` takes, so a concurrent backup can neither be
        # counted as an anchor mid-publication nor have its files planned away.
        # Identity is established before anything is planned, and comes from the
        # running platform rather than from the backup directory.
        if identity is None:
            identity = load_authoritative_identity()
        result["authoritative_identity"] = identity.as_dict()

        with backup_lock(backup_dir):
            sets = discover_sets(backup_dir)
            plan = plan_retention(
                sets, retention_days=retention_days, keep_minimum=keep_minimum,
                now=now, identity=identity, purge_invalid=purge_invalid,
            )
            outcome = apply_retention(
                plan, execute=execute, backup_dir=backup_dir, identity=identity,
            )
            # Post-delete: prove, under the same lock, that every protected
            # survivor still satisfies the full contract after the deletions.
            if execute:
                survivor_stamps, post_delete = reverify_anchors(backup_dir, plan, identity)
            else:
                survivor_stamps, post_delete = list(plan["protected_floor"]), []
            survivors = len(survivor_stamps)

        if execute and survivors < keep_minimum:
            raise RetentionUnsafe(
                f"after deletion only {survivors} of {keep_minimum} protected survivors "
                f"verify: {post_delete}. Operator intervention required."
            )

        plan_public = {key: value for key, value in plan.items() if not key.startswith("_")}
        failed_post_delete = [item for item in post_delete if not item["verified"]]
        result.update(
            {
                "ok": not outcome["errors"] and not failed_post_delete,
                "classification": (
                    "BACKUP_RETENTION_SUCCEEDED"
                    if not outcome["errors"] and not failed_post_delete
                    else "BACKUP_RETENTION_PARTIAL_FAILURE"
                ),
                "plan": plan_public,
                "mutations": outcome,
                "protected_survivors": sorted(survivor_stamps),
                "post_delete_verification": post_delete,
                "verified_sets_remaining": survivors,
                "operator_action_required": bool(outcome["errors"]) or bool(failed_post_delete),
            }
        )
        if (outcome["errors"] or failed_post_delete) and alert_on_failure:
            _alert_failure(result, reason="files_not_deleted")
        return result
    except Exception as exc:
        result.update(
            {
                "ok": False,
                "classification": "BACKUP_RETENTION_FAILED",
                "error_type": type(exc).__name__,
                "error": str(exc)[:500],
                "operator_action_required": True,
            }
        )
        if alert_on_failure:
            _alert_failure(result, reason="exception", exc=exc)
        return result


def _alert_failure(result: dict[str, Any], *, reason: str, exc: BaseException | None = None) -> None:
    try:
        from ops.operational_alert import INCIDENT_BACKUP_RETENTION_FAILED, report_operational_failure

        report_operational_failure(
            incident_code=INCIDENT_BACKUP_RETENTION_FAILED,
            title="Backup retention did not complete",
            summary=(
                "Automatic backup retention failed. Backup storage will keep growing "
                "until this is resolved, and unchecked growth ends in a disk-full "
                "outage that takes down PostgreSQL, MinIO and every scheduled job."
            ),
            component="ops.backup_retention",
            severity="error",
            exception_type=type(exc).__name__ if exc else None,
            error_message=str(exc) if exc else reason,
            suggested_action=(
                "Run `ops/backup_retention.py` without --execute to inspect the plan, "
                "then resolve the filesystem or permission problem it reports."
            ),
            details={"reason": reason, "backup_dir": result.get("backup_dir")},
        )
    except Exception:
        pass


def classify(backup_dir: Path | None = None, *, now: datetime | None = None) -> dict[str, Any]:
    """Name every recognised backup shape in a directory. Reads nothing else.

    STRICTLY READ-ONLY, AND WITHOUT THE THINGS `run()` NEEDS. No lock, no
    platform-identity attestation, no checksum walk over multi-GB archives, no
    incident on failure — so an operator (or an audit) can ask "what is actually
    in `backups/`, and what would expire?" against production without any risk
    of a side effect. The audit that produced this work created a production
    incident by asking that question through the nominal dry run instead.

    It deliberately does NOT decide deletion: age eligibility is reported, and
    the survivor floor — which only `run()` can prove — is not applied. Nothing
    here is a deletion authorisation.
    """
    backup_dir = backup_dir or Path(os.getenv("BACKUP_DIR") or DEFAULT_BACKUP_DIR)
    moment = now or datetime.now(timezone.utc)
    retention_days = PLATFORM_BACKUP_SET.retention_days
    cutoff = moment - timedelta(days=retention_days)

    sets = discover_sets(backup_dir)
    entries: list[dict[str, Any]] = []
    totals: dict[str, dict[str, int]] = {}
    for item in sets:
        expired = item.created_at < cutoff
        gated = item.has_suffixed_members
        entries.append({
            **item.as_dict(),
            "expired": expired,
            "age_eligible_without_flag": bool(expired and not gated),
            "needs_purge_invalid": bool(expired and gated),
        })
        key = f"{item.kind}{'_expired' if expired else '_within_retention'}"
        bucket = totals.setdefault(key, {"sets": 0, "bytes": 0})
        bucket["sets"] += 1
        bucket["bytes"] += item.total_bytes

    eligible = [entry for entry in entries if entry["age_eligible_without_flag"]]
    return {
        "schema": "log-platform-backup-classification/v1",
        "read_only": True,
        "backup_dir": str(backup_dir),
        "generated_at": moment.isoformat(),
        "retention_days": retention_days,
        "retention_days_source": (
            "ops.retention_registry.PLATFORM_BACKUP_SET.retention_days"
        ),
        "cutoff": cutoff.isoformat(),
        "recognised_sets": len(sets),
        "totals_by_kind": {key: totals[key] for key in sorted(totals)},
        "age_eligible_sets": len(eligible),
        "age_eligible_bytes": sum(entry["total_bytes"] for entry in eligible),
        "sets": entries,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministic platform backup retention")
    parser.add_argument("--backup-dir", type=Path, default=None)
    parser.add_argument(
        "--retention-days", type=int, default=None,
        help=(
            "Operational override. May only SHORTEN the central policy in "
            "ops/retention_registry.py; a longer value fails closed."
        ),
    )
    parser.add_argument("--keep-minimum", type=int, default=None)
    parser.add_argument(
        "--execute", action="store_true", help="Delete planned sets (default: dry run)"
    )
    parser.add_argument(
        "--purge-invalid", action="store_true",
        help="Also remove expired .partial/.failed remnants (never known-good sets)",
    )
    parser.add_argument(
        "--no-record", action="store_true",
        help=(
            "Do not raise an operational incident on failure (read-only "
            "rehearsal or audit). Same convention as "
            "`ops.hard_retention --no-record`: a run that is only being "
            "INSPECTED must not write operational state."
        ),
    )
    parser.add_argument(
        "--classify", action="store_true",
        help=(
            "Read-only: name every recognised backup shape and what would be "
            "age-eligible. Takes no lock, attests no identity, records nothing."
        ),
    )
    args = parser.parse_args(argv)

    if args.classify:
        print(json.dumps(
            classify(args.backup_dir), indent=2, sort_keys=True, default=str,
        ))
        return 0

    result = run(
        backup_dir=args.backup_dir, retention_days=args.retention_days,
        keep_minimum=args.keep_minimum, execute=args.execute,
        purge_invalid=args.purge_invalid,
        alert_on_failure=not args.no_record,
    )
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
