from __future__ import annotations

import hashlib
import json
import os
import re
import traceback
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests

from api.timezone_utils import set_pg_session_timezone
from jobs.api.telematics.secret_resolver import resolve_secret
from jobs.common.emailer import send_html_email
from jobs.reports.stage2.detector import DETECT_THRESHOLD, detect_report_type
from jobs.reports.stage2.io import read_csv_loose, split_into_tables
from jobs.reports.stage2.registry import (
    REGISTERED_REPORTS,
    get_report_cls,
    reconcile_registry,
)
from jobs.reports.stage2.scoring import compute_clean_score, compute_final_score, make_decision
from jobs.reports.stage2.validation import validate
from api.client import ArtifactUploadResult
from jobs.reports.stage2.batch_contract import (
    STAGE2_CLEANED_ARTIFACT_CONTRACT_VERSION,
    STAGE2_CLEANED_ARTIFACT_IDEMPOTENCY_SCOPE,
    Stage2BatchError,
    Stage2BatchResult,
    Stage2ItemResult,
    Stage2Outcome,
    cleaned_artifact_idempotency_key,
    has_batch_failures,
    is_retryable_historical_state,
    item_carries_operator_ownership,
    stage2_advisory_lock_key,
)


JOB_SOURCE = "jobs.reports.stage2.job_stage2"
DEFAULT_CANONICAL_DIR = "/home/logplatform/data/reports/canonical_csv"
DEFAULT_OUT_DIR = "/tmp/log-platform-stage2"
PENDING_REVIEW_REPORT_TYPE = "PENDING_REVIEW"
DEFAULT_PENDING_REVIEW_NOTIFY_TO = "owner@example.invalid"
DEFAULT_ARTIFACT_EXPLORER_BASE_URL = "http://localhost:8000"
_LOW_CONFIDENCE_NOTIFICATION_SENT_RUNS: set[str] = set()
LOW_CONFIDENCE_PENDING_REASON = "low_detection_confidence"
CLIENT_ID_MATCH_COLUMNS = ("registration", "chassis_number", "driver_name")
CLIENT_DETECTION_BATCH_SIZE = 1000
REPORT_207_TYPE = "report_207"
REPORT_207_RECORD_ID_COLUMNS = (
    "Data i czas",
    "Nr rejestracyjny",
    "Prędkość",
    "Ograniczenie prędkości drogowej",
    "Lokalizacja",
)


@dataclass(frozen=True)
class Stage2ClientAccount:
    client_id: str
    client_code: str
    client_name: str | None
    client_db_host: str
    client_db_port: int
    client_db_name: str
    client_db_user: str
    client_db_password_secret_ref: str
    client_db_schema: str


def _require_dependency(module: str, feature: str):
    try:
        return __import__(module)
    except ImportError as exc:
        raise RuntimeError(f"Missing dependency for {feature}: {module}") from exc


def _pg_conn():
    psycopg = _require_dependency("psycopg", "Postgres connection")
    from psycopg.rows import dict_row

    dsn = (
        f"host={os.getenv('POSTGRES_HOST', '127.0.0.1')} "
        f"port={os.getenv('POSTGRES_PORT', '5432')} "
        f"dbname={os.getenv('POSTGRES_DB', 'logdb')} "
        f"user={os.getenv('POSTGRES_USER', 'loguser')} "
        f"password={os.getenv('POSTGRES_PASSWORD', '')}"
    )
    return set_pg_session_timezone(psycopg.connect(dsn, row_factory=dict_row))


def _safe_ident(name: str) -> str:
    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", name or ""):
        raise ValueError(f"Unsafe SQL identifier: {name!r}")
    return name


def _client_business_pg_conn(account: Stage2ClientAccount):
    psycopg = _require_dependency("psycopg", "client business Postgres connection")
    return set_pg_session_timezone(psycopg.connect(
        host=account.client_db_host,
        port=account.client_db_port,
        dbname=account.client_db_name,
        user=account.client_db_user,
        password=resolve_secret(account.client_db_password_secret_ref),
    ))


def _load_stage2_registry_config(cur, *, report_type: str | None) -> dict:
    if not report_type:
        return {"id_sync_column_name": None, "record_id_ingredients": None}
    cur.execute("SELECT to_regclass(%s) AS rel", ("workflow_b_control.report_type_registry",))
    rel_row = cur.fetchone()
    if not rel_row or rel_row.get("rel") is None:
        return {
            "id_sync_column_name": None,
            "record_id_ingredients": None,
            "config_unavailable_reason": "report_type_registry_missing",
        }
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'workflow_b_control'
          AND table_name = 'report_type_registry'
          AND column_name = ANY(%s::text[])
        """,
        (["id_sync_column_name", "record_id_ingredients"],),
    )
    present = {row["column_name"] for row in cur.fetchall()}
    missing = sorted({"id_sync_column_name", "record_id_ingredients"} - present)
    if missing:
        return {
            "id_sync_column_name": None,
            "record_id_ingredients": None,
            "config_unavailable_reason": "report_type_registry_config_columns_missing",
            "missing_columns": missing,
        }
    cur.execute(
        """
        SELECT id_sync_column_name, record_id_ingredients
        FROM workflow_b_control.report_type_registry
        WHERE report_type = %s
        LIMIT 1
        """,
        (report_type,),
    )
    row = cur.fetchone()
    if not row:
        return {"id_sync_column_name": None, "record_id_ingredients": None, "registry_row_missing": True}
    return {
        "id_sync_column_name": row.get("id_sync_column_name"),
        "record_id_ingredients": row.get("record_id_ingredients"),
    }


def _load_enabled_client_accounts(cur) -> list[Stage2ClientAccount]:
    cur.execute(
        """
        SELECT
          client_id::text,
          client_code,
          client_name,
          client_db_host,
          client_db_port,
          client_db_name,
          client_db_user,
          client_db_password_secret_ref,
          client_db_schema
        FROM workflow_a_control.client_account
        WHERE enabled = true
          AND client_code IS NOT NULL
          AND btrim(client_code) <> ''
        ORDER BY client_code
        """
    )
    return [
        Stage2ClientAccount(
            client_id=row["client_id"],
            client_code=row["client_code"],
            client_name=row.get("client_name"),
            client_db_host=row["client_db_host"],
            client_db_port=int(row["client_db_port"]),
            client_db_name=row["client_db_name"],
            client_db_user=row["client_db_user"],
            client_db_password_secret_ref=row["client_db_password_secret_ref"],
            client_db_schema=row["client_db_schema"] or "public",
        )
        for row in cur.fetchall()
    ]


def _resolve_input_files(params: dict) -> list[Path]:
    input_files = params.get("input_files")
    if input_files:
        files = [Path(p) for p in input_files]
    else:
        input_dir = params.get("input_dir")
        if not input_dir:
            base = Path(os.getenv("REPORTS_DATA_DIR", os.path.dirname(DEFAULT_CANONICAL_DIR)))
            input_dir = str(base / "normalized")
        files = sorted(Path(input_dir).glob("**/*.csv"))

    limit = params.get("limit")
    if limit is not None:
        files = files[: int(limit)]
    return files


def _dataframe_row_count(df) -> int:
    try:
        return int(len(df.index))
    except AttributeError:
        return int(len(df))


def _canonical_record_value(value) -> str:
    if value is None:
        return ""
    try:
        if value != value:
            return ""
    except Exception:
        pass
    text = str(value).strip()
    if text.lower() in {"nan", "nat", "none"}:
        return ""
    return text


def _unique_non_empty_column_values(df, column_name: str) -> list[str]:
    if column_name not in list(df.columns):
        raise RuntimeError(
            f"Configured id_sync_column_name {column_name!r} does not exist in cleaned report columns"
        )
    seen: set[str] = set()
    out: list[str] = []
    for value in df[column_name].tolist():
        text = _canonical_record_value(value)
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _parse_record_id_ingredients(value: str | None) -> list[str]:
    if value is None:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def _record_id_for_values(values: list[str]) -> str:
    payload = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _record_id_for_report_207(canonical_key: tuple[str, ...], duplicate_ordinal: int) -> str:
    payload = json.dumps(
        [REPORT_207_TYPE, list(canonical_key), str(duplicate_ordinal)],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _add_report_207_record_id_column(cleaned_df, *, client, run_id: str, context: dict):
    df = cleaned_df.copy()
    if "record_id" in list(df.columns):
        df = df.drop(columns=["record_id"])

    missing = [column for column in REPORT_207_RECORD_ID_COLUMNS if column not in list(df.columns)]
    if missing:
        raise RuntimeError(
            "Report 207 record_id generation missing cleaned report columns: "
            + ", ".join(missing)
        )

    duplicate_counts: dict[tuple[str, ...], int] = {}
    record_ids = []
    duplicate_ordinals = []
    for _, row in df.iterrows():
        canonical_key = tuple(
            _canonical_record_value(row[column])
            for column in REPORT_207_RECORD_ID_COLUMNS
        )
        duplicate_ordinal = duplicate_counts.get(canonical_key, 0) + 1
        duplicate_counts[canonical_key] = duplicate_ordinal
        duplicate_ordinals.append(duplicate_ordinal)
        record_ids.append(_record_id_for_report_207(canonical_key, duplicate_ordinal))

    df["record_id"] = record_ids
    repeated_key_rows = sum(ordinal > 1 for ordinal in duplicate_ordinals)
    client.log(
        "INFO",
        "SCRIPT",
        JOB_SOURCE,
        "Stage2 Report 207 record_id generated",
        run_id=run_id,
        context={
            **context,
            "record_id_algorithm": "report_207_business_key_v2",
            "record_id_ingredients": list(REPORT_207_RECORD_ID_COLUMNS),
            "duplicate_ordinal_basis": "cleaned_row_order",
            "rows": len(record_ids),
            "repeated_business_key_rows": repeated_key_rows,
        },
    )
    return df


def _record_id_metadata_for_report(report_type: str | None, configured_ingredients: str | None) -> dict:
    if report_type == REPORT_207_TYPE:
        return {
            "record_id_algorithm": "report_207_business_key_v2",
            "record_id_ingredients": list(REPORT_207_RECORD_ID_COLUMNS),
            "duplicate_ordinal_basis": "cleaned_row_order",
        }
    return {
        "record_id_algorithm": "configured_ingredients_v1",
        "record_id_ingredients": _parse_record_id_ingredients(configured_ingredients),
        "duplicate_ordinal_basis": None,
    }


def _add_record_id_column(cleaned_df, *, record_id_ingredients: str | None, client, run_id: str, context: dict):
    if context.get("report_type") == REPORT_207_TYPE:
        return _add_report_207_record_id_column(
            cleaned_df,
            client=client,
            run_id=run_id,
            context=context,
        )

    ingredients = _parse_record_id_ingredients(record_id_ingredients)
    df = cleaned_df.copy()
    if "record_id" in list(df.columns):
        df = df.drop(columns=["record_id"])

    if not ingredients:
        df["record_id"] = None
        client.log(
            "WARNING",
            "SCRIPT",
            JOB_SOURCE,
            "Stage2 record_id generation skipped",
            run_id=run_id,
            context={**context, "reason": "record_id_ingredients_not_configured"},
        )
        return df

    missing = [column for column in ingredients if column not in list(df.columns)]
    if missing:
        raise RuntimeError(
            "Configured record_id_ingredients reference missing cleaned report columns: "
            + ", ".join(missing)
        )

    record_ids = []
    for _, row in df.iterrows():
        values = [_canonical_record_value(row[column]) for column in ingredients]
        record_ids.append(_record_id_for_values(values))
    df["record_id"] = record_ids
    client.log(
        "INFO",
        "SCRIPT",
        JOB_SOURCE,
        "Stage2 record_id generated",
        run_id=run_id,
        context={**context, "record_id_ingredients": ingredients, "rows": len(record_ids)},
    )
    return df


def _query_client_trip_matches(account: Stage2ClientAccount, candidate_values: list[str]) -> dict:
    schema = _safe_ident(account.client_db_schema or "public")
    matches = {"match_count": 0, "columns": set(), "sample_values": []}
    if not candidate_values:
        return matches

    conn = _client_business_pg_conn(account)
    try:
        with conn.cursor() as cur:
            for offset in range(0, len(candidate_values), CLIENT_DETECTION_BATCH_SIZE):
                batch = candidate_values[offset : offset + CLIENT_DETECTION_BATCH_SIZE]
                cur.execute(
                    f"""
                    SELECT DISTINCT match_column, match_value
                    FROM (
                      SELECT 'registration' AS match_column, btrim(registration::text) AS match_value
                      FROM {schema}.client_trips
                      WHERE NULLIF(btrim(registration::text), '') = ANY(%s::text[])
                      UNION ALL
                      SELECT 'chassis_number' AS match_column, btrim(chassis_number::text) AS match_value
                      FROM {schema}.client_trips
                      WHERE NULLIF(btrim(chassis_number::text), '') = ANY(%s::text[])
                      UNION ALL
                      SELECT 'driver_name' AS match_column, btrim(driver_name::text) AS match_value
                      FROM {schema}.client_trips
                      WHERE NULLIF(btrim(driver_name::text), '') = ANY(%s::text[])
                    ) AS matched
                    WHERE match_value IS NOT NULL
                    ORDER BY match_column, match_value
                    """,
                    (batch, batch, batch),
                )
                for row in cur.fetchall():
                    match_column = row[0]
                    match_value = row[1]
                    matches["match_count"] += 1
                    matches["columns"].add(match_column)
                    if len(matches["sample_values"]) < 5:
                        matches["sample_values"].append(match_value)
    finally:
        conn.close()
    return matches


def _resolve_client_code_for_cleaned_report(
    *,
    client,
    platform_cur,
    run_id: str,
    report_type: str | None,
    raw_file_id: str | None,
    cleaned_df,
    id_sync_column_name: str | None,
) -> str | None:
    context = {
        "report_type": report_type,
        "raw_file_id": raw_file_id,
        "id_sync_column_name": (id_sync_column_name or "").strip(),
    }
    configured_column = (id_sync_column_name or "").strip()
    if not configured_column:
        client.log(
            "WARNING",
            "SCRIPT",
            JOB_SOURCE,
            "Stage2 client_code detection skipped",
            run_id=run_id,
            context={**context, "reason": "id_sync_column_name_not_configured"},
        )
        return None

    candidate_values = _unique_non_empty_column_values(cleaned_df, configured_column)
    client.log(
        "INFO",
        "SCRIPT",
        JOB_SOURCE,
        "Stage2 client_code detection candidates",
        run_id=run_id,
        context={**context, "candidate_value_count": len(candidate_values)},
    )
    if not candidate_values:
        client.log(
            "WARNING",
            "SCRIPT",
            JOB_SOURCE,
            "Stage2 client_code detection found no usable report values",
            run_id=run_id,
            context=context,
        )
        return None

    accounts = _load_enabled_client_accounts(platform_cur)
    matches_by_client: dict[str, dict] = {}
    for account in accounts:
        try:
            matches = _query_client_trip_matches(account, candidate_values)
        except Exception as exc:
            raise RuntimeError(
                "client_code detection failed while querying client_trips "
                f"for client_code={account.client_code!r}: {exc}"
            ) from exc
        if matches["match_count"]:
            matches_by_client[account.client_code] = matches

    if not matches_by_client:
        client.log(
            "WARNING",
            "SCRIPT",
            JOB_SOURCE,
            "Stage2 client_code detection found no matching client",
            run_id=run_id,
            context={**context, "candidate_value_count": len(candidate_values), "client_accounts_checked": len(accounts)},
        )
        return None

    if len(matches_by_client) > 1:
        diagnostics = {
            code: {
                "match_count": data["match_count"],
                "columns": sorted(data["columns"]),
                "sample_values": data["sample_values"],
            }
            for code, data in sorted(matches_by_client.items())
        }
        client.log(
            "ERROR",
            "SCRIPT",
            JOB_SOURCE,
            "Stage2 ambiguous client_code detection",
            run_id=run_id,
            context={**context, "matches_by_client": diagnostics},
        )
        raise RuntimeError(
            "ambiguous client_code detection: matched client_codes="
            + ", ".join(sorted(matches_by_client))
        )

    client_code = next(iter(matches_by_client))
    data = matches_by_client[client_code]
    client.log(
        "INFO",
        "SCRIPT",
        JOB_SOURCE,
        "Stage2 client_code detected",
        run_id=run_id,
        context={
            **context,
            "candidate_value_count": len(candidate_values),
            "matched_client_code": client_code,
            "match_count": data["match_count"],
            "matched_columns": sorted(data["columns"]),
        },
    )
    return client_code


def _get_raw_file_id(cur, file_path: Path) -> str | None:
    """Resolve ingest.raw_file id for a normalized CSV path; returns None if not found."""
    metadata = _get_raw_file_metadata(cur, file_path)
    return metadata["raw_file_id"] if metadata else None


def _get_raw_file_metadata(cur, file_path: Path) -> dict | None:
    """Resolve ingest.raw_file metadata for a normalized CSV path."""
    cur.execute(
        """
        SELECT id, original_filename, sha256
        FROM ingest.raw_file
        WHERE normalized_csv_path = %s OR sha256 = %s
        LIMIT 1
        """,
        (str(file_path), file_path.stem),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {
        "raw_file_id": str(row["id"]),
        "original_filename": row.get("original_filename") or file_path.name,
        "source_identity": row.get("sha256"),
    }


def _upload_stage2_artifact(
    client,
    *,
    path: Path,
    run_id: str,
    raw_file_id: str | None,
    report_type: str | None,
    artifact_role: str,
    original_filename: str | None,
    client_code: str | None = None,
    metadata: dict | None = None,
) -> str | ArtifactUploadResult:
    upload_kwargs = {}
    if artifact_role == "cleaned":
        if not raw_file_id:
            raise RuntimeError("Stage 2 cleaned upload requires persisted raw_file_id")
        source_identity = (metadata or {}).get("source_identity")
        if not source_identity:
            raise RuntimeError("Stage 2 cleaned upload requires immutable source identity")
        upload_kwargs = {
            "idempotency_scope": STAGE2_CLEANED_ARTIFACT_IDEMPOTENCY_SCOPE,
            "idempotency_key": cleaned_artifact_idempotency_key(raw_file_id, source_identity),
            "structured_response": True,
        }
    return client.upload_artifact(
        str(path),
        kind="REPORT",
        run_id=run_id,
        raw_file_id=raw_file_id,
        workflow_name="workflow_b",
        stage_name="stage_2_clean",
        artifact_role=artifact_role,
        report_type=report_type,
        client_code=client_code,
        original_filename=original_filename or path.name,
        metadata=metadata,
        **upload_kwargs,
    )


def _persist_stage2(
    cur,
    *,
    file_path: Path,
    report_type: str | None,
    status: str,
    scores: dict,
    schema_diff: dict,
    pending_reason: str | None,
    outcome_category: str | None = None,
    retryable: bool | None = None,
    cleaned_artifact_id: str | None = None,
) -> None:
    cur.execute(
        """
        UPDATE ingest.raw_file
        SET
          stage2_status=%s,
          stage2_report_type=%s,
          stage2_scores=%s::jsonb,
          stage2_schema_diff=%s::jsonb,
          stage2_pending_reason=%s,
          stage2_outcome_category=%s,
          stage2_retryable=%s,
          stage2_cleaned_artifact_id=COALESCE(%s, stage2_cleaned_artifact_id),
          stage2_updated_at=NOW()
        WHERE normalized_csv_path=%s OR sha256=%s
        """,
        (
            status,
            report_type,
            json.dumps(scores, ensure_ascii=False),
            json.dumps(schema_diff, ensure_ascii=False),
            pending_reason,
            outcome_category,
            retryable,
            cleaned_artifact_id,
            str(file_path),
            file_path.stem,
        ),
    )


def _persist_raw_file_client_code(cur, *, file_path: Path, client_code: str | None) -> None:
    cur.execute(
        """
        UPDATE ingest.raw_file
        SET client_code=%s
        WHERE normalized_csv_path=%s OR sha256=%s
        """,
        (client_code, str(file_path), file_path.stem),
    )


def _save_cleaned(cleaned_df, *, source_path: Path, report_type: str) -> Path:
    out_dir = Path(DEFAULT_OUT_DIR) / "cleaned"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{source_path.stem}__{report_type}.csv"
    cleaned_df.to_csv(out_path, sep=";", index=False, encoding="utf-8-sig")
    return out_path


def _low_confidence_artifact_metadata(detection) -> dict:
    return {
        "detected_candidate_report_type": detection.report_type,
        "detect_score": detection.detect_score,
        "candidates_top3": detection.candidates_top3,
    }


def _record_low_confidence_pending_review(
    client,
    cur,
    *,
    path: Path,
    run_id: str,
    raw_file_id: str | None,
    original_filename: str | None,
    detection,
    pending_reviews: list[dict],
) -> None:
    scores = {
        "detect_score": detection.detect_score,
        "clean_score": 0.0,
        "schema_score": 0.0,
        "final_score": detection.detect_score,
    }
    schema_diff = {
        "missing_required": [],
        "extra_columns": [],
        "row_count": 0,
        "null_rate_by_col": {},
        "errors": ["low_detection_confidence"],
        "warnings": [],
        "candidates_top3": detection.candidates_top3,
    }
    _persist_stage2(
        cur,
        file_path=path,
        report_type=detection.report_type,
        status="PENDING_REVIEW",
        scores=scores,
        schema_diff=schema_diff,
        pending_reason=detection.pending_reason,
        outcome_category=Stage2Outcome.AMBIGUOUS_DETECTION.value,
        retryable=False,
    )
    artifact_id = _upload_stage2_artifact(
        client,
        path=path,
        run_id=run_id,
        raw_file_id=raw_file_id,
        report_type=PENDING_REVIEW_REPORT_TYPE,
        artifact_role="debug_sample",
        original_filename=original_filename,
        metadata=_low_confidence_artifact_metadata(detection),
    )
    pending_reviews.append(
        {
            "run_id": run_id,
            "raw_file_id": raw_file_id,
            "filename": str(path),
            "original_filename": original_filename or path.name,
            "detected_candidate_report_type": detection.report_type,
            "detect_score": detection.detect_score,
            "pending_reason": detection.pending_reason,
            "artifact_id": artifact_id,
        }
    )


def _fetch_low_confidence_pending_review_rows_for_run(cur, *, run_id: str) -> list[dict]:
    cur.execute(
        """
        SELECT DISTINCT ON (rf.id)
          a.run_id::text AS run_id,
          rf.id::text AS raw_file_id,
          COALESCE(rf.normalized_csv_path, rf.raw_path, a.filename) AS filename,
          rf.original_filename,
          COALESCE(
            a.metadata_json ->> 'detected_candidate_report_type',
            rf.stage2_report_type
          ) AS detected_candidate_report_type,
          COALESCE(
            a.metadata_json ->> 'detect_score',
            rf.stage2_scores ->> 'detect_score'
          ) AS detect_score,
          rf.stage2_pending_reason AS pending_reason,
          a.artifact_id::text AS artifact_id
        FROM artifacts a
        JOIN ingest.raw_file rf ON rf.id = a.raw_file_id
        WHERE a.run_id = %s
          AND a.workflow_name = 'workflow_b'
          AND a.stage_name = 'stage_2_clean'
          AND rf.stage2_status = 'PENDING_REVIEW'
          AND rf.stage2_pending_reason = %s
        ORDER BY
          rf.id,
          (
            a.artifact_role = 'debug_sample'
            AND a.report_type = 'PENDING_REVIEW'
          ) DESC,
          a.created_at DESC
        """,
        (run_id, LOW_CONFIDENCE_PENDING_REASON),
    )
    return [dict(row) for row in cur.fetchall()]


def _fetch_pending_review_reason_counts_for_run(cur, *, run_id: str) -> dict[str, int]:
    cur.execute(
        """
        SELECT COALESCE(rf.stage2_pending_reason, '<NULL>') AS pending_reason,
               COUNT(DISTINCT rf.id) AS count
        FROM artifacts a
        JOIN ingest.raw_file rf ON rf.id = a.raw_file_id
        WHERE a.run_id = %s
          AND a.workflow_name = 'workflow_b'
          AND a.stage_name = 'stage_2_clean'
          AND rf.stage2_status = 'PENDING_REVIEW'
        GROUP BY COALESCE(rf.stage2_pending_reason, '<NULL>')
        ORDER BY pending_reason
        """,
        (run_id,),
    )
    return {str(row["pending_reason"]): int(row["count"]) for row in cur.fetchall()}


def _merge_notification_rows(primary_rows: list[dict], fallback_rows: list[dict]) -> list[dict]:
    merged: list[dict] = []
    seen: set[tuple[str | None, str | None]] = set()
    for row in primary_rows + fallback_rows:
        key = (row.get("raw_file_id"), row.get("artifact_id"))
        if key in seen:
            continue
        seen.add(key)
        merged.append(row)
    return merged


def _artifact_explorer_base_url() -> str:
    return (os.getenv("ARTIFACT_EXPLORER_BASE_URL") or DEFAULT_ARTIFACT_EXPLORER_BASE_URL).rstrip("/")


def _artifact_explorer_link(*, artifact_id: str | None, run_id: str, raw_file_id: str | None) -> str:
    base_url = _artifact_explorer_base_url()
    if artifact_id:
        return f"{base_url}/artifact-explorer/artifacts/{artifact_id}"
    params = {"run_id": run_id}
    if raw_file_id:
        params["raw_file_id"] = raw_file_id
    return f"{base_url}/artifact-explorer?{urlencode(params)}"


def _build_low_confidence_notification_email(rows: list[dict]) -> tuple[str, str, str]:
    count = len(rows)
    subject = f"[Automations] Stage 2 pending review: low detection confidence ({count})"
    intro = (
        f"Stage 2 detected {count} report(s) that require manual review because "
        "the report type could not be detected with safe confidence."
    )
    header = (
        "<tr>"
        "<th>Run ID</th>"
        "<th>Filename</th>"
        "<th>Original filename</th>"
        "<th>Candidate type</th>"
        "<th>Score</th>"
        "<th>Reason</th>"
        "<th>Artifact Explorer</th>"
        "</tr>"
    )
    body_rows = []
    text_lines = [intro, ""]
    for row in rows:
        score = row.get("detect_score")
        score_text = f"{float(score):.4f}" if score is not None else ""
        link = _artifact_explorer_link(
            artifact_id=row.get("artifact_id"),
            run_id=str(row.get("run_id") or ""),
            raw_file_id=row.get("raw_file_id"),
        )
        body_rows.append(
            "<tr>"
            f"<td>{escape(str(row.get('run_id') or ''))}</td>"
            f"<td>{escape(str(row.get('filename') or ''))}</td>"
            f"<td>{escape(str(row.get('original_filename') or ''))}</td>"
            f"<td>{escape(str(row.get('detected_candidate_report_type') or ''))}</td>"
            f"<td>{escape(score_text)}</td>"
            f"<td>{escape(str(row.get('pending_reason') or ''))}</td>"
            f'<td><a href="{escape(link, quote=True)}">Open artifact</a></td>'
            "</tr>"
        )
        text_lines.append(
            " | ".join(
                [
                    str(row.get("run_id") or ""),
                    str(row.get("filename") or ""),
                    str(row.get("original_filename") or ""),
                    str(row.get("detected_candidate_report_type") or ""),
                    score_text,
                    str(row.get("pending_reason") or ""),
                    link,
                ]
            )
        )

    html_body = (
        "<html><body>"
        f"<p>{escape(intro)}</p>"
        '<table style="border-collapse:collapse;" border="1" cellpadding="6" cellspacing="0">'
        f"{header}{''.join(body_rows)}"
        "</table>"
        "</body></html>"
    )
    return subject, html_body, "\n".join(text_lines)


def _parse_notification_recipients(value: str | None) -> list[str]:
    if value is None:
        value = DEFAULT_PENDING_REVIEW_NOTIFY_TO
    return [addr.strip() for addr in value.replace(";", ",").split(",") if addr.strip()]


def _missing_smtp_config_vars() -> list[str]:
    missing = []
    if not (os.getenv("AUTOMATION_SMTP_HOST") or "").strip():
        missing.append("AUTOMATION_SMTP_HOST")
    return missing


def _notify_low_confidence_pending_reviews(
    client,
    *,
    run_id: str,
    rows: list[dict],
    db_eligible_count: int = 0,
    memory_collected_count: int = 0,
    pending_reason_counts: dict[str, int] | None = None,
) -> None:
    unsupported_reason_counts = {
        reason: count
        for reason, count in (pending_reason_counts or {}).items()
        if reason != LOW_CONFIDENCE_PENDING_REASON
    }
    if unsupported_reason_counts:
        client.log(
            "INFO",
            "SCRIPT",
            JOB_SOURCE,
            "Stage2 pending-review notification excludes unsupported pending reasons",
            run_id=run_id,
            context={"pending_reason_counts": unsupported_reason_counts},
        )

    client.log(
        "INFO",
        "SCRIPT",
        JOB_SOURCE,
        "Stage2 pending-review notification eligibility",
        run_id=run_id,
        context={
            "eligible_count": len(rows),
            "db_eligible_count": db_eligible_count,
            "memory_collected_count": memory_collected_count,
            "pending_reason": LOW_CONFIDENCE_PENDING_REASON,
        },
    )

    if not rows:
        client.log(
            "INFO",
            "SCRIPT",
            JOB_SOURCE,
            "Stage2 pending-review notification skipped",
            run_id=run_id,
            context={"reason": "zero_rows"},
        )
        return
    # The platform has no run-level notification state table. This prevents
    # duplicate sends within a single long-lived job process; retried processes
    # may send again for the same runner-created run_id.
    if run_id in _LOW_CONFIDENCE_NOTIFICATION_SENT_RUNS:
        client.log(
            "INFO",
            "SCRIPT",
            JOB_SOURCE,
            "Stage2 pending-review notification skipped",
            run_id=run_id,
            context={"reason": "duplicate_run_id"},
        )
        return

    recipients = _parse_notification_recipients(os.getenv("STAGE2_PENDING_REVIEW_NOTIFY_TO"))
    if not recipients:
        client.log(
            "WARNING",
            "SCRIPT",
            JOB_SOURCE,
            "Stage2 pending-review notification skipped",
            run_id=run_id,
            context={"reason": "missing_recipient", "missing_env": ["STAGE2_PENDING_REVIEW_NOTIFY_TO"]},
        )
        return

    missing_smtp = _missing_smtp_config_vars()
    if missing_smtp:
        client.log(
            "WARNING",
            "SCRIPT",
            JOB_SOURCE,
            "Stage2 pending-review notification skipped",
            run_id=run_id,
            context={"reason": "missing_smtp_config", "missing_env": missing_smtp},
        )
        return

    subject, html_body, text_body = _build_low_confidence_notification_email(rows)
    try:
        send_html_email(
            to_addrs=recipients,
            subject=subject,
            html_body=html_body,
            text_body=text_body,
        )
    except Exception as exc:
        client.log(
            "WARNING",
            "SCRIPT",
            JOB_SOURCE,
            "Stage2 pending-review notification email failed",
            run_id=run_id,
            context={"pending_review_count": len(rows), "error": str(exc)[:400]},
        )
        return

    _LOW_CONFIDENCE_NOTIFICATION_SENT_RUNS.add(run_id)
    client.log(
        "INFO",
        "SCRIPT",
        JOB_SOURCE,
        "Stage2 pending-review notification email sent",
        run_id=run_id,
        context={"pending_review_count": len(rows)},
    )


def _log_registry_reconciliation(client, *, cur, run_id: str) -> None:
    """Log how the Python registry compares to ``report_type_registry`` rows.

    The Python registry stays authoritative for runtime, but operators should
    see drift quickly: rows enabled in the DB that the Python code does not
    know about, Python types missing from the DB, and disagreeing cleaner
    entrypoints.
    """

    try:
        report = reconcile_registry(cur)
    except Exception as exc:
        client.log(
            "WARNING",
            "SCRIPT",
            JOB_SOURCE,
            "Stage2 registry reconciliation failed",
            run_id=run_id,
            context={"error": str(exc)[:400]},
        )
        return

    db_summary = {
        report_type: {
            "enabled": defn.enabled,
            "implementation_status": defn.implementation_status,
            "cleaner_entrypoint": defn.cleaner_entrypoint,
        }
        for report_type, defn in sorted(report.db_definitions.items())
    }
    client.log(
        "INFO",
        "SCRIPT",
        JOB_SOURCE,
        "Stage2 registry reconciliation",
        run_id=run_id,
        context={
            "python_registered_count": len(REGISTERED_REPORTS),
            "python_registered_types": [cls.TYPE for cls in REGISTERED_REPORTS],
            "db_registered_count": len(report.db_definitions),
            "db_registered_types": sorted(report.db_definitions.keys()),
            "db_unavailable_reason": report.db_unavailable_reason,
            "db_summary": db_summary,
        },
    )
    if report.db_only:
        client.log(
            "WARNING",
            "SCRIPT",
            JOB_SOURCE,
            "Stage2 registry drift: report types present in DB but missing from Python registry (cannot run at runtime)",
            run_id=run_id,
            context={"db_only_report_types": report.db_only},
        )
    if report.python_only:
        client.log(
            "WARNING",
            "SCRIPT",
            JOB_SOURCE,
            "Stage2 registry drift: report types present in Python registry but missing from DB registry (DB read-model is stale)",
            run_id=run_id,
            context={"python_only_report_types": report.python_only},
        )
    if report.cleaner_mismatches:
        client.log(
            "WARNING",
            "SCRIPT",
            JOB_SOURCE,
            "Stage2 registry drift: cleaner entrypoint mismatch between DB and Python",
            run_id=run_id,
            context={"cleaner_mismatches": report.cleaner_mismatches},
        )


def _normalized_csv_preview(raw_df, *, max_rows: int = 8, max_cells_per_row: int = 12) -> list[list[str]]:
    """Return the first ``max_rows`` rows as plain strings for diagnostics."""

    preview: list[list[str]] = []
    for i in range(min(len(raw_df), max_rows)):
        row = raw_df.iloc[i].tolist()
        cells: list[str] = []
        for j, value in enumerate(row):
            if j >= max_cells_per_row:
                break
            cells.append(str(value)[:80])
        preview.append(cells)
    return preview


def _detection_candidates_full(tables) -> list[dict]:
    candidates: list[dict] = []
    for report_cls in REGISTERED_REPORTS:
        try:
            score = float(report_cls.detect(tables))
        except Exception as exc:
            candidates.append(
                {"report_type": report_cls.TYPE, "score": 0.0, "error": str(exc)[:200]}
            )
            continue
        candidates.append(
            {"report_type": report_cls.TYPE, "score": round(max(0.0, min(score, 1.0)), 4)}
        )
    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates


def _run_single_legacy(client, run_id: str, params: dict):
    files = _resolve_input_files(params)
    low_confidence_pending_reviews: list[dict] = []
    db_low_confidence_pending_reviews: list[dict] = []
    pending_reason_counts: dict[str, int] = {}
    debug_detection = bool(params.get("debug_detection", False))
    client.log(
        "INFO",
        "SCRIPT",
        JOB_SOURCE,
        "Stage2.1 started",
        run_id=run_id,
        context={
            "files": len(files),
            "detect_threshold": DETECT_THRESHOLD,
            "debug_detection": debug_detection,
        },
    )

    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            _log_registry_reconciliation(client, cur=cur, run_id=run_id)
            for path in files:
                try:
                    raw_file_metadata = _get_raw_file_metadata(cur, path)
                    raw_file_id = raw_file_metadata["raw_file_id"] if raw_file_metadata else None
                    original_filename = raw_file_metadata["original_filename"] if raw_file_metadata else path.name
                    if raw_file_id is None:
                        detail = (
                            "input_files mode does not prepare Stage 3 unless the supplied path matches "
                            "ingest.raw_file.normalized_csv_path or sha256; no raw_file-linked cleaned artifact will be persisted"
                            if params.get("input_files")
                            else "artifact will have no raw_file_id"
                        )
                        client.log(
                            "WARNING",
                            "SCRIPT",
                            JOB_SOURCE,
                            "No ingest.raw_file row for path; " + detail,
                            run_id=run_id,
                            context={"path": str(path)},
                        )
                    client.log("INFO", "SCRIPT", JOB_SOURCE, "Processing file", run_id=run_id, context={"path": str(path)})

                    raw_df = read_csv_loose(str(path))
                    tables = split_into_tables(raw_df)

                    detection = detect_report_type(tables)
                    if debug_detection or detection.pending_reason:
                        client.log(
                            "INFO",
                            "SCRIPT",
                            JOB_SOURCE,
                            "Stage2 detection diagnostics",
                            run_id=run_id,
                            context={
                                "path": str(path),
                                "raw_file_id": raw_file_id,
                                "table_count": len(tables),
                                "first_table_rows": len(tables[0]) if tables else 0,
                                "first_table_columns": list(tables[0].columns) if tables else [],
                                "csv_preview_rows": _normalized_csv_preview(raw_df),
                                "candidates_full": _detection_candidates_full(tables),
                                "detected_report_type": detection.report_type,
                                "detect_score": detection.detect_score,
                                "pending_reason": detection.pending_reason,
                                "detect_threshold": DETECT_THRESHOLD,
                            },
                        )

                    if detection.pending_reason:
                        _record_low_confidence_pending_review(
                            client,
                            cur,
                            path=path,
                            run_id=run_id,
                            raw_file_id=raw_file_id,
                            original_filename=original_filename,
                            detection=detection,
                            pending_reviews=low_confidence_pending_reviews,
                        )
                        client.log(
                            "WARNING",
                            "SCRIPT",
                            JOB_SOURCE,
                            "Pending review due to low detection confidence",
                            run_id=run_id,
                            context={
                                "path": str(path),
                                "report_type": detection.report_type,
                                "detect_score": detection.detect_score,
                                "candidates_top3": detection.candidates_top3,
                            },
                        )
                        continue

                    report_cls = get_report_cls(detection.report_type or "")
                    if report_cls is None:
                        raise RuntimeError(f"Unregistered report type: {detection.report_type}")

                    try:
                        cleaned = report_cls.clean(tables if getattr(report_cls, "MULTI_TABLE", False) else tables[0])
                    except NotImplementedError as exc:
                        scores = {
                            "detect_score": detection.detect_score,
                            "clean_score": 0.0,
                            "schema_score": 0.0,
                            "final_score": detection.detect_score,
                        }
                        schema_diff = {
                            "missing_required": [],
                            "extra_columns": [],
                            "row_count": 0,
                            "null_rate_by_col": {},
                            "errors": ["cleaning_not_implemented"],
                            "warnings": [],
                            "candidates_top3": detection.candidates_top3,
                        }
                        _persist_stage2(
                            cur,
                            file_path=path,
                            report_type=detection.report_type,
                            status="PENDING_REVIEW",
                            scores=scores,
                            schema_diff=schema_diff,
                            pending_reason="cleaning_not_implemented",
                            outcome_category=Stage2Outcome.UNSUPPORTED_REPORT.value,
                            retryable=False,
                        )
                        _upload_stage2_artifact(
                            client,
                            path=path,
                            run_id=run_id,
                            raw_file_id=raw_file_id,
                            report_type=detection.report_type,
                            artifact_role="debug_sample",
                            original_filename=original_filename,
                        )
                        client.log(
                            "WARNING",
                            "SCRIPT",
                            JOB_SOURCE,
                            "Pending review due to TODO cleaner",
                            run_id=run_id,
                            context={"path": str(path), "report_type": detection.report_type, "error": str(exc)},
                        )
                        continue

                    validation = validate(cleaned, report_cls)
                    clean_score = compute_clean_score(cleaned, report_cls)
                    final_score = compute_final_score(
                        detect_score=detection.detect_score,
                        clean_score=clean_score,
                        schema_score=validation.schema_score,
                    )
                    decision = make_decision(final_score=final_score, validation=validation)

                    scores = {
                        "detect_score": detection.detect_score,
                        "clean_score": clean_score,
                        "schema_score": validation.schema_score,
                        "final_score": final_score,
                    }
                    schema_diff = dict(validation.schema_diff)
                    schema_diff["candidates_top3"] = detection.candidates_top3

                    registry_config = _load_stage2_registry_config(cur, report_type=detection.report_type)
                    if registry_config.get("config_unavailable_reason"):
                        client.log(
                            "WARNING",
                            "SCRIPT",
                            JOB_SOURCE,
                            "Stage2 registry runtime config unavailable",
                            run_id=run_id,
                            context={
                                "path": str(path),
                                "report_type": detection.report_type,
                                "raw_file_id": raw_file_id,
                                "reason": registry_config.get("config_unavailable_reason"),
                                "missing_columns": registry_config.get("missing_columns"),
                            },
                        )
                    if registry_config.get("registry_row_missing"):
                        client.log(
                            "WARNING",
                            "SCRIPT",
                            JOB_SOURCE,
                            "Stage2 registry row missing for report type",
                            run_id=run_id,
                            context={"path": str(path), "report_type": detection.report_type, "raw_file_id": raw_file_id},
                        )

                    cleaned.df = _add_record_id_column(
                        cleaned.df,
                        record_id_ingredients=registry_config.get("record_id_ingredients"),
                        client=client,
                        run_id=run_id,
                        context={"path": str(path), "report_type": detection.report_type, "raw_file_id": raw_file_id},
                    )
                    record_id_metadata = _record_id_metadata_for_report(
                        detection.report_type,
                        registry_config.get("record_id_ingredients"),
                    )
                    record_id_ingredients = record_id_metadata["record_id_ingredients"]

                    resolved_client_code = _resolve_client_code_for_cleaned_report(
                        client=client,
                        platform_cur=cur,
                        run_id=run_id,
                        report_type=detection.report_type,
                        raw_file_id=raw_file_id,
                        cleaned_df=cleaned.df,
                        id_sync_column_name=registry_config.get("id_sync_column_name"),
                    )
                    if not resolved_client_code:
                        default_client_code = getattr(report_cls, "DEFAULT_CLIENT_CODE", None)
                        if default_client_code:
                            resolved_client_code = str(default_client_code).strip() or None
                            client.log(
                                "INFO",
                                "SCRIPT",
                                JOB_SOURCE,
                                "Stage2 using report default client_code",
                                run_id=run_id,
                                context={
                                    "path": str(path),
                                    "report_type": detection.report_type,
                                    "raw_file_id": raw_file_id,
                                    "client_code": resolved_client_code,
                                },
                            )
                    _persist_raw_file_client_code(cur, file_path=path, client_code=resolved_client_code)
                    cleaned.metadata["client_code"] = resolved_client_code
                    cleaned.metadata["id_sync_column_name"] = (
                        (registry_config.get("id_sync_column_name") or "").strip() or None
                    )
                    cleaned.metadata["record_id_ingredients"] = record_id_ingredients
                    cleaned.metadata["record_id_algorithm"] = record_id_metadata["record_id_algorithm"]
                    cleaned.metadata["duplicate_ordinal_basis"] = record_id_metadata["duplicate_ordinal_basis"]

                    out_cleaned = _save_cleaned(cleaned.df, source_path=path, report_type=detection.report_type or "unknown")
                    rows_output = _dataframe_row_count(cleaned.df)
                    cleaned_artifact_id = _upload_stage2_artifact(
                        client,
                        path=out_cleaned,
                        run_id=run_id,
                        raw_file_id=raw_file_id,
                        report_type=detection.report_type,
                        artifact_role="cleaned",
                        original_filename=original_filename,
                        client_code=resolved_client_code,
                        metadata={
                            "workflow_name": "workflow_b",
                            "stage_name": "stage_2_clean",
                            "artifact_role": "cleaned",
                            "raw_file_id": raw_file_id,
                            "report_type": detection.report_type,
                            "client_code": resolved_client_code,
                            "id_sync_column_name": cleaned.metadata["id_sync_column_name"],
                            "record_id_ingredients": record_id_ingredients,
                            "record_id_algorithm": cleaned.metadata["record_id_algorithm"],
                            "duplicate_ordinal_basis": cleaned.metadata["duplicate_ordinal_basis"],
                            "rows_output": rows_output,
                            "source_identity": raw_file_metadata["source_identity"],
                            "artifact_layout_version": 2,
                            "cleaned_output_contract_version": STAGE2_CLEANED_ARTIFACT_CONTRACT_VERSION,
                        },
                    )
                    if not isinstance(cleaned_artifact_id, ArtifactUploadResult):
                        raise RuntimeError("Keyed Stage 2 upload did not return structured result")
                    client.log(
                        "INFO",
                        "SCRIPT",
                        JOB_SOURCE,
                        "Stage2 cleaned artifact uploaded",
                        run_id=run_id,
                        context={
                            "raw_file_id": raw_file_id,
                            "detected_report_type": detection.report_type,
                            "client_code": resolved_client_code,
                            "cleaned_artifact_id": cleaned_artifact_id.artifact_id,
                            "artifact_idempotency_status": cleaned_artifact_id.idempotency_status,
                            "artifact_role": "cleaned",
                            "artifact_stage_name": "stage_2_clean",
                            "artifact_path": str(out_cleaned),
                            "rows_output": rows_output,
                            "artifact_committed": True,
                        },
                    )
                    if decision.status == "PENDING_REVIEW":
                        _upload_stage2_artifact(
                            client,
                            path=path,
                            run_id=run_id,
                            raw_file_id=raw_file_id,
                            report_type=detection.report_type,
                            artifact_role="debug_sample",
                            original_filename=original_filename,
                            client_code=resolved_client_code,
                        )

                    _persist_stage2(
                        cur,
                        file_path=path,
                        report_type=detection.report_type,
                        status=decision.status,
                        scores=scores,
                        schema_diff=schema_diff,
                        pending_reason=decision.pending_reason,
                        outcome_category=(
                            Stage2Outcome.SUCCEEDED_REUSED.value
                            if decision.status == "OK" and cleaned_artifact_id.idempotency_status == "reused"
                            else Stage2Outcome.SUCCEEDED_CREATED.value
                            if decision.status == "OK"
                            else Stage2Outcome.REJECTED_VALIDATION.value
                        ),
                        retryable=False,
                        cleaned_artifact_id=cleaned_artifact_id.artifact_id,
                    )

                    client.log(
                        "INFO",
                        "SCRIPT",
                        JOB_SOURCE,
                        "File processed",
                        run_id=run_id,
                        context={
                            "path": str(path),
                            "report_type": detection.report_type,
                            "final_score": final_score,
                            "status": decision.status,
                            "client_code": resolved_client_code,
                        },
                    )

                    if validation.warnings or decision.warning:
                        client.log(
                            "WARNING",
                            "SCRIPT",
                            JOB_SOURCE,
                            "Stage2 drift warning",
                            run_id=run_id,
                            context={
                                "path": str(path),
                                "warnings": validation.warnings,
                                "decision_warning": decision.warning,
                            },
                        )

                except Exception as exc:
                    tb_max = 12000
                    tb_text = traceback.format_exc()
                    if len(tb_text) > tb_max:
                        tb_text = tb_text[:tb_max] + "\n...[traceback truncated]"
                    client.log(
                        "ERROR",
                        "SCRIPT",
                        JOB_SOURCE,
                        "Stage2 file processing failed",
                        run_id=run_id,
                        context={"path": str(path), "error": str(exc)[:600]},
                        error=tb_text,
                    )
                    conflict = isinstance(exc, requests.HTTPError) and exc.response is not None and exc.response.status_code == 409
                    non_retryable = isinstance(exc, (KeyError, TypeError)) or any(
                        marker in str(exc)
                        for marker in (
                            "Unsafe SQL identifier",
                            "record_id_ingredients",
                            "id_sync_column_name",
                            "ambiguous client_code detection",
                            "Unregistered report type",
                        )
                    )
                    _persist_stage2(
                        cur,
                        file_path=path,
                        report_type=None,
                        status="PENDING_REVIEW",
                        scores={"detect_score": 0.0, "clean_score": 0.0, "schema_score": 0.0, "final_score": 0.0},
                        schema_diff={
                            "missing_required": [],
                            "extra_columns": [],
                            "row_count": 0,
                            "null_rate_by_col": {},
                            "errors": ["stage2_exception"],
                            "warnings": [],
                        },
                        pending_reason=("stage2_idempotency_conflict" if conflict else "stage2_exception"),
                        outcome_category=(
                            Stage2Outcome.FAILED_IDEMPOTENCY_CONFLICT.value
                            if conflict
                            else Stage2Outcome.FAILED_NON_RETRYABLE.value
                            if non_retryable
                            else Stage2Outcome.FAILED_RETRYABLE.value
                        ),
                        retryable=False if conflict or non_retryable else True,
                    )

            conn.commit()
            try:
                db_low_confidence_pending_reviews = _fetch_low_confidence_pending_review_rows_for_run(cur, run_id=run_id)
                pending_reason_counts = _fetch_pending_review_reason_counts_for_run(cur, run_id=run_id)
            except Exception as exc:
                client.log(
                    "WARNING",
                    "SCRIPT",
                    JOB_SOURCE,
                    "Stage2 pending-review notification DB lookup failed",
                    run_id=run_id,
                    context={
                        "error": str(exc)[:400],
                        "memory_collected_count": len(low_confidence_pending_reviews),
                    },
                )
    finally:
        conn.close()

    notification_rows = _merge_notification_rows(
        db_low_confidence_pending_reviews,
        low_confidence_pending_reviews,
    )
    _notify_low_confidence_pending_reviews(
        client,
        run_id=run_id,
        rows=notification_rows,
        db_eligible_count=len(db_low_confidence_pending_reviews),
        memory_collected_count=len(low_confidence_pending_reviews),
        pending_reason_counts=pending_reason_counts,
    )
    client.log("INFO", "SCRIPT", JOB_SOURCE, "Stage2.1 finished", run_id=run_id, context={"files": len(files)})


def _row_value(row, key: str, index: int = 0):
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[key]
    except (TypeError, KeyError):
        return row[index]


def _candidate_rows(conn, params: dict) -> list[dict]:
    limit = int(params.get("limit", 100))
    if limit <= 0:
        raise ValueError("limit must be a positive integer")
    retry_technical = bool(params.get("retry_technical_failures", True))
    raw_file_ids = [str(value) for value in (params.get("raw_file_ids") or [])]
    paths = _resolve_input_files(params) if params.get("input_files") or params.get("input_dir") else []

    with conn.cursor() as cur:
        if paths:
            rows = []
            seen = set()
            for path in paths:
                cur.execute(
                    """SELECT id::text AS raw_file_id, normalized_csv_path, original_filename,
                              sha256 AS source_identity, status, stage2_status, stage2_report_type,
                              stage2_pending_reason, stage2_outcome_category, stage2_retryable,
                              stage2_cleaned_artifact_id::text AS stage2_cleaned_artifact_id, client_code
                         FROM ingest.raw_file
                        WHERE normalized_csv_path = %s OR sha256 = %s
                        ORDER BY id""",
                    (str(path), path.stem),
                )
                matches = list(cur.fetchall())
                if len(matches) != 1:
                    raise ValueError(f"Manual Stage 2 path must resolve to exactly one raw_file_id: {path}")
                item = dict(matches[0])
                if item["raw_file_id"] not in seen:
                    rows.append(item)
                    seen.add(item["raw_file_id"])
            return rows[:limit]

        predicates = ["status = 'NORMALIZED'"]
        values: list = []
        if raw_file_ids:
            predicates.append("id = ANY(%s::uuid[])")
            values.append(raw_file_ids)
        else:
            eligible = ["stage2_status IS NULL"]
            if retry_technical:
                eligible.extend([
                    "stage2_retryable = true",
                    "(stage2_status = 'PENDING_REVIEW' AND stage2_pending_reason = 'stage2_exception' AND stage2_retryable IS NULL)",
                ])
            predicates.append("(" + " OR ".join(eligible) + ")")
        values.append(limit)
        cur.execute(
            f"""SELECT id::text AS raw_file_id, normalized_csv_path, original_filename,
                       sha256 AS source_identity, status, stage2_status, stage2_report_type,
                       stage2_pending_reason, stage2_outcome_category, stage2_retryable,
                       stage2_cleaned_artifact_id::text AS stage2_cleaned_artifact_id, client_code
                  FROM ingest.raw_file
                 WHERE {' AND '.join(predicates)}
                 ORDER BY id
                 LIMIT %s""",
            values,
        )
        return [dict(row) for row in cur.fetchall()]


def _valid_cleaned_artifact_id(cur, row: dict) -> str | None:
    artifact_id = row.get("stage2_cleaned_artifact_id")
    if artifact_id:
        cur.execute(
            "SELECT artifact_id::text FROM artifacts WHERE artifact_id=%s AND raw_file_id=%s",
            (artifact_id, row["raw_file_id"]),
        )
        found = cur.fetchone()
        if found:
            return str(_row_value(found, "artifact_id"))
    cur.execute(
        """SELECT artifact_id::text AS artifact_id
             FROM artifacts
            WHERE raw_file_id=%s AND workflow_name='workflow_b'
              AND stage_name='stage_2_clean' AND artifact_role='cleaned'
            ORDER BY created_at DESC LIMIT 1""",
        (row["raw_file_id"],),
    )
    found = cur.fetchone()
    return str(_row_value(found, "artifact_id")) if found else None


def _acquire_item_lock(conn, raw_file_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (stage2_advisory_lock_key(raw_file_id),))
        return bool(_row_value(cur.fetchone(), "pg_try_advisory_lock"))


def _release_item_lock(conn, raw_file_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_unlock(%s)", (stage2_advisory_lock_key(raw_file_id),))


def _refresh_raw_file(conn, raw_file_id: str) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id::text AS raw_file_id, normalized_csv_path, original_filename,
                      sha256 AS source_identity, status, stage2_status, stage2_report_type,
                      stage2_pending_reason, stage2_outcome_category, stage2_retryable,
                      stage2_cleaned_artifact_id::text AS stage2_cleaned_artifact_id, client_code
                 FROM ingest.raw_file WHERE id=%s""",
            (raw_file_id,),
        )
        row = cur.fetchone()
    if not row:
        raise RuntimeError(f"raw_file_id disappeared during Stage 2 claim: {raw_file_id}")
    return dict(row)


def _item_from_persisted(row: dict) -> Stage2ItemResult:
    category = row.get("stage2_outcome_category")
    try:
        outcome = Stage2Outcome(category)
    except (TypeError, ValueError):
        if row.get("stage2_status") == "OK":
            outcome = Stage2Outcome.SUCCEEDED_CREATED
        elif row.get("stage2_pending_reason") == LOW_CONFIDENCE_PENDING_REASON:
            outcome = Stage2Outcome.AMBIGUOUS_DETECTION
        elif row.get("stage2_pending_reason") == "cleaning_not_implemented":
            outcome = Stage2Outcome.UNSUPPORTED_REPORT
        elif is_retryable_historical_state(row.get("stage2_status"), row.get("stage2_pending_reason")):
            outcome = Stage2Outcome.FAILED_RETRYABLE
        else:
            outcome = Stage2Outcome.PENDING_HUMAN_REVIEW
    artifact_status = (
        "reused" if outcome == Stage2Outcome.SUCCEEDED_REUSED
        else "created" if outcome == Stage2Outcome.SUCCEEDED_CREATED
        else "none"
    )
    key = cleaned_artifact_idempotency_key(row["raw_file_id"], row["source_identity"])
    return Stage2ItemResult(
        raw_file_id=row["raw_file_id"], client_code=row.get("client_code"),
        report_type=row.get("stage2_report_type"), outcome=outcome,
        persisted_status=row.get("stage2_status"), reason_code=row.get("stage2_pending_reason"),
        retryable=bool(row.get("stage2_retryable")),
        review_required=outcome in {
            Stage2Outcome.PENDING_HUMAN_REVIEW, Stage2Outcome.REJECTED_VALIDATION,
            Stage2Outcome.UNSUPPORTED_REPORT, Stage2Outcome.AMBIGUOUS_DETECTION,
        },
        artifact_id=row.get("stage2_cleaned_artifact_id"),
        artifact_idempotency_status=artifact_status,
        idempotency_digest_short=key[:12], source_identity=row.get("source_identity"),
        error_category=(category if outcome.value.startswith("FAILED_") else None),
    )


# P0-C. The two halves of Stage 2's discovery predicate, written once so the
# reconciliation sweep is provably the complement of what discovery re-picks
# rather than an independently drifting copy of it.
#
# `_candidate_rows` re-picks a NORMALIZED file when its Stage 2 state is unset,
# explicitly retryable, or the historical retryable shape. Anything else Stage 2
# will never look at again.
#: One *page* of the unrouted sweep, not one cycle's coverage. The sweep pages
#: through the whole qualifying set with keyset pagination, so this bounds a
#: single query and a single page of memory.
#:
#: It used to bound the sweep outright, and that was a starvation defect rather
#: than a resource bound: the page is ordered oldest-first, so a persistent
#: backlog of 200 unresolved rows meant rows 201+ were never inspected on any
#: cycle, and a newly arrived unresolved report got neither a review item nor a
#: per-input incident — permanently. Logging that the page was full reported the
#: cliff without removing it, and raising the number only moves it.
UNROUTED_SWEEP_LIMIT = 200

#: Pure resource backstop on the paginated sweep: at most this many pages, so a
#: pathological set cannot make one cycle unbounded. It is not the operating
#: bound — 50 x 200 is two orders of magnitude above the real set, which grows by
#: about four files a week — and reaching it is a loud WARNING, not a silent cut.
UNROUTED_SWEEP_MAX_PAGES = 50

#: Sort key of the sweep. `stage2_updated_at` is nullable and the pagination is
#: keyset, so the null is folded to a real timestamp: a NULL sorts with 1970,
#: still ahead of every real value, and the `(key, id)` pair stays a total order
#: that a cursor can be compared against.
_SWEEP_CURSOR_SQL = "COALESCE(stage2_updated_at, 'epoch'::timestamptz)"

STAGE2_REDISCOVERY_SQL = """(
    stage2_status IS NULL
    OR stage2_retryable = true
    OR (stage2_status = 'PENDING_REVIEW'
        AND stage2_pending_reason = 'stage2_exception'
        AND stage2_retryable IS NULL)
)"""

# Stage 3 (`_select_stage3_candidates`) consumes a file only when Stage 2 landed
# on 'OK' *and* it can be routed: a client code, a report type, and a cleaned
# artifact to load. A file that satisfies the first and fails the second is
# invisible to both stages while looking successful in every Stage 2 summary.
STAGE3_ROUTABLE_SQL = """(
    stage2_status = 'OK'
    AND client_code IS NOT NULL AND btrim(client_code) <> ''
    AND stage2_report_type IS NOT NULL AND btrim(stage2_report_type) <> ''
    AND EXISTS (
        SELECT 1 FROM artifacts a
         WHERE a.raw_file_id = ingest.raw_file.id
           AND a.workflow_name = 'workflow_b'
           AND a.stage_name = 'stage_2_clean'
           AND a.artifact_role = 'cleaned'
    )
)"""


def stage2_unrouted_files(conn, *, limit: int = UNROUTED_SWEEP_LIMIT,
                          after: tuple[Any, str] | None = None) -> list[dict]:
    """One page of the NORMALIZED inputs that no stage will ever pick up again.

    Read-only by construction: one SELECT, no lock, no UPDATE. A file appearing
    here is *not* reprocessed — reprocessing it is an operator decision, and
    silently retrying a file that was parked for human review is exactly how a
    duplicate Stage 3 load would be created.

    `stage3_status` being set at all excludes the row: a file that reached Stage 3
    is owned by Stage 3's own recovery story (P0-E), not by this sweep. Only files
    that never got that far are unrouted in the sense P0-C means.

    `after` is the keyset cursor — the `(sweep_cursor_at, raw_file_id)` pair of
    the last row of the previous page. Row-comparison against the same expression
    the ORDER BY uses is what makes the pagination complete and non-overlapping
    without OFFSET's cost or its skipped-row hazard.
    """
    keyset = ""
    params: list[Any] = []
    if after is not None:
        keyset = f"AND ({_SWEEP_CURSOR_SQL}, id) > (%s::timestamptz, %s::uuid)"
        params.extend([after[0], after[1]])
    params.append(limit)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id::text AS raw_file_id,
                   client_code,
                   stage2_status,
                   stage2_report_type,
                   stage2_pending_reason,
                   stage2_outcome_category,
                   stage2_retryable,
                   stage2_cleaned_artifact_id::text AS stage2_cleaned_artifact_id,
                   sha256 AS source_identity,
                   stage2_updated_at,
                   {_SWEEP_CURSOR_SQL} AS sweep_cursor_at
              FROM ingest.raw_file
             WHERE status = 'NORMALIZED'
               AND (stage3_status IS NULL OR btrim(stage3_status) = '')
               AND NOT {STAGE2_REDISCOVERY_SQL}
               AND NOT {STAGE3_ROUTABLE_SQL}
               {keyset}
             ORDER BY {_SWEEP_CURSOR_SQL} ASC, id ASC
             LIMIT %s
            """,
            tuple(params),
        )
        rows = [dict(row) for row in cur.fetchall()]
    conn.rollback()
    return rows


@dataclass(frozen=True)
class Stage2UnroutedSweep:
    """What one cycle's reconciliation sweep actually covered."""

    rows_inspected: int = 0
    pages_read: int = 0
    #: True only when the page-count backstop stopped the sweep before the set
    #: was exhausted. Normal completion — including a completely empty set — is
    #: False, so this flag means "there are unowned files this cycle did not
    #: look at", which is the only condition worth a WARNING.
    truncated: bool = False


def _stranded_item(row: dict) -> Stage2ItemResult:
    """Classify one unrouted file by *why* nobody owns it."""
    if row.get("stage2_status") == "OK":
        outcome = Stage2Outcome.STRANDED_UNROUTABLE
        if not (row.get("client_code") or "").strip():
            reason = "missing_client_code"
        elif not (row.get("stage2_report_type") or "").strip():
            reason = "missing_stage2_report_type"
        else:
            reason = "missing_stage2_cleaned_artifact"
    else:
        outcome = Stage2Outcome.STRANDED_AWAITING_REVIEW
        reason = row.get("stage2_pending_reason") or "unrouted_stage2_state"
    return Stage2ItemResult(
        raw_file_id=row["raw_file_id"],
        outcome=outcome,
        persisted_status=row.get("stage2_status"),
        reason_code=reason,
        # Never retryable: a retryable file is by definition one Stage 2 discovery
        # would have re-picked, so it cannot be in this set at all.
        retryable=False,
        # The whole point. `Stage2BatchResult.operator_action_required` is what
        # carries this up to the orchestrator's SUCCEEDED_WITH_REVIEW_ITEMS, so
        # the state keeps asserting itself on every cycle until an operator
        # resolves it — instead of being announced once and forgotten.
        review_required=True,
        client_code=row.get("client_code"),
        report_type=row.get("stage2_report_type"),
        artifact_id=row.get("stage2_cleaned_artifact_id"),
        source_identity=row.get("source_identity"),
        error_category=row.get("stage2_outcome_category"),
    )


def reconcile_unrouted_stage2_files(
    conn, result: Stage2BatchResult, *,
    page_size: int = UNROUTED_SWEEP_LIMIT,
    max_pages: int = UNROUTED_SWEEP_MAX_PAGES,
) -> Stage2UnroutedSweep:
    """Give every durably unowned input a review item, in place.

    A file this batch already parked for review, or failed, is left alone: the
    processing path already owns it and reporting it twice would only duplicate
    the signal.

    A file this batch processed into an unowned state is a different case, and
    the one that matters. Stage 2 can land on `stage2_status='OK'` with no
    `client_code` — the production report_112 shape — and that file is appended
    here as an ordinary `SUCCEEDED_CREATED`. Nothing about it is retryable,
    rediscoverable or Stage-3 routable, so if the sweep skipped it merely
    because its id is present, the cycle that *created* the stranded file would
    end as plain SUCCEEDED and the state would surface only at the next
    scheduled cycle, 10-14 hours later.

    So the durable classification supersedes the processing item in place: same
    position, no duplicate id, and `operator_action_required` becomes true in
    the same cycle.

    **Every** qualifying row, not the oldest page of them. The sweep walks the
    set with keyset pagination until it is exhausted, because the ordering is
    oldest-first and a persistent backlog the size of one page would otherwise
    hide every newer unresolved input forever — on this cycle and on all of
    them. Bounded resource use comes from the page size and the page-count
    backstop, not from refusing to look.
    """
    owned: set[str] = set()
    first_position: dict[str, int] = {}
    for index, item in enumerate(result.items):
        first_position.setdefault(item.raw_file_id, index)
        if item_carries_operator_ownership(item):
            owned.add(item.raw_file_id)

    cursor: tuple[Any, str] | None = None
    rows_inspected = 0
    pages_read = 0
    truncated = False
    while True:
        if pages_read >= max_pages:
            truncated = True
            break
        rows = stage2_unrouted_files(conn, limit=page_size, after=cursor)
        pages_read += 1
        for row in rows:
            raw_file_id = row["raw_file_id"]
            if raw_file_id in owned:
                continue
            position = first_position.get(raw_file_id)
            if position is None:
                first_position[raw_file_id] = len(result.items)
                result.items.append(_stranded_item(row))
            else:
                result.items[position] = _stranded_item(row)
        rows_inspected += len(rows)
        if len(rows) < page_size:
            break
        last_cursor_at = rows[-1].get("sweep_cursor_at")
        if last_cursor_at is None:
            # No cursor column means the next page cannot be proved to advance,
            # and repeating an identical query is an infinite loop, not a sweep.
            truncated = True
            break
        cursor = (last_cursor_at, rows[-1]["raw_file_id"])
    return Stage2UnroutedSweep(
        rows_inspected=rows_inspected, pages_read=pages_read, truncated=truncated
    )


def process_stage2_batch(client, run_id: str, params: dict) -> Stage2BatchResult:
    if not isinstance(params, dict):
        raise ValueError("params must be a dict")
    force = bool(params.get("force_reprocess", False))
    if force and not (params.get("raw_file_ids") or params.get("input_files")):
        raise ValueError("force_reprocess requires explicit raw_file_ids or input_files")
    targeted = bool(
        params.get("raw_file_ids") or params.get("input_files") or params.get("input_dir")
    )
    conn = _pg_conn()
    result = Stage2BatchResult()
    try:
        candidates = _candidate_rows(conn, params)
        result.discovered_candidate_count = len(candidates)
        result.eligible_count = len(candidates)
        for candidate in candidates:
            raw_file_id = candidate["raw_file_id"]
            if not _acquire_item_lock(conn, raw_file_id):
                result.items.append(Stage2ItemResult(raw_file_id, Stage2Outcome.SKIPPED_LOCKED))
                continue
            try:
                current = _refresh_raw_file(conn, raw_file_id)
                if current["status"] != "NORMALIZED" or not current.get("normalized_csv_path"):
                    result.items.append(Stage2ItemResult(raw_file_id, Stage2Outcome.SKIPPED_INELIGIBLE))
                    continue
                with conn.cursor() as cur:
                    completed_artifact = _valid_cleaned_artifact_id(cur, current) if current.get("stage2_status") == "OK" else None
                    if completed_artifact and not force:
                        if not current.get("stage2_cleaned_artifact_id"):
                            cur.execute("UPDATE ingest.raw_file SET stage2_cleaned_artifact_id=%s WHERE id=%s", (completed_artifact, raw_file_id))
                            conn.commit()
                        result.items.append(Stage2ItemResult(
                            raw_file_id, Stage2Outcome.SKIPPED_COMPLETED,
                            persisted_status="OK", report_type=current.get("stage2_report_type"),
                            client_code=current.get("client_code"), artifact_id=completed_artifact,
                            source_identity=current.get("source_identity"),
                        ))
                        continue
                    if current.get("stage2_status") == "OK" and not completed_artifact and not force:
                        cur.execute(
                            """UPDATE ingest.raw_file SET stage2_outcome_category=%s,
                                      stage2_retryable=false, stage2_pending_reason=%s WHERE id=%s""",
                            (Stage2Outcome.FAILED_NON_RETRYABLE.value, "completed_missing_artifact_link", raw_file_id),
                        )
                        conn.commit()
                        current = _refresh_raw_file(conn, raw_file_id)
                        result.items.append(_item_from_persisted(current))
                        continue
                _run_single_legacy(client, run_id, {
                    "input_files": [current["normalized_csv_path"]],
                    "limit": 1,
                    "debug_detection": bool(params.get("debug_detection", False)),
                })
                result.items.append(_item_from_persisted(_refresh_raw_file(conn, raw_file_id)))
            finally:
                _release_item_lock(conn, raw_file_id)

        # P0-C. Only the autonomous sweep reconciles. A targeted operator run
        # names the files it is interested in, and answering it with every
        # unrelated stranded file in the platform would bury its actual result.
        if not targeted:
            swept = reconcile_unrouted_stage2_files(conn, result)
            if getattr(swept, "truncated", False):
                # The sweep normally exhausts the whole unowned set, so this is
                # the resource backstop firing, not the ordinary page bound: some
                # unowned files got neither a review item nor a per-input
                # incident this cycle. A bound that looks like coverage is how an
                # input becomes invisible, so it is never absorbed silently.
                client.log(
                    "WARNING", "SCRIPT", JOB_SOURCE,
                    "Stage 2 unrouted sweep hit its page-count backstop; some unowned files were not inspected",
                    run_id=run_id,
                    context={"page_size": UNROUTED_SWEEP_LIMIT,
                             "max_pages": UNROUTED_SWEEP_MAX_PAGES,
                             "rows_inspected": getattr(swept, "rows_inspected", None)},
                )
    finally:
        conn.close()
    if has_batch_failures(result):
        raise Stage2BatchError(result)
    return result


def run(client, run_id: str, params: dict) -> Stage2BatchResult:
    result = process_stage2_batch(client, run_id, params)
    client.log("INFO", "SCRIPT", JOB_SOURCE, "Stage2 batch result", run_id=run_id, context=result.to_dict())
    return result
