"""Immutable v5 bindings for new forward environment promotions.

The v4 collector remains available for historical journal and recovery-v2
inspection.  New forward plans use this module exclusively.
"""
from __future__ import annotations

import json
import re
import stat
import subprocess
from pathlib import Path
from typing import Mapping, Sequence

from ops import environment_identity_promotion as promotion
from ops import promotion_plan_v4 as v4
from ops.runtime_identity_recovery import resolve_recovery_owner, sha256_file


CONTRACT_VERSION = 5
ATTESTATION_CONTRACT = "v5"
SUDO_COLLECTION_METHOD = "sudo_n_l_normalized_v1"
HELPER_PATH = "/usr/local/sbin/log-platform-environment-identity-helper"
HELPER_RULE = f"(root) NOPASSWD: {HELPER_PATH} *"

IMPLEMENTATION_ASSET_PATHS = (
    "ops/environment_identity_file.py",
    "ops/environment_identity_promotion.py",
    "ops/failed_promotion_recovery.py",
    "ops/git_repository_state.py",
    "ops/promote_environment_identity.py",
    "ops/promotion_plan_v4.py",
    "ops/promotion_plan_v5.py",
    "ops/runtime_identity_readiness.py",
    "ops/runtime_identity_recovery.py",
)

EXCLUDED_ACTIONS = tuple(sorted((
    "automatic_resume",
    "automatic_retry",
    "automatic_rollback",
    "backup_invocation",
    "business_data_mutation",
    "docker_api_recreation_during_initial_promotion",
    "docker_daemon_restart",
    "email_activation",
    "email_send",
    "imap_access",
    "manual_direct_client_marker_update",
    "alpha_backfill",
    "prune_invocation",
    "recovery_artifact_removal",
    "recovery_evidence_cleanup",
    "schedule_change",
    "smtp_access",
    "snapshot_recalculation",
    "systemctl_daemon_reload",
    "systemd_api_restart_during_initial_promotion",
    "timer_restart",
    "unrelated_schema_change",
    "uuid_change",
    "uuid_generation",
    "worker_activation",
)))


# Shared v4-era collectors remain byte-for-byte compatible for recovery-v2.
checkpoint_and_recovery_bindings = v4.checkpoint_and_recovery_bindings
runtime_bindings = v4.runtime_bindings
canonical_identity_binding = v4.canonical_identity_binding
validate_installed_assets = v4.validate_installed_assets
operation_binding = v4.operation_binding


def effective_sudo_policy_binding() -> dict[str, object]:
    """Return the bounded helper-specific effective sudo contract."""
    try:
        result = subprocess.run(
            ["sudo", "-n", "-l"], check=False, capture_output=True,
            text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise promotion.PromotionError(
            "SUDO_POLICY_DRIFT", "effective sudo policy inspection failed",
        ) from exc
    if result.returncode != 0:
        raise promotion.PromotionError(
            "SUDO_POLICY_DRIFT", "effective sudo policy inspection was refused",
        )
    helper_lines = [
        line.strip() for line in result.stdout.splitlines()
        if HELPER_PATH in line
    ]
    exact_indexes = [index for index, line in enumerate(helper_lines) if line == HELPER_RULE]
    exact_rule_count = len(exact_indexes)
    no_later_conflict = (
        exact_rule_count == 1
        and exact_indexes[0] == len(helper_lines) - 1
        and len(helper_lines) == 1
    )
    binding = {
        "collection_method_version": SUDO_COLLECTION_METHOD,
        "helper_absolute_path": HELPER_PATH,
        "matching_rule_count": len(helper_lines),
        "effective_rule": HELPER_RULE if exact_rule_count == 1 else None,
        "nopasswd": exact_rule_count == 1,
        "run_as_user": "root" if exact_rule_count == 1 else None,
        "no_later_conflicting_matching_rule": no_later_conflict,
        "effective_policy_fingerprint": promotion.sha256_bytes(
            result.stdout.rstrip("\n").encode("utf-8")
        ),
    }
    if (
        binding["matching_rule_count"] != 1
        or binding["effective_rule"] != HELPER_RULE
        or binding["nopasswd"] is not True
        or binding["run_as_user"] != "root"
        or binding["no_later_conflicting_matching_rule"] is not True
    ):
        raise promotion.PromotionError(
            "SUDO_POLICY_DRIFT",
            "effective sudo policy does not contain exactly one unconflicted helper rule",
        )
    return binding


def implementation_asset_bindings(repository_root: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for relative_path in sorted(IMPLEMENTATION_ASSET_PATHS):
        path = repository_root / relative_path
        try:
            metadata = path.lstat()
        except FileNotFoundError as exc:
            raise promotion.PromotionError(
                "PROMOTION_IMPLEMENTATION_ASSET_DRIFT",
                "security-critical promotion implementation asset is missing",
                details={"path": relative_path},
            ) from exc
        regular = stat.S_ISREG(metadata.st_mode)
        symlink = stat.S_ISLNK(metadata.st_mode)
        if not regular or symlink:
            raise promotion.PromotionError(
                "PROMOTION_IMPLEMENTATION_ASSET_DRIFT",
                "security-critical promotion implementation asset is not a regular non-symlink file",
                details={"path": relative_path},
            )
        rows.append({
            "path": relative_path,
            "sha256": sha256_file(path),
            "regular_file": True,
            "symlink": False,
        })
    return rows


_EVIDENCE_REFERENCE = re.compile(
    r"^ROLLBACK_EVIDENCE path=(?P<path>\S+) sha256=(?P<sha256>[0-9a-f]{64})$"
)


def _historical_evidence_binding(
    *, journal: Mapping[str, object], original_plan: Mapping[str, object],
) -> dict[str, object]:
    reference = _EVIDENCE_REFERENCE.fullmatch(str(journal.get("error") or ""))
    if reference is None:
        raise promotion.PromotionError(
            "PROMOTION_HISTORY_DRIFT",
            "rolled-back promotion lacks an exact rollback-evidence reference",
        )
    path = Path(reference.group("path"))
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise promotion.PromotionError(
            "PROMOTION_HISTORY_DRIFT", "historical rollback evidence is missing",
            details={"path": str(path)},
        ) from exc
    regular = stat.S_ISREG(metadata.st_mode)
    symlink = stat.S_ISLNK(metadata.st_mode)
    digest = sha256_file(path) if regular and not symlink else None
    try:
        evidence = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise promotion.PromotionError(
            "PROMOTION_HISTORY_DRIFT", "historical rollback evidence is invalid",
            details={"path": str(path)},
        ) from exc
    promotion_id = str(journal["promotion_id"])
    plan_digest = promotion.plan_hash(original_plan)
    owner = resolve_recovery_owner()
    reference_matches = (
        regular and not symlink
        and metadata.st_uid == owner.uid and metadata.st_gid == owner.gid
        and stat.S_IMODE(metadata.st_mode) == 0o600
        and journal.get("state") == "rolled_back"
        and journal.get("current_step") is None
        and digest == reference.group("sha256")
        and evidence.get("schema") == "failed_environment_identity_rollback_evidence_v1"
        and str(evidence.get("promotion_id")) == promotion_id
        and evidence.get("original_promotion_plan_sha256") == journal.get("plan_sha256")
        and evidence.get("journal_final_state") == "rolled_back"
        and plan_digest == journal.get("plan_sha256")
    )
    if not reference_matches:
        raise promotion.PromotionError(
            "PROMOTION_HISTORY_DRIFT",
            "historical journal, plan, and rollback evidence do not match",
            details={"promotion_id": promotion_id},
        )
    original_version = original_plan.get("promotion_plan_contract_version")
    if original_version != 4 or original_plan.get("contract_version") != 4:
        raise promotion.PromotionError(
            "PROMOTION_HISTORY_DRIFT",
            "rolled-back historical promotion is not the expected v4 contract",
        )
    return {
        "promotion_id": promotion_id,
        "original_contract": "v4",
        "original_plan_sha256": str(journal["plan_sha256"]),
        "final_journal_state": str(journal["state"]),
        "current_step": journal.get("current_step"),
        "rollback_direction": f"rollback_to_{original_plan['source_environment']}",
        "rollback_evidence_path": str(path.absolute()),
        "rollback_evidence_sha256": digest,
        "evidence_schema": evidence.get("schema"),
        "evidence_metadata": {
            "uid": metadata.st_uid,
            "gid": metadata.st_gid,
            "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
            "regular_file": regular,
            "symlink": symlink,
            "recovery_plan_sha256": evidence.get("recovery_plan_sha256"),
            "repository_head": evidence.get("repository_head"),
            "journal_final_state": evidence.get("journal_final_state"),
        },
        "journal_evidence_reference_match": True,
        "historical_completed_steps": {
            "values": list(journal.get("completed_steps") or []),
            "authority": "historical_evidence_only",
        },
        "durable_current_state_authoritative": True,
    }


def historical_recovery_bindings(platform_conn) -> dict[str, object]:
    with platform_conn.cursor() as cur:
        cur.execute(
            f"SELECT promotion_id::text AS promotion_id, state, current_step, "
            f"completed_steps, error, immutable_plan_json, plan_sha256 "
            f"FROM {promotion.JOURNAL_TABLE} ORDER BY promotion_id::text"
        )
        journals = [dict(row) for row in cur.fetchall()]
    active = [row for row in journals if row["state"] in {"planned", "in_progress"}]
    incomplete_recovery = [row for row in journals if row["state"] == "failed"]
    historical_rows = [row for row in journals if row["state"] == "rolled_back"]
    return {
        "historical_row_count": len(historical_rows),
        "total_journal_row_count": len(journals),
        "active_promotion_row_count": len(active),
        "incomplete_recovery_row_count": len(incomplete_recovery),
        "rolled_back_rows_excluded_from_active_blocking": True,
        "rolled_back_rows_cryptographically_bound_as_history": True,
        "entries": [
            _historical_evidence_binding(
                journal=row, original_plan=dict(row["immutable_plan_json"]),
            )
            for row in sorted(historical_rows, key=lambda item: str(item["promotion_id"]))
        ],
    }


def database_and_history_bindings(
    *, platform_conn, client_connections: Mapping[str, object], clients: Sequence[object],
    platform_marker: Mapping[str, object], client_markers: Mapping[str, Mapping[str, object]],
    source: str, target: str,
) -> tuple[dict[str, object], dict[str, object]]:
    database = v4.database_bindings(
        platform_conn=platform_conn, client_connections=client_connections,
        clients=clients, platform_marker=platform_marker,
        client_markers=client_markers, source=source, target=target,
    )
    history = historical_recovery_bindings(platform_conn)
    database["platform"]["promotion_journal_rows"] = history["total_journal_row_count"]
    database["platform"]["incomplete_promotion_rows"] = (
        history["active_promotion_row_count"] + history["incomplete_recovery_row_count"]
    )
    return database, history


def binding_drift_code(planned: Mapping[str, object], current: Mapping[str, object]) -> str:
    if (
        planned.get("promotion_plan_contract_version") != CONTRACT_VERSION
        or current.get("promotion_plan_contract_version") != CONTRACT_VERSION
        or planned.get("contract_version") != CONTRACT_VERSION
        or current.get("contract_version") != CONTRACT_VERSION
    ):
        return "PROMOTION_PLAN_CONTRACT_SUPERSEDED"
    categories = (
        ("effective_sudo_policy", "SUDO_POLICY_DRIFT"),
        ("implementation_assets", "PROMOTION_IMPLEMENTATION_ASSET_DRIFT"),
        ("historical_recovery", "PROMOTION_HISTORY_DRIFT"),
        ("excluded_actions", "PROMOTION_EXCLUSION_CONTRACT_DRIFT"),
        ("operation_identity", "REPOSITORY_OR_HOST_DRIFT"),
        ("canonical_identity", "CANONICAL_IDENTITY_DRIFT"),
        ("database_bindings", "DATABASE_IDENTITY_DRIFT"),
        ("runtime_bindings", "RUNTIME_OR_COMPOSE_DRIFT"),
        ("checkpoint_binding", "CHECKPOINT_DRIFT"),
        ("recovery_binding", "RECOVERY_DRIFT"),
        ("consumer_installation_evidence", "INSTALLED_ASSET_DRIFT"),
    )
    for key, code in categories:
        if planned.get(key) != current.get(key):
            return code
    return "PROMOTION_IMMUTABLE_BINDING_DRIFT"
