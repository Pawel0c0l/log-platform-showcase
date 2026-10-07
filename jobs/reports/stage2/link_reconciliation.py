from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable

from api.artifacts.layout import sanitize_artifact_component


PLAN_SCHEMA_VERSION = "workflow_b.stage2.cleaned_link_plan.v1"
CANDIDATE_QUERY_CONTRACT_VERSION = "workflow_b.stage2.completed_unlinked.v1"
CLEANED_LINK_CONTRACT_VERSION = "workflow_b.stage2.cleaned.v1"
LOCK_NAMESPACE = "workflow_b.stage2.historical_cleaned_link_reconciliation.v1"
COMPLETED_RAW_STATUS = "NORMALIZED"
COMPLETED_STAGE2_STATUS = "OK"


class LinkClassification(StrEnum):
    SAFE_TO_LINK = "SAFE_TO_LINK"
    ALREADY_LINKED_VALID = "ALREADY_LINKED_VALID"
    BLOCKED_NO_ARTIFACT = "BLOCKED_NO_ARTIFACT"
    BLOCKED_MULTIPLE_COMPATIBLE_ARTIFACTS = "BLOCKED_MULTIPLE_COMPATIBLE_ARTIFACTS"
    BLOCKED_INCOMPATIBLE_ARTIFACT_METADATA = "BLOCKED_INCOMPATIBLE_ARTIFACT_METADATA"
    BLOCKED_OBJECT_MISSING = "BLOCKED_OBJECT_MISSING"
    BLOCKED_OBJECT_CHECKSUM_MISMATCH = "BLOCKED_OBJECT_CHECKSUM_MISMATCH"
    BLOCKED_OBJECT_SIZE_MISMATCH = "BLOCKED_OBJECT_SIZE_MISMATCH"
    BLOCKED_INVALID_RAW_FILE_STATE = "BLOCKED_INVALID_RAW_FILE_STATE"
    BLOCKED_EXISTING_LINK_CONFLICT = "BLOCKED_EXISTING_LINK_CONFLICT"
    BLOCKED_ENVIRONMENT_IDENTITY = "BLOCKED_ENVIRONMENT_IDENTITY"


@dataclass(frozen=True, slots=True)
class PlanEntry:
    raw_file_id: str
    proposed_artifact_id: str | None
    expected_stage2_status: str
    expected_link_is_null: bool
    classification: LinkClassification
    artifact_sha256: str | None = None
    artifact_size: int | None = None
    lineage_fingerprint: str | None = None
    object_exists: bool = False

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["classification"] = self.classification.value
        return value


@dataclass(slots=True)
class ReconciliationResult:
    environment_identity_verified: bool
    entries: list[PlanEntry]
    objects_checked: int = 0
    object_checks_passed: int = 0
    checksum_checks_passed: int = 0
    size_checks_passed: int = 0
    plan_path: str | None = None
    plan_digest: str | None = None
    database_state_changed: bool = False
    minio_state_changed: bool = False

    @property
    def classification_counts(self) -> dict[str, int]:
        return {item.value: sum(e.classification == item for e in self.entries) for item in LinkClassification}

    @property
    def candidate_count(self) -> int:
        return sum(e.expected_link_is_null for e in self.entries)

    @property
    def safe_to_link_count(self) -> int:
        return self.classification_counts[LinkClassification.SAFE_TO_LINK.value]

    @property
    def already_linked_valid_count(self) -> int:
        return self.classification_counts[LinkClassification.ALREADY_LINKED_VALID.value]

    @property
    def blocked_count(self) -> int:
        return sum(v for k, v in self.classification_counts.items() if k.startswith("BLOCKED_"))

    @property
    def execution_permitted(self) -> bool:
        return self.environment_identity_verified and self.candidate_count > 0 and self.blocked_count == 0 and self.safe_to_link_count == self.candidate_count

    def to_dict(self) -> dict[str, Any]:
        return {
            "environment_identity_verified": self.environment_identity_verified,
            "candidate_count": self.candidate_count,
            "already_linked_valid_count": self.already_linked_valid_count,
            "safe_to_link_count": self.safe_to_link_count,
            "blocked_count": self.blocked_count,
            "classification_counts": self.classification_counts,
            "objects_checked": self.objects_checked,
            "object_checks_passed": self.object_checks_passed,
            "checksum_checks_passed": self.checksum_checks_passed,
            "size_checks_passed": self.size_checks_passed,
            "plan_path": self.plan_path,
            "plan_digest": self.plan_digest,
            "execution_permitted": self.execution_permitted,
            "operator_action_required": self.blocked_count > 0,
            "entries": [e.to_dict() for e in self.entries],
            "database_state_changed": self.database_state_changed,
            "minio_state_changed": self.minio_state_changed,
        }


def advisory_lock_key() -> int:
    return int.from_bytes(hashlib.sha256(LOCK_NAMESPACE.encode()).digest()[:8], "big", signed=True)


def select_completed_rows(conn, *, for_update: bool = False) -> list[dict[str, Any]]:
    suffix = " FOR UPDATE" if for_update else ""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id::text AS raw_file_id, status, stage2_status, stage2_report_type,
                      client_code, sha256 AS source_identity,
                      stage2_cleaned_artifact_id::text AS stage2_cleaned_artifact_id
                 FROM ingest.raw_file
                WHERE status=%s AND stage2_status=%s
                ORDER BY id""" + suffix,
            (COMPLETED_RAW_STATUS, COMPLETED_STAGE2_STATUS),
        )
        return [dict(row) for row in cur.fetchall()]


def load_artifacts(conn, raw_file_id: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT artifact_id::text AS artifact_id, raw_file_id::text AS raw_file_id,
                      kind, content_type, size_bytes, sha256, storage_backend, storage_key,
                      workflow_name, stage_name, artifact_role, report_type, client_code,
                      layout_version, metadata_json, idempotency_scope, idempotency_key
                 FROM artifacts WHERE raw_file_id=%s ORDER BY artifact_id""",
            (raw_file_id,),
        )
        return [dict(row) for row in cur.fetchall()]


def artifact_compatible(raw: dict[str, Any], artifact: dict[str, Any]) -> bool:
    if artifact.get("raw_file_id") != raw.get("raw_file_id"):
        return False
    if (artifact.get("workflow_name"), artifact.get("stage_name"), artifact.get("artifact_role")) != ("workflow_b", "stage_2_clean", "cleaned"):
        return False
    if artifact.get("kind") not in {"REPORT", "REPORT_CLEANED"}:
        return False
    if artifact.get("content_type") not in {"text/csv", "application/csv", "application/vnd.ms-excel", "application/octet-stream"}:
        return False
    if not artifact.get("sha256") or int(artifact.get("size_bytes") or 0) <= 0:
        return False
    artifact_report_type = artifact.get("report_type")
    raw_report_type = raw.get("stage2_report_type")
    if artifact_report_type and raw_report_type and artifact_report_type not in {raw_report_type, sanitize_artifact_component(raw_report_type)}:
        return False
    for field, raw_field in (("client_code", "client_code"),):
        if artifact.get(field) and raw.get(raw_field) and artifact[field] != raw[raw_field]:
            return False
    metadata = artifact.get("metadata_json") or {}
    checks = {
        "workflow_name": "workflow_b", "stage_name": "stage_2_clean", "artifact_role": "cleaned",
        "raw_file_id": raw.get("raw_file_id"), "report_type": raw.get("stage2_report_type"),
        "client_code": raw.get("client_code"), "source_identity": raw.get("source_identity"),
    }
    for key, expected in checks.items():
        actual = metadata.get(key)
        if key == "report_type" and actual is not None and expected is not None and str(actual) in {str(expected), sanitize_artifact_component(str(expected))}:
            continue
        if actual is not None and expected is not None and str(actual) != str(expected):
            return False
    if metadata.get("cleaned_output_contract_version") not in {None, CLEANED_LINK_CONTRACT_VERSION}:
        return False
    if metadata.get("artifact_layout_version") not in {None, 1, 2}:
        return False
    return True


def _lineage_fingerprint(raw: dict[str, Any], artifact: dict[str, Any]) -> str:
    payload = [raw.get("raw_file_id"), artifact.get("artifact_id"), artifact.get("sha256"), artifact.get("size_bytes"), artifact.get("workflow_name"), artifact.get("stage_name"), artifact.get("artifact_role"), artifact.get("report_type"), artifact.get("client_code"), artifact.get("layout_version"), artifact.get("idempotency_scope")]
    return hashlib.sha256(json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()[:16]


def validate_object(s3, bucket: str, artifact: dict[str, Any]) -> LinkClassification | None:
    try:
        stat = s3.head_object(Bucket=bucket, Key=artifact["storage_key"])
    except Exception:
        return LinkClassification.BLOCKED_OBJECT_MISSING
    if int(stat.get("ContentLength", -1)) != int(artifact["size_bytes"]):
        return LinkClassification.BLOCKED_OBJECT_SIZE_MISMATCH
    metadata = {str(k).lower(): str(v) for k, v in (stat.get("Metadata") or {}).items()}
    object_sha = metadata.get("sha256") or metadata.get("x-amz-meta-sha256")
    if object_sha is None:
        digest = hashlib.sha256()
        response = s3.get_object(Bucket=bucket, Key=artifact["storage_key"])
        body = response["Body"]
        try:
            while True:
                chunk = body.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        finally:
            body.close()
        object_sha = digest.hexdigest()
    if object_sha != artifact["sha256"]:
        return LinkClassification.BLOCKED_OBJECT_CHECKSUM_MISMATCH
    return None


def reconcile_read_only(conn, s3, bucket: str) -> ReconciliationResult:
    entries: list[PlanEntry] = []
    result = ReconciliationResult(True, entries)
    for raw in select_completed_rows(conn):
        artifacts = load_artifacts(conn, raw["raw_file_id"])
        compatible = [a for a in artifacts if artifact_compatible(raw, a)]
        linked = raw.get("stage2_cleaned_artifact_id")
        if linked:
            match = next((a for a in compatible if a["artifact_id"] == linked), None)
            classification = LinkClassification.ALREADY_LINKED_VALID if match else LinkClassification.BLOCKED_EXISTING_LINK_CONFLICT
            entries.append(PlanEntry(raw["raw_file_id"], linked, raw["stage2_status"], False, classification))
            continue
        if not artifacts:
            entries.append(PlanEntry(raw["raw_file_id"], None, raw["stage2_status"], True, LinkClassification.BLOCKED_NO_ARTIFACT))
            continue
        if not compatible:
            entries.append(PlanEntry(raw["raw_file_id"], None, raw["stage2_status"], True, LinkClassification.BLOCKED_INCOMPATIBLE_ARTIFACT_METADATA))
            continue
        if len(compatible) > 1:
            entries.append(PlanEntry(raw["raw_file_id"], None, raw["stage2_status"], True, LinkClassification.BLOCKED_MULTIPLE_COMPATIBLE_ARTIFACTS))
            continue
        artifact = compatible[0]
        result.objects_checked += 1
        failure = validate_object(s3, bucket, artifact)
        if failure is None:
            result.object_checks_passed += 1
            result.checksum_checks_passed += 1
            result.size_checks_passed += 1
        entries.append(PlanEntry(raw["raw_file_id"], artifact["artifact_id"], raw["stage2_status"], True, failure or LinkClassification.SAFE_TO_LINK, artifact["sha256"], int(artifact["size_bytes"]), _lineage_fingerprint(raw, artifact), failure is None))
    entries.sort(key=lambda entry: entry.raw_file_id)
    return result


def canonical_payload(result: ReconciliationResult, *, environment: str, database_name: str, platform_identity_id: str, repository_commit: str) -> dict[str, Any]:
    return {
        "plan_schema_version": PLAN_SCHEMA_VERSION,
        "environment": environment,
        "database_name": database_name,
        "platform_identity_id": platform_identity_id,
        "repository_commit": repository_commit,
        "candidate_query_contract_version": CANDIDATE_QUERY_CONTRACT_VERSION,
        "cleaned_link_contract_version": CLEANED_LINK_CONTRACT_VERSION,
        "candidate_count": result.candidate_count,
        "safe_to_link_count": result.safe_to_link_count,
        "blocked_count": result.blocked_count,
        "classification_counts": result.classification_counts,
        "entries": [entry.to_dict() for entry in result.entries],
    }


def payload_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def write_plan(path: Path, payload: dict[str, Any], *, created_at: str) -> str:
    digest = payload_digest(payload)
    document = {"created_at": created_at, "plan_digest": digest, "plan_payload": payload}
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, (json.dumps(document, sort_keys=True, indent=2) + "\n").encode())
    finally:
        os.close(fd)
    os.chmod(path, 0o600)
    return digest


def execute_reviewed_plan(conn, s3, bucket: str, document: dict[str, Any], *, expected_digest: str, expect_count: int, environment: str, database_name: str, platform_identity_id: str) -> int:
    payload = document.get("plan_payload") or {}
    if payload_digest(payload) != expected_digest or document.get("plan_digest") != expected_digest:
        raise RuntimeError("reviewed plan digest mismatch")
    if (payload.get("environment"), payload.get("database_name"), payload.get("platform_identity_id")) != (environment, database_name, platform_identity_id):
        raise RuntimeError("reviewed plan environment identity mismatch")
    if payload.get("blocked_count") or payload.get("safe_to_link_count") != expect_count or payload.get("candidate_count") != expect_count:
        raise RuntimeError("reviewed plan count or blocked-state mismatch")
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s) AS locked", (advisory_lock_key(),))
        if not dict(cur.fetchone())["locked"]:
            raise RuntimeError("reconciliation advisory lock is held")
    try:
        current = reconcile_read_only(conn, s3, bucket)
        current_payload = canonical_payload(current, environment=environment, database_name=database_name, platform_identity_id=platform_identity_id, repository_commit=payload.get("repository_commit"))
        if current_payload != payload:
            raise RuntimeError("reviewed plan is stale")
        rows = select_completed_rows(conn, for_update=True)
        unlinked = {row["raw_file_id"]: row for row in rows if not row.get("stage2_cleaned_artifact_id")}
        if len(unlinked) != expect_count:
            raise RuntimeError("candidate count drift during transaction")
        with conn.cursor() as cur:
            affected = 0
            for entry in payload["entries"]:
                if entry["classification"] != LinkClassification.SAFE_TO_LINK.value:
                    continue
                cur.execute("UPDATE ingest.raw_file SET stage2_cleaned_artifact_id=%s WHERE id=%s AND status=%s AND stage2_status=%s AND stage2_cleaned_artifact_id IS NULL", (entry["proposed_artifact_id"], entry["raw_file_id"], COMPLETED_RAW_STATUS, COMPLETED_STAGE2_STATUS))
                affected += cur.rowcount
            if affected != expect_count:
                raise RuntimeError("affected-row count mismatch")
        conn.commit()
        return affected
    except Exception:
        conn.rollback()
        raise
    finally:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (advisory_lock_key(),))
        conn.commit()
