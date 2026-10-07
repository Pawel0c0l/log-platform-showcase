#!/usr/bin/env python3
"""Deterministic tests for backup retention (P0-4 / Codex BLOCKER 1).

Temporary directories only; never touches the real `backups/`:

    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" \
        .venv/bin/python ops/tests_manual/test_backup_retention.py

The previous version of this file built "good" backups out of `b"x" * 1024`
written into three correctly named files. Every one of them was unrestorable, and
the suite passed — which is precisely why retention shipped counting corrupt
triples toward its safety floor. Fixtures here are built the way `ops/backup.sh`
builds them: a real gzip stream, a real tar.gz, a manifest carrying real sha256
digests and sizes, and 0600 modes throughout. `make_valid_set()` produces a set
that genuinely passes `ops/backup_manifest.verify_manifest()`; the corruption
helpers each break exactly one clause of that contract.
"""
from __future__ import annotations

import gzip
import io
import json
import os
import sys
import tarfile
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import ops.backup_retention as br  # noqa: E402
from ops.backup_manifest import CONTRACT_VERSION, SCHEMA, sha256_file  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)

ENVIRONMENT = "test"
DATABASE = "p0_test_platform_db"
PLATFORM_UUID = "666ff6cc-aa5b-4c07-8eaa-3a95d3a4bd2c"

# Stands in for `load_authoritative_identity()`, which in production attests
# against ops_control.environment_identity. Injected here so no test needs a DB
# — and so no test can accidentally let the backup directory define identity.
IDENTITY = br.AuthoritativeIdentity(
    environment=ENVIRONMENT, database=DATABASE, platform_uuid=PLATFORM_UUID,
)


def stamp(days_ago: int) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y%m%d_%H%M%S")


def make_valid_set(
    root: Path,
    ts: str,
    *,
    environment: str = ENVIRONMENT,
    database: str = DATABASE,
    platform_uuid: str = PLATFORM_UUID,
) -> None:
    """A set that genuinely passes the authoritative verification contract."""
    postgres = root / f"postgres_{ts}.sql.gz"
    minio = root / f"minio_{ts}.tar.gz"
    manifest = root / f"backup_{ts}.manifest.json"

    postgres.write_bytes(gzip.compress(f"-- pg_dump {ts}\nSELECT 1;\n".encode()))

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        payload = f"object-{ts}".encode()
        info = tarfile.TarInfo(name=f"bucket/object_{ts}.bin")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    minio.write_bytes(buffer.getvalue())

    for path in (postgres, minio):
        os.chmod(path, 0o600)

    manifest.write_text(
        json.dumps(
            {
                "schema": SCHEMA,
                "backup_script_contract_version": CONTRACT_VERSION,
                "timestamp": ts,
                "environment": environment,
                "database": database,
                "platform_uuid": platform_uuid,
                "repository_commit": "0" * 40,
                "postgres": {
                    "filename": postgres.name,
                    "size_bytes": postgres.stat().st_size,
                    "sha256": sha256_file(postgres),
                    "validation": {"gzip_test": True, "logical_read": True},
                },
                "minio": {
                    "filename": minio.name,
                    "size_bytes": minio.stat().st_size,
                    "sha256": sha256_file(minio),
                    "archive_entry_count": 1,
                    "source_file_count": 1,
                    "validation": {"full_tar_traversal": True, "entry_count": True},
                },
                "completed_at": NOW.isoformat(),
            },
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    os.chmod(manifest, 0o600)


def make_structural_only_set(root: Path, ts: str, *, suffix: str = "") -> None:
    """Correctly named, correct modes, complete triple — and unrestorable.

    This is the shape the old fixtures used as a "good" backup, and the shape a
    process killed between `mv` and `verify_pair` leaves behind.
    """
    for name in (f"postgres_{ts}.sql.gz", f"minio_{ts}.tar.gz", f"backup_{ts}.manifest.json"):
        path = root / f"{name}{suffix}"
        path.write_bytes(b"x" * 1024)
        os.chmod(path, 0o600)


def corrupt_archive(root: Path, ts: str) -> None:
    """Silent bit-rot: same filename, same size, different bytes.

    Size-preserving on purpose. A size change would be caught by the cheaper
    length clause; flipping a byte in place is what actually forces the sha256
    comparison to be the thing standing between corruption and a survivor anchor.
    """
    path = root / f"postgres_{ts}.sql.gz"
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0xFF
    path.write_bytes(bytes(data))
    os.chmod(path, 0o600)


def break_manifest_json(root: Path, ts: str) -> None:
    path = root / f"backup_{ts}.manifest.json"
    path.write_text("{ this is not json", encoding="utf-8")
    os.chmod(path, 0o600)


def remove_archive(root: Path, ts: str) -> None:
    (root / f"minio_{ts}.tar.gz").unlink()


def repoint_manifest_archive(root: Path, ts: str) -> None:
    """All three files present; the manifest references an archive that is not there.

    Structurally indistinguishable from a good set — only reading the manifest
    catches it.
    """
    path = root / f"backup_{ts}.manifest.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["postgres"]["filename"] = f"postgres_{ts}_moved.sql.gz"
    path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def verified_stamps(root: Path) -> set[str]:
    return {
        item.timestamp for item in br.discover_sets(root)
        if br.verify_set(item, identity=IDENTITY).verified
    }


# ------------------------------------------------- authoritative validity


def test_only_genuinely_verifiable_sets_count() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_valid_set(root, stamp(1))
        make_structural_only_set(root, stamp(2))

        make_valid_set(root, stamp(3))
        corrupt_archive(root, stamp(3))

        make_valid_set(root, stamp(4))
        break_manifest_json(root, stamp(4))

        make_valid_set(root, stamp(5))
        remove_archive(root, stamp(5))

        make_valid_set(root, stamp(6))
        repoint_manifest_archive(root, stamp(6))

        assert verified_stamps(root) == {stamp(1)}, verified_stamps(root)

        reasons = {
            item.timestamp: br.verify_set(item, identity=IDENTITY).invalid_reason
            for item in br.discover_sets(root)
        }
        assert "unreadable manifest" in reasons[stamp(2)]
        assert "checksum mismatch" in reasons[stamp(3)]
        assert "unreadable manifest" in reasons[stamp(4)]
        assert "incomplete_or_suffixed_members" in reasons[stamp(5)]
        assert "does not belong to this backup set" in reasons[stamp(6)]
        print("PASS: corrupt, unparseable, incomplete and mis-referenced sets are all invalid")


# ------------------------------------------- second Codex review reproductions


def cross_reference_manifest(root: Path, ts: str, donor_ts: str) -> None:
    """Write set `ts` whose manifest points at `donor_ts`'s archives.

    Sizes and hashes are the donor's real ones, so every clause except member
    identity passes. This is the exact shape Codex used to manufacture three
    "valid" survivor anchors that all depended on one older set.
    """
    donor = json.loads((root / f"backup_{donor_ts}.manifest.json").read_text(encoding="utf-8"))
    payload = dict(donor)
    payload["timestamp"] = ts
    manifest = root / f"backup_{ts}.manifest.json"
    manifest.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.chmod(manifest, 0o600)
    # The set's own archives exist so it is structurally complete; the manifest
    # simply never refers to them.
    for name in (f"postgres_{ts}.sql.gz", f"minio_{ts}.tar.gz"):
        path = root / name
        path.write_bytes(b"not the file the manifest describes")
        os.chmod(path, 0o600)


def test_cross_referenced_manifests_are_never_anchors() -> None:
    """Codex reproduction A — three fake anchors depending on one real backup.

    Before the member-identity clause, T2/T3/T4 each verified independently,
    formed the survivor floor, and retention then deleted T1 — the only file set
    any of them actually pointed at. Nothing restorable would have remained.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        t1 = stamp(40)
        make_valid_set(root, t1)
        for days in (3, 2, 1):
            cross_reference_manifest(root, stamp(days), t1)

        assert verified_stamps(root) == {t1}, verified_stamps(root)
        for days in (3, 2, 1):
            reason = br.verify_set(
                next(i for i in br.discover_sets(root) if i.timestamp == stamp(days)),
                identity=IDENTITY,
            ).invalid_reason
            assert "does not belong to this backup set" in reason, reason

        result = br.run(identity=IDENTITY, backup_dir=root, retention_days=14,
                        keep_minimum=3, execute=True, now=NOW, alert_on_failure=False)
        assert not result["ok"], "the floor cannot be met, so retention must fail closed"
        assert result["classification"] == "BACKUP_RETENTION_FAILED"
        assert not result.get("mutations", {}).get("deleted_files")
        # The one genuinely restorable backup is untouched.
        assert (root / f"postgres_{t1}.sql.gz").exists()
        assert (root / f"minio_{t1}.tar.gz").exists()
        assert verified_stamps(root) == {t1}
        print("PASS: cross-referenced manifests never anchor; the real backup survives")


def test_exact_member_identity_is_required_per_section() -> None:
    """Either member pointing at another set's file is fatal on its own."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        t1, t2 = stamp(9), stamp(1)
        make_valid_set(root, t1)
        make_valid_set(root, t2)
        donor = json.loads((root / f"backup_{t1}.manifest.json").read_text(encoding="utf-8"))

        for section in ("postgres", "minio"):
            # Rebuild a clean T2 each time so exactly one section is borrowed and
            # the reported reason names that section.
            make_valid_set(root, t2)
            payload = json.loads((root / f"backup_{t2}.manifest.json").read_text(encoding="utf-8"))
            # Correct size and hash for the donor file — only the identity is wrong.
            payload[section] = dict(donor[section])
            path = root / f"backup_{t2}.manifest.json"
            path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            os.chmod(path, 0o600)

            item = next(i for i in br.discover_sets(root) if i.timestamp == t2)
            checked = br.verify_set(item, identity=IDENTITY)
            assert not checked.verified, f"{section} cross-reference accepted"
            assert section in checked.invalid_reason
            assert "does not belong to this backup set" in checked.invalid_reason
        print("PASS: a manifest may not borrow either member from another set")


def test_foreign_identity_backups_never_anchor_the_current_platform() -> None:
    """Codex reproduction B — newer staging backups must not redefine identity.

    Identity comes from the running platform, so the newest file in the directory
    has no vote. Previously the newest accepted set became the reference and the
    real production backups read as foreign.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        current = [stamp(days) for days in (30, 29, 28)]
        for ts in current:
            make_valid_set(root, ts)
        # Newer, internally perfect, and belonging to another platform entirely.
        foreign = [stamp(days) for days in (3, 2, 1)]
        for ts in foreign:
            make_valid_set(root, ts, environment="staging", database="staging_db",
                           platform_uuid="99999999-9999-9999-9999-999999999999")

        anchors, examined = br.establish_anchors(
            br.discover_sets(root), keep_minimum=3, identity=IDENTITY
        )
        assert sorted(item.timestamp for item in anchors) == sorted(current), (
            f"anchors must be the current platform's own backups, got "
            f"{[i.timestamp for i in anchors]}"
        )
        rejected = {i.timestamp: i.invalid_reason for i in examined if not i.verified}
        assert set(rejected) == set(foreign)
        assert all("mismatch" in reason for reason in rejected.values()), rejected

        # And the current-platform backups survive an aggressive window.
        result = br.run(identity=IDENTITY, backup_dir=root, retention_days=1,
                        keep_minimum=3, execute=True, now=NOW, alert_on_failure=False)
        assert result["ok"], result
        assert set(result["protected_survivors"]) == set(current)
        assert verified_stamps(root) == set(current)
        assert result["authoritative_identity"] == IDENTITY.as_dict()
        print("PASS: foreign backups never anchor, however new; current backups survive")


def test_archive_readability_matches_backup_sh_verify() -> None:
    """`verify_pair` runs gzip -t and tar -tzf; retention must not be weaker.

    Sizes and hashes are recomputed to match, so only a real archive traversal
    can tell this set is unusable.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ts = stamp(1)
        make_valid_set(root, ts)
        archive = root / f"postgres_{ts}.sql.gz"
        archive.write_bytes(b"\x1f\x8b\x08\x00" + b"\x00" * 64)  # gzip magic, broken body
        os.chmod(archive, 0o600)
        manifest_path = root / f"backup_{ts}.manifest.json"
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["postgres"]["size_bytes"] = archive.stat().st_size
        payload["postgres"]["sha256"] = sha256_file(archive)
        manifest_path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n",
                                 encoding="utf-8")
        os.chmod(manifest_path, 0o600)

        item = next(i for i in br.discover_sets(root) if i.timestamp == ts)
        # Hashes agree, so a checksum-only contract would call this valid.
        assert br.verify_set(item, identity=IDENTITY, check_archives=False).verified
        checked = br.verify_set(item, identity=IDENTITY)
        assert not checked.verified
        assert "unreadable" in checked.invalid_reason, checked.invalid_reason
        print("PASS: retention traverses archives exactly as backup.sh verify does")


def test_survivor_floor_holds_after_every_destructive_run() -> None:
    """`valid_current_identity_survivors >= KEEP_MINIMUM`, checked after mutation."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for days in (60, 50, 40, 30, 5, 4, 3, 2, 1):
            make_valid_set(root, stamp(days))

        result = br.run(identity=IDENTITY, backup_dir=root, retention_days=14,
                        keep_minimum=3, execute=True, now=NOW, alert_on_failure=False)
        assert result["ok"], result
        # Structured evidence the operator needs.
        assert len(result["protected_survivors"]) == 3
        assert all(item["verified"] for item in result["post_delete_verification"])
        assert result["operator_action_required"] is False

        survivors = verified_stamps(root)
        assert len(survivors) >= 3, survivors
        assert set(result["protected_survivors"]) <= survivors
        print("PASS: the verified survivor floor is proven again after deletion")


def test_a_corrupt_triple_can_never_be_a_survivor_anchor() -> None:
    """Codex BLOCKER 1, exactly: the realistic path to zero restorable backups."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # One genuinely restorable backup, old enough to be expired.
        make_valid_set(root, stamp(40))
        # Newer sets that look perfect and restore nothing.
        for days in (1, 2, 3):
            make_structural_only_set(root, stamp(days))

        result = br.run(identity=IDENTITY, backup_dir=root, retention_days=14, keep_minimum=3,
                        execute=True, now=NOW, alert_on_failure=False)

        assert not result["ok"], "retention must not succeed on an unprovable floor"
        assert result["classification"] == "BACKUP_RETENTION_FAILED"
        assert "verified" in result["error"]
        assert verified_stamps(root) == {stamp(40)}, "the only restorable backup survived"
        assert not result.get("mutations", {}).get("deleted_files")
        assert (root / f"postgres_{stamp(40)}.sql.gz").exists()
        print("PASS: corrupt triples cannot form the floor; the last valid backup survives")


def test_retention_fails_closed_below_the_verified_floor() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_valid_set(root, stamp(30))
        make_valid_set(root, stamp(29))
        make_structural_only_set(root, stamp(1))

        result = br.run(identity=IDENTITY, backup_dir=root, retention_days=14, keep_minimum=3,
                        execute=True, now=NOW, alert_on_failure=False)
        assert not result["ok"] and result["classification"] == "BACKUP_RETENTION_FAILED"
        assert verified_stamps(root) == {stamp(30), stamp(29)}
        print("PASS: fewer verified anchors than keep_minimum deletes nothing")


def test_identity_mismatch_is_not_an_anchor() -> None:
    """A backup of another environment is not this platform's last good copy."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_valid_set(root, stamp(1))
        make_valid_set(root, stamp(2), environment="staging", database="otherdb")
        make_valid_set(root, stamp(3), platform_uuid="99999999-9999-9999-9999-999999999999")

        anchors, examined = br.establish_anchors(br.discover_sets(root), keep_minimum=3, identity=IDENTITY)
        assert [item.timestamp for item in anchors] == [stamp(1)]
        rejected = {item.timestamp: item.invalid_reason for item in examined if not item.verified}
        assert set(rejected) == {stamp(2), stamp(3)}
        assert all("mismatch" in reason for reason in rejected.values()), rejected
        print("PASS: sets from another environment or platform never anchor the floor")


def test_partial_and_failed_sets_are_never_anchors() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for days in (1, 2, 3):
            make_valid_set(root, stamp(days))
        make_structural_only_set(root, stamp(0), suffix=".partial")
        make_structural_only_set(root, stamp(4), suffix=".failed")

        plan = br.plan_retention(br.discover_sets(root), retention_days=14,
                                 keep_minimum=3, now=NOW, identity=IDENTITY)
        assert plan["protected_floor"] == sorted([stamp(1), stamp(2), stamp(3)])
        print("PASS: .partial/.failed remnants are excluded from the floor")


# ------------------------------------------------------------- behaviour


def test_expired_sets_are_deleted_and_recent_ones_survive() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        old = [stamp(days) for days in (60, 50, 40, 30)]
        recent = [stamp(days) for days in (3, 2, 1, 0)]
        for ts in old + recent:
            make_valid_set(root, ts)

        result = br.run(identity=IDENTITY, backup_dir=root, retention_days=14, keep_minimum=3,
                        execute=True, now=NOW, alert_on_failure=False)
        assert result["ok"], result
        assert verified_stamps(root) == set(recent)
        assert result["verified_sets_remaining"] >= 3
        print("PASS: expired verified sets are deleted and recent ones survive")


def test_the_floor_beats_any_retention_window() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        stamps = [stamp(days) for days in (60, 50, 40, 30, 20)]
        for ts in stamps:
            make_valid_set(root, ts)

        # Every set is far outside a one-day window.
        result = br.run(identity=IDENTITY, backup_dir=root, retention_days=1, keep_minimum=3,
                        execute=True, now=NOW, alert_on_failure=False)
        assert result["ok"], result
        assert verified_stamps(root) == set(stamps[-3:]), "the newest three verified sets survive"
        print("PASS: the verified safety floor survives an aggressive retention window")


def test_a_single_valid_backup_is_never_deleted() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_valid_set(root, stamp(365))
        result = br.run(identity=IDENTITY, backup_dir=root, retention_days=1, keep_minimum=1,
                        execute=True, now=NOW, alert_on_failure=False)
        assert result["ok"], result
        assert verified_stamps(root) == {stamp(365)}
        print("PASS: the only verified backup is never deleted, however old")


def test_unrelated_files_are_never_touched() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for days in (1, 2, 3, 40):
            make_valid_set(root, stamp(days))
        keepers = ["notes.txt", "postgres_backup.sql.gz", "systemd", ".backup.lock"]
        for name in keepers[:-1]:
            (root / name).write_text("keep me", encoding="utf-8")
        (root / "systemd").unlink()
        (root / "systemd").mkdir()

        br.run(identity=IDENTITY, backup_dir=root, retention_days=14, keep_minimum=3, execute=True,
               now=NOW, alert_on_failure=False)
        assert (root / "notes.txt").exists()
        assert (root / "postgres_backup.sql.gz").exists()
        assert (root / "systemd").is_dir()
        print("PASS: files outside the backup naming contract are never deleted")


def test_dry_run_and_execute_agree_and_repeat_is_idempotent() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for days in (60, 50, 3, 2, 1):
            make_valid_set(root, stamp(days))

        dry = br.run(identity=IDENTITY, backup_dir=root, retention_days=14, keep_minimum=3,
                     execute=False, now=NOW, alert_on_failure=False)
        before = sorted(path.name for path in root.iterdir())
        assert sorted(path.name for path in root.iterdir()) == before

        wet = br.run(identity=IDENTITY, backup_dir=root, retention_days=14, keep_minimum=3,
                     execute=True, now=NOW, alert_on_failure=False)
        # The plan an operator reviewed is the plan that ran.
        assert dry["mutations"]["deleted_files"] == wet["mutations"]["deleted_files"]
        assert dry["plan"]["protected_floor"] == wet["plan"]["protected_floor"]

        again = br.run(identity=IDENTITY, backup_dir=root, retention_days=14, keep_minimum=3,
                       execute=True, now=NOW, alert_on_failure=False)
        assert again["ok"] and again["mutations"]["deleted_files"] == []
        assert again["mutations"]["freed_bytes"] == 0
        print("PASS: dry run matches execute, and repeating retention changes nothing")


def test_purge_invalid_is_opt_in() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for days in (1, 2, 3):
            make_valid_set(root, stamp(days))
        make_structural_only_set(root, stamp(40), suffix=".failed")

        br.run(identity=IDENTITY, backup_dir=root, retention_days=14, keep_minimum=3, execute=True,
               now=NOW, alert_on_failure=False)
        assert (root / f"postgres_{stamp(40)}.sql.gz.failed").exists()

        br.run(identity=IDENTITY, backup_dir=root, retention_days=14, keep_minimum=3, execute=True,
               purge_invalid=True, now=NOW, alert_on_failure=False)
        assert not (root / f"postgres_{stamp(40)}.sql.gz.failed").exists()
        assert verified_stamps(root) == {stamp(1), stamp(2), stamp(3)}
        print("PASS: expired .failed remnants are removed only when explicitly requested")


# ------------------------------------- central policy + legacy set lifecycle


def make_legacy_pair(root: Path, ts: str) -> None:
    """The shape `ops/backup.sh` produced BEFORE it wrote manifests.

    Two archives, one timestamp, no manifest, no suffix. Real gzip and tar.gz
    streams, because "legacy" describes the era of the format, not the quality
    of the bytes — these were complete backups when they were taken.
    """
    postgres = root / f"postgres_{ts}.sql.gz"
    minio = root / f"minio_{ts}.tar.gz"
    postgres.write_bytes(gzip.compress(f"-- pg_dump {ts}\nSELECT 1;\n".encode()))
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        payload = f"legacy-{ts}".encode()
        info = tarfile.TarInfo(name=f"bucket/object_{ts}.bin")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    minio.write_bytes(buffer.getvalue())
    for path in (postgres, minio):
        os.chmod(path, 0o600)


def test_the_window_comes_from_the_central_registry() -> None:
    """The 14 is the registry's, and this module may not own a second one."""
    from ops import retention_registry as rr

    assert br.DEFAULT_RETENTION_DAYS == rr.PLATFORM_BACKUP_SET.retention_days, (
        "ops.backup_retention must READ the window from the topology the backup "
        "shadow is derived from, never declare its own"
    )
    # And the registry entry an operator reads must be the same number again.
    assert rr.get("filesystem.platform_backup_sets").retention.value == (
        rr.PLATFORM_BACKUP_SET.retention_days
    )
    # The shadow every hard-retention enforcement lead depends on is computed
    # from that number, which is why a longer window silently invalidates it.
    assert rr.backup_shadow_days() == rr.PLATFORM_BACKUP_SET.retention_days + 1
    print("PASS: one backup-retention number, declared in the registry")


def test_a_longer_window_fails_closed_from_every_lever() -> None:
    """Configuration may shorten the central policy; it may never lengthen it."""
    from ops import retention_registry as rr

    declared = rr.PLATFORM_BACKUP_SET.retention_days
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for days in (1, 2, 3, 60):
            make_valid_set(root, stamp(days))

        # The CLI/caller lever.
        longer = br.run(identity=IDENTITY, backup_dir=root, retention_days=declared + 1,
                        keep_minimum=3, execute=True, now=NOW, alert_on_failure=False)
        assert not longer["ok"], "a longer window must not be applied"
        assert longer["classification"] == "BACKUP_RETENTION_FAILED"
        assert "exceeds the central policy" in longer["error"], longer["error"]
        assert (root / f"postgres_{stamp(60)}.sql.gz").exists(), "nothing was deleted"

        # The environment lever, which the systemd unit could also carry.
        previous = os.environ.get("BACKUP_RETENTION_DAYS")
        os.environ["BACKUP_RETENTION_DAYS"] = str(declared + 30)
        try:
            env_run = br.run(identity=IDENTITY, backup_dir=root, keep_minimum=3,
                             execute=True, now=NOW, alert_on_failure=False)
        finally:
            if previous is None:
                os.environ.pop("BACKUP_RETENTION_DAYS", None)
            else:
                os.environ["BACKUP_RETENTION_DAYS"] = previous
        assert not env_run["ok"] and env_run["classification"] == "BACKUP_RETENTION_FAILED"
        assert "BACKUP_RETENTION_DAYS" in env_run["error"], env_run["error"]
        assert (root / f"postgres_{stamp(60)}.sql.gz").exists()

        # Shorter is still a legitimate operational choice.
        shorter = br.run(identity=IDENTITY, backup_dir=root, retention_days=1,
                         keep_minimum=3, execute=True, now=NOW, alert_on_failure=False)
        assert shorter["ok"], shorter
        assert shorter["retention_days"] == 1
        # Even the pure planner refuses, so no caller can route around `run()`.
        try:
            br.plan_retention(br.discover_sets(root), retention_days=declared + 1,
                              keep_minimum=3, now=NOW, identity=IDENTITY)
        except rr.BackupRetentionPolicyConflict:
            pass
        else:
            raise AssertionError("plan_retention accepted a window past the policy")
    print("PASS: a longer backup window fails closed from CLI, environment and planner")


def test_a_legacy_pair_is_recognised_and_expires() -> None:
    """THE production defect: no manifest meant no expiry, forever.

    Eleven legacy sets — the oldest from February 2026, about 39 GB — were being
    kept as `invalid_remnant_retained` while the registry said 14 days, so the
    end-to-end hard-retention proof for everything inside the backup set was not
    true.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for days in (1, 2, 3):
            make_valid_set(root, stamp(days))
        expired_pair, fresh_pair = stamp(40), stamp(5)
        make_legacy_pair(root, expired_pair)
        make_legacy_pair(root, fresh_pair)

        sets = {item.timestamp: item for item in br.discover_sets(root)}
        assert sets[expired_pair].kind == br.KIND_LEGACY_PAIR
        assert not sets[expired_pair].can_anchor, (
            "a legacy pair has no manifest, so 'restorable' cannot be proven "
            "for it; recognising the shape must not grant it authority"
        )
        assert sets[stamp(1)].kind == br.KIND_MANIFEST_SET
        assert sets[stamp(1)].can_anchor

        plan = br.plan_retention(br.discover_sets(root), retention_days=14,
                                 keep_minimum=3, now=NOW, identity=IDENTITY)
        assert plan["protected_floor"] == sorted([stamp(1), stamp(2), stamp(3)]), (
            "the floor is made of verified manifest sets only"
        )
        planned = {entry["timestamp"] for entry in plan["delete"]}
        assert expired_pair in planned, "an expired legacy pair must now expire"
        assert fresh_pair not in planned, "a legacy pair inside the window survives"
        assert plan["delete_by_kind"][br.KIND_LEGACY_PAIR]["sets"] == 1

        result = br.run(identity=IDENTITY, backup_dir=root, retention_days=14,
                        keep_minimum=3, execute=True, now=NOW, alert_on_failure=False)
        assert result["ok"], result
        assert not (root / f"postgres_{expired_pair}.sql.gz").exists()
        assert not (root / f"minio_{expired_pair}.tar.gz").exists()
        assert (root / f"postgres_{fresh_pair}.sql.gz").exists()
        assert verified_stamps(root) == {stamp(1), stamp(2), stamp(3)}
    print("PASS: a recognised legacy pair expires on the central window")


def test_an_unsuffixed_orphan_half_also_expires() -> None:
    """A lone `minio_<ts>.tar.gz` is a backup copy with no restore value.

    Three of the eleven production legacy sets are exactly this shape. It can
    never anchor and can never be restored from, but it does hold MinIO objects
    past the shadow, so an unbounded lifetime for it is the same defect.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for days in (1, 2, 3):
            make_valid_set(root, stamp(days))
        orphan = stamp(60)
        make_legacy_pair(root, orphan)
        (root / f"postgres_{orphan}.sql.gz").unlink()

        item = next(i for i in br.discover_sets(root) if i.timestamp == orphan)
        assert item.kind == br.KIND_INCOMPLETE_REMNANT and not item.can_anchor

        br.run(identity=IDENTITY, backup_dir=root, retention_days=14, keep_minimum=3,
               execute=True, now=NOW, alert_on_failure=False)
        assert not (root / f"minio_{orphan}.tar.gz").exists()
        assert verified_stamps(root) == {stamp(1), stamp(2), stamp(3)}
    print("PASS: an expired unsuffixed orphan member no longer lives forever")


def test_classification_is_read_only_and_names_every_shape() -> None:
    """`--classify` is the surface an audit should have used. It must be inert.

    The audit that produced this work asked "what is in backups/?" through the
    nominal dry run, which attested identity, failed on a missing target
    environment and PERSISTED a production incident. This path takes no lock,
    opens no connection, records nothing, and deletes nothing.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_valid_set(root, stamp(1))
        make_valid_set(root, stamp(40))
        make_legacy_pair(root, stamp(50))
        make_structural_only_set(root, stamp(60), suffix=".failed")
        (root / "client_alpha_main_20260727_103333.dump").write_bytes(b"client data")
        before = sorted(path.name for path in root.iterdir())

        report = br.classify(root, now=NOW)
        assert report["read_only"] is True
        assert report["retention_days"] == br.DEFAULT_RETENTION_DAYS
        kinds = {entry["timestamp"]: entry["kind"] for entry in report["sets"]}
        assert kinds == {
            stamp(1): br.KIND_MANIFEST_SET,
            stamp(40): br.KIND_MANIFEST_SET,
            stamp(50): br.KIND_LEGACY_PAIR,
            stamp(60): br.KIND_INCOMPLETE_REMNANT,
        }, kinds
        eligible = {
            entry["timestamp"] for entry in report["sets"]
            if entry["age_eligible_without_flag"]
        }
        assert eligible == {stamp(40), stamp(50)}
        gated = {
            entry["timestamp"] for entry in report["sets"]
            if entry["needs_purge_invalid"]
        }
        assert gated == {stamp(60)}, "a .failed remnant keeps its opt-in gate"
        # The client dump is not a backup set and is invisible to this module.
        assert all("client_alpha_main" not in name
                   for entry in report["sets"] for name in entry["members"])
        assert sorted(path.name for path in root.iterdir()) == before, (
            "classification mutated the directory"
        )
    print("PASS: classification names every shape, touches nothing")


def test_unrelated_client_dumps_are_invisible_to_expiry() -> None:
    """Fail-closed is a property of DISCOVERY, and age must not weaken it."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for days in (1, 2, 3):
            make_valid_set(root, stamp(days))
        bystanders = [
            "client_alpha_main_20260127_103333.dump",
            "telematics_main_stage3c_20260127_121852.dump",
            "logdb_before_044_database_export_20260103T075322Z.sql.gz",
            "platform_schema_pre_052_20260128_092237.sql.gz",
            "postgres_backup.sql.gz",
            "minio.tar.gz",
            "backup_notes.manifest.json",
        ]
        for name in bystanders:
            path = root / name
            path.write_bytes(b"not a platform backup set")
            # Far outside any retention window; age must not make them eligible.
            os.utime(path, (0, 0))

        result = br.run(identity=IDENTITY, backup_dir=root, retention_days=14,
                        keep_minimum=3, execute=True, purge_invalid=True,
                        now=NOW, alert_on_failure=False)
        assert result["ok"], result
        for name in bystanders:
            assert (root / name).exists(), f"{name} was deleted"
    print("PASS: files outside the naming contract stay invisible, at any age")


def test_no_record_suppresses_the_operational_incident() -> None:
    """A read-only rehearsal must not write operational state.

    `ops.hard_retention` has had `--no-record` for exactly this; backup
    retention had no equivalent, so an inspection that failed — a missing target
    environment, an unmounted backup root — persisted an incident an operator
    then had to triage. Same convention, same word.
    """
    import contextlib
    import io as _io

    calls: list[dict] = []

    def fake_run(**kwargs):
        calls.append(kwargs)
        return {"ok": True}

    original = br.run
    br.run = fake_run  # type: ignore[assignment]
    try:
        with contextlib.redirect_stdout(_io.StringIO()):
            # An unknown flag would SystemExit(2) here, so reaching the
            # assertions is itself proof the flag exists.
            br.main(["--no-record"])
            br.main([])
    finally:
        br.run = original  # type: ignore[assignment]
    assert calls[0]["alert_on_failure"] is False, "--no-record must suppress the incident"
    assert calls[1]["alert_on_failure"] is True, "recording stays the default"
    print("PASS: --no-record suppresses the incident an inspection must not write")


def test_invalid_configuration_and_missing_root_fail_closed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for days in (1, 2, 3):
            make_valid_set(root, stamp(days))
        for kwargs in ({"retention_days": 0}, {"keep_minimum": 0}):
            result = br.run(identity=IDENTITY, backup_dir=root, execute=True, now=NOW,
                            alert_on_failure=False, **kwargs)
            assert not result["ok"]
            assert result["classification"] == "BACKUP_RETENTION_FAILED"
        assert verified_stamps(root) == {stamp(1), stamp(2), stamp(3)}

    # A configured backup root that is absent means the disk is not mounted.
    missing = br.run(identity=IDENTITY, backup_dir=Path("/nonexistent/backups"), retention_days=14,
                     keep_minimum=3, execute=True, now=NOW, alert_on_failure=False)
    assert not missing["ok"], "an absent backup root is a backup outage, not a success"
    assert missing["classification"] == "BACKUP_RETENTION_FAILED"
    print("PASS: bad configuration and a missing backup root both fail closed")


# --------------------------------------------------------------- locking


def test_backup_lock_prevents_the_concurrent_race() -> None:
    """`ops/backup.sh` holds this exact lock while publishing a set.

    Proven against a real `flock` held by a separate process on the same file, so
    this exercises kernel behaviour rather than an in-process flag.
    """
    import subprocess

    # A separate process taking the same flock() on the same path — the exact
    # mechanism `ops/backup.sh` uses (`exec 9>"$LOCK_FILE"; flock -n 9`). It holds
    # the lock until its stdin closes, so release is deterministic rather than
    # timing-dependent.
    holder_script = (
        "import fcntl,sys;"
        "h=open(sys.argv[1],'a+');"
        "fcntl.flock(h.fileno(), fcntl.LOCK_EX);"
        "sys.stdout.write('locked\\n'); sys.stdout.flush();"
        "sys.stdin.read()"
    )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for days in (1, 2, 3, 40):
            make_valid_set(root, stamp(days))
        lock_path = root / br.LOCK_FILENAME

        holder = subprocess.Popen(
            [sys.executable, "-c", holder_script, str(lock_path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )
        try:
            assert holder.stdout.readline().strip() == "locked"

            result = br.run(identity=IDENTITY, backup_dir=root, retention_days=14, keep_minimum=3,
                            execute=True, now=NOW, alert_on_failure=False)
            assert not result["ok"], "retention must refuse to run beside a backup"
            assert "holds" in result["error"]
            assert (root / f"postgres_{stamp(40)}.sql.gz").exists(), "nothing deleted"
        finally:
            holder.stdin.close()
            holder.wait(timeout=10)

        result = br.run(identity=IDENTITY, backup_dir=root, retention_days=14, keep_minimum=3,
                        execute=True, now=NOW, alert_on_failure=False)
        assert result["ok"], result
        assert not (root / f"postgres_{stamp(40)}.sql.gz").exists()
        print("PASS: a concurrent backup blocks retention; releasing the lock unblocks it")


def test_survivor_floor_is_reverified_immediately_before_deletion() -> None:
    """The last gate before an irreversible unlink is evidence, not an earlier belief."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for days in (1, 2, 3):
            make_valid_set(root, stamp(days))
        make_valid_set(root, stamp(40))

        sets = br.discover_sets(root)
        plan = br.plan_retention(sets, retention_days=14, keep_minimum=3, now=NOW,
                              identity=IDENTITY)
        assert plan["protected_floor"] == sorted([stamp(1), stamp(2), stamp(3)])

        # Something corrupts the anchors between planning and deletion.
        for days in (1, 2):
            corrupt_archive(root, stamp(days))

        try:
            br.apply_retention(plan, execute=True, backup_dir=root, identity=IDENTITY)
        except br.RetentionUnsafe as exc:
            assert "re-verification failed" in str(exc)
        else:
            raise AssertionError("deletion proceeded on an unproven floor")
        assert (root / f"postgres_{stamp(40)}.sql.gz").exists(), "nothing was deleted"
        print("PASS: anchors are re-verified under the lock just before deleting")


# ------------------------------ restore / survivor identity parity (final HIGH)


PRODUCTION_IDENTITY = br.AuthoritativeIdentity(
    environment="production",
    database="logdb",
    platform_uuid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
)


def make_production_set(root: Path, ts: str) -> None:
    make_valid_set(root, ts, environment=PRODUCTION_IDENTITY.environment,
                   database=PRODUCTION_IDENTITY.database,
                   platform_uuid=PRODUCTION_IDENTITY.platform_uuid)


def make_staging_set(root: Path, ts: str) -> None:
    """A genuinely valid, self-contained staging backup.

    Everything about it is correct — exact members, real hashes, readable gzip
    and tar. Only the identity is wrong, which is precisely why a contract
    without identity pins declared it VALID for a production restore.
    """
    make_valid_set(root, ts, environment="staging", database="staging_db",
                   platform_uuid="99999999-9999-9999-9999-999999999999")


def test_staging_backup_is_rejected_by_the_production_restore_verifier() -> None:
    """The exact final Codex reproduction, through the real CLI the docs invoke."""
    import ops.verify_backup_set as vbs

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        good, foreign = stamp(2), stamp(1)
        make_production_set(root, good)
        make_staging_set(root, foreign)

        # Sanity: the staging set really is internally perfect. Without identity
        # pins it passes every other clause, which is the whole defect.
        item = next(i for i in br.discover_sets(root) if i.timestamp == foreign)
        assert br.verify_set(item, identity=None, check_archives=True).verified, (
            "the fixture must be a genuinely valid self-contained backup"
        )

        loader = lambda: PRODUCTION_IDENTITY  # noqa: E731 - attested target
        assert vbs.main([foreign, "--backup-dir", str(root)], identity_loader=loader) == 1, (
            "a staging backup must not verify for a production restore"
        )
        assert vbs.main([good, "--backup-dir", str(root)], identity_loader=loader) == 0
        print("PASS: a valid staging backup is rejected by the production restore verifier")


def test_survivor_verification_fails_closed_on_any_single_failure() -> None:
    """One bad survivor must make the whole verification stage non-zero.

    The previous procedure looped with `|| echo "FAILED: $STAMP"`, which prints a
    warning and leaves the stage's exit status at 0 — the operator's "verify every
    survivor" step could not actually fail.
    """
    import ops.verify_backup_set as vbs

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        survivors = [stamp(3), stamp(2), stamp(1)]
        for ts in survivors:
            make_production_set(root, ts)
        loader = lambda: PRODUCTION_IDENTITY  # noqa: E731

        args = [*survivors, "--backup-dir", str(root)]
        assert vbs.main(args, identity_loader=loader) == 0

        # One survivor goes bad; the aggregate verdict must follow.
        corrupt_archive(root, survivors[1])
        assert vbs.main(args, identity_loader=loader) == 1, (
            "one failing survivor must fail the whole verification stage"
        )

        # A foreign set among otherwise-good survivors is equally fatal.
        corrupt = survivors[1]
        make_production_set(root, corrupt)
        make_staging_set(root, stamp(4))
        assert vbs.main([*survivors, stamp(4), "--backup-dir", str(root)],
                        identity_loader=loader) == 1

        # A named set that no longer exists is a failure, never a silent skip.
        assert vbs.main(["20200101_000000", "--backup-dir", str(root)],
                        identity_loader=loader) == 1
        print("PASS: survivor verification fails closed on any single failure")


def test_verification_fails_closed_when_identity_cannot_be_attested() -> None:
    """An unattestable target is never a licence to proceed."""
    import ops.verify_backup_set as vbs

    def broken_loader():
        raise RuntimeError("ops_control.environment_identity unavailable")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        ts = stamp(1)
        make_production_set(root, ts)
        assert vbs.main([ts, "--backup-dir", str(root)], identity_loader=broken_loader) == 2
        print("PASS: verification fails closed when the target identity is unknown")


# The authoritative identity bootstrap, as the documented procedures spell it.
# `ops.backup_retention` and `ops.verify_backup_set` attest the platform identity
# and need LOG_PLATFORM_EXPECTED_PLATFORM_IDENTITY_ID plus the expected
# PostgreSQL values, which live in the repository .env rather than in the
# canonical identity file. Invoked bare they exit 1 with
# EXPECTED_PLATFORM_IDENTITY_MISSING before verifying anything.
IDENTITY_BOOTSTRAP = ".venv/bin/python ops/run_with_environment_identity.py --"
DOCUMENTED_VERIFIER = f"{IDENTITY_BOOTSTRAP} .venv/bin/python -m ops.verify_backup_set"


def extract_survivor_verification_block() -> str:
    """The real fenced bash block from `docs/11_operational_readiness.md`.

    The documented block *is* the implementation for a manual procedure, so the
    test executes that exact text rather than a paraphrase of it. Keeping one
    copy of the control flow is the point: a second implementation maintained
    only for tests would be free to disagree with the documentation.
    """
    doc = (REPO_ROOT / "docs/11_operational_readiness.md").read_text(encoding="utf-8")
    blocks = []
    current: list[str] | None = None
    for line in doc.splitlines():
        stripped = line.strip()
        if stripped.startswith("```bash"):
            current = []
            continue
        if stripped == "```" and current is not None:
            blocks.append("\n".join(current))
            current = None
            continue
        if current is not None:
            # Fenced blocks are indented inside the numbered list; dedent by the
            # list indent so the extracted text is runnable as-is.
            current.append(line[3:] if line.startswith("   ") else line)
    matching = [item for item in blocks if "ops.verify_backup_set" in item and "jq" in item]
    assert len(matching) == 1, f"expected exactly one survivor block, found {len(matching)}"
    return matching[0]


def run_documented_survivor_block(
    *, verifier_exit: int, workdir: Path
) -> tuple[int, bool, str]:
    """Execute the documented block with the verifier stubbed to a given exit.

    Returns (block exit status, whether the post-verification commands ran,
    combined output). `df` is stubbed to drop a marker file, so "did the stage
    continue?" is observed rather than assumed.
    """
    import subprocess

    block = extract_survivor_verification_block()

    bin_dir = workdir / "bin"
    bin_dir.mkdir()
    marker = workdir / "post-verification-ran"

    # Stubbed verifier: same CLI shape, chosen exit status.
    stub_python = bin_dir / "stub-python"
    stub_python.write_text(
        "#!/bin/sh\n"
        'echo "verifier invoked: $*"\n'
        f"exit {verifier_exit}\n",
        encoding="utf-8",
    )
    # `df` stands in for every command that must not run after a failure.
    (bin_dir / "df").write_text(
        f"#!/bin/sh\ntouch '{marker}'\necho 'df ran'\n", encoding="utf-8"
    )
    for path in (stub_python, bin_dir / "df"):
        os.chmod(path, 0o755)

    retention_run = workdir / "retention-run.json"
    retention_run.write_text(
        json.dumps(
            {
                "ok": True,
                "operator_action_required": False,
                "protected_survivors": ["20260809_030000", "20260808_030000"],
                "post_delete_verification": [],
                "mutations": {"freed_bytes": 123},
            }
        ),
        encoding="utf-8",
    )

    script = block.replace(DOCUMENTED_VERIFIER, str(stub_python)).replace(
        "/tmp/retention-run.json", str(retention_run)
    )
    # Doubles as a regression guard: if the documented block ever drops the
    # identity bootstrap, this substitution stops matching and the test fails
    # rather than silently exercising a form the operator would not run.
    assert str(stub_python) in script, (
        "verifier substitution failed — the documented block no longer invokes "
        f"the verifier as: {DOCUMENTED_VERIFIER}"
    )

    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")
    completed = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, env=env, cwd=workdir,
    )
    return completed.returncode, marker.exists(), completed.stdout + completed.stderr


def test_documented_survivor_block_fails_closed_end_to_end() -> None:
    """The COMPLETE documented block must carry the verifier's failure.

    The previous guard only checked that `|| echo` was absent, which the block
    satisfied while still ending in `echo`/`jq`/`df` — three successful commands
    that reset the stage's exit status to 0. An operator following the procedure
    saw success after a genuinely failed verification. This executes the block.
    """
    # All survivors valid: the stage completes and the inspection commands run.
    with tempfile.TemporaryDirectory() as tmp:
        code, later_ran, output = run_documented_survivor_block(
            verifier_exit=0, workdir=Path(tmp)
        )
        assert code == 0, f"a passing verification must exit 0: {output}"
        assert later_ran, "the post-verification inspection must run on success"
        assert "verifier invoked" in output
        # The survivor list really did reach the verifier as arguments.
        assert "20260809_030000" in output and "20260808_030000" in output

    # Every failure mode: corrupt / foreign / missing survivor all exit 1, an
    # unattestable identity exits 2, and an unexpected status must not be special.
    for label, verifier_exit in (
        ("corrupt survivor", 1),
        ("foreign survivor", 1),
        ("missing survivor", 1),
        ("identity unattestable", 2),
        ("unexpected verifier error", 3),
    ):
        with tempfile.TemporaryDirectory() as tmp:
            code, later_ran, output = run_documented_survivor_block(
                verifier_exit=verifier_exit, workdir=Path(tmp)
            )
            assert code != 0, f"{label}: block exited 0 despite verifier {verifier_exit}"
            assert code == verifier_exit, (
                f"{label}: block exit {code} lost the verifier's status {verifier_exit}"
            )
            assert not later_ran, (
                f"{label}: post-verification commands ran after a failed verification"
            )
            assert "SURVIVOR VERIFICATION FAILED" in output, (
                f"{label}: the operator was not told to stop"
            )
    print("PASS: the documented survivor block fails closed and keeps the verifier's status")


def test_documented_survivor_block_rejects_an_empty_survivor_list() -> None:
    """No survivors is a failure, not a vacuous success."""
    import subprocess

    block = extract_survivor_verification_block()
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        retention_run = workdir / "retention-run.json"
        retention_run.write_text(json.dumps({"protected_survivors": []}), encoding="utf-8")
        script = block.replace(DOCUMENTED_VERIFIER, "/bin/true").replace(
            "/tmp/retention-run.json", str(retention_run)
        )
        assert "/bin/true" in script, "verifier substitution failed"
        completed = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, cwd=workdir
        )
        assert completed.returncode != 0, "an empty survivor list must not pass"
    print("PASS: an empty protected-survivor list fails the verification stage")


def test_documented_procedures_use_the_pinned_verifier() -> None:
    """The docs must invoke the helper that cannot omit identity pins.

    Asserted against the documents themselves, because for a manual restore the
    documented command *is* the implementation.
    """
    recovery = (REPO_ROOT / "docs/09_disaster_recovery.md").read_text(encoding="utf-8")
    readiness = (REPO_ROOT / "docs/11_operational_readiness.md").read_text(encoding="utf-8")

    for name, doc in (("09_disaster_recovery", recovery), ("11_operational_readiness", readiness)):
        assert "ops.verify_backup_set" in doc, f"{name} must use the pinned verifier"
        # The unpinned CLI must not be the thing a procedure relies on: its
        # identity flags are optional, which is how staging passed as production.
        assert "ops/backup_manifest.py verify" not in doc, (
            f"{name} still calls the unpinned verifier directly"
        )
        # Executable lines only — prose explaining the pattern that was removed is
        # documentation, not a masked failure.
        masked = [
            line for line in doc.splitlines()
            if "|| echo" in line and not line.strip().startswith("#")
        ]
        assert not masked, f"{name} still masks a failure with `|| echo`: {masked}"
    print("PASS: both documented procedures use the identity-pinned verifier")


# Every operator-facing surface that shows how to run an identity-attesting
# module. The verifier's own usage docstring counts: an operator reading
# `--help` or the top of the file copies what it shows.
IDENTITY_ATTESTING_MODULES = ("ops.backup_retention", "ops.verify_backup_set")
OPERATOR_FACING_SOURCES = (
    "docs/09_disaster_recovery.md",
    "docs/11_operational_readiness.md",
    "docs/17_production_hardening_roadmap.md",
    "ops/verify_backup_set.py",
)


def operator_facing_invocations(text: str) -> list[str]:
    """Lines that actually show an operator how to run one of these modules.

    An executable example names a Python interpreter *and* runs the module with
    `-m`. That distinction is what keeps this from flagging prose which merely
    mentions `-m ops.backup_retention` while describing a contract, an import, an
    implementation detail, or a test fixture that reconstructs an invalid form on
    purpose. Deliberately does NOT exempt comments: a commented-out example is
    still something an operator pastes.
    """
    return [
        line.strip()
        for line in text.splitlines()
        if "python" in line
        and any(f"-m {module}" in line for module in IDENTITY_ATTESTING_MODULES)
    ]


def test_documented_procedures_bootstrap_the_platform_identity() -> None:
    """Every operator-facing invocation must go through the identity wrapper.

    Both modules attest the platform identity. `load_runtime_identity()` needs
    LOG_PLATFORM_EXPECTED_PLATFORM_IDENTITY_ID and the four expected PostgreSQL
    values, which live in the repository .env — not in
    /etc/log-platform/environment-identity.env (target environment only) and not
    in /etc/log-platform/runtime.env (no LOG_PLATFORM_* key at all). An example
    that invokes them bare exits 1 with EXPECTED_PLATFORM_IDENTITY_MISSING before
    verifying or planning anything: fail-closed, but the operator is stopped for
    the wrong reason and scheduled retention never runs at all.

    Scoped to the operator-facing surfaces rather than the whole repository: the
    first version of this guard covered only the two procedure documents, and an
    independent review found bare examples surviving in the roadmap and in the
    verifier's own usage docstring.
    """
    for relative in OPERATOR_FACING_SOURCES:
        text = (REPO_ROOT / relative).read_text(encoding="utf-8")
        invocations = operator_facing_invocations(text)
        assert invocations, f"{relative}: no operator-facing invocation found at all"
        for line in invocations:
            assert "ops/run_with_environment_identity.py --" in line, (
                f"{relative}: bypasses the identity bootstrap: {line}"
            )
            # The module must sit BEHIND the separator, or it is an argument to
            # the wrapper rather than the command the wrapper execs.
            _, _, after = line.partition("ops/run_with_environment_identity.py --")
            assert any(f"-m {module}" in after for module in IDENTITY_ATTESTING_MODULES), (
                f"{relative}: module is not behind the `--` separator: {line}"
            )
        # Identity is attested, never typed in.
        assert "LOG_PLATFORM_EXPECTED_PLATFORM_IDENTITY_ID=" not in text, (
            f"{relative}: hard-codes an expected platform identity value"
        )
    print(
        f"PASS: all {len(OPERATOR_FACING_SOURCES)} operator-facing surfaces bootstrap identity"
    )


def test_documented_restore_locks_before_it_verifies() -> None:
    """The restore critical section must start at the lock, not at verification.

    Restore is manual by design, so the documented sequence *is* the
    implementation. Verifying first and locking afterwards lets retention delete
    the set in between, and the operator discovers it half-way through consuming
    it. Asserted structurally against the doc so the ordering cannot drift.
    """
    doc = (REPO_ROOT / "docs/09_disaster_recovery.md").read_text(encoding="utf-8")
    block_start = doc.index("flock -x backups/.backup.lock")
    block = doc[block_start:doc.index("RESTORE\n", block_start)]

    verify_at = block.index("ops.verify_backup_set")
    consume_at = min(
        block.index("gzip -dc"),
        block.index("docker compose exec -T postgres psql"),
    )
    assert verify_at < consume_at, "verification must precede consumption"

    # Both must live inside the single flock-ed heredoc.
    assert block.count("flock") == 1, "one lock acquisition, one critical section"

    # And nothing may verify outside the lock: a bare verify command before the
    # flock is exactly the split this guards against.
    prologue = doc[:block_start]
    assert "ops.verify_backup_set" not in prologue, (
        "a verification step outside the lock reintroduces the delete-between race"
    )
    assert "set -euo pipefail" in block, "a failed verify must abort before consuming"
    print("PASS: the documented restore holds one lock across select, verify and consume")


def test_retention_failure_reaches_the_operator_alert_path() -> None:
    """Silent retention failure is the failure mode that ends in a full disk."""
    import ops.operational_alert as oa

    captured: list[dict] = []
    original = oa.report_operational_failure
    oa.report_operational_failure = lambda **kwargs: captured.append(kwargs)  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for days in (1, 2, 3):
                make_valid_set(root, stamp(days))

            ok = br.run(identity=IDENTITY, backup_dir=root, retention_days=14, keep_minimum=3,
                        execute=False, now=NOW, alert_on_failure=True)
            assert ok["ok"] and not captured, "a successful retention run must not alert"

            failed = br.run(identity=IDENTITY, backup_dir=root, retention_days=0, execute=True,
                            now=NOW, alert_on_failure=True)
            assert failed["classification"] == "BACKUP_RETENTION_FAILED"
            assert len(captured) == 1, f"expected one alert, got {len(captured)}"
            assert captured[0]["incident_code"] == oa.INCIDENT_BACKUP_RETENTION_FAILED
            assert captured[0]["component"] == "ops.backup_retention"
            assert verified_stamps(root) == {stamp(1), stamp(2), stamp(3)}
    finally:
        oa.report_operational_failure = original  # type: ignore[assignment]
    print("PASS: a retention failure raises an operator incident and deletes nothing")


def main() -> None:
    test_only_genuinely_verifiable_sets_count()
    test_cross_referenced_manifests_are_never_anchors()
    test_exact_member_identity_is_required_per_section()
    test_foreign_identity_backups_never_anchor_the_current_platform()
    test_archive_readability_matches_backup_sh_verify()
    test_survivor_floor_holds_after_every_destructive_run()
    test_a_corrupt_triple_can_never_be_a_survivor_anchor()
    test_retention_fails_closed_below_the_verified_floor()
    test_identity_mismatch_is_not_an_anchor()
    test_partial_and_failed_sets_are_never_anchors()
    test_expired_sets_are_deleted_and_recent_ones_survive()
    test_the_floor_beats_any_retention_window()
    test_a_single_valid_backup_is_never_deleted()
    test_unrelated_files_are_never_touched()
    test_dry_run_and_execute_agree_and_repeat_is_idempotent()
    test_purge_invalid_is_opt_in()
    test_the_window_comes_from_the_central_registry()
    test_a_longer_window_fails_closed_from_every_lever()
    test_a_legacy_pair_is_recognised_and_expires()
    test_an_unsuffixed_orphan_half_also_expires()
    test_classification_is_read_only_and_names_every_shape()
    test_unrelated_client_dumps_are_invisible_to_expiry()
    test_no_record_suppresses_the_operational_incident()
    test_invalid_configuration_and_missing_root_fail_closed()
    test_backup_lock_prevents_the_concurrent_race()
    test_survivor_floor_is_reverified_immediately_before_deletion()
    test_staging_backup_is_rejected_by_the_production_restore_verifier()
    test_survivor_verification_fails_closed_on_any_single_failure()
    test_verification_fails_closed_when_identity_cannot_be_attested()
    test_documented_survivor_block_fails_closed_end_to_end()
    test_documented_survivor_block_rejects_an_empty_survivor_list()
    test_documented_procedures_use_the_pinned_verifier()
    test_documented_procedures_bootstrap_the_platform_identity()
    test_documented_restore_locks_before_it_verifies()
    test_retention_failure_reaches_the_operator_alert_path()
    print("OK - backup retention tests passed")


if __name__ == "__main__":
    main()
