from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable

import requests

from api.client import ArtifactUploadResult


WORKFLOW_NAME = "workflow_b"
STAGE_NAME = "stage_1_fetch"
RAW_ROLE = "raw"
NORMALIZED_ROLE = "normalized"
STAGE1_RAW_ARTIFACT_CONTRACT_VERSION = "v1"
STAGE1_NORMALIZED_ARTIFACT_CONTRACT_VERSION = "v1"
STAGE1_RAW_ARTIFACT_IDEMPOTENCY_SCOPE = "workflow_b.stage1.raw.v1"
STAGE1_NORMALIZED_ARTIFACT_IDEMPOTENCY_SCOPE = "workflow_b.stage1.normalized.v1"
STAGE1_ARTIFACT_LOCK_NAMESPACE = "workflow_b.stage1.artifact_sync.v1"
ROLE_CONFIG = {
    RAW_ROLE: (STAGE1_RAW_ARTIFACT_CONTRACT_VERSION, STAGE1_RAW_ARTIFACT_IDEMPOTENCY_SCOPE),
    NORMALIZED_ROLE: (
        STAGE1_NORMALIZED_ARTIFACT_CONTRACT_VERSION,
        STAGE1_NORMALIZED_ARTIFACT_IDEMPOTENCY_SCOPE,
    ),
}


class Stage1ArtifactSyncOutcome(StrEnum):
    CREATED = "CREATED"
    REUSED = "REUSED"
    LINKED_EXISTING = "LINKED_EXISTING"
    SKIPPED_ALREADY_SYNCHRONIZED = "SKIPPED_ALREADY_SYNCHRONIZED"
    SKIPPED_LOCKED = "SKIPPED_LOCKED"
    SKIPPED_NOT_REQUIRED = "SKIPPED_NOT_REQUIRED"
    FAILED_RETRYABLE_UPLOAD = "FAILED_RETRYABLE_UPLOAD"
    FAILED_RETRYABLE_PERSISTENCE = "FAILED_RETRYABLE_PERSISTENCE"
    FAILED_NON_RETRYABLE_CONFLICT = "FAILED_NON_RETRYABLE_CONFLICT"
    BLOCKED_SOURCE_MISSING = "BLOCKED_SOURCE_MISSING"
    BLOCKED_AMBIGUOUS_EXISTING_ARTIFACT = "BLOCKED_AMBIGUOUS_EXISTING_ARTIFACT"
    BLOCKED_INVALID_LINEAGE = "BLOCKED_INVALID_LINEAGE"


@dataclass(slots=True)
class Stage1ArtifactSyncItemResult:
    raw_file_id: str
    artifact_role: str
    outcome: Stage1ArtifactSyncOutcome
    artifact_id: str | None = None
    idempotency_status: str = "none"
    retryable: bool = False
    operator_action_required: bool = False
    idempotency_digest_short: str | None = None
    contract_version: str | None = None
    error_category: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["outcome"] = self.outcome.value
        return value


@dataclass(slots=True)
class Stage1ArtifactReconciliationResult:
    stage_name: str = "workflow_b.stage1.artifact_sync"
    records_discovered: int = 0
    records_inspected: int = 0
    items: list[Stage1ArtifactSyncItemResult] = field(default_factory=list)

    def count(self, outcome: Stage1ArtifactSyncOutcome) -> int:
        return sum(item.outcome == outcome for item in self.items)

    @property
    def retryable_work_remains(self) -> bool:
        return any(item.retryable for item in self.items)

    @property
    def operator_action_required(self) -> bool:
        return any(item.operator_action_required for item in self.items)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage_name": self.stage_name,
            "records_discovered": self.records_discovered,
            "records_inspected": self.records_inspected,
            "artifact_roles_expected": len(self.items),
            "already_synchronized": self.count(Stage1ArtifactSyncOutcome.SKIPPED_ALREADY_SYNCHRONIZED),
            "attempted_uploads": sum(item.outcome in {
                Stage1ArtifactSyncOutcome.CREATED,
                Stage1ArtifactSyncOutcome.REUSED,
                Stage1ArtifactSyncOutcome.FAILED_RETRYABLE_UPLOAD,
                Stage1ArtifactSyncOutcome.FAILED_NON_RETRYABLE_CONFLICT,
            } for item in self.items),
            "created": self.count(Stage1ArtifactSyncOutcome.CREATED),
            "reused": self.count(Stage1ArtifactSyncOutcome.REUSED),
            "linked_existing": self.count(Stage1ArtifactSyncOutcome.LINKED_EXISTING),
            "skipped_locked": self.count(Stage1ArtifactSyncOutcome.SKIPPED_LOCKED),
            "source_missing": self.count(Stage1ArtifactSyncOutcome.BLOCKED_SOURCE_MISSING),
            "ambiguous": self.count(Stage1ArtifactSyncOutcome.BLOCKED_AMBIGUOUS_EXISTING_ARTIFACT),
            "retryable_failures": sum(item.retryable for item in self.items),
            "non_retryable_conflicts": self.count(Stage1ArtifactSyncOutcome.FAILED_NON_RETRYABLE_CONFLICT),
            "retryable_work_remains": self.retryable_work_remains,
            "operator_action_required": self.operator_action_required,
            "items": [item.to_dict() for item in self.items],
        }


class Stage1ArtifactSyncError(RuntimeError):
    def __init__(self, result: Stage1ArtifactReconciliationResult):
        self.result = result
        super().__init__(
            "Stage 1 artifact synchronization contained retryable or integrity failures"
        )

    @property
    def partial_result(self) -> Stage1ArtifactReconciliationResult:
        return self.result


def stage1_artifact_idempotency_key(
    raw_file_id: str,
    source_sha256: str,
    artifact_role: str,
    *,
    contract_version: str | None = None,
) -> str:
    if artifact_role not in ROLE_CONFIG:
        raise ValueError(f"Unsupported Stage 1 artifact role: {artifact_role}")
    default_version, _ = ROLE_CONFIG[artifact_role]
    version = contract_version or default_version
    payload = json.dumps(
        [f"workflow_b.stage1.{artifact_role}", version, str(raw_file_id), str(source_sha256)],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def stage1_artifact_lock_key(raw_file_id: str, artifact_role: str) -> int:
    digest = hashlib.sha256(
        f"{STAGE1_ARTIFACT_LOCK_NAMESPACE}\0{raw_file_id}\0{artifact_role}".encode("utf-8")
    ).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _row_value(row, key: str, index: int = 0):
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[key]
    except (TypeError, KeyError):
        return row[index]


def _expected_roles(row: dict, role_filter: set[str] | None) -> list[str]:
    roles = []
    if row.get("raw_path"):
        roles.append(RAW_ROLE)
    if row.get("status") == "NORMALIZED" and row.get("normalized_csv_path"):
        roles.append(NORMALIZED_ROLE)
    return [role for role in roles if role_filter is None or role in role_filter]


def _source_path(row: dict, role: str) -> Path | None:
    value = row.get("raw_path") if role == RAW_ROLE else row.get("normalized_csv_path")
    return Path(value) if value else None


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _metadata(row: dict, role: str) -> dict[str, Any] | None:
    if role == RAW_ROLE:
        return None
    saved = row.get("stage1_normalized_artifact_metadata")
    if isinstance(saved, str):
        saved = json.loads(saved)
    return dict(saved or {"report_key": row.get("report_key"), "sha256": row.get("sha256")})


def _artifact_rows(cur, raw_file_id: str, role: str) -> list[dict]:
    cur.execute(
        """SELECT artifact_id::text AS artifact_id, sha256
             FROM artifacts
            WHERE raw_file_id=%s AND workflow_name=%s AND stage_name=%s
              AND artifact_role=%s
            ORDER BY created_at, artifact_id""",
        (raw_file_id, WORKFLOW_NAME, STAGE_NAME, role),
    )
    return [dict(row) for row in cur.fetchall()]


def _try_lock(conn, raw_file_id: str, role: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (stage1_artifact_lock_key(raw_file_id, role),))
        acquired = bool(_row_value(cur.fetchone(), "pg_try_advisory_lock"))
    conn.commit()
    return acquired


def _unlock(conn, raw_file_id: str, role: str) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_unlock(%s)", (stage1_artifact_lock_key(raw_file_id, role),))
    conn.commit()


def _failure_item(row: dict, role: str, outcome: Stage1ArtifactSyncOutcome, *, category: str) -> Stage1ArtifactSyncItemResult:
    version, _ = ROLE_CONFIG[role]
    key = stage1_artifact_idempotency_key(row["raw_file_id"], row["sha256"], role)
    retryable = outcome in {
        Stage1ArtifactSyncOutcome.FAILED_RETRYABLE_UPLOAD,
        Stage1ArtifactSyncOutcome.FAILED_RETRYABLE_PERSISTENCE,
    }
    return Stage1ArtifactSyncItemResult(
        row["raw_file_id"], role, outcome,
        retryable=retryable,
        operator_action_required=not retryable,
        idempotency_digest_short=key[:12],
        contract_version=version,
        error_category=category,
    )


def _sync_role(conn, client, run_id: str, row: dict, role: str, *, dry_run: bool) -> Stage1ArtifactSyncItemResult:
    raw_file_id = row["raw_file_id"]
    version, scope = ROLE_CONFIG[role]
    key = stage1_artifact_idempotency_key(raw_file_id, row["sha256"], role)
    short = key[:12]
    if not dry_run and not _try_lock(conn, raw_file_id, role):
        return Stage1ArtifactSyncItemResult(
            raw_file_id, role, Stage1ArtifactSyncOutcome.SKIPPED_LOCKED,
            idempotency_digest_short=short, contract_version=version,
        )
    try:
        with conn.cursor() as cur:
            current = _load_raw_file(cur, raw_file_id)
            if current is None or role not in _expected_roles(current, {role}):
                return Stage1ArtifactSyncItemResult(
                    raw_file_id, role, Stage1ArtifactSyncOutcome.SKIPPED_NOT_REQUIRED,
                    idempotency_digest_short=short, contract_version=version,
                )
            existing = _artifact_rows(cur, raw_file_id, role)
        conn.commit()
        if len(existing) > 1:
            return _failure_item(
                current, role, Stage1ArtifactSyncOutcome.BLOCKED_AMBIGUOUS_EXISTING_ARTIFACT,
                category="multiple_existing_artifacts",
            )
        if len(existing) == 1:
            path = _source_path(current, role)
            if path is None or not path.is_file():
                return _failure_item(
                    current, role, Stage1ArtifactSyncOutcome.BLOCKED_SOURCE_MISSING,
                    category="durable_source_missing",
                )
            if existing[0].get("sha256") != _file_sha256(path):
                return _failure_item(
                    current, role, Stage1ArtifactSyncOutcome.BLOCKED_INVALID_LINEAGE,
                    category="existing_artifact_checksum_mismatch",
                )
            return Stage1ArtifactSyncItemResult(
                raw_file_id, role, Stage1ArtifactSyncOutcome.LINKED_EXISTING,
                artifact_id=existing[0]["artifact_id"], idempotency_status="existing",
                idempotency_digest_short=short, contract_version=version,
            )
        path = _source_path(current, role)
        if path is None or not path.is_file():
            return _failure_item(
                current, role, Stage1ArtifactSyncOutcome.BLOCKED_SOURCE_MISSING,
                category="durable_source_missing",
            )
        if dry_run:
            return Stage1ArtifactSyncItemResult(
                raw_file_id, role, Stage1ArtifactSyncOutcome.SKIPPED_NOT_REQUIRED,
                idempotency_digest_short=short, contract_version=version,
            )
        try:
            response = client.upload_artifact(
                str(path), kind="REPORT", run_id=run_id, raw_file_id=raw_file_id,
                workflow_name=WORKFLOW_NAME, stage_name=STAGE_NAME, artifact_role=role,
                report_type="unknown", original_filename=current.get("original_filename") or path.name,
                metadata=_metadata(current, role), idempotency_scope=scope,
                idempotency_key=key, structured_response=True,
            )
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 409:
                return _failure_item(
                    current, role, Stage1ArtifactSyncOutcome.FAILED_NON_RETRYABLE_CONFLICT,
                    category="idempotency_conflict",
                )
            return _failure_item(
                current, role, Stage1ArtifactSyncOutcome.FAILED_RETRYABLE_UPLOAD,
                category="artifact_http_failure",
            )
        except Exception:
            return _failure_item(
                current, role, Stage1ArtifactSyncOutcome.FAILED_RETRYABLE_UPLOAD,
                category="artifact_upload_failure",
            )
        if not isinstance(response, ArtifactUploadResult):
            return _failure_item(
                current, role, Stage1ArtifactSyncOutcome.FAILED_RETRYABLE_PERSISTENCE,
                category="invalid_artifact_response",
            )
        if response.idempotency_status not in {"created", "reused"}:
            return _failure_item(
                current, role, Stage1ArtifactSyncOutcome.FAILED_RETRYABLE_PERSISTENCE,
                category="invalid_idempotency_status",
            )
        outcome = (
            Stage1ArtifactSyncOutcome.REUSED
            if response.idempotency_status == "reused"
            else Stage1ArtifactSyncOutcome.CREATED
        )
        return Stage1ArtifactSyncItemResult(
            raw_file_id, role, outcome, artifact_id=response.artifact_id,
            idempotency_status=response.idempotency_status,
            idempotency_digest_short=short, contract_version=version,
        )
    finally:
        if not dry_run:
            _unlock(conn, raw_file_id, role)


def _load_raw_file(cur, raw_file_id: str) -> dict | None:
    cur.execute(
        """SELECT id::text AS raw_file_id, sha256, original_filename, report_key,
                  status, persisted, raw_path, normalized_csv_path,
                  stage1_normalized_artifact_metadata
             FROM ingest.raw_file WHERE id=%s""",
        (raw_file_id,),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def _candidate_rows(conn, *, raw_file_ids: list[str] | None, limit: int, role_filter: set[str] | None) -> list[dict]:
    if limit <= 0:
        raise ValueError("limit must be a positive integer")
    where = ["persisted=true", "status <> 'DUPLICATE_CONTENT'", "raw_path IS NOT NULL"]
    params: list[Any] = []
    if raw_file_ids:
        where.append("id=ANY(%s::uuid[])")
        params.append(raw_file_ids)
    else:
        where.append(
            """(
              (SELECT count(*) FROM artifacts a
                 WHERE a.raw_file_id=ingest.raw_file.id
                   AND a.workflow_name='workflow_b' AND a.stage_name='stage_1_fetch'
                   AND a.artifact_role='raw'
              ) <> 1
              OR (
                status='NORMALIZED' AND normalized_csv_path IS NOT NULL
                AND (SELECT count(*) FROM artifacts a
                   WHERE a.raw_file_id=ingest.raw_file.id
                     AND a.workflow_name='workflow_b' AND a.stage_name='stage_1_fetch'
                     AND a.artifact_role='normalized'
                ) <> 1
              )
            )"""
        )
    params.append(limit)
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT id::text AS raw_file_id, sha256, original_filename, report_key,
                       status, persisted, raw_path, normalized_csv_path,
                       stage1_normalized_artifact_metadata
                  FROM ingest.raw_file WHERE {' AND '.join(where)}
                 ORDER BY id LIMIT %s""",
            params,
        )
        rows = [dict(row) for row in cur.fetchall()]
    conn.rollback()
    return [row for row in rows if _expected_roles(row, role_filter)]


def reconcile_stage1_artifacts_batch(
    client,
    run_id: str,
    *,
    connection_factory: Callable[[], Any],
    raw_file_ids: list[str] | None = None,
    limit: int = 50,
    roles: list[str] | None = None,
    dry_run: bool = True,
) -> Stage1ArtifactReconciliationResult:
    role_filter = set(roles) if roles else None
    if role_filter and not role_filter.issubset(ROLE_CONFIG):
        raise ValueError("roles must contain only raw and/or normalized")
    conn = connection_factory()
    result = Stage1ArtifactReconciliationResult()
    try:
        rows = _candidate_rows(conn, raw_file_ids=raw_file_ids, limit=limit, role_filter=role_filter)
        result.records_discovered = len(rows)
        for row in rows:
            result.records_inspected += 1
            for role in _expected_roles(row, role_filter):
                result.items.append(_sync_role(conn, client, run_id, row, role, dry_run=dry_run))
    finally:
        conn.close()
    if not dry_run and any(
        item.retryable or item.operator_action_required for item in result.items
    ):
        raise Stage1ArtifactSyncError(result)
    return result
