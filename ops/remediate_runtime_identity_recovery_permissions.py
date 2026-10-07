#!/usr/bin/env python3
"""Dry-run-first repair of the fixed runtime-identity recovery-root mode."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import socket
import stat
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ops.git_repository_state import RepositoryStateError, require_clean_repository  # noqa: E402
from ops.runtime_identity_recovery import (  # noqa: E402
    DIRECTORY_MODE, FILE_MODE, RecoveryStorageError, default_recovery_root,
    resolve_recovery_owner, sha256_file, validate_preserved_recovery_pair,
    validate_recovery_root,
)


class PermissionRemediationError(RuntimeError):
    def __init__(self, code: str, details: dict[str, object] | None = None) -> None:
        self.code = code
        self.details = details or {}
        super().__init__(code)


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _safe_metadata(path: Path, *, expected_uid: int, expected_gid: int, expected_mode: int) -> dict[str, object]:
    if path.is_symlink() or not path.is_dir():
        raise PermissionRemediationError("RECOVERY_ROOT_UNSAFE", {"path": str(path)})
    metadata = path.lstat()
    if metadata.st_uid != expected_uid or metadata.st_gid != expected_gid:
        raise PermissionRemediationError(
            "RECOVERY_ROOT_OWNER_MISMATCH",
            {"path": str(path), "expected_uid": expected_uid, "expected_gid": expected_gid,
             "actual_uid": metadata.st_uid, "actual_gid": metadata.st_gid},
        )
    actual_mode = stat.S_IMODE(metadata.st_mode)
    if actual_mode != expected_mode:
        raise PermissionRemediationError(
            "RECOVERY_ROOT_CURRENT_MODE_MISMATCH",
            {"path": str(path), "expected_mode": f"{expected_mode:04o}",
             "actual_mode": f"{actual_mode:04o}"},
        )
    return {
        "path": str(path), "owner_uid": metadata.st_uid, "owner_gid": metadata.st_gid,
        "mode": f"{actual_mode:04o}", "type": "directory", "symlink": False,
    }


def _validate_child(path: Path, *, root: Path, owner_uid: int, owner_gid: int, expected_sha256: str) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise PermissionRemediationError("RECOVERY_ARTIFACT_UNSAFE", {"path": str(path)})
    try:
        relative = path.absolute().relative_to(root.absolute())
    except ValueError as exc:
        raise PermissionRemediationError("RECOVERY_ARTIFACT_OUTSIDE_ROOT", {"path": str(path)}) from exc
    current = root
    for part in relative.parts[:-1]:
        current /= part
        metadata = current.lstat()
        if (stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != owner_uid or metadata.st_gid != owner_gid
                or stat.S_IMODE(metadata.st_mode) != DIRECTORY_MODE):
            raise PermissionRemediationError(
                "RECOVERY_CHILD_DIRECTORY_INVALID", {"path": str(current)}
            )
    metadata = path.lstat()
    if (metadata.st_uid != owner_uid or metadata.st_gid != owner_gid
            or stat.S_IMODE(metadata.st_mode) != FILE_MODE):
        raise PermissionRemediationError(
            "RECOVERY_ARTIFACT_METADATA_INVALID", {"path": str(path)}
        )
    digest = sha256_file(path)
    if digest != expected_sha256:
        raise PermissionRemediationError("RECOVERY_ARTIFACT_HASH_MISMATCH", {"path": str(path)})
    return {"path": str(path), "sha256": digest, "mode": "0600"}


def build_plan(args: argparse.Namespace) -> dict[str, object]:
    try:
        state = require_clean_repository(REPO_ROOT)
        owner = resolve_recovery_owner()
    except RepositoryStateError as exc:
        raise PermissionRemediationError(exc.classification, exc.details) from exc
    except RecoveryStorageError as exc:
        raise PermissionRemediationError(exc.code, exc.details) from exc
    if state.head != args.expected_repository_head:
        raise PermissionRemediationError("REPOSITORY_HEAD_MISMATCH")
    if socket.gethostname() != args.expected_host:
        raise PermissionRemediationError("HOST_IDENTITY_MISMATCH")
    root = default_recovery_root(REPO_ROOT)
    root_row = _safe_metadata(
        root, expected_uid=owner.uid, expected_gid=owner.gid,
        expected_mode=int(args.expected_current_mode, 8),
    )
    backup = _validate_child(
        args.preserved_backup.absolute(), root=root, owner_uid=owner.uid, owner_gid=owner.gid,
        expected_sha256=args.expected_preserved_backup_sha256,
    )
    evidence = _validate_child(
        args.recovery_evidence.absolute(), root=root, owner_uid=owner.uid, owner_gid=owner.gid,
        expected_sha256=args.expected_recovery_evidence_sha256,
    )
    try:
        payload = json.loads(args.recovery_evidence.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PermissionRemediationError("RECOVERY_EVIDENCE_INVALID", {"path": str(args.recovery_evidence)}) from exc
    expected_plan_sha256 = args.preserved_backup.parent.name
    if (not isinstance(payload, dict) or payload.get("plan_sha256") != expected_plan_sha256
            or payload.get("preserved_path") != str(args.preserved_backup)
            or payload.get("sha256") != args.expected_preserved_backup_sha256
            or payload.get("source_removed_after_verified_preservation") is not True
            or not isinstance(payload.get("metadata"), dict)
            or payload["metadata"].get("owner") != owner.user
            or payload["metadata"].get("group") != owner.group
            or payload["metadata"].get("mode") != "0600"):
        raise PermissionRemediationError("RECOVERY_EVIDENCE_BINDING_INVALID", {"path": str(args.recovery_evidence)})
    recovery_binding = {"plan_sha256": expected_plan_sha256, "backup": backup, "evidence": evidence}
    return {
        "contract_version": 1, "operation": "remediate_runtime_identity_recovery_permissions",
        "host": args.expected_host, "repository_head": state.head,
        "recovery_root": {**root_row, "target_mode": "0700"},
        "recovery_owner": owner.as_plan(),
        "preserved_recovery_binding": recovery_binding,
        "actions": ["chmod_recovery_root_0700", "verify_recovery_contract"],
        "systemd_changes": False, "daemon_reload": False, "restarts": [],
        "timer_restarts": [], "docker_recreation": False,
    }


def attestation(plan: dict[str, object]) -> str:
    digest = hashlib.sha256(canonical_json(plan).encode()).hexdigest()
    return (f"REMEDIATE_RUNTIME_IDENTITY_RECOVERY_PERMISSIONS host={plan['host']} "
            f"head={plan['repository_head']} plan_sha256={digest} no_systemd=true")


def execution_command(args: argparse.Namespace, required: str) -> str:
    return shlex.join([
        "sudo", str(REPO_ROOT / ".venv/bin/python"), "-I", str(Path(__file__).resolve()),
        "--expected-host", args.expected_host,
        "--expected-repository-head", args.expected_repository_head,
        "--expected-current-mode", args.expected_current_mode,
        "--preserved-backup", str(args.preserved_backup),
        "--expected-preserved-backup-sha256", args.expected_preserved_backup_sha256,
        "--recovery-evidence", str(args.recovery_evidence),
        "--expected-recovery-evidence-sha256", args.expected_recovery_evidence_sha256,
        "--execute", "--attestation", required,
    ])


def execute(args: argparse.Namespace, plan: dict[str, object]) -> dict[str, object]:
    if os.geteuid() != 0:
        raise PermissionRemediationError("ROOT_REQUIRED")
    if build_plan(args) != plan:
        raise PermissionRemediationError("PERMISSION_REMEDIATION_PLAN_CHANGED")
    try:
        owner = resolve_recovery_owner()
    except RecoveryStorageError as exc:
        raise PermissionRemediationError(exc.code, exc.details) from exc
    root = default_recovery_root(REPO_ROOT)
    _safe_metadata(
        root, expected_uid=owner.uid, expected_gid=owner.gid,
        expected_mode=int(args.expected_current_mode, 8),
    )
    os.chmod(root, DIRECTORY_MODE)
    parent_fd = os.open(root.parent, os.O_RDONLY)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    try:
        validate_recovery_root(root, repository_root=REPO_ROOT, owner=owner, phase="permission_remediation")
        validate_preserved_recovery_pair(
            evidence_path=args.recovery_evidence, backup_path=args.preserved_backup, root=root,
            repository_root=REPO_ROOT, owner=owner,
            expected_evidence_sha256=args.expected_recovery_evidence_sha256,
            expected_backup_sha256=args.expected_preserved_backup_sha256,
        )
    except RecoveryStorageError as exc:
        raise PermissionRemediationError(exc.code, exc.details) from exc
    return {
        "writes_performed": True, "changed_paths": [str(root)],
        "recovery_root_mode": "0700", "systemd_changes": False,
        "daemon_reload": False, "restarts": [], "docker_recreation": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-host", required=True)
    parser.add_argument("--expected-repository-head", required=True)
    parser.add_argument("--expected-current-mode", choices=("0755",), required=True)
    parser.add_argument("--preserved-backup", type=Path, required=True)
    parser.add_argument("--expected-preserved-backup-sha256", required=True)
    parser.add_argument("--recovery-evidence", type=Path, required=True)
    parser.add_argument("--expected-recovery-evidence-sha256", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--attestation")
    args = parser.parse_args(argv)
    plan = build_plan(args)
    digest = hashlib.sha256(canonical_json(plan).encode()).hexdigest()
    required = attestation(plan)
    output = {
        "mode": "execute" if args.execute else "dry_run", "writes_performed": False,
        "plan": plan, "plan_sha256": digest, "required_attestation": required,
        "execution_command": execution_command(args, required),
    }
    if not args.execute:
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0
    if args.attestation != required:
        raise PermissionRemediationError("ATTESTATION_MISMATCH")
    output["execution"] = execute(args, plan)
    output["writes_performed"] = True
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PermissionRemediationError as exc:
        print(json.dumps({"classification": exc.code, "writes_performed": False, **exc.details}, indent=2, sort_keys=True))
        raise SystemExit(2)
