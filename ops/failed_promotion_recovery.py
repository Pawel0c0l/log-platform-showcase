"""Immutable failed-promotion recovery-v2 planning and evidence contracts."""
from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

from ops import environment_identity_promotion as promotion
from ops.runtime_identity_recovery import (
    DIRECTORY_MODE,
    FILE_MODE,
    resolve_recovery_owner,
    sha256_file,
    validate_recovery_root,
)


CONTRACT = "failed_environment_identity_recovery_plan_v2"
ATTESTATION_CONTRACT = "recovery-v2"
OPERATION = "recover_failed_environment_identity_promotion"
DIRECTION = "rollback_to_local_dev"
OLD_AUDIT_PLAN_SHA256 = "9d417d3bb88451d1edb1bba4c67564ae0b3947a59cec93060bd25f7ad6278cf5"
EVIDENCE_SCHEMA = "failed_environment_identity_rollback_evidence_v1"


def _metadata(path: Path, *, include_hash: bool = True) -> dict[str, object]:
    row = path.lstat()
    result: dict[str, object] = {
        "path": str(path.absolute()),
        "uid": row.st_uid,
        "gid": row.st_gid,
        "mode": f"{stat.S_IMODE(row.st_mode):04o}",
        "regular_file": stat.S_ISREG(row.st_mode),
        "directory": stat.S_ISDIR(row.st_mode),
        "symlink": stat.S_ISLNK(row.st_mode),
    }
    if include_hash:
        if not result["regular_file"] or result["symlink"]:
            raise promotion.PromotionError(
                "RECOVERY_ASSET_DRIFT", "recovery binding is not a regular non-symlink file",
                details={"path": str(path)},
            )
        result["sha256"] = sha256_file(path)
        result["size"] = row.st_size
    return result


def effective_helper_policy_binding() -> dict[str, object]:
    try:
        result = subprocess.run(
            ["sudo", "-n", "-l"], check=False, capture_output=True,
            text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise promotion.PromotionError(
            "RECOVERY_PRIVILEGE_ASSET_DRIFT", "effective sudo policy inspection failed",
        ) from exc
    if result.returncode != 0:
        raise promotion.PromotionError(
            "RECOVERY_PRIVILEGE_ASSET_DRIFT", "effective sudo policy inspection was refused",
        )
    exact = "(root) NOPASSWD: /usr/local/sbin/log-platform-environment-identity-helper *"
    matches = [line.strip() for line in result.stdout.splitlines() if "log-platform-environment-identity-helper" in line]
    if matches != [exact]:
        raise promotion.PromotionError(
            "RECOVERY_PRIVILEGE_ASSET_DRIFT",
            "effective sudo policy does not contain exactly one approved helper rule",
        )
    canonical_output = result.stdout.rstrip("\n")
    return {
        "matching_rule": exact,
        "matching_rule_count": 1,
        "sudo_n_l_sha256_without_trailing_newline": promotion.sha256_bytes(canonical_output.encode("utf-8")),
    }


def implementation_bindings(repository_root: Path) -> list[dict[str, object]]:
    paths = (
        repository_root / "ops/environment_identity_promotion.py",
        repository_root / "ops/promote_environment_identity.py",
        repository_root / "ops/failed_promotion_recovery.py",
    )
    return [_metadata(path) for path in paths]


def reconcile_promotion_steps(
    *, journal: Mapping[str, object], plan: Mapping[str, object],
    surfaces: Mapping[str, object],
) -> dict[str, object]:
    reported = set(str(value) for value in journal.get("completed_steps") or [])
    source = str(plan["source_environment"])
    target = str(plan["target_environment"])
    actual = dict(surfaces["surfaces"])
    rows: list[dict[str, object]] = []
    supported = True
    for client in plan["clients"]:
        code = str(client["client_code"])
        step = f"{promotion.STEP_CLIENT_PREFIX}{code}"
        value = str(actual[step])
        if value == target:
            action = "idempotent_no_op"
        elif value == source:
            action = "run_guarded_primitive"
        else:
            action = "unsupported_divergence"
            supported = False
        rows.append({
            "step": step, "journal_reported_complete": step in reported,
            "actual_environment": value, "required_action": action,
            "divergence": (step in reported and value != target),
        })
    for step, key in (
        (promotion.STEP_CONTROL_PLANE, "control_plane"),
        (promotion.STEP_PLATFORM_MARKER, "platform_marker"),
        (promotion.STEP_RUNTIME_FILE, "runtime_file"),
    ):
        values = actual[key] if key == "control_plane" else actual.get(key)
        if isinstance(values, dict):
            value_set = sorted(set(str(value) for value in values.values()))
        else:
            value_set = [str(values)]
        if any(value not in {source, target} for value in value_set):
            supported = False
            action = "unsupported_divergence"
        elif all(value == target for value in value_set):
            action = "idempotent_no_op"
        else:
            action = "reconcile_from_durable_state"
        rows.append({
            "step": step, "journal_reported_complete": step in reported,
            "actual_environment": values, "required_action": action,
            "divergence": step in reported and any(value != target for value in value_set),
        })
    return {"supported": supported, "steps": rows, "actual_state_authoritative": True}


def evidence_path_for(*, recovery_root: Path, promotion_id: str) -> Path:
    return recovery_root / "promotion-recovery" / promotion_id / "rollback-evidence.json"


def build_plan(
    *, repository_root: Path, repository_state, journal: Mapping[str, object],
    original_plan: Mapping[str, object], canonical_binding: Mapping[str, object],
    database_binding: Mapping[str, object], runtime_binding: Mapping[str, object],
    privilege_binding: Mapping[str, object], checkpoint_binding: Mapping[str, object],
    provisioning_recovery_binding: Mapping[str, object], recovery_root: Path,
    reconciliation: Mapping[str, object], active_promotion_rows: int,
) -> dict[str, object]:
    if not reconciliation.get("supported"):
        raise promotion.PromotionError(
            "JOURNAL_REALITY_DIVERGENCE_UNSUPPORTED",
            "journal-versus-reality divergence cannot be reconciled safely",
        )
    promotion_id = str(journal["promotion_id"])
    clients = sorted(original_plan["clients"], key=lambda row: row["client_code"])
    return {
        "operation": {
            "name": OPERATION,
            "contract": CONTRACT,
            "attestation_contract": ATTESTATION_CONTRACT,
            "direction": DIRECTION,
            "host": socket.gethostname(),
            "repository_path": str(repository_root.resolve()),
            "branch": repository_state.branch,
            "head": repository_state.head,
            "promotion_id": promotion_id,
            "original_promotion_contract": f"v{original_plan['contract_version']}",
            "original_promotion_plan_sha256": str(journal["plan_sha256"]),
            "original_source_environment": str(original_plan["source_environment"]),
            "original_target_environment": str(original_plan["target_environment"]),
        },
        "journal": {
            "state": journal["state"],
            "current_step": journal["current_step"],
            "completed_steps": list(journal.get("completed_steps") or []),
            "failure_classification": str(journal.get("error") or "").split(":", 1)[0],
            "active_promotion_rows": active_promotion_rows,
            "promotion_backup_path": journal.get("runtime_file_backup_path"),
            "promotion_backup_sha256": journal.get("runtime_file_before_sha256"),
            "promotion_recovery_evidence_path": None,
            "promotion_recovery_evidence_sha256": None,
        },
        "current_durable_database_state": database_binding,
        "canonical_identity": canonical_binding,
        "runtime": runtime_binding,
        "privilege_and_assets": {
            **dict(privilege_binding),
            "implementation": implementation_bindings(repository_root),
        },
        "checkpoint": checkpoint_binding,
        "provisioning_recovery": provisioning_recovery_binding,
        "reconciliation": reconciliation,
        "uuid_policy": "preserve_existing_database_uuids",
        "clients": [
            {"client_code": row["client_code"], "database_uuid": row["database_uuid"]}
            for row in clients
        ],
        "ordered_actions": [
            "revalidate_all_immutable_bindings",
            "acquire_promotion_recovery_lock",
            "leave_canonical_identity_unchanged",
            "do_not_invoke_canonical_helper",
            "rollback_client_marker:BRAVO00016",
            "fresh_verify_client_marker:BRAVO00016",
            "rollback_client_marker:ALPHA00001",
            "fresh_verify_client_marker:ALPHA00001",
            "reverse_platform_marker_and_selected_control_plane_only",
            "commit_platform_transaction_with_propagated_errors",
            "fresh_verify_all_surfaces",
            "verify_api_health_and_unchanged_runtime_ids",
            "require_readiness:PRODUCTION_PROMOTION_READY",
            "commit_journal_transition:rolled_back",
            "reconnect_and_verify_journal_transition",
            "atomically_create_checksum_bound_rollback_evidence",
            "emit_success_after_all_commits_and_verifications",
        ],
        "transaction_boundaries": {
            "lock": "dedicated autocommit session-level advisory-lock connection",
            "clients": "one IDLE top-level transaction per migration-045 call; fresh read-only connection verifies each commit",
            "platform": "one explicit transaction for platform marker and selected control-plane reversal; commit before verification",
            "journal": "separate explicit rolled_back transaction after cross-surface verification; reconnect before evidence",
        },
        "rollback_evidence": {
            "schema": EVIDENCE_SCHEMA,
            "path": str(evidence_path_for(recovery_root=recovery_root, promotion_id=promotion_id)),
            "mode": "0600",
            "atomic": True,
            "checksum": "sha256",
        },
        "excluded_actions": [
            "canonical_helper_invocation", "canonical_file_write", "api_restart",
            "docker_recreation", "timer_restart", "prune_or_backup_invocation",
            "worker_activation", "email_or_smtp_or_imap", "backfill",
            "snapshot_recalculation", "schedule_change", "business_data_mutation",
            "unrelated_schema_operation", "database_uuid_change",
        ],
        "expected_final_state": {
            "canonical": "local_dev", "platform_marker": "local_dev",
            "BRAVO00016": {"marker": "local_dev", "control_plane": "local_dev"},
            "ALPHA00001": {"marker": "local_dev", "control_plane": "local_dev"},
            "uuids": "unchanged", "runtime": "unchanged_and_healthy",
            "restart_required": False, "docker_recreation_required": False,
            "journal": "rolled_back_with_evidence_reference",
            "readiness": "PRODUCTION_PROMOTION_READY",
        },
    }


def attestation(plan: Mapping[str, object]) -> str:
    operation = plan["operation"]
    clients = ",".join(f"{row['client_code']}:{row['database_uuid']}" for row in plan["clients"])
    return (
        "RECOVER_FAILED_ENVIRONMENT_IDENTITY "
        f"host={operation['host']} head={operation['head']} contract={ATTESTATION_CONTRACT} "
        f"promotion_id={operation['promotion_id']} original_plan_sha256={operation['original_promotion_plan_sha256']} "
        f"direction={DIRECTION} clients={clients} recovery_plan_sha256={promotion.plan_hash(plan)} "
        "UUIDS_UNCHANGED=true"
    )


def validate_execution_approval(
    plan: Mapping[str, object], *, provided_plan_sha256: str | None,
    provided_attestation: str | None,
) -> None:
    if provided_plan_sha256 == OLD_AUDIT_PLAN_SHA256 or "contract=recovery-v1" in str(provided_attestation or ""):
        raise promotion.PromotionError(
            "RECOVERY_V1_NON_EXECUTABLE",
            "audit-only recovery-v1 plans and attestations are never executable",
            exit_code=promotion.EXIT_INVALID,
        )
    digest = promotion.plan_hash(plan)
    if provided_plan_sha256 != digest:
        raise promotion.PromotionError(
            "RECOVERY_IMMUTABLE_PLAN_MISMATCH",
            "provided recovery plan SHA-256 does not match current canonical bytes",
            exit_code=promotion.EXIT_INVALID,
        )
    if provided_attestation != attestation(plan):
        raise promotion.PromotionError(
            "RECOVERY_ATTESTATION_MISMATCH",
            "rollback execute requires the exact recovery-v2 attestation",
            exit_code=promotion.EXIT_INVALID,
        )


def _validate_directory(path: Path, *, uid: int, gid: int) -> None:
    row = path.lstat()
    if (not stat.S_ISDIR(row.st_mode) or stat.S_ISLNK(row.st_mode)
            or row.st_uid != uid or row.st_gid != gid
            or stat.S_IMODE(row.st_mode) != DIRECTORY_MODE):
        raise promotion.PromotionError("ROLLBACK_EVIDENCE_FAILURE", "rollback evidence directory metadata is unsafe", details={"path": str(path)})


def write_evidence(*, repository_root: Path, plan: Mapping[str, object], payload: Mapping[str, object]) -> dict[str, object]:
    target = Path(str(plan["rollback_evidence"]["path"]))
    owner = resolve_recovery_owner()
    recovery_root = target.parents[2]
    validate_recovery_root(recovery_root, repository_root=repository_root, owner=owner, phase="promotion_rollback_evidence")
    for directory in (target.parents[1], target.parent):
        if not directory.exists():
            directory.mkdir(mode=DIRECTORY_MODE)
            os.chown(directory, owner.uid, owner.gid)
        _validate_directory(directory, uid=owner.uid, gid=owner.gid)
    evidence = {
        "schema": EVIDENCE_SCHEMA,
        "recovery_plan_sha256": promotion.plan_hash(plan),
        "promotion_id": plan["operation"]["promotion_id"],
        "original_promotion_plan_sha256": plan["operation"]["original_promotion_plan_sha256"],
        "repository_head": plan["operation"]["head"],
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        **dict(payload),
    }
    raw = (promotion.canonical_json(evidence) + "\n").encode("utf-8")
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        os.fchmod(fd, FILE_MODE)
        os.fchown(fd, owner.uid, owner.gid)
        with os.fdopen(fd, "wb", closefd=True) as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, target)
        directory_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception as exc:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise promotion.PromotionError("ROLLBACK_EVIDENCE_FAILURE", "rollback evidence could not be written atomically") from exc
    return {"path": str(target), "sha256": promotion.sha256_bytes(raw), "payload": evidence}
