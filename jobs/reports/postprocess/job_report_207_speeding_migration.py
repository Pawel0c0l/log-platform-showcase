from __future__ import annotations

import json
import os
import re
import traceback
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from api.timezone_utils import DEFAULT_BUSINESS_TIMEZONE, set_pg_session_timezone
from jobs.api.telematics.secret_resolver import resolve_secret
from jobs.reports.stage3 import permissions
from jobs.reports.stage3.schema_readiness import (
    MissingSchemaObject,
    RuntimeSchemaMutationDisabledError,
    Stage3SchemaReadinessError,
)
from jobs.trip_metrics_population_source import (
    TRIP_METRICS_SOURCE_MISMATCH_REASON,
    TRIP_METRICS_SOURCE_REPORT_207,
    is_required_trip_metrics_source,
    normalize_trip_metrics_population_source,
)


SOURCE = "jobs.reports.postprocess.job_report_207_speeding_migration"
REPORT_SCHEMA = "telematics_reports"
REPORT_TABLE = "report_207"
CLIENT_TRIPS_SCHEMA = "public"
CLIENT_TRIPS_TABLE = "client_trips"
REPORT_SPEED_COLUMN = "Prędkość"
REPORT_TIMESTAMP_COLUMN = "Data i czas"
REPORT_EVENT_TIMEZONE = DEFAULT_BUSINESS_TIMEZONE
REPORT_REGISTRATION_COLUMN = "Nr rejestracyjny"
EXCEL_SERIAL_DATE_BASE = "1899-12-30"
EXCEL_SERIAL_MIN = 36526
EXCEL_SERIAL_MAX_EXCLUSIVE = 73051
REPORT_TRACKING_COLUMNS = {
    "migrated_to_client_db": "BOOLEAN NOT NULL DEFAULT FALSE",
    "migrated_to_client_db_at": "TIMESTAMPTZ NULL",
    "migrated_to_client_trip_id": "TEXT NULL",
    "migrated_to_client_db_error": "TEXT NULL",
}
RETRYABLE_ERROR_CODE_NO_MATCHING_TRIP = "NO_MATCHING_TRIP"
CLIENT_TRIPS_COUNTER_COLUMNS = {
    "speeding_140_160_count": "INTEGER NOT NULL DEFAULT 0",
    "speeding_160_170_count": "INTEGER NOT NULL DEFAULT 0",
    "speeding_170_plus_count": "INTEGER NOT NULL DEFAULT 0",
}
SAFE_CLIENT_CODE_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")


@dataclass(frozen=True)
class ClientDbConfig:
    client_code: str
    client_id: str | None
    client_db_host: str
    client_db_port: int
    client_db_name: str
    client_db_user: str
    client_db_password_secret_ref: str
    client_db_sslmode: str = "prefer"
    trip_metrics_population_source: str = TRIP_METRICS_SOURCE_REPORT_207


class ClientProcessingError(RuntimeError):
    def __init__(self, config: ClientDbConfig, phase: str, original: Exception) -> None:
        self.client_code = config.client_code
        self.client_db_name = config.client_db_name
        self.phase = phase
        self.original = original
        self.original_message = str(original)
        super().__init__(self.original_message)


@dataclass
class ClientSummary:
    client_code: str
    client_db_name: str
    report_table_exists: bool = False
    candidate_rows: int = 0
    valid_speed_rows: int = 0
    speed_140_160_rows: int = 0
    speed_160_170_rows: int = 0
    speed_170_plus_rows: int = 0
    matched_rows: int = 0
    unmatched_rows: int = 0
    ambiguous_rows: int = 0
    invalid_rows: int = 0
    sub_threshold_rows: int = 0
    migrated_rows: int = 0
    incremented_140_160: int = 0
    incremented_160_170: int = 0
    incremented_170_plus: int = 0
    columns_to_add_report_207: list[str] = field(default_factory=list)
    columns_to_add_client_trips: list[str] = field(default_factory=list)
    dry_run: bool = False
    force_retry_errors: bool = False
    would_auto_grant_permissions: bool = False
    status: str = "OK"
    errors: list[dict[str, str]] = field(default_factory=list)
    trip_metrics_population_source: str | None = None
    required_trip_metrics_population_source: str | None = None
    skip_reason: str | None = None

    def add_error(self, phase: str, message: str) -> None:
        self.errors.append(
            {
                "client_code": self.client_code,
                "client_db_name": self.client_db_name,
                "phase": phase,
                "message": str(message),
            }
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "client_code": self.client_code,
            "client_db_name": self.client_db_name,
            "report_event_timezone": REPORT_EVENT_TIMEZONE,
            "report_table_exists": self.report_table_exists,
            "candidate_rows": self.candidate_rows,
            "valid_speed_rows": self.valid_speed_rows,
            "speed_140_160_rows": self.speed_140_160_rows,
            "speed_160_170_rows": self.speed_160_170_rows,
            "speed_170_plus_rows": self.speed_170_plus_rows,
            "matched_rows": self.matched_rows,
            "unmatched_rows": self.unmatched_rows,
            "ambiguous_rows": self.ambiguous_rows,
            "invalid_rows": self.invalid_rows,
            "sub_threshold_rows": self.sub_threshold_rows,
            "migrated_rows": self.migrated_rows,
            "incremented_140_160": self.incremented_140_160,
            "incremented_160_170": self.incremented_160_170,
            "incremented_170_plus": self.incremented_170_plus,
            "columns_to_add_report_207": list(self.columns_to_add_report_207),
            "columns_to_add_client_trips": list(self.columns_to_add_client_trips),
            "dry_run": self.dry_run,
            "force_retry_errors": self.force_retry_errors,
            "would_auto_grant_permissions": self.would_auto_grant_permissions,
            "status": self.status,
            "trip_metrics_population_source": self.trip_metrics_population_source,
            "required_trip_metrics_population_source": self.required_trip_metrics_population_source,
            "skip_reason": self.skip_reason,
            "errors": list(self.errors),
        }


def run(client, run_id: str, params: dict):
    params = params or {}
    client_code = _optional_client_code(params.get("client_code"))
    limit = _optional_int(params.get("limit"))
    dry_run = _bool_param(params.get("dry_run", False))
    force_retry_errors = _bool_param(params.get("force_retry_errors", False))
    auto_grant_permissions = _bool_param(params.get("auto_grant_permissions", False))
    raw_file_id = _optional_raw_file_id(params.get("raw_file_id"))

    _log(
        client,
        run_id,
        "INFO",
        "Workflow B report_207 speeding migration started",
        context={
            "client_code": client_code,
            "limit": limit,
            "dry_run": dry_run,
            "force_retry_errors": force_retry_errors,
            "auto_grant_permissions": auto_grant_permissions,
            "raw_file_id": raw_file_id,
            "report_event_timezone": REPORT_EVENT_TIMEZONE,
        },
    )

    summaries: list[dict[str, Any]] = []
    errors = 0
    operator_action_error: Exception | None = None
    with _platform_pg_conn() as platform_conn:
        clients = _load_enabled_clients(platform_conn, client_code=client_code)
        if not clients:
            _log(
                client,
                run_id,
                "INFO",
                "No enabled clients found for report_207 speeding migration",
                context={"client_code": client_code, "report_event_timezone": REPORT_EVENT_TIMEZONE},
            )
            return {"clients": []}

        for config in clients:
            try:
                if not is_required_trip_metrics_source(
                    config.trip_metrics_population_source,
                    TRIP_METRICS_SOURCE_REPORT_207,
                ):
                    summary = ClientSummary(
                        client_code=config.client_code,
                        client_db_name=config.client_db_name,
                        dry_run=dry_run,
                        force_retry_errors=force_retry_errors,
                        would_auto_grant_permissions=False,
                        status="SKIPPED",
                        trip_metrics_population_source=config.trip_metrics_population_source,
                        required_trip_metrics_population_source=TRIP_METRICS_SOURCE_REPORT_207,
                        skip_reason=TRIP_METRICS_SOURCE_MISMATCH_REASON,
                    )
                    summary_dict = summary.as_dict()
                    summaries.append(summary_dict)
                    _log(
                        client,
                        run_id,
                        "INFO",
                        "Workflow B report_207 speeding migration client skipped by trip metrics source",
                        context=summary_dict,
                    )
                    print(json.dumps(summary_dict, sort_keys=True), flush=True)
                    continue

                if auto_grant_permissions:
                    raise ClientProcessingError(
                        config,
                        "auto_grant_permissions",
                        RuntimeSchemaMutationDisabledError("auto_grant_permissions"),
                    )
                summary = _process_client(
                    config,
                    limit=limit,
                    dry_run=dry_run,
                    force_retry_errors=force_retry_errors,
                    would_auto_grant_permissions=auto_grant_permissions and dry_run,
                    raw_file_id=raw_file_id,
                )
                summaries.append(summary.as_dict())
                _log(
                    client,
                    run_id,
                    "INFO",
                    "Workflow B report_207 speeding migration client processed",
                    context=summary.as_dict(),
                )
                if dry_run:
                    print(json.dumps(summary.as_dict(), sort_keys=True), flush=True)
            except Exception as exc:
                _rollback_quietly(platform_conn)
                errors += 1
                error_entry = _error_entry_from_exception(exc, config, default_phase="process_client")
                if operator_action_error is None:
                    operator_action_error = _operator_action_error(exc)
                summary = ClientSummary(
                    client_code=config.client_code,
                    client_db_name=config.client_db_name,
                    dry_run=dry_run,
                    force_retry_errors=force_retry_errors,
                    would_auto_grant_permissions=auto_grant_permissions and dry_run,
                    status="ERROR",
                    errors=[error_entry],
                )
                summary_dict = summary.as_dict()
                summaries.append(summary_dict)
                _log(
                    client,
                    run_id,
                    "ERROR",
                    f"Workflow B report_207 speeding migration client failed: {type(exc).__name__}: {exc}",
                    context=summary_dict,
                    error=traceback.format_exc(),
                )
                print(json.dumps(summary_dict, sort_keys=True), flush=True)

    _log(
        client,
        run_id,
        "INFO",
        "Workflow B report_207 speeding migration finished",
        context={
            "clients": len(summaries),
            "errors": errors,
            "dry_run": dry_run,
            "report_event_timezone": REPORT_EVENT_TIMEZONE,
        },
    )
    if errors:
        if operator_action_error is not None:
            raise operator_action_error
        raise RuntimeError(_format_final_error(errors, summaries))
    return {"clients": summaries}


def _process_client(
    config: ClientDbConfig,
    *,
    limit: int | None,
    dry_run: bool,
    force_retry_errors: bool,
    would_auto_grant_permissions: bool = False,
    raw_file_id: str | None = None,
) -> ClientSummary:
    summary = ClientSummary(
        client_code=config.client_code,
        client_db_name=config.client_db_name,
        dry_run=dry_run,
        force_retry_errors=force_retry_errors,
        would_auto_grant_permissions=would_auto_grant_permissions,
        trip_metrics_population_source=config.trip_metrics_population_source,
        required_trip_metrics_population_source=TRIP_METRICS_SOURCE_REPORT_207,
    )
    phase = "connect_client_db"
    conn = None
    try:
        with _client_business_pg_conn(config) as conn:
            with conn.cursor() as cur:
                phase = "check_report_table"
                if not _table_exists(cur, REPORT_SCHEMA, REPORT_TABLE):
                    raise Stage3SchemaReadinessError([
                        MissingSchemaObject(REPORT_SCHEMA, REPORT_TABLE, "table", REPORT_TABLE)
                    ])
                summary.report_table_exists = True

                phase = "inspect_schema"
                report_columns = _existing_columns(cur, REPORT_SCHEMA, REPORT_TABLE)
                client_trip_columns = _existing_columns(cur, CLIENT_TRIPS_SCHEMA, CLIENT_TRIPS_TABLE)
                _require_report_source_columns(report_columns)
                if raw_file_id and "_raw_file_id" not in report_columns:
                    raise Stage3SchemaReadinessError([
                        MissingSchemaObject(REPORT_SCHEMA, REPORT_TABLE, "column", "_raw_file_id")
                    ])
                _require_client_trips_match_columns(client_trip_columns)

                summary.columns_to_add_report_207 = [
                    name for name in REPORT_TRACKING_COLUMNS if name not in report_columns
                ]
                summary.columns_to_add_client_trips = [
                    name for name in CLIENT_TRIPS_COUNTER_COLUMNS if name not in client_trip_columns
                ]

                if dry_run:
                    phase = "select_candidates"
                    counts = _analyze_report_rows(
                        cur,
                        limit=limit,
                        force_retry_errors=force_retry_errors,
                        has_migrated_column="migrated_to_client_db" in report_columns,
                        has_error_column="migrated_to_client_db_error" in report_columns,
                        raw_file_id=raw_file_id,
                    )
                    _apply_counts(summary, counts, dry_run=True)
                    _rollback_quietly(conn)
                    return summary

                phase = "validate_schema"
                _require_report_207_schema_ready(
                    cur,
                    missing_report_columns=summary.columns_to_add_report_207,
                    missing_client_trips_columns=summary.columns_to_add_client_trips,
                )
                phase = "match_rows"
                counts = _migrate_report_rows(
                    cur,
                    limit=limit,
                    force_retry_errors=force_retry_errors,
                    raw_file_id=raw_file_id,
                )
                _apply_counts(summary, counts, dry_run=False)
            conn.commit()
            return summary
    except Exception as exc:
        if conn is not None:
            _rollback_quietly(conn)
        if isinstance(exc, ClientProcessingError):
            raise
        raise ClientProcessingError(config, phase, exc) from exc


def _analyze_report_rows(
    cur,
    *,
    limit: int | None,
    force_retry_errors: bool,
    has_migrated_column: bool,
    has_error_column: bool,
    raw_file_id: str | None = None,
) -> dict[str, int]:
    query = _build_analysis_sql(
        include_updates=False,
        limit=limit,
        force_retry_errors=force_retry_errors,
        has_migrated_column=has_migrated_column,
        has_error_column=has_error_column,
        raw_file_id=raw_file_id,
    )
    params: list[Any] = []
    if raw_file_id is not None:
        params.append(raw_file_id)
    if limit is not None:
        params.append(limit)
    cur.execute(query, params)
    return _row_to_counts(cur.fetchone() or {})


def _migrate_report_rows(
    cur,
    *,
    limit: int | None,
    force_retry_errors: bool,
    raw_file_id: str | None = None,
) -> dict[str, int]:
    query = _build_analysis_sql(
        include_updates=True,
        limit=limit,
        force_retry_errors=force_retry_errors,
        has_migrated_column=True,
        has_error_column=True,
        raw_file_id=raw_file_id,
    )
    params: list[Any] = []
    if raw_file_id is not None:
        params.append(raw_file_id)
    if limit is not None:
        params.append(limit)
    cur.execute(query, params)
    return _row_to_counts(cur.fetchone() or {})


def _build_analysis_sql(
    *,
    include_updates: bool,
    limit: int | None,
    force_retry_errors: bool,
    has_migrated_column: bool,
    has_error_column: bool,
    raw_file_id: str | None = None,
) -> str:
    report = _qualified_ident(REPORT_SCHEMA, REPORT_TABLE)
    trips = _qualified_ident(CLIENT_TRIPS_SCHEMA, CLIENT_TRIPS_TABLE)
    migrated_filter = (
        f"COALESCE(r.{_qi('migrated_to_client_db')}, FALSE) IS NOT TRUE"
        if has_migrated_column
        else "TRUE"
    )
    error_filter = (
        "TRUE"
        if force_retry_errors or not has_error_column
        else (
            f"(r.{_qi('migrated_to_client_db_error')} IS NULL "
            f"OR btrim(r.{_qi('migrated_to_client_db_error')}) = '' "
            f"OR r.{_qi('migrated_to_client_db_error')} = '{RETRYABLE_ERROR_CODE_NO_MATCHING_TRIP}')"
        )
    )
    limit_clause = "LIMIT %s" if limit is not None else ""
    raw_file_filter = f"AND r.{_qi('_raw_file_id')} = %s" if raw_file_id is not None else ""
    report_event_timezone = REPORT_EVENT_TIMEZONE.replace("'", "''")
    ctes = f"""
WITH candidates AS (
    SELECT
        r.ctid AS report_ctid,
        btrim(COALESCE(r.{_qi(REPORT_REGISTRATION_COLUMN)}, '')) AS registration,
        btrim(COALESCE(r.{_qi(REPORT_SPEED_COLUMN)}, '')) AS speed_raw,
        btrim(COALESCE(r.{_qi(REPORT_TIMESTAMP_COLUMN)}, '')) AS event_ts_raw
    FROM {report} AS r
    WHERE {migrated_filter}
      AND {error_filter}
      {raw_file_filter}
    ORDER BY r.ctid
    {limit_clause}
),
normalized AS (
    SELECT
        *,
        regexp_replace(replace(speed_raw, ',', '.'), '[[:space:]]+', '', 'g') AS speed_clean,
        CASE
            WHEN event_ts_raw ~ '^[+-]?[0-9]+([.][0-9]+)?$' THEN event_ts_raw::numeric
            ELSE NULL
        END AS event_serial_value
    FROM candidates
),
parsed AS (
    SELECT
        *,
        CASE
            WHEN speed_clean ~ '^[+-]?[0-9]+([.][0-9]+)?$' THEN speed_clean::numeric
            ELSE NULL
        END AS speed_value,
        CASE
            WHEN event_serial_value >= {EXCEL_SERIAL_MIN}
                 AND event_serial_value < {EXCEL_SERIAL_MAX_EXCLUSIVE}
                THEN (
                    timestamp '{EXCEL_SERIAL_DATE_BASE}'
                    + round(event_serial_value * 86400) * interval '1 second'
                ) AT TIME ZONE '{report_event_timezone}'
            WHEN event_ts_raw ~ '^[0-9]{{4}}-[0-9]{{1,2}}-[0-9]{{1,2}}[ T][0-9]{{1,2}}:[0-9]{{2}}(:[0-9]{{2}})?$'
                THEN replace(event_ts_raw, 'T', ' ')::timestamp AT TIME ZONE '{report_event_timezone}'
            WHEN event_ts_raw ~ '^[0-9]{{1,2}}[.][0-9]{{1,2}}[.][0-9]{{4}}[[:space:]]+[0-9]{{1,2}}:[0-9]{{2}}(:[0-9]{{2}})?$'
                THEN to_timestamp(
                    CASE
                        WHEN event_ts_raw ~ ':[0-9]{{2}}:[0-9]{{2}}$' THEN event_ts_raw
                        ELSE event_ts_raw || ':00'
                    END,
                    'DD.MM.YYYY HH24:MI:SS'
                )::timestamp AT TIME ZONE '{report_event_timezone}'
            WHEN event_ts_raw ~ '^[0-9]{{1,2}}/[0-9]{{1,2}}/[0-9]{{4}}[[:space:]]+[0-9]{{1,2}}:[0-9]{{2}}(:[0-9]{{2}})?$'
                THEN to_timestamp(
                    CASE
                        WHEN event_ts_raw ~ ':[0-9]{{2}}:[0-9]{{2}}$' THEN event_ts_raw
                        ELSE event_ts_raw || ':00'
                    END,
                    'DD/MM/YYYY HH24:MI:SS'
                )::timestamp AT TIME ZONE '{report_event_timezone}'
            ELSE NULL
        END AS event_ts
    FROM normalized
),
classified_input AS (
    SELECT
        *,
        CASE
            WHEN registration = '' THEN 'INVALID_REGISTRATION'
            WHEN speed_raw = '' OR speed_value IS NULL THEN 'INVALID_SPEED'
            WHEN speed_value > 140 AND (event_ts_raw = '' OR event_ts IS NULL) THEN 'INVALID_TIMESTAMP'
            ELSE NULL
        END AS invalid_error,
        CASE
            WHEN speed_value > 140 AND speed_value <= 160 THEN 'speeding_140_160_count'
            WHEN speed_value > 160 AND speed_value <= 170 THEN 'speeding_160_170_count'
            WHEN speed_value > 170 THEN 'speeding_170_plus_count'
            ELSE NULL
        END AS bucket
    FROM parsed
),
invalid_rows AS (
    SELECT report_ctid, invalid_error
    FROM classified_input
    WHERE invalid_error IS NOT NULL
),
valid_speeding AS (
    SELECT *
    FROM classified_input
    WHERE invalid_error IS NULL
      AND bucket IS NOT NULL
      AND event_ts IS NOT NULL
      AND registration <> ''
),
trip_matches AS (
    SELECT
        v.report_ctid,
        v.bucket,
        t.client_id,
        t.provider_trip_id,
        COALESCE(NULLIF(t.record_id::text, ''), t.provider_trip_id::text) AS trip_id_text
    FROM valid_speeding AS v
    JOIN {trips} AS t
      ON btrim(t.registration) = v.registration
     AND v.event_ts >= t.start_timestamp
     AND v.event_ts <= t.end_timestamp
),
match_totals AS (
    SELECT report_ctid, COUNT(*) AS match_count
    FROM trip_matches
    GROUP BY report_ctid
),
exact_matches AS (
    SELECT tm.*
    FROM trip_matches AS tm
    JOIN match_totals AS mt ON mt.report_ctid = tm.report_ctid
    WHERE mt.match_count = 1
),
unmatched_rows AS (
    SELECT v.report_ctid, '{RETRYABLE_ERROR_CODE_NO_MATCHING_TRIP}' AS error_code
    FROM valid_speeding AS v
    LEFT JOIN match_totals AS mt ON mt.report_ctid = v.report_ctid
    WHERE mt.report_ctid IS NULL
),
ambiguous_rows AS (
    SELECT mt.report_ctid, 'AMBIGUOUS_TRIP_MATCH' AS error_code
    FROM match_totals AS mt
    WHERE mt.match_count > 1
),
error_rows AS (
    SELECT report_ctid, invalid_error AS error_code FROM invalid_rows
    UNION ALL
    SELECT report_ctid, error_code FROM unmatched_rows
    UNION ALL
    SELECT report_ctid, error_code FROM ambiguous_rows
),
increments AS (
    SELECT
        client_id,
        provider_trip_id,
        COUNT(*) FILTER (WHERE bucket = 'speeding_140_160_count')::integer AS inc_140_160,
        COUNT(*) FILTER (WHERE bucket = 'speeding_160_170_count')::integer AS inc_160_170,
        COUNT(*) FILTER (WHERE bucket = 'speeding_170_plus_count')::integer AS inc_170_plus
    FROM exact_matches
    GROUP BY client_id, provider_trip_id
)
"""
    if include_updates:
        ctes += f""",
updated_trips AS (
    UPDATE {trips} AS t
       SET speeding_140_160_count = COALESCE(t.speeding_140_160_count, 0) + i.inc_140_160,
           speeding_160_170_count = COALESCE(t.speeding_160_170_count, 0) + i.inc_160_170,
           speeding_170_plus_count = COALESCE(t.speeding_170_plus_count, 0) + i.inc_170_plus
    FROM increments AS i
    WHERE t.client_id = i.client_id
      AND t.provider_trip_id = i.provider_trip_id
    RETURNING 1
),
marked_migrated AS (
    UPDATE {report} AS r
       SET migrated_to_client_db = TRUE,
           migrated_to_client_db_at = now(),
           migrated_to_client_trip_id = em.trip_id_text,
           migrated_to_client_db_error = NULL
    FROM exact_matches AS em
    WHERE r.ctid = em.report_ctid
    RETURNING 1
),
marked_errors AS (
    UPDATE {report} AS r
       SET migrated_to_client_db = FALSE,
           migrated_to_client_db_at = NULL,
           migrated_to_client_trip_id = NULL,
           migrated_to_client_db_error = er.error_code
    FROM error_rows AS er
    WHERE r.ctid = er.report_ctid
    RETURNING 1
)
"""
    migrated_rows_expr = (
        "(SELECT COUNT(*) FROM marked_migrated)::integer"
        if include_updates
        else "(SELECT COUNT(*) FROM exact_matches)::integer"
    )
    update_reference_exprs = (
        ",\n    (SELECT COUNT(*) FROM updated_trips)::integer AS updated_trip_groups,"
        "\n    (SELECT COUNT(*) FROM marked_errors)::integer AS marked_error_rows"
        if include_updates
        else ""
    )
    return ctes + f"""
SELECT
    (SELECT COUNT(*) FROM candidates)::integer AS candidate_rows,
    (SELECT COUNT(*) FROM valid_speeding)::integer AS valid_speed_rows,
    (SELECT COUNT(*) FROM valid_speeding WHERE bucket = 'speeding_140_160_count')::integer AS speed_140_160_rows,
    (SELECT COUNT(*) FROM valid_speeding WHERE bucket = 'speeding_160_170_count')::integer AS speed_160_170_rows,
    (SELECT COUNT(*) FROM valid_speeding WHERE bucket = 'speeding_170_plus_count')::integer AS speed_170_plus_rows,
    (SELECT COUNT(*) FROM exact_matches)::integer AS matched_rows,
    (SELECT COUNT(*) FROM unmatched_rows)::integer AS unmatched_rows,
    (SELECT COUNT(*) FROM ambiguous_rows)::integer AS ambiguous_rows,
    (SELECT COUNT(*) FROM invalid_rows)::integer AS invalid_rows,
    (SELECT COUNT(*) FROM classified_input
        WHERE invalid_error IS NULL AND bucket IS NULL)::integer AS sub_threshold_rows,
    {migrated_rows_expr} AS migrated_rows,
    COALESCE((SELECT SUM(inc_140_160) FROM increments), 0)::integer AS incremented_140_160,
    COALESCE((SELECT SUM(inc_160_170) FROM increments), 0)::integer AS incremented_160_170,
    COALESCE((SELECT SUM(inc_170_plus) FROM increments), 0)::integer AS incremented_170_plus
    {update_reference_exprs}
"""


def _apply_counts(summary: ClientSummary, counts: dict[str, int], *, dry_run: bool) -> None:
    for name in [
        "candidate_rows",
        "valid_speed_rows",
        "speed_140_160_rows",
        "speed_160_170_rows",
        "speed_170_plus_rows",
        "matched_rows",
        "unmatched_rows",
        "ambiguous_rows",
        "invalid_rows",
        "sub_threshold_rows",
        "migrated_rows",
        "incremented_140_160",
        "incremented_160_170",
        "incremented_170_plus",
    ]:
        setattr(summary, name, int(counts.get(name, 0) or 0))
    if dry_run:
        summary.migrated_rows = 0
    if summary.errors:
        summary.status = "ERROR"
    elif summary.ambiguous_rows or summary.unmatched_rows or summary.invalid_rows:
        summary.status = "WARNING"
    else:
        summary.status = "OK"


def _require_report_207_schema_ready(
    cur,
    *,
    missing_report_columns: list[str],
    missing_client_trips_columns: list[str],
) -> None:
    missing = [
        MissingSchemaObject(REPORT_SCHEMA, REPORT_TABLE, "column", column)
        for column in missing_report_columns
    ]
    missing.extend(
        MissingSchemaObject(CLIENT_TRIPS_SCHEMA, CLIENT_TRIPS_TABLE, "column", column)
        for column in missing_client_trips_columns
    )
    if not _record_id_unique_index_exists(cur, REPORT_SCHEMA, REPORT_TABLE):
        missing.append(
            MissingSchemaObject(
                REPORT_SCHEMA,
                REPORT_TABLE,
                "unique index",
                f"{REPORT_TABLE}__record_id_uidx",
            )
        )
    if missing:
        raise Stage3SchemaReadinessError(missing)


def _record_id_unique_index_exists(cur, schema_name: str, table_name: str) -> bool:
    cur.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM pg_indexes
            WHERE schemaname = %s
              AND tablename = %s
              AND indexname = %s
              AND indexdef ILIKE 'CREATE UNIQUE INDEX%%'
              AND indexdef ILIKE '%%record_id%%WHERE%%NULLIF%%BTRIM%%record_id%%IS NOT NULL%%'
        ) AS exists
        """,
        (schema_name, table_name, f"{table_name}__record_id_uidx"),
    )
    row = cur.fetchone()
    return bool(row and row.get("exists"))


def _require_report_source_columns(existing_columns: set[str]) -> None:
    missing = [
        column
        for column in [REPORT_TIMESTAMP_COLUMN, REPORT_REGISTRATION_COLUMN, REPORT_SPEED_COLUMN]
        if column not in existing_columns
    ]
    if missing:
        raise Stage3SchemaReadinessError([
            MissingSchemaObject(REPORT_SCHEMA, REPORT_TABLE, "column", column)
            for column in missing
        ])


def _require_client_trips_match_columns(existing_columns: set[str]) -> None:
    required = {"client_id", "provider_trip_id", "registration", "start_timestamp", "end_timestamp", "record_id"}
    missing = sorted(required - set(existing_columns))
    if missing:
        raise Stage3SchemaReadinessError([
            MissingSchemaObject(CLIENT_TRIPS_SCHEMA, CLIENT_TRIPS_TABLE, "column", column)
            for column in missing
        ])


def _table_exists(cur, schema_name: str, table_name: str) -> bool:
    cur.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = %s
              AND table_name = %s
        ) AS exists
        """,
        (schema_name, table_name),
    )
    row = cur.fetchone()
    return bool(row and row.get("exists"))


def _existing_columns(cur, schema_name: str, table_name: str) -> set[str]:
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = %s
          AND table_name = %s
        """,
        (schema_name, table_name),
    )
    return {str(row["column_name"]) for row in cur.fetchall()}


def _load_enabled_clients(platform_conn, *, client_code: str | None = None) -> list[ClientDbConfig]:
    where = ["enabled IS TRUE"]
    params: list[Any] = []
    if client_code:
        where.append("client_code = %s")
        params.append(client_code)
    with platform_conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT
                client_id,
                client_code,
                client_db_host,
                client_db_port,
                client_db_name,
                client_db_user,
                client_db_password_secret_ref,
                trip_metrics_population_source
            FROM workflow_a_control.client_account
            WHERE {' AND '.join(where)}
            ORDER BY client_code
            """,
            params,
        )
        rows = cur.fetchall()
    clients: list[ClientDbConfig] = []
    for row in rows:
        code = str(row.get("client_code") or "").strip()
        missing = [
            name
            for name in ["client_db_host", "client_db_name", "client_db_user", "client_db_password_secret_ref"]
            if not row.get(name)
        ]
        if not code or missing:
            continue
        clients.append(
            ClientDbConfig(
                client_code=code,
                client_id=str(row["client_id"]) if row.get("client_id") is not None else None,
                client_db_host=str(row["client_db_host"]),
                client_db_port=int(row.get("client_db_port") or 5432),
                client_db_name=str(row["client_db_name"]),
                client_db_user=str(row["client_db_user"]),
                client_db_password_secret_ref=str(row["client_db_password_secret_ref"]),
                trip_metrics_population_source=normalize_trip_metrics_population_source(
                    row.get("trip_metrics_population_source")
                ),
            )
        )
    return clients


def _platform_pg_conn():
    import psycopg
    from psycopg.rows import dict_row

    dsn = (
        f"host={os.getenv('POSTGRES_HOST', '127.0.0.1')} "
        f"port={os.getenv('POSTGRES_PORT', '5432')} "
        f"dbname={os.getenv('POSTGRES_DB', 'logdb')} "
        f"user={os.getenv('POSTGRES_USER', 'loguser')} "
        f"password={os.getenv('POSTGRES_PASSWORD', '')}"
    )
    return set_pg_session_timezone(psycopg.connect(dsn, row_factory=dict_row))


def _client_business_pg_conn(config: ClientDbConfig):
    import psycopg
    from psycopg.rows import dict_row

    password = resolve_secret(config.client_db_password_secret_ref)
    return set_pg_session_timezone(psycopg.connect(
        host=config.client_db_host,
        port=config.client_db_port,
        dbname=config.client_db_name,
        user=config.client_db_user,
        password=password,
        sslmode=config.client_db_sslmode,
        row_factory=dict_row,
    ))


def _qi(identifier: str) -> str:
    return permissions.quote_ident(identifier)


def _qualified_ident(schema_name: str, table_name: str) -> str:
    return f"{_qi(schema_name)}.{_qi(table_name)}"


def _row_to_counts(row: dict[str, Any]) -> dict[str, int]:
    return {
        name: int(row.get(name) or 0)
        for name in [
            "candidate_rows",
            "valid_speed_rows",
            "speed_140_160_rows",
            "speed_160_170_rows",
            "speed_170_plus_rows",
            "matched_rows",
            "unmatched_rows",
            "ambiguous_rows",
            "invalid_rows",
            "sub_threshold_rows",
            "migrated_rows",
            "incremented_140_160",
            "incremented_160_170",
            "incremented_170_plus",
        ]
    }


def _error_entry_from_exception(
    exc: Exception,
    config: ClientDbConfig,
    *,
    default_phase: str,
) -> dict[str, str]:
    if isinstance(exc, ClientProcessingError):
        phase = exc.phase
        message = exc.original_message
    else:
        phase = default_phase
        message = str(exc)
    return {
        "client_code": config.client_code,
        "client_db_name": config.client_db_name,
        "phase": phase,
        "message": message,
    }



def _operator_action_error(exc: Exception) -> Exception | None:
    original = exc.original if isinstance(exc, ClientProcessingError) else exc
    if isinstance(original, (Stage3SchemaReadinessError, RuntimeSchemaMutationDisabledError)):
        return original
    return None

def _format_final_error(error_count: int, summaries: list[dict[str, Any]]) -> str:
    failed: list[str] = []
    for summary in summaries:
        if summary.get("status") != "ERROR":
            continue
        errors = summary.get("errors") or []
        first = errors[0] if errors else {}
        if isinstance(first, dict):
            message = str(first.get("message") or "unknown error")
        else:
            message = str(first or "unknown error")
        failed.append(f"{summary.get('client_code')}: {message}")
    suffix = f": {'; '.join(failed)}" if failed else ""
    return f"report_207 speeding migration failed for {error_count} client(s){suffix}"


def _bool_param(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _optional_int(value: Any) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    parsed = int(value)
    if parsed <= 0:
        raise ValueError("limit must be a positive integer")
    return parsed


def _optional_client_code(value: Any) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    client_code = str(value).strip()
    if not SAFE_CLIENT_CODE_RE.match(client_code):
        raise ValueError(f"Unsafe client_code value: {client_code!r}")
    return client_code


def _optional_raw_file_id(value: Any) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip()
    try:
        parsed = str(UUID(text))
    except ValueError as exc:
        raise ValueError("raw_file_id must be a canonical UUID") from exc
    if parsed != text:
        raise ValueError("raw_file_id must be a canonical UUID")
    return parsed


def _rollback_quietly(conn) -> None:
    try:
        conn.rollback()
    except Exception:
        pass


def _log(client, run_id: str, level: str, message: str, *, context: dict[str, Any] | None = None, error: str | None = None) -> None:
    if not client or not hasattr(client, "log"):
        return
    log_context = dict(context or {})
    log_context.setdefault("report_event_timezone", REPORT_EVENT_TIMEZONE)
    kwargs: dict[str, Any] = {
        "run_id": run_id,
        "context": log_context,
    }
    if error:
        kwargs["error"] = error
    client.log(level, "SCRIPT", SOURCE, message, **kwargs)
