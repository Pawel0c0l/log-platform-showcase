"""Deterministic, secret-free immutable bindings for promotion plan contract v4."""
from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
from pathlib import Path
from typing import Mapping, Sequence

from ops import environment_identity_promotion as promotion
from ops.runtime_identity_recovery import (
    DIRECTORY_MODE, FILE_MODE, default_recovery_root, resolve_recovery_owner,
    sha256_file, validate_preserved_recovery_pair, validate_recovery_root,
)

CONTRACT_VERSION = 4
OPERATION = "promote_environment_identity"


def _absolute(path: Path) -> str:
    return str(path.absolute())


def _metadata(path: Path, *, include_hash: bool) -> dict[str, object]:
    metadata = path.lstat()
    result: dict[str, object] = {
        "path": _absolute(path), "uid": metadata.st_uid, "gid": metadata.st_gid,
        "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
        "regular_file": stat.S_ISREG(metadata.st_mode),
        "directory": stat.S_ISDIR(metadata.st_mode), "symlink": stat.S_ISLNK(metadata.st_mode),
    }
    if include_hash:
        if not result["regular_file"] or result["symlink"]:
            raise promotion.PromotionError("PROMOTION_BINDING_FILE_UNSAFE", "immutable binding file is not a regular non-symlink file", details={"path": _absolute(path)})
        result["sha256"] = sha256_file(path)
        result["size"] = metadata.st_size
    return result


def checkpoint_and_recovery_bindings(*, repository_root: Path, checkpoint: Path,
        recovery_root: Path, backup_path: Path, evidence_path: Path) -> tuple[dict[str, object], dict[str, object]]:
    checkpoint = checkpoint.absolute(); recovery_root = recovery_root.absolute()
    backup_path = backup_path.absolute(); evidence_path = evidence_path.absolute()
    checkpoint_meta = _metadata(checkpoint, include_hash=True)
    try:
        checkpoint_payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    except Exception as exc:
        raise promotion.PromotionError("CHECKPOINT_DRIFT", "checkpoint is not valid JSON", details={"path": str(checkpoint)}) from exc
    owner = resolve_recovery_owner()
    if (checkpoint_meta["uid"] != owner.uid or checkpoint_meta["gid"] != owner.gid
            or checkpoint_meta["mode"] != f"{FILE_MODE:04o}" or checkpoint_meta["symlink"]):
        raise promotion.PromotionError("CHECKPOINT_DRIFT", "checkpoint metadata does not match the protected v4 contract", details={"path": str(checkpoint)})
    validate_recovery_root(recovery_root, repository_root=repository_root, owner=owner, phase="promotion_v4")
    pair = validate_preserved_recovery_pair(
        evidence_path=evidence_path, backup_path=backup_path, root=recovery_root,
        repository_root=repository_root, owner=owner,
        expected_evidence_sha256=sha256_file(evidence_path), expected_backup_sha256=sha256_file(backup_path),
    )
    evidence_meta = _metadata(evidence_path, include_hash=True)
    backup_meta = _metadata(backup_path, include_hash=True)
    plan_dir_meta = _metadata(backup_path.parent, include_hash=False)
    if plan_dir_meta["mode"] != f"{DIRECTORY_MODE:04o}" or backup_meta["mode"] != f"{FILE_MODE:04o}" or evidence_meta["mode"] != f"{FILE_MODE:04o}":
        raise promotion.PromotionError("RECOVERY_DRIFT", "recovery modes do not match the v4 contract")
    checkpoint_binding = {
        **checkpoint_meta,
        "kind": checkpoint_payload.get("kind"),
        "source_environment": checkpoint_payload.get("source_environment") or checkpoint_payload.get("environment"),
        "platform_uuid": checkpoint_payload.get("platform_uuid"),
        "repository_head": checkpoint_payload.get("repository_head"),
        "verified": checkpoint_payload.get("verified") is True,
        "client_uuids": {str(k): str(v) for k, v in sorted(dict(checkpoint_payload.get("clients") or {}).items())},
    }
    recovery_binding = {
        "root": _metadata(recovery_root, include_hash=False),
        "owner": owner.as_plan(),
        "plan_directory": plan_dir_meta,
        "preserved_backup": backup_meta,
        "evidence": evidence_meta,
        "provisioning_plan_sha256": pair["plan_sha256"],
        "checkpoint_sha256_from_evidence": json.loads(evidence_path.read_text(encoding="utf-8")).get("checkpoint_sha256"),
    }
    if recovery_binding["checkpoint_sha256_from_evidence"] != checkpoint_binding["sha256"]:
        raise promotion.PromotionError("RECOVERY_CHECKPOINT_DRIFT", "recovery evidence checkpoint hash differs from the selected checkpoint")
    return checkpoint_binding, recovery_binding


def _run(command: list[str], *, cwd: Path | None = None, code: str) -> str:
    try:
        result = subprocess.run(command, cwd=cwd, check=False, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        raise promotion.PromotionError(code, "runtime binding probe failed") from exc
    if result.returncode != 0:
        raise promotion.PromotionError(code, "runtime binding probe was rejected", details={"command": command[:3], "exit_code": result.returncode})
    return result.stdout


def runtime_bindings(*, repository_root: Path, runtime_convergence: Mapping[str, object]) -> dict[str, object]:
    processes = {str(row["component"]): dict(row) for row in runtime_convergence.get("running_processes", [])}
    systemd_values = {}
    for line in _run(["systemctl", "show", "log-platform-api.service", "-p", "MainPID", "-p", "InvocationID", "-p", "ExecMainStartTimestamp", "-p", "NeedDaemonReload", "-p", "DropInPaths", "-p", "ActiveState", "-p", "SubState"], code="SYSTEMD_PROVENANCE_DRIFT").splitlines():
        if "=" in line: systemd_values[line.split("=", 1)[0]] = line.split("=", 1)[1]
    dropins = [Path(item) for item in systemd_values.get("DropInPaths", "").split()]
    dropin_bindings = [_metadata(path, include_hash=True) for path in dropins]
    docker_id = _run(["docker", "compose", "ps", "-q", "api"], cwd=repository_root, code="DOCKER_PROVENANCE_DRIFT").strip()
    if not docker_id:
        raise promotion.PromotionError("DOCKER_PROVENANCE_DRIFT", "Compose api container is absent")
    inspect_payload = json.loads(_run(["docker", "inspect", docker_id], code="DOCKER_PROVENANCE_DRIFT"))
    if len(inspect_payload) != 1:
        raise promotion.PromotionError("DOCKER_PROVENANCE_DRIFT", "Compose api container identity is ambiguous")
    container = inspect_payload[0]; labels = dict(container.get("Config", {}).get("Labels") or {})
    compose_files = [str(Path(item).absolute()) for item in labels.get("com.docker.compose.project.config_files", "").split(",") if item]
    compose_command = ["docker", "compose", "--project-name", labels.get("com.docker.compose.project", "")]
    for compose_file in compose_files: compose_command += ["--file", compose_file]
    fingerprint_line = _run(compose_command + ["config", "--hash", "api"], cwd=Path(labels.get("com.docker.compose.project.working_dir", repository_root)), code="DOCKER_COMPOSE_DRIFT").strip()
    fingerprint = fingerprint_line.split()[-1] if fingerprint_line else ""
    if (int(systemd_values.get("MainPID") or 0) <= 0 or not systemd_values.get("InvocationID")
            or systemd_values.get("NeedDaemonReload") != "no" or not dropin_bindings):
        raise promotion.PromotionError("SYSTEMD_PROVENANCE_DRIFT", "systemd API provenance is incomplete")
    if (container.get("Id") != docker_id or labels.get("com.docker.compose.service") != "api"
            or not compose_files or len(fingerprint) != 64):
        raise promotion.PromotionError("DOCKER_COMPOSE_DRIFT", "Docker Compose API provenance is incomplete")
    systemd_process = processes.get("systemd_api", {}); docker_process = processes.get("docker_api", {})
    systemd_health = {"probe": "http://127.0.0.1:8000/docs", "passed": systemd_process.get("health_check_passed")}
    docker_health = {"probe": "http://127.0.0.1:8000/docs", "passed": docker_process.get("health_check_passed")}
    systemd_health["fingerprint"] = promotion.sha256_bytes(promotion.canonical_json(systemd_health).encode())
    docker_health["fingerprint"] = promotion.sha256_bytes(promotion.canonical_json(docker_health).encode())
    return {
        "readiness_classification": runtime_convergence.get("classification"),
        "reload_required_consumers": sorted(str(v) for v in runtime_convergence.get("reload_required", [])),
        "systemd_api": {
            "service": "log-platform-api.service", "pid": int(systemd_values.get("MainPID") or 0),
            "invocation_id": systemd_values.get("InvocationID"), "process_start_timestamp": systemd_process.get("process_started_at"),
            "active_state": systemd_values.get("ActiveState"), "sub_state": systemd_values.get("SubState"),
            "need_daemon_reload": systemd_values.get("NeedDaemonReload"), "ordered_dropins": dropin_bindings,
            "effective_canonical_source": runtime_convergence.get("systemd_effective_canonical_source"),
            "configuration_provenance": "CONVERGED" if systemd_process.get("configuration_provenance") else "NOT_CONVERGED",
            "process_provenance": "CONVERGED" if systemd_process.get("process_provenance") else "NOT_CONVERGED", "semantic_identity": systemd_process.get("effective_environment"),
            "health": systemd_health,
        },
        "docker_api": {
            "project": labels.get("com.docker.compose.project"), "service": labels.get("com.docker.compose.service"),
            "working_directory": str(Path(labels.get("com.docker.compose.project.working_dir", "")).absolute()),
            "compose_files": compose_files, "environment_file_contract": {"label": labels.get("com.docker.compose.project.environment_file") or None, "canonical_source_resolved": runtime_convergence.get("docker_compose_canonical_source")},
            "container_id": container.get("Id"), "container_created_at": container.get("Created"), "image_id": container.get("Image"),
            "compose_configuration_fingerprint": fingerprint,
            "canonical_environment_source": "/etc/log-platform/environment-identity.env",
            "configuration_provenance": "CONVERGED" if docker_process.get("configuration_provenance") else "NOT_CONVERGED",
            "container_provenance": "CONVERGED" if docker_process.get("process_provenance") else "NOT_CONVERGED", "semantic_identity": docker_process.get("effective_environment"),
            "health": docker_health,
        },
        "per_invocation_consumers": {
            "prune": runtime_convergence.get("per_invocation_consumers", {}).get("prune"),
            "backup": runtime_convergence.get("per_invocation_consumers", {}).get("backup"),
            "timer_restart_required": runtime_convergence.get("timer_restarts_required"),
        },
    }


def database_bindings(*, platform_conn, client_connections: Mapping[str, object], clients: Sequence[object],
        platform_marker: Mapping[str, object], client_markers: Mapping[str, Mapping[str, object]], source: str, target: str) -> dict[str, object]:
    with platform_conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM public.schema_migrations WHERE filename='053_environment_identity_promotion_journal.sql'")
        migration_053 = int(cur.fetchone()["n"])
        cur.execute(f"SELECT count(*) AS n, count(*) FILTER (WHERE state IN ('planned','in_progress','failed')) AS incomplete FROM {promotion.JOURNAL_TABLE}")
        journal = dict(cur.fetchone())
        cur.execute("SELECT client_code,client_db_environment,client_db_identity_id::text AS database_uuid FROM workflow_a_control.client_account WHERE client_code=ANY(%s) ORDER BY client_code", ([c.client_code for c in clients],))
        controls = {str(row["client_code"]): dict(row) for row in cur.fetchall()}
    client_rows=[]
    for client in sorted(clients, key=lambda row: row.client_code):
        capability = promotion.client_promotion_capability(client_connections[client.client_code], expected_user=client.database_user)
        marker = client_markers[client.client_code]; control = controls[client.client_code]
        client_rows.append({
            "client_code": client.client_code, "database_uuid": client.database_uuid, "database_name": client.database_name,
            "current_marker": marker["environment"], "target_marker": target,
            "current_control_plane_environment": control["client_db_environment"], "target_control_plane_environment": target,
            "control_plane_uuid": control["database_uuid"], "migration_045_capability": capability,
            "guarded_primitive": "ops_control.promote_environment_identity_v1(uuid,text,text,text,uuid,text)",
            "direct_marker_update_expected": False, "uuid_retained": True,
        })
    return {
        "platform": {"database_uuid": platform_marker["database_uuid"], "database_name": platform_marker["database_name"],
            "database_role": platform_marker.get("database_role"), "current_marker": platform_marker["environment"], "target_marker": target,
            "migration_053": {"filename": "053_environment_identity_promotion_journal.sql", "applied_count": migration_053},
            "promotion_journal_rows": int(journal["n"]), "incomplete_promotion_rows": int(journal["incomplete"]),
            "advisory_lock_identity": promotion.ADVISORY_LOCK_NAME},
        "clients": client_rows,
    }


def canonical_identity_binding(runtime_file, *, target: str) -> dict[str, object]:
    metadata = runtime_file.path.lstat()
    return {"path": _absolute(runtime_file.path), "current_value": runtime_file.values[promotion.TARGET_ENVIRONMENT_KEY],
        "target_value": target, "current_sha256": runtime_file.checksum, "target_sha256": promotion.intended_checksum(target),
        "current_metadata": {"uid": metadata.st_uid, "gid": metadata.st_gid, "mode": f"{stat.S_IMODE(metadata.st_mode):04o}", "regular_file": stat.S_ISREG(metadata.st_mode), "symlink": stat.S_ISLNK(metadata.st_mode), "assignment_count": 1},
        "required_target_metadata": {"uid": metadata.st_uid, "gid": metadata.st_gid, "mode": f"{stat.S_IMODE(metadata.st_mode):04o}", "regular_file": True, "symlink": False, "assignment_count": 1}}


def validate_installed_assets(plan: Mapping[str, object]) -> None:
    for row in plan.get("consumer_installation_evidence", []):
        path = Path(str(row.get("target") or ""))
        if path.is_symlink() or not path.is_file():
            raise promotion.PromotionError("INSTALLED_ASSET_DRIFT", "planned installed asset is unavailable", details={"path": str(path)})
        metadata = path.stat()
        if (sha256_file(path) != row.get("installed_sha256")
                or f"{stat.S_IMODE(metadata.st_mode):04o}" != row.get("expected_mode")
                or metadata.st_uid != 0 or metadata.st_gid != 0
                or row.get("metadata_matches") is not True):
            raise promotion.PromotionError(
                "INSTALLED_ASSET_DRIFT",
                "planned installed asset hash or root ownership/mode changed",
                details={"path": str(path)},
            )


def binding_drift_code(planned: Mapping[str, object], current: Mapping[str, object]) -> str:
    categories = (
        ("operation_identity", "REPOSITORY_OR_HOST_DRIFT"),
        ("canonical_identity", "CANONICAL_IDENTITY_DRIFT"),
        ("database_bindings", "DATABASE_IDENTITY_DRIFT"),
        ("runtime_bindings", "RUNTIME_OR_COMPOSE_DRIFT"),
        ("checkpoint_binding", "CHECKPOINT_DRIFT"),
        ("recovery_binding", "RECOVERY_DRIFT"),
        ("consumer_installation_evidence", "INSTALLED_ASSET_DRIFT"),
    )
    for key, code in categories:
        if planned.get(key) != current.get(key): return code
    return "PROMOTION_IMMUTABLE_BINDING_DRIFT"


def operation_binding(*, repository_root: Path, repository_state, source: str, target: str) -> dict[str, object]:
    return {"operation": OPERATION, "host": socket.gethostname(), "repository_path": str(repository_root.resolve()),
        "repository_head": repository_state.head, "repository_branch": repository_state.branch,
        "source_environment": source, "target_environment": target, "uuid_policy": "preserve_existing_database_uuids"}
