#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

SCHEMA = "log-platform-backup-manifest/v1"
CONTRACT_VERSION = "2"

# The authoritative naming contract, in one place. `ops/backup.sh` writes exactly
# these names and `ops/backup_retention.py` groups by them, so a set's members are
# fully determined by its timestamp.
MEMBER_FILENAME_PATTERNS = {
    "postgres": "postgres_{ts}.sql.gz",
    "minio": "minio_{ts}.tar.gz",
    "manifest": "backup_{ts}.manifest.json",
}


def member_filename(section: str, timestamp: str) -> str:
    return MEMBER_FILENAME_PATTERNS[section].format(ts=timestamp)


def _archive_is_readable(section: str, path: Path) -> str | None:
    """Mirror `verify_pair`'s `gzip -t` / `tar -tzf` in-process.

    `ops/backup.sh` runs both before declaring a set verified. Retention has to
    apply the same readability bar, or "valid" would mean something weaker there
    than it does at creation time.
    """
    try:
        if section == "postgres":
            import gzip

            with gzip.open(path, "rb") as handle:
                while handle.read(1024 * 1024):
                    pass
        else:
            import tarfile

            with tarfile.open(path, "r:gz") as archive:
                for _ in archive:
                    pass
    except Exception as exc:
        return f"{section} archive unreadable: {type(exc).__name__}"
    return None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create(args: argparse.Namespace) -> None:
    output = Path(args.output)
    if output.exists() or output.is_symlink():
        raise SystemExit("manifest output already exists")
    postgres = Path(args.postgres)
    minio = Path(args.minio)
    payload = {
        "schema": SCHEMA,
        "backup_script_contract_version": CONTRACT_VERSION,
        "timestamp": args.timestamp,
        "environment": args.environment,
        "database": args.database,
        "platform_uuid": args.platform_uuid,
        "repository_commit": args.repository_commit,
        "postgres": {
            "filename": postgres.name.removesuffix(".partial"),
            "size_bytes": postgres.stat().st_size,
            "sha256": sha256_file(postgres),
            "validation": {"gzip_test": True, "logical_read": True},
        },
        "minio": {
            "filename": minio.name.removesuffix(".partial"),
            "size_bytes": minio.stat().st_size,
            "sha256": sha256_file(minio),
            "archive_entry_count": args.minio_entry_count,
            "source_file_count": args.minio_source_file_count,
            "validation": {"full_tar_traversal": True, "entry_count": True},
        },
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    output.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.chmod(output, 0o600)


class ManifestVerificationError(Exception):
    """A backup set failed the authoritative validity contract."""


def verify_manifest(
    manifest_path: Path,
    *,
    timestamp: str,
    expected: Mapping[str, str | None] | None = None,
    check_archives: bool = False,
) -> dict[str, Any]:
    """The single authoritative definition of "this backup set is restorable".

    Returns the manifest payload, or raises `ManifestVerificationError` naming the
    first violated rule. `ops/backup.sh verify` and `ops/backup_retention.py` both
    go through here: a second, weaker definition of validity elsewhere is exactly
    how retention came to treat a correctly named but unverified triple as a safe
    survivor anchor.

    Every check is a restorability property:

    * contract/schema — an unreadable or foreign manifest proves nothing;
    * timestamp — the manifest must describe the set it is filed under;
    * identity — environment/database/platform_uuid/repository_commit, when the
      caller pins them, so a backup of another database is never a survivor;
    * **member identity** — each referenced filename must be exactly the name the
      naming contract derives from `timestamp`. Checking only that the basename
      was *safe* let a manifest filed as T2 reference T1's archives with T1's
      correct sizes and hashes: three such sets verified independently, became
      the survivor floor, and retention then deleted the single real backup they
      all pointed at. A set must be self-contained to be a survivor;
    * archive presence, mode, size and **sha256**;
    * `check_archives` — full gzip/tar traversal, matching `verify_pair`.
    """
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ManifestVerificationError(f"unreadable manifest: {type(exc).__name__}") from exc
    if not isinstance(payload, dict):
        raise ManifestVerificationError("manifest is not an object")
    if payload.get("schema") != SCHEMA or payload.get("backup_script_contract_version") != CONTRACT_VERSION:
        raise ManifestVerificationError("unsupported backup manifest contract")
    if payload.get("timestamp") != timestamp:
        raise ManifestVerificationError("manifest timestamp mismatch")
    for field in ("environment", "database", "platform_uuid", "repository_commit"):
        want = (expected or {}).get(field)
        if want is not None and payload.get(field) != want:
            raise ManifestVerificationError(f"manifest {field} mismatch")
    for section in ("postgres", "minio"):
        block = payload.get(section)
        if not isinstance(block, dict):
            raise ManifestVerificationError(f"missing {section} section")
        filename = block.get("filename")
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise ManifestVerificationError(f"unsafe {section} filename")
        required = member_filename(section, timestamp)
        if filename != required:
            raise ManifestVerificationError(
                f"{section} member does not belong to this backup set: "
                f"manifest references {filename!r}, set {timestamp} requires {required!r}"
            )
        path = manifest_path.parent / filename
        if not path.is_file() or path.is_symlink():
            raise ManifestVerificationError(f"missing {section} archive")
        stat = path.stat()
        if stat.st_mode & 0o777 != 0o600:
            raise ManifestVerificationError(f"unsafe {section} archive mode")
        if stat.st_size != block.get("size_bytes"):
            raise ManifestVerificationError(f"{section} size mismatch")
        if sha256_file(path) != block.get("sha256"):
            raise ManifestVerificationError(f"{section} checksum mismatch")
        if check_archives:
            problem = _archive_is_readable(section, path)
            if problem:
                raise ManifestVerificationError(problem)
    if manifest_path.stat().st_mode & 0o777 != 0o600:
        raise ManifestVerificationError("unsafe manifest mode")
    return payload


def verify(args: argparse.Namespace) -> None:
    manifest_path = Path(args.manifest)
    expected = {
        field: getattr(args, f"expected_{field}")
        for field in ("environment", "database", "platform_uuid", "repository_commit")
    }
    try:
        # `--check-archives` is opt-in so `ops/backup.sh verify_pair` keeps its
        # current cost: it already runs `gzip -t` and `tar -tzf` in shell before
        # calling this. Retention has no such shell wrapper and asks for the
        # traversal through the Python API instead.
        payload = verify_manifest(
            manifest_path, timestamp=args.timestamp, expected=expected,
            check_archives=bool(getattr(args, "check_archives", False)),
        )
    except ManifestVerificationError as exc:
        # Preserve the original CLI contract exactly: message on stderr, non-zero
        # exit. `ops/backup.sh` depends on this and is unchanged.
        raise SystemExit(str(exc)) from exc
    print(json.dumps({
        "status": "VALID",
        "timestamp": payload["timestamp"],
        "postgres_size_bytes": payload["postgres"]["size_bytes"],
        "minio_size_bytes": payload["minio"]["size_bytes"],
        "minio_archive_entry_count": payload["minio"]["archive_entry_count"],
    }, sort_keys=True))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    sub = result.add_subparsers(dest="command", required=True)
    make = sub.add_parser("create")
    for name in ("output", "timestamp", "environment", "database", "platform_uuid", "repository_commit", "postgres", "minio"):
        make.add_argument(f"--{name.replace('_', '-')}", required=True)
    make.add_argument("--minio-entry-count", type=int, required=True)
    make.add_argument("--minio-source-file-count", type=int, required=True)
    check = sub.add_parser("verify")
    check.add_argument("--manifest", required=True)
    check.add_argument("--timestamp", required=True)
    check.add_argument("--check-archives", action="store_true",
                       help="Also traverse the gzip/tar archives (backup.sh does this in shell)")
    for name in ("environment", "database", "platform_uuid", "repository_commit"):
        check.add_argument(f"--expected-{name.replace('_', '-')}")
    return result


def main() -> None:
    args = parser().parse_args()
    if args.command == "create":
        create(args)
    else:
        verify(args)


if __name__ == "__main__":
    main()
