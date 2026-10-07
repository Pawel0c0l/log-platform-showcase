#!/usr/bin/env python3
"""Dry-run-first provisioning of the canonical runtime identity contract."""
from __future__ import annotations

import argparse
import grp
import hashlib
import json
import os
import pwd
import re
import shlex
import socket
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ops.environment_identity_file import (  # noqa: E402
    CANONICAL_IDENTITY_FILE, HELPER_LIBRARY_INSTALL_PATH, IDENTITY_KEY,
    identity_assignments_in_env_file, inspect_identity_file, intended_checksum,
    render_identity_file, sha256_bytes, validate_environment,
)
from ops.git_repository_state import (  # noqa: E402
    RepositoryState, RepositoryStateError, require_clean_repository,
)
from ops.release_boundary import (  # noqa: E402
    INSTALLED_WRAPPER_PATH,
    wrapper_replaceability,
)
from ops.wrapper_install_lock import wrapper_install_lock  # noqa: E402
from ops.runtime_identity_inspection import RootInspectionError, inspect_root_sources  # noqa: E402
from ops.runtime_identity_recovery import (  # noqa: E402
    RecoveryStorageError, RecoveryStore, default_recovery_root, execution_id,
    resolve_recovery_owner, sha256_file, validate_recovery_root,
)
from ops.systemd_environment_files import (  # noqa: E402
    canonical_source_effective, merged_dropin_text,
)


class ProvisioningError(RuntimeError):
    def __init__(self, code: str, details: dict[str, object] | None = None) -> None:
        self.code = code; self.details = details or {}; super().__init__(code)


class PartialStateError(ProvisioningError):
    pass


API_DROPIN_TARGET = Path("/etc/systemd/system/log-platform-api.service.d/zz-environment-identity.conf")
OBSOLETE_API_DROPIN = Path("/etc/systemd/system/log-platform-api.service.d/90-environment-identity.conf")
SOURCE_TARGETS = (
    (REPO_ROOT / "ops/environment_identity_file.py", HELPER_LIBRARY_INSTALL_PATH, 0o644),
    (REPO_ROOT / "ops/systemd/proposed/log-platform-environment-identity-helper", Path("/usr/local/sbin/log-platform-environment-identity-helper"), 0o755),
    (REPO_ROOT / "ops/systemd/proposed/log-platform-environment-identity.sudoers", Path("/etc/sudoers.d/log-platform-environment-identity"), 0o440),
    (REPO_ROOT / "ops/systemd/proposed/log-job-runner.sh", Path("/usr/local/bin/log-job-runner.sh"), 0o755),
    (REPO_ROOT / "ops/systemd/proposed/log-platform-api.service.d/zz-environment-identity.conf", API_DROPIN_TARGET, 0o644),
    (REPO_ROOT / "ops/systemd/proposed/log-platform-prune.service.d/90-environment-identity.conf", Path("/etc/systemd/system/log-platform-prune.service.d/90-environment-identity.conf"), 0o644),
    (REPO_ROOT / "ops/systemd/proposed/log-backup.service.d/90-environment-identity.conf", Path("/etc/systemd/system/log-backup.service.d/90-environment-identity.conf"), 0o644),
)
CONFLICT_SOURCES = (
    REPO_ROOT / ".env", Path("/etc/log-platform-host.env"),
    Path("/etc/log-platform/runtime.env"), Path("/etc/log-platform/backup.env"),
)
ROOT_OWNED_CONFLICT_SOURCES = frozenset(CONFLICT_SOURCES[1:])
RELOAD_ACTIONS = (
    "systemctl daemon-reload", "systemctl restart log-platform-api.service",
    "docker compose -f docker-compose.yml up -d --no-deps --force-recreate api",
)


def _rooted(path: Path, root: Path) -> Path:
    return root / path.relative_to("/") if path.is_absolute() and root != Path("/") else path


def _execution_repository_root(root: Path) -> Path:
    return REPO_ROOT if root == Path("/") else _rooted(REPO_ROOT, root)


def _repository_state(args: argparse.Namespace) -> RepositoryState:
    try: state = require_clean_repository(REPO_ROOT)
    except RepositoryStateError as exc: raise ProvisioningError(exc.classification, exc.details) from exc
    if state.head != args.expected_repository_head:
        raise ProvisioningError("REPOSITORY_HEAD_MISMATCH", {"repository_root": str(state.root), "head": state.head, "branch": state.branch})
    return state


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _validate_backup_reference(path: Path) -> str:
    if path.is_symlink() or not path.is_file(): raise ProvisioningError("BACKUP_REFERENCE_INVALID")
    try: payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc: raise ProvisioningError("BACKUP_REFERENCE_INVALID") from exc
    if not isinstance(payload, dict) or (payload.get("verified") is not True and not payload.get("validation")):
        raise ProvisioningError("BACKUP_REFERENCE_NOT_VERIFIED")
    return sha256_file(path)


def _plan(args: argparse.Namespace, repository_state: RepositoryState) -> dict[str, object]:
    environment = validate_environment(args.expected_current_environment)
    if socket.gethostname() != args.expected_host: raise ProvisioningError("HOST_IDENTITY_MISMATCH")
    root = args.root.absolute(); conflicts = []
    for logical in (path for path in CONFLICT_SOURCES if path not in ROOT_OWNED_CONFLICT_SOURCES):
        values = identity_assignments_in_env_file(_rooted(logical, root))
        if len(values) > 1: raise ProvisioningError(f"DUPLICATE_IDENTITY_DEFINITION:{logical}")
        if values:
            if values[0] != environment: raise ProvisioningError(f"SOURCE_ENVIRONMENT_MISMATCH:{logical}")
            conflicts.append({"path": str(logical), "current_environment": values[0], "action": "remove_identity_assignment"})
    privileged_paths = {str(path) for path in CONFLICT_SOURCES if path in ROOT_OWNED_CONFLICT_SOURCES}
    if privileged_paths:
        try: inspection = inspect_root_sources()
        except RootInspectionError as exc:
            raise ProvisioningError(exc.classification, {"reason_code": exc.reason_code, **exc.details}) from exc
        rows = {str(row["path"]): row for row in inspection["sources"]}
        if set(rows) != privileged_paths: raise ProvisioningError("ROOT_IDENTITY_INSPECTION_FAILED", {"reason_code": "SOURCE_SCOPE_MISMATCH"})
        for logical in (path for path in CONFLICT_SOURCES if path in ROOT_OWNED_CONFLICT_SOURCES):
            row = rows[str(logical)]
            if row["has_active_assignment"]:
                if row["canonical_value"] != environment: raise ProvisioningError(f"SOURCE_ENVIRONMENT_MISMATCH:{logical}")
                conflicts.append({"path": str(logical), "current_environment": row["canonical_value"], "action": "remove_identity_assignment", "inspection_sha256": row["sha256"]})
    # SOURCE_TARGETS holds the development-tree wrapper as the expected content
    # of /usr/local/bin/log-job-runner.sh, so this routine will overwrite
    # whatever is installed there. Only two states are safe to overwrite: no
    # wrapper at all, and a wrapper that is conclusively the development one.
    #
    # Everything else fails closed, including a wrapper that merely looks
    # unfamiliar. Hash equality alone is too brittle to carry this decision: any
    # later commit touching the release wrapper changes its bytes, so a valid
    # *older* release wrapper on the host would otherwise read as "unknown" —
    # and treating unknown as replaceable is precisely how a live production
    # boundary disappears during routine identity maintenance, with every
    # release pointer still looking healthy afterwards.
    installed_wrapper = _rooted(INSTALLED_WRAPPER_PATH, root)
    decision = wrapper_replaceability(repo_root=REPO_ROOT, installed_wrapper=installed_wrapper)
    if not decision["replaceable"]:
        raise ProvisioningError(str(decision["classification"]), {
            "installed_wrapper": str(installed_wrapper),
            "installed_wrapper_variant": decision["variant"],
            "reason": decision["reason"],
            "remediation": "see docs/07_operations.md -> Release boundary before re-provisioning identity",
        })
    assets = []
    for source, target, mode in SOURCE_TARGETS:
        assets.append({"source": str(source.relative_to(REPO_ROOT)), "target": str(target), "owner": "root", "group": "root", "mode": f"{mode:04o}", "sha256": sha256_bytes(source.read_bytes())})
    checkpoint_sha256 = _validate_backup_reference(args.backup_reference)
    recovery_root = Path(getattr(args, "recovery_root", None) or default_recovery_root(REPO_ROOT))
    try:
        owner = resolve_recovery_owner() if root == Path("/") else resolve_recovery_owner(
            real_uid=os.getuid(), effective_uid=os.geteuid()
        )
        actual_recovery_root = recovery_root if root == Path("/") else _rooted(recovery_root, root)
        validate_recovery_root(actual_recovery_root, repository_root=_execution_repository_root(root),
                               owner=owner, phase="provisioning_plan")
    except RecoveryStorageError as exc:
        raise ProvisioningError(exc.code, exc.details) from exc
    operation_id = execution_id(operation="provision", repository_head=repository_state.head, checkpoint_sha256=checkpoint_sha256)
    return {
        "contract_version": 2, "host": args.expected_host, "repository_head": repository_state.head,
        "environment": environment, "canonical_identity_file": str(CANONICAL_IDENTITY_FILE),
        "canonical_identity_sha256": intended_checksum(environment), "canonical_owner": "root",
        "canonical_group": "logplatform", "canonical_mode": "0640",
        "conflicting_declarations": conflicts, "install_assets": assets,
        "obsolete_api_dropin": str(OBSOLETE_API_DROPIN),
        "docker_contract": "docker-compose.yml canonical env_file; recreate only api",
        "daemon_reload": True, "required_reload_actions": list(RELOAD_ACTIONS),
        "timer_restarts_required": False,
        "backup_reference": str(args.backup_reference.absolute()), "backup_reference_sha256": checkpoint_sha256,
        "recovery": {"root": str(recovery_root.absolute()), "operation": "provision", "execution_id": operation_id,
                     "owner": owner.as_plan(), "directory_mode": "0700", "file_mode": "0600", "outside_worktree": True},
        "post_write_validation": ["clean_worktree", "canonical_identity", "legacy_assignments_absent",
                                  "asset_hashes_and_metadata", "effective_systemd_environment_files", "recovery_evidence"],
    }


def _attestation(plan: dict[str, object]) -> str:
    digest = hashlib.sha256(_canonical_json(plan).encode()).hexdigest()
    return f"PROVISION_RUNTIME_ENVIRONMENT_IDENTITY host={plan['host']} head={plan['repository_head']} environment={plan['environment']} plan_sha256={digest}"


def _execution_command(args: argparse.Namespace, attestation: str) -> str:
    return shlex.join(["sudo", str(REPO_ROOT / ".venv/bin/python"), "-I", str(REPO_ROOT / "ops/provision_runtime_environment_identity.py"),
                       "--expected-host", args.expected_host, "--expected-repository-head", args.expected_repository_head,
                       "--expected-current-environment", args.expected_current_environment, "--backup-reference", str(args.backup_reference),
                       "--execute", "--attestation", attestation])


def _write_all(fd: int, raw: bytes) -> None:
    view = memoryview(raw)
    while view:
        written = os.write(fd, view)
        if written <= 0: raise ProvisioningError("FILE_WRITE_DID_NOT_PROGRESS")
        view = view[written:]


def _atomic_write(path: Path, raw: bytes, mode: int, *, target_uid: int | None = None, target_gid: int | None = None) -> None:
    if path.is_symlink(): raise ProvisioningError(f"SYMLINK_REJECTED:{path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.lstat() if path.exists() else None
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        os.fchown(fd, target_uid if target_uid is not None else (existing.st_uid if existing else os.geteuid()),
                  target_gid if target_gid is not None else (existing.st_gid if existing else os.getegid()))
        _write_all(fd, raw); os.fsync(fd); os.close(fd); fd = -1
        os.replace(name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(directory_fd)
        finally: os.close(directory_fd)
    finally:
        if fd >= 0: os.close(fd)
        Path(name).unlink(missing_ok=True)


def _without_identity(path: Path) -> bytes:
    raw = path.read_text(encoding="utf-8").splitlines(keepends=True); kept = []; removed = 0
    assignment = re.compile(rf"^\s*(?:export\s+)?{re.escape(IDENTITY_KEY)}\s*=")
    for line in raw:
        if line.strip() and not line.lstrip().startswith("#") and assignment.match(line): removed += 1
        else: kept.append(line)
    if removed != 1: raise ProvisioningError(f"IDENTITY_DECLARATION_CHANGED:{path}")
    return "".join(kept).encode("utf-8")


def _systemd_effective_text(root: Path) -> str:
    if root == Path("/"):
        result = subprocess.run(["systemctl", "cat", "log-platform-api.service"], check=True, capture_output=True, text=True, timeout=10)
        return result.stdout
    return merged_dropin_text(_rooted(Path("/etc/systemd/system/log-platform-api.service.d"), root))


def _validate_post_write(args: argparse.Namespace, plan: dict[str, object], recovery: dict[str, object]) -> dict[str, object]:
    root = args.root.absolute(); expected_uid = 0 if root == Path("/") else os.geteuid()
    expected_gid = grp.getgrnam("logplatform").gr_gid if root == Path("/") else os.getegid()
    state = inspect_identity_file(_rooted(CANONICAL_IDENTITY_FILE, root), expected_uid=expected_uid, expected_gid=expected_gid, expected_mode=0o640)
    if state.environment != plan["environment"] or state.checksum != plan["canonical_identity_sha256"]:
        raise ProvisioningError("POST_WRITE_CANONICAL_IDENTITY_INVALID")
    for logical in CONFLICT_SOURCES:
        actual = _rooted(logical, root)
        if actual.exists() and identity_assignments_in_env_file(actual):
            raise ProvisioningError(f"POST_WRITE_LEGACY_IDENTITY_REMAINS:{logical}")
    for row in plan["install_assets"]:
        target = _rooted(Path(str(row["target"])), root)
        if target.is_symlink() or not target.is_file() or sha256_file(target) != row["sha256"]:
            raise ProvisioningError(f"POST_WRITE_ASSET_INVALID:{row['target']}")
        metadata = target.stat()
        target_uid = 0 if root == Path("/") else os.geteuid(); target_gid = 0 if root == Path("/") else os.getegid()
        if stat.S_IMODE(metadata.st_mode) != int(str(row["mode"]), 8) or metadata.st_uid != target_uid or metadata.st_gid != target_gid:
            raise ProvisioningError(f"POST_WRITE_ASSET_METADATA_INVALID:{row['target']}")
    if not canonical_source_effective(_systemd_effective_text(root)):
        raise ProvisioningError("POST_WRITE_SYSTEMD_CANONICAL_SOURCE_INEFFECTIVE")
    evidence = Path(str(recovery["evidence_path"]))
    if evidence.is_symlink() or not evidence.is_file() or stat.S_IMODE(evidence.stat().st_mode) != 0o600:
        raise ProvisioningError("POST_WRITE_RECOVERY_EVIDENCE_INVALID")
    execution_repo = _execution_repository_root(root)
    artifacts = [str(path.relative_to(execution_repo)) for path in execution_repo.rglob("*.runtime-identity-*.bak")]
    if artifacts: raise ProvisioningError("POST_WRITE_WORKTREE_BACKUP_ARTIFACT", {"paths": artifacts[:20]})
    if root == Path("/"):
        try: require_clean_repository(REPO_ROOT)
        except RepositoryStateError as exc: raise ProvisioningError("POST_WRITE_REPOSITORY_DIRTY", exc.details) from exc
    return {"canonical_identity": "valid", "legacy_assignments": 0, "assets": "verified",
            "systemd_canonical_source": "effective", "recovery_evidence": "verified", "worktree": "clean"}


def _execute(args: argparse.Namespace, plan: dict[str, object]) -> dict[str, object]:
    if os.geteuid() != 0 and args.root == Path("/"): raise ProvisioningError("ROOT_REQUIRED")
    planned_assets = {str(row["target"]): row for row in plan["install_assets"]}; verified_assets = []
    for source, target, mode in SOURCE_TARGETS:
        raw = source.read_bytes(); planned = planned_assets.get(str(target))
        if not planned or planned.get("source") != str(source.relative_to(REPO_ROOT)) or planned.get("owner") != "root" or planned.get("group") != "root" or planned.get("mode") != f"{mode:04o}" or planned.get("sha256") != sha256_bytes(raw):
            raise ProvisioningError(f"INSTALL_ASSET_PLAN_MISMATCH:{target}")
        verified_assets.append((raw, target, mode))
    if len(verified_assets) != len(plan["install_assets"]): raise ProvisioningError("INSTALL_ASSET_SCOPE_MISMATCH")
    root = args.root.absolute(); owner = resolve_recovery_owner()
    recovery_root = Path(str(plan["recovery"]["root"])) if root == Path("/") else _rooted(Path(str(plan["recovery"]["root"])), root)
    try:
        store = RecoveryStore(root=recovery_root, repository_root=_execution_repository_root(root), operation="provision",
                              plan_sha256=hashlib.sha256(_canonical_json(plan).encode()).hexdigest(), repository_head=str(plan["repository_head"]),
                              checkpoint=args.backup_reference, checkpoint_sha256=str(plan["backup_reference_sha256"]), owner=owner)
        store.prepare()
        store.preserve(_rooted(CANONICAL_IDENTITY_FILE, root), logical_path=CANONICAL_IDENTITY_FILE)
        for row in plan["conflicting_declarations"]:
            logical = Path(str(row["path"])); store.preserve(_rooted(logical, root), logical_path=logical)
        for _, target, _ in SOURCE_TARGETS: store.preserve(_rooted(target, root), logical_path=target)
        prepared = store.write_evidence(state="prepared")
    except RecoveryStorageError as exc: raise ProvisioningError(exc.code, exc.details) from exc
    completed: list[str] = ["recovery_verified"]
    try:
        canonical = _rooted(CANONICAL_IDENTITY_FILE, root)
        if canonical.exists() and inspect_identity_file(canonical).environment != plan["environment"]:
            raise ProvisioningError("CANONICAL_IDENTITY_CONFLICT")
        _atomic_write(canonical, render_identity_file(str(plan["environment"])), 0o640,
                      target_uid=0 if root == Path("/") else os.geteuid(), target_gid=owner.gid)
        completed.append("canonical_identity")
        for row in plan["conflicting_declarations"]:
            logical = Path(str(row["path"])); actual = _rooted(logical, root)
            _atomic_write(actual, _without_identity(actual), stat.S_IMODE(actual.stat().st_mode))
        completed.append("legacy_declarations")
        asset_uid = 0 if root == Path("/") else os.geteuid(); asset_gid = 0 if root == Path("/") else os.getegid()
        # The wrapper decision must be taken here, not at plan time, and under
        # the same host lock a cutover holds while it installs. Otherwise a
        # cutover landing between planning and this loop is overwritten by a
        # stale plan: the development wrapper goes back on top of a freshly
        # installed release wrapper, production silently returns to the mutable
        # tree, and every release pointer still reads healthy. Holding the lock
        # is not enough on its own — the identity is re-read inside it, because
        # the lock says nothing about what the file contained beforehand.
        with wrapper_install_lock():
            wrapper_installed = _rooted(INSTALLED_WRAPPER_PATH, root)
            recheck = wrapper_replaceability(repo_root=REPO_ROOT, installed_wrapper=wrapper_installed)
            if not recheck["replaceable"]:
                raise ProvisioningError(str(recheck["classification"]), {
                    "installed_wrapper": str(wrapper_installed),
                    "installed_wrapper_variant": recheck["variant"],
                    "reason": recheck["reason"],
                    "detected_at": "pre_write_recheck",
                    "remediation": "see docs/07_operations.md -> Release boundary before re-provisioning identity",
                })
            for raw, target, mode in verified_assets:
                _atomic_write(_rooted(target, root), raw, mode, target_uid=asset_uid, target_gid=asset_gid)
        completed.append("assets")
        if root == Path("/"):
            subprocess.run(["systemctl", "daemon-reload"], check=True); completed.append("daemon_reload")
        recovery = store.write_evidence(state="writes_complete")
        validation = _validate_post_write(args, plan, recovery)
        recovery = store.write_evidence(state="validated")
        return {"completed_steps": completed + ["post_write_validation"], "recovery": recovery, "post_write_validation": validation}
    except Exception as exc:
        try: recovery = store.write_evidence(state="partial_state")
        except Exception: recovery = prepared
        reason = exc.code if isinstance(exc, ProvisioningError) else type(exc).__name__
        raise PartialStateError("RUNTIME_IDENTITY_PROVISIONING_FAILED_PARTIAL_STATE",
                                {"completed_steps": completed, "failed_verification": reason,
                                 "recovery": recovery, "rollback_ready": bool(recovery.get("rollback_ready"))}) from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-host", required=True); parser.add_argument("--expected-repository-head", required=True)
    parser.add_argument("--expected-current-environment", required=True); parser.add_argument("--backup-reference", type=Path, required=True)
    parser.add_argument("--execute", action="store_true"); parser.add_argument("--attestation")
    parser.add_argument("--root", type=Path, default=Path("/"), help=argparse.SUPPRESS)
    parser.add_argument("--recovery-root", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv); repository_state = _repository_state(args); _validate_backup_reference(args.backup_reference)
    plan = _plan(args, repository_state); required = _attestation(plan)
    output = {"mode": "execute" if args.execute else "dry_run", "writes_performed": False, "plan": plan,
              "plan_sha256": hashlib.sha256(_canonical_json(plan).encode()).hexdigest(), "required_attestation": required,
              "execution_command": _execution_command(args, required)}
    if not args.execute: print(json.dumps(output, indent=2, sort_keys=True)); return 0
    if args.attestation != required: raise ProvisioningError("ATTESTATION_MISMATCH")
    _repository_state(args)
    try: execution = _execute(args, plan)
    except PartialStateError as exc:
        print(json.dumps({"classification": exc.code, "writes_performed": True, **exc.details}, sort_keys=True, indent=2)); return 6
    output.update({"classification": "RUNTIME_IDENTITY_PROVISIONED_RELOAD_PENDING", "writes_performed": True,
                   "reloads_performed": ["systemctl daemon-reload"] if args.root == Path("/") else [],
                   "restarts_performed": [], "execution": execution})
    print(json.dumps(output, indent=2, sort_keys=True)); return 0


if __name__ == "__main__":
    try: raise SystemExit(main())
    except ProvisioningError as exc:
        print(json.dumps({"classification": exc.code, "writes_performed": False, **exc.details}, sort_keys=True, indent=2)); raise SystemExit(2)
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr); raise SystemExit(2)
