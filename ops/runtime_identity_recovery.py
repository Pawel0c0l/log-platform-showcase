"""Restricted external recovery storage for runtime identity operations."""
from __future__ import annotations

import grp
import hashlib
import json
import os
import pwd
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

DEFAULT_RECOVERY_USER = "logplatform"
DIRECTORY_MODE = 0o700
FILE_MODE = 0o600


class RecoveryStorageError(RuntimeError):
    def __init__(self, code: str, details: dict[str, object] | None = None) -> None:
        self.code = code
        self.details = details or {}
        super().__init__(code)


@dataclass(frozen=True)
class RecoveryOwner:
    user: str
    group: str
    uid: int
    gid: int

    def as_plan(self) -> dict[str, object]:
        return {"user": self.user, "group": self.group, "uid": self.uid, "gid": self.gid}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def default_recovery_root(repository_root: Path) -> Path:
    return repository_root.resolve().parent / f"{repository_root.name}-runtime-identity-recovery"


def resolve_recovery_owner(
    service_user: str = DEFAULT_RECOVERY_USER,
    *,
    environ: Mapping[str, str] | None = None,
    real_uid: int | None = None,
    effective_uid: int | None = None,
) -> RecoveryOwner:
    try:
        account = pwd.getpwnam(service_user)
        group = grp.getgrnam(service_user)
    except KeyError as exc:
        raise RecoveryStorageError(
            "RECOVERY_OWNER_UNRESOLVED", {"expected_user": service_user}
        ) from exc
    owner = RecoveryOwner(service_user, group.gr_name, account.pw_uid, account.pw_gid)
    env = os.environ if environ is None else environ
    ruid = os.getuid() if real_uid is None else real_uid
    euid = os.geteuid() if effective_uid is None else effective_uid
    sudo_values = {key: env.get(key) for key in ("SUDO_USER", "SUDO_UID", "SUDO_GID")}
    present = [value is not None for value in sudo_values.values()]
    if euid == 0 and any(present):
        valid = all(present)
        try:
            sudo_uid = int(sudo_values["SUDO_UID"] or "")
            sudo_gid = int(sudo_values["SUDO_GID"] or "")
        except ValueError:
            valid = False
            sudo_uid = sudo_gid = -1
        if (not valid or sudo_values["SUDO_USER"] != owner.user
                or sudo_uid != owner.uid or sudo_gid != owner.gid):
            raise RecoveryStorageError(
                "RECOVERY_SUDO_ORIGIN_INVALID",
                {
                    "expected_user": owner.user,
                    "expected_uid": owner.uid,
                    "expected_gid": owner.gid,
                    "actual_user": sudo_values["SUDO_USER"],
                    "actual_uid": sudo_uid,
                    "actual_gid": sudo_gid,
                    "real_uid": ruid,
                    "effective_uid": euid,
                },
            )
    elif euid != 0 and (ruid != owner.uid or euid != owner.uid):
        raise RecoveryStorageError(
            "RECOVERY_OPERATOR_IDENTITY_INVALID",
            {
                "expected_uid": owner.uid,
                "real_uid": ruid,
                "effective_uid": euid,
            },
        )
    return owner


def execution_id(*, operation: str, repository_head: str, checkpoint_sha256: str) -> str:
    raw = f"{operation}\0{repository_head}\0{checkpoint_sha256}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _assert_no_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise RecoveryStorageError(
                "RECOVERY_PATH_SYMLINK", {"path": str(current)}
            )


def _metadata_details(
    path: Path, metadata: os.stat_result, *, owner: RecoveryOwner, expected_mode: int, phase: str
) -> dict[str, object]:
    return {
        "path": str(path),
        "phase": phase,
        "expected_uid": owner.uid,
        "expected_gid": owner.gid,
        "expected_mode": f"{expected_mode:04o}",
        "actual_uid": metadata.st_uid,
        "actual_gid": metadata.st_gid,
        "actual_mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
        "real_uid": os.getuid(),
        "effective_uid": os.geteuid(),
    }


def validate_recovery_root(
    path: Path, *, repository_root: Path, owner: RecoveryOwner, phase: str = "recovery_root"
) -> Path:
    root = path.absolute()
    repo = repository_root.resolve()
    if root == Path("/"):
        raise RecoveryStorageError("RECOVERY_ROOT_INVALID", {"path": str(root), "phase": phase})
    _assert_no_symlink_components(root)
    try:
        root.resolve(strict=False).relative_to(repo)
    except ValueError:
        pass
    else:
        raise RecoveryStorageError(
            "RECOVERY_ROOT_INSIDE_WORKTREE", {"path": str(root), "phase": phase}
        )
    if root.exists():
        metadata = root.lstat()
        details = _metadata_details(root, metadata, owner=owner, expected_mode=DIRECTORY_MODE, phase=phase)
        if not stat.S_ISDIR(metadata.st_mode):
            raise RecoveryStorageError("RECOVERY_ROOT_NOT_DIRECTORY", details)
        if metadata.st_uid != owner.uid or metadata.st_gid != owner.gid:
            raise RecoveryStorageError("RECOVERY_ROOT_OWNER_MISMATCH", details)
        if stat.S_IMODE(metadata.st_mode) != DIRECTORY_MODE:
            raise RecoveryStorageError("RECOVERY_ROOT_PERMISSIONS", details)
    return root


def validate_recovery_artifact(
    path: Path, *, root: Path, repository_root: Path, owner: RecoveryOwner,
    expected_sha256: str | None = None, phase: str = "recovery_artifact"
) -> Path:
    validated_root = validate_recovery_root(
        root, repository_root=repository_root, owner=owner, phase=phase
    )
    candidate = path.absolute()
    _assert_no_symlink_components(candidate)
    try:
        relative = candidate.relative_to(validated_root)
    except ValueError as exc:
        raise RecoveryStorageError(
            "RECOVERY_ARTIFACT_OUTSIDE_ROOT", {"path": str(candidate), "phase": phase}
        ) from exc
    current = validated_root
    for part in relative.parts[:-1]:
        current /= part
        metadata = current.lstat()
        details = _metadata_details(
            current, metadata, owner=owner, expected_mode=DIRECTORY_MODE, phase=phase
        )
        if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != owner.uid
                or metadata.st_gid != owner.gid or stat.S_IMODE(metadata.st_mode) != DIRECTORY_MODE):
            raise RecoveryStorageError("RECOVERY_DIRECTORY_METADATA_INVALID", details)
    metadata = candidate.lstat()
    details = _metadata_details(candidate, metadata, owner=owner, expected_mode=FILE_MODE, phase=phase)
    if not stat.S_ISREG(metadata.st_mode):
        raise RecoveryStorageError("RECOVERY_ARTIFACT_NOT_REGULAR", details)
    if metadata.st_uid != owner.uid or metadata.st_gid != owner.gid:
        raise RecoveryStorageError("RECOVERY_ARTIFACT_OWNER_MISMATCH", details)
    if stat.S_IMODE(metadata.st_mode) != FILE_MODE:
        raise RecoveryStorageError("RECOVERY_ARTIFACT_PERMISSIONS", details)
    if expected_sha256 is not None and sha256_file(candidate) != expected_sha256:
        raise RecoveryStorageError(
            "RECOVERY_ARTIFACT_HASH_MISMATCH", {"path": str(candidate), "phase": phase}
        )
    return candidate


def _atomic_write(
    path: Path, raw: bytes, *, mode: int, owner: RecoveryOwner, replace: bool = False
) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RecoveryStorageError("RECOVERY_DESTINATION_UNSAFE", {"path": str(path)})
    if path.exists() and not replace:
        metadata = path.stat()
        if (path.read_bytes() != raw or stat.S_IMODE(metadata.st_mode) != mode
                or metadata.st_uid != owner.uid or metadata.st_gid != owner.gid):
            raise RecoveryStorageError("RECOVERY_DESTINATION_CONFLICT", {"path": str(path)})
        return
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        os.fchown(fd, owner.uid, owner.gid)
        view = memoryview(raw)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise RecoveryStorageError("RECOVERY_WRITE_FAILED", {"path": str(path)})
            view = view[count:]
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(temporary, path)
        _fsync_directory(path.parent)
        metadata = path.stat()
        if (not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != mode
                or metadata.st_uid != owner.uid or metadata.st_gid != owner.gid
                or path.read_bytes() != raw):
            raise RecoveryStorageError(
                "RECOVERY_DESTINATION_VERIFICATION_FAILED", {"path": str(path)}
            )
    finally:
        if fd >= 0:
            os.close(fd)
        Path(temporary).unlink(missing_ok=True)


class RecoveryStore:
    def __init__(
        self, *, root: Path, repository_root: Path, operation: str, plan_sha256: str,
        repository_head: str, checkpoint: Path, checkpoint_sha256: str, owner: RecoveryOwner
    ) -> None:
        self.repository_root = repository_root.resolve()
        self.owner = owner
        self.root = validate_recovery_root(
            root, repository_root=self.repository_root, owner=owner, phase="store_initialization"
        )
        self.operation = operation
        self.plan_sha256 = plan_sha256
        self.repository_head = repository_head
        self.checkpoint = checkpoint.absolute()
        self.checkpoint_sha256 = checkpoint_sha256
        self.execution_id = execution_id(
            operation=operation, repository_head=repository_head, checkpoint_sha256=checkpoint_sha256
        )
        self.directory = self.root / operation / repository_head[:12] / self.execution_id
        self.manifest_path = self.directory / "recovery-evidence.json"
        self.backups: list[dict[str, object]] = []

    def prepare(self) -> None:
        for directory in (
            self.root, self.root / self.operation,
            self.root / self.operation / self.repository_head[:12], self.directory,
        ):
            _assert_no_symlink_components(directory)
            if directory.exists():
                metadata = directory.lstat()
                details = _metadata_details(
                    directory, metadata, owner=self.owner, expected_mode=DIRECTORY_MODE,
                    phase="store_prepare",
                )
                if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != self.owner.uid
                        or metadata.st_gid != self.owner.gid
                        or stat.S_IMODE(metadata.st_mode) != DIRECTORY_MODE):
                    raise RecoveryStorageError("RECOVERY_DIRECTORY_METADATA_INVALID", details)
            else:
                directory.mkdir(mode=DIRECTORY_MODE)
                os.chown(directory, self.owner.uid, self.owner.gid)
                os.chmod(directory, DIRECTORY_MODE)
                _fsync_directory(directory.parent)
        validate_recovery_root(
            self.root, repository_root=self.repository_root, owner=self.owner, phase="store_prepared"
        )

    def preserve(self, actual_path: Path, *, logical_path: Path) -> dict[str, object] | None:
        if not actual_path.exists():
            return None
        if actual_path.is_symlink() or not actual_path.is_file():
            raise RecoveryStorageError("RECOVERY_SOURCE_UNSAFE", {"path": str(actual_path)})
        raw = actual_path.read_bytes()
        source_hash = hashlib.sha256(raw).hexdigest()
        token = hashlib.sha256(str(logical_path).encode()).hexdigest()[:12]
        destination = self.directory / f"{logical_path.name}.{token}.bak"
        _atomic_write(destination, raw, mode=FILE_MODE, owner=self.owner)
        if destination.read_bytes() != raw or sha256_file(destination) != source_hash:
            raise RecoveryStorageError("RECOVERY_VERIFICATION_FAILED", {"path": str(destination)})
        entry = {
            "logical_source": str(logical_path), "source_path": str(actual_path),
            "source_sha256": source_hash, "backup_path": str(destination),
            "backup_sha256": source_hash, "mode": "0600", "size": len(raw),
            "owner_uid": self.owner.uid, "owner_gid": self.owner.gid,
        }
        self.backups.append(entry)
        return entry

    def write_evidence(self, *, state: str) -> dict[str, object]:
        payload = {
            "schema_version": 2, "operation": self.operation,
            "execution_id": self.execution_id, "plan_sha256": self.plan_sha256,
            "repository_head": self.repository_head,
            "checkpoint_reference": str(self.checkpoint),
            "checkpoint_sha256": self.checkpoint_sha256,
            "recovery_owner": self.owner.as_plan(),
            "directory_mode": "0700", "file_mode": "0600",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "state": state, "backups": self.backups,
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        _atomic_write(
            self.manifest_path, raw, mode=FILE_MODE, owner=self.owner, replace=True
        )
        if sha256_file(self.manifest_path) != hashlib.sha256(raw).hexdigest():
            raise RecoveryStorageError("RECOVERY_EVIDENCE_INVALID", {"path": str(self.manifest_path)})
        return {
            "directory": str(self.directory), "evidence_path": str(self.manifest_path),
            "evidence_sha256": sha256_file(self.manifest_path), "backups": self.backups,
            "rollback_ready": True, "state": state, "owner": self.owner.as_plan(),
        }


def validate_preserved_recovery_pair(
    *, evidence_path: Path, backup_path: Path, root: Path, repository_root: Path,
    owner: RecoveryOwner, expected_evidence_sha256: str, expected_backup_sha256: str
) -> dict[str, object]:
    validate_recovery_artifact(
        evidence_path, root=root, repository_root=repository_root, owner=owner,
        expected_sha256=expected_evidence_sha256, phase="preserved_recovery_evidence",
    )
    validate_recovery_artifact(
        backup_path, root=root, repository_root=repository_root, owner=owner,
        expected_sha256=expected_backup_sha256, phase="preserved_recovery_backup",
    )
    try:
        payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RecoveryStorageError(
            "RECOVERY_EVIDENCE_INVALID", {"path": str(evidence_path)}
        ) from exc
    if not isinstance(payload, dict):
        raise RecoveryStorageError("RECOVERY_EVIDENCE_INVALID", {"path": str(evidence_path)})
    plan_sha256 = payload.get("plan_sha256")
    expected_plan_sha256 = backup_path.parent.name
    if (not isinstance(plan_sha256, str) or len(plan_sha256) != 64
            or plan_sha256 != expected_plan_sha256):
        raise RecoveryStorageError(
            "RECOVERY_EVIDENCE_PLAN_MISMATCH",
            {"path": str(evidence_path), "expected_plan_sha256": expected_plan_sha256},
        )
    legacy_path = payload.get("preserved_path")
    if legacy_path is not None:
        valid = (
            legacy_path == str(backup_path)
            and payload.get("sha256") == expected_backup_sha256
            and payload.get("size") == backup_path.stat().st_size
            and payload.get("source_removed_after_verified_preservation") is True
            and isinstance(payload.get("metadata"), dict)
            and payload["metadata"].get("owner") == owner.user
            and payload["metadata"].get("group") == owner.group
            and payload["metadata"].get("mode") == "0600"
        )
    else:
        rows = payload.get("backups")
        matches = [
            row for row in rows if isinstance(row, dict) and row.get("backup_path") == str(backup_path)
        ] if isinstance(rows, list) else []
        valid = (
            len(matches) == 1
            and matches[0].get("backup_sha256") == expected_backup_sha256
            and matches[0].get("mode") == "0600"
        )
    if not valid:
        raise RecoveryStorageError(
            "RECOVERY_EVIDENCE_BACKUP_MISMATCH", {"path": str(evidence_path)}
        )
    return {
        "plan_sha256": plan_sha256, "backup_path": str(backup_path),
        "backup_sha256": expected_backup_sha256, "evidence_path": str(evidence_path),
        "evidence_sha256": expected_evidence_sha256,
    }


def discover_backup(
    evidence_path: Path, *, logical_source: Path, root: Path | None = None,
    repository_root: Path | None = None, owner: RecoveryOwner | None = None
) -> Path:
    if root is not None and repository_root is not None and owner is not None:
        validate_recovery_artifact(
            evidence_path, root=root, repository_root=repository_root, owner=owner,
            phase="rollback_evidence",
        )
    elif evidence_path.is_symlink() or not evidence_path.is_file():
        raise RecoveryStorageError("RECOVERY_EVIDENCE_INVALID", {"path": str(evidence_path)})
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    matches = [
        row for row in payload.get("backups", [])
        if row.get("logical_source") == str(logical_source)
    ]
    if len(matches) != 1:
        raise RecoveryStorageError("RECOVERY_BACKUP_NOT_UNIQUE")
    backup = Path(matches[0]["backup_path"])
    if root is not None and repository_root is not None and owner is not None:
        validate_recovery_artifact(
            backup, root=root, repository_root=repository_root, owner=owner,
            expected_sha256=str(matches[0]["backup_sha256"]), phase="rollback_backup",
        )
    elif (backup.is_symlink() or not backup.is_file()
          or sha256_file(backup) != matches[0]["backup_sha256"]):
        raise RecoveryStorageError("RECOVERY_BACKUP_INVALID", {"path": str(backup)})
    return backup
