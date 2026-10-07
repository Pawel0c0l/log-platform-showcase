from __future__ import annotations

import json
import os
import re
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from api.timezone_utils import DEFAULT_BUSINESS_TIMEZONE, set_pg_session_timezone
from jobs.api.telematics.secret_resolver import resolve_secret
from jobs.common import environment_identity
from jobs.reports.stage3 import permissions
from jobs.trip_metrics_population_source import (
    TRIP_METRICS_SOURCE_D105_2_ECODRIVING,
    TRIP_METRICS_SOURCE_MISMATCH_REASON,
    is_required_trip_metrics_source,
    normalize_trip_metrics_population_source,
)


SOURCE = "jobs.reports.postprocess.job_d105_2_ecodriving_trip_metrics_migration"
OPERATION_NAME = "d105_2_trip_metrics_migration"
REPO_ROOT = Path(__file__).resolve().parents[3]
REPORT_SCHEMA = "telematics_reports"
REPORT_TABLE = "report_d105_2_ecodriving"
CLIENT_TRIPS_SCHEMA = "public"
CLIENT_TRIPS_TABLE = "client_trips"
REPORT_EVENT_TIMEZONE = DEFAULT_BUSINESS_TIMEZONE
REPORT_REGISTRATION_COLUMN = "Nr Rejestracyjny"
REPORT_START_COLUMN = "Czas rozpoczęcia"
REPORT_END_COLUMN = "Czas zakończenia"
REPORT_OVERREV_COLUMN = "przekroczenia obr/min"
REPORT_SPEED_140_160_COLUMN = "> 140kmh"
REPORT_SPEED_160_170_COLUMN = "> 160kmh"
REPORT_SPEED_170_PLUS_COLUMN = "> 170kmh"
REPORT_REQUIRED_COLUMNS = [
    REPORT_REGISTRATION_COLUMN,
    REPORT_START_COLUMN,
    REPORT_END_COLUMN,
    REPORT_OVERREV_COLUMN,
    REPORT_SPEED_140_160_COLUMN,
    REPORT_SPEED_160_170_COLUMN,
    REPORT_SPEED_170_PLUS_COLUMN,
]
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
ERROR_CODE_AMBIGUOUS_TRIP_MATCH = "AMBIGUOUS_TRIP_MATCH"
ERROR_CODE_INVALID_REGISTRATION = "INVALID_REGISTRATION"
ERROR_CODE_INVALID_TIMESTAMP = "INVALID_TIMESTAMP"
ERROR_CODE_INVALID_METRIC_COUNTS = "INVALID_METRIC_COUNTS"
CLIENT_TRIPS_COUNTER_COLUMNS = {
    "speeding_140_160_count": "INTEGER NOT NULL DEFAULT 0",
    "speeding_160_170_count": "INTEGER NOT NULL DEFAULT 0",
    "speeding_170_plus_count": "INTEGER NOT NULL DEFAULT 0",
    "overrev_events_count": "INTEGER NOT NULL DEFAULT 0",
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
    trip_metrics_population_source: str = TRIP_METRICS_SOURCE_D105_2_ECODRIVING
    client_db_environment: str | None = None
    client_db_identity_id: str | None = None


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
    non_zero_metric_rows: int = 0
    zero_metric_rows: int = 0
    exact_match_rows: int = 0
    minute_fallback_match_rows: int = 0
    rounded_fallback_match_rows: int = 0
    matched_rows: int = 0
    unmatched_rows: int = 0
    ambiguous_rows: int = 0
    invalid_registration_rows: int = 0
    invalid_timestamp_rows: int = 0
    invalid_metric_rows: int = 0
    migrated_rows: int = 0
    zero_metric_rows_marked: int = 0
    rows_incrementing_140_160: int = 0
    rows_incrementing_160_170: int = 0
    rows_incrementing_170_plus: int = 0
    rows_incrementing_overrev: int = 0
    incremented_140_160: int = 0
    incremented_160_170: int = 0
    incremented_170_plus: int = 0
    incremented_overrev: int = 0
    columns_to_add_report_d105_2_ecodriving: list[str] = field(default_factory=list)
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
            "non_zero_metric_rows": self.non_zero_metric_rows,
            "zero_metric_rows": self.zero_metric_rows,
            "exact_match_rows": self.exact_match_rows,
            "minute_fallback_match_rows": self.minute_fallback_match_rows,
            "rounded_fallback_match_rows": self.rounded_fallback_match_rows,
            "matched_rows": self.matched_rows,
            "unmatched_rows": self.unmatched_rows,
            "ambiguous_rows": self.ambiguous_rows,
            "invalid_registration_rows": self.invalid_registration_rows,
            "invalid_timestamp_rows": self.invalid_timestamp_rows,
            "invalid_metric_rows": self.invalid_metric_rows,
            "migrated_rows": self.migrated_rows,
            "zero_metric_rows_marked": self.zero_metric_rows_marked,
            "rows_incrementing_140_160": self.rows_incrementing_140_160,
            "rows_incrementing_160_170": self.rows_incrementing_160_170,
            "rows_incrementing_170_plus": self.rows_incrementing_170_plus,
            "rows_incrementing_overrev": self.rows_incrementing_overrev,
            "incremented_140_160": self.incremented_140_160,
            "incremented_160_170": self.incremented_160_170,
            "incremented_170_plus": self.incremented_170_plus,
            "incremented_overrev": self.incremented_overrev,
            "columns_to_add_report_d105_2_ecodriving": list(self.columns_to_add_report_d105_2_ecodriving),
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
    production_write_confirmation = params.get("production_write_confirmation")
    runtime_identity = environment_identity.load_runtime_identity()
    if (
        runtime_identity.environment == environment_identity.ENV_PRODUCTION
        and not dry_run
        and not client_code
    ):
        raise environment_identity.EnvironmentIdentityError(
            "PRODUCTION_WRITE_CLIENT_SCOPE_REQUIRED",
            "production write-mode requires an explicit client_code",
        )
    environment_identity.require_clean_production_worktree(
        runtime_identity,
        repo_root=REPO_ROOT,
    )
    if (
        runtime_identity.environment == environment_identity.ENV_PRODUCTION
        and dry_run
        and auto_grant_permissions
    ):
        raise environment_identity.EnvironmentIdentityError(
            "PRODUCTION_DRY_RUN_WRITE_OPTION_ENABLED",
            "production dry-run requires auto_grant_permissions=false",
        )

    _log(
        client,
        run_id,
        "INFO",
        "Workflow B D105.2 EcoDriving trip metrics migration started",
        context={
            "client_code": client_code,
            "limit": limit,
            "dry_run": dry_run,
            "force_retry_errors": force_retry_errors,
            "auto_grant_permissions": auto_grant_permissions,
            "report_event_timezone": REPORT_EVENT_TIMEZONE,
            "target_environment": runtime_identity.environment,
        },
    )

    summaries: list[dict[str, Any]] = []
    errors = 0
    with _platform_pg_conn() as platform_conn:
        platform_attestation = environment_identity.attest_platform_identity(
            platform_conn,
            runtime_identity,
        )
        _log(
            client,
            run_id,
            "INFO",
            "Platform environment identity verified",
            context=platform_attestation.context(),
        )
        clients = _load_enabled_clients(platform_conn, client_code=client_code)
        if not clients:
            _log(
                client,
                run_id,
                "INFO",
                "No enabled clients found for D105.2 EcoDriving trip metrics migration",
                context={"client_code": client_code, "report_event_timezone": REPORT_EVENT_TIMEZONE},
            )
            return {"clients": []}

        for config in clients:
            try:
                if not is_required_trip_metrics_source(
                    config.trip_metrics_population_source,
                    TRIP_METRICS_SOURCE_D105_2_ECODRIVING,
                ):
                    summary = ClientSummary(
                        client_code=config.client_code,
                        client_db_name=config.client_db_name,
                        dry_run=dry_run,
                        force_retry_errors=force_retry_errors,
                        would_auto_grant_permissions=False,
                        status="SKIPPED",
                        trip_metrics_population_source=config.trip_metrics_population_source,
                        required_trip_metrics_population_source=TRIP_METRICS_SOURCE_D105_2_ECODRIVING,
                        skip_reason=TRIP_METRICS_SOURCE_MISMATCH_REASON,
                    )
                    summary_dict = summary.as_dict()
                    summaries.append(summary_dict)
                    _log(
                        client,
                        run_id,
                        "INFO",
                        "Workflow B D105.2 EcoDriving trip metrics migration client skipped by trip metrics source",
                        context=summary_dict,
                    )
                    print(json.dumps(summary_dict, sort_keys=True), flush=True)
                    continue

                client_attestation = _attest_client_environment(
                    config,
                    runtime_identity=runtime_identity,
                )
                environment_identity.require_production_write_confirmation(
                    runtime_identity,
                    client_code=config.client_code,
                    operation_name=OPERATION_NAME,
                    provided=production_write_confirmation,
                    dry_run=dry_run,
                )
                _log(
                    client,
                    run_id,
                    "INFO",
                    "Client database environment identity verified",
                    context=client_attestation.context(),
                )
                if auto_grant_permissions and not dry_run:
                    try:
                        _ensure_stage3_permissions_for_client_db(config)
                    except Exception as exc:
                        raise ClientProcessingError(config, "auto_grant_permissions", exc) from exc
                summary = _process_client(
                    config,
                    limit=limit,
                    dry_run=dry_run,
                    force_retry_errors=force_retry_errors,
                    would_auto_grant_permissions=auto_grant_permissions and dry_run,
                )
                summaries.append(summary.as_dict())
                _log(
                    client,
                    run_id,
                    "INFO",
                    "Workflow B D105.2 EcoDriving trip metrics migration client processed",
                    context=summary.as_dict(),
                )
                if dry_run:
                    print(json.dumps(summary.as_dict(), sort_keys=True), flush=True)
            except Exception as exc:
                _rollback_quietly(platform_conn)
                errors += 1
                error_entry = _error_entry_from_exception(exc, config, default_phase="process_client")
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
                    f"Workflow B D105.2 EcoDriving trip metrics migration client failed: {type(exc).__name__}: {exc}",
                    context=summary_dict,
                    error=traceback.format_exc(),
                )
                print(json.dumps(summary_dict, sort_keys=True), flush=True)

    _log(
        client,
        run_id,
        "INFO",
        "Workflow B D105.2 EcoDriving trip metrics migration finished",
        context={
            "clients": len(summaries),
            "errors": errors,
            "dry_run": dry_run,
            "report_event_timezone": REPORT_EVENT_TIMEZONE,
        },
    )
    if errors:
        raise RuntimeError(_format_final_error(errors, summaries))
    return {"clients": summaries}


def _process_client(
    config: ClientDbConfig,
    *,
    limit: int | None,
    dry_run: bool,
    force_retry_errors: bool,
    would_auto_grant_permissions: bool = False,
) -> ClientSummary:
    summary = ClientSummary(
        client_code=config.client_code,
        client_db_name=config.client_db_name,
        dry_run=dry_run,
        force_retry_errors=force_retry_errors,
        would_auto_grant_permissions=would_auto_grant_permissions,
        trip_metrics_population_source=config.trip_metrics_population_source,
        required_trip_metrics_population_source=TRIP_METRICS_SOURCE_D105_2_ECODRIVING,
    )
    phase = "connect_client_db"
    conn = None
    try:
        with _client_business_pg_conn(config) as conn:
            with conn.cursor() as cur:
                phase = "check_report_table"
                if not _table_exists(cur, REPORT_SCHEMA, REPORT_TABLE):
                    summary.status = "SKIPPED"
                    summary.add_error("check_report_table", f"{REPORT_SCHEMA}.{REPORT_TABLE} does not exist")
                    _rollback_quietly(conn)
                    return summary
                summary.report_table_exists = True

                phase = "inspect_schema"
                report_columns = _existing_columns(cur, REPORT_SCHEMA, REPORT_TABLE)
                client_trip_columns = _existing_columns(cur, CLIENT_TRIPS_SCHEMA, CLIENT_TRIPS_TABLE)
                _require_report_source_columns(report_columns)
                _require_client_trips_match_columns(client_trip_columns)

                summary.columns_to_add_report_d105_2_ecodriving = [
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
                    )
                    _apply_counts(summary, counts, dry_run=True)
                    _rollback_quietly(conn)
                    return summary

                phase = "ensure_schema"
                _ensure_report_tracking_columns(cur, summary.columns_to_add_report_d105_2_ecodriving)
                _ensure_client_trips_counter_columns(cur, summary.columns_to_add_client_trips)
                phase = "match_rows"
                counts = _migrate_report_rows(
                    cur,
                    limit=limit,
                    force_retry_errors=force_retry_errors,
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
) -> dict[str, int]:
    query = _build_analysis_sql(
        include_updates=False,
        limit=limit,
        force_retry_errors=force_retry_errors,
        has_migrated_column=has_migrated_column,
        has_error_column=has_error_column,
    )
    params: list[Any] = []
    if limit is not None:
        params.append(limit)
    cur.execute(query, params)
    return _row_to_counts(cur.fetchone() or {})


def _migrate_report_rows(
    cur,
    *,
    limit: int | None,
    force_retry_errors: bool,
) -> dict[str, int]:
    query = _build_analysis_sql(
        include_updates=True,
        limit=limit,
        force_retry_errors=force_retry_errors,
        has_migrated_column=True,
        has_error_column=True,
    )
    params: list[Any] = []
    if limit is not None:
        params.append(limit)
    cur.execute(query, params)
    return _row_to_counts(cur.fetchone() or {})


def _timestamp_parse_sql(raw_column: str, serial_column: str) -> str:
    report_event_timezone = REPORT_EVENT_TIMEZONE.replace("'", "''")
    return f"""
        CASE
            WHEN {serial_column} >= {EXCEL_SERIAL_MIN}
                 AND {serial_column} < {EXCEL_SERIAL_MAX_EXCLUSIVE}
                THEN (
                    timestamp '{EXCEL_SERIAL_DATE_BASE}'
                    + round({serial_column} * 86400) * interval '1 second'
                ) AT TIME ZONE '{report_event_timezone}'
            WHEN {raw_column} ~ '^[0-9]{{4}}-[0-9]{{1,2}}-[0-9]{{1,2}}[ T][0-9]{{1,2}}:[0-9]{{2}}(:[0-9]{{2}})?([.][0-9]+)?(Z|[+-][0-9]{{2}}:?[0-9]{{2}})$'
                THEN replace({raw_column}, 'Z', '+00:00')::timestamptz
            WHEN {raw_column} ~ '^[0-9]{{4}}-[0-9]{{1,2}}-[0-9]{{1,2}}[ T][0-9]{{1,2}}:[0-9]{{2}}(:[0-9]{{2}})?$'
                THEN replace({raw_column}, 'T', ' ')::timestamp AT TIME ZONE '{report_event_timezone}'
            WHEN {raw_column} ~ '^[0-9]{{1,2}}[.][0-9]{{1,2}}[.][0-9]{{4}}[[:space:]]+[0-9]{{1,2}}:[0-9]{{2}}(:[0-9]{{2}})?$'
                THEN to_timestamp(
                    CASE
                        WHEN {raw_column} ~ ':[0-9]{{2}}:[0-9]{{2}}$' THEN {raw_column}
                        ELSE {raw_column} || ':00'
                    END,
                    'DD.MM.YYYY HH24:MI:SS'
                )::timestamp AT TIME ZONE '{report_event_timezone}'
            WHEN {raw_column} ~ '^[0-9]{{1,2}}/[0-9]{{1,2}}/[0-9]{{4}}[[:space:]]+[0-9]{{1,2}}:[0-9]{{2}}(:[0-9]{{2}})?$'
                THEN to_timestamp(
                    CASE
                        WHEN {raw_column} ~ ':[0-9]{{2}}:[0-9]{{2}}$' THEN {raw_column}
                        ELSE {raw_column} || ':00'
                    END,
                    'DD/MM/YYYY HH24:MI:SS'
                )::timestamp AT TIME ZONE '{report_event_timezone}'
            ELSE NULL
        END
    """


def _metric_parse_sql(clean_column: str) -> str:
    return f"""
        CASE
            WHEN {clean_column} ~ '^[+]?[0-9]+([.]0+)?$' THEN {clean_column}::numeric::integer
            ELSE NULL
        END
    """


def _build_analysis_sql(
    *,
    include_updates: bool,
    limit: int | None,
    force_retry_errors: bool,
    has_migrated_column: bool,
    has_error_column: bool,
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
    start_ts_expr = _timestamp_parse_sql("start_ts_raw", "start_serial_value")
    end_ts_expr = _timestamp_parse_sql("end_ts_raw", "end_serial_value")
    ctes = f"""
WITH candidates AS (
    SELECT
        r.ctid AS report_ctid,
        btrim(COALESCE(r.{_qi(REPORT_REGISTRATION_COLUMN)}, '')) AS registration,
        btrim(COALESCE(r.{_qi(REPORT_START_COLUMN)}, '')) AS start_ts_raw,
        btrim(COALESCE(r.{_qi(REPORT_END_COLUMN)}, '')) AS end_ts_raw,
        btrim(COALESCE(r.{_qi(REPORT_OVERREV_COLUMN)}, '')) AS overrev_raw,
        btrim(COALESCE(r.{_qi(REPORT_SPEED_140_160_COLUMN)}, '')) AS speed_140_160_raw,
        btrim(COALESCE(r.{_qi(REPORT_SPEED_160_170_COLUMN)}, '')) AS speed_160_170_raw,
        btrim(COALESCE(r.{_qi(REPORT_SPEED_170_PLUS_COLUMN)}, '')) AS speed_170_plus_raw
    FROM {report} AS r
    WHERE {migrated_filter}
      AND {error_filter}
    ORDER BY r.ctid
    {limit_clause}
),
normalized AS (
    SELECT
        *,
        regexp_replace(replace(overrev_raw, ',', '.'), '[[:space:]]+', '', 'g') AS overrev_clean,
        regexp_replace(replace(speed_140_160_raw, ',', '.'), '[[:space:]]+', '', 'g') AS speed_140_160_clean,
        regexp_replace(replace(speed_160_170_raw, ',', '.'), '[[:space:]]+', '', 'g') AS speed_160_170_clean,
        regexp_replace(replace(speed_170_plus_raw, ',', '.'), '[[:space:]]+', '', 'g') AS speed_170_plus_clean,
        CASE WHEN start_ts_raw ~ '^[+-]?[0-9]+([.][0-9]+)?$' THEN start_ts_raw::numeric ELSE NULL END AS start_serial_value,
        CASE WHEN end_ts_raw ~ '^[+-]?[0-9]+([.][0-9]+)?$' THEN end_ts_raw::numeric ELSE NULL END AS end_serial_value
    FROM candidates
),
parsed AS (
    SELECT
        *,
        {_metric_parse_sql('overrev_clean')} AS overrev_count,
        {_metric_parse_sql('speed_140_160_clean')} AS speed_140_160_count,
        {_metric_parse_sql('speed_160_170_clean')} AS speed_160_170_count,
        {_metric_parse_sql('speed_170_plus_clean')} AS speed_170_plus_count,
        {start_ts_expr} AS start_ts,
        {end_ts_expr} AS end_ts
    FROM normalized
),
classified_input AS (
    SELECT
        *,
        CASE
            WHEN registration = '' THEN '{ERROR_CODE_INVALID_REGISTRATION}'
            WHEN start_ts_raw = '' OR end_ts_raw = '' OR start_ts IS NULL OR end_ts IS NULL THEN '{ERROR_CODE_INVALID_TIMESTAMP}'
            WHEN overrev_raw = '' OR speed_140_160_raw = '' OR speed_160_170_raw = '' OR speed_170_plus_raw = ''
              OR overrev_count IS NULL OR speed_140_160_count IS NULL OR speed_160_170_count IS NULL OR speed_170_plus_count IS NULL
              THEN '{ERROR_CODE_INVALID_METRIC_COUNTS}'
            ELSE NULL
        END AS invalid_error
    FROM parsed
),
invalid_rows AS (
    SELECT report_ctid, invalid_error
    FROM classified_input
    WHERE invalid_error IS NOT NULL
),
valid_rows AS (
    SELECT *
    FROM classified_input
    WHERE invalid_error IS NULL
),
zero_metric_rows AS (
    SELECT *
    FROM valid_rows
    WHERE overrev_count = 0
      AND speed_140_160_count = 0
      AND speed_160_170_count = 0
      AND speed_170_plus_count = 0
),
metric_rows AS (
    SELECT *
    FROM valid_rows
    WHERE NOT (
        overrev_count = 0
        AND speed_140_160_count = 0
        AND speed_160_170_count = 0
        AND speed_170_plus_count = 0
    )
),
exact_trip_matches AS (
    SELECT
        m.report_ctid,
        m.speed_140_160_count,
        m.speed_160_170_count,
        m.speed_170_plus_count,
        m.overrev_count,
        t.client_id,
        t.provider_trip_id,
        COALESCE(NULLIF(t.record_id::text, ''), t.provider_trip_id::text) AS trip_id_text,
        'exact' AS match_strategy
    FROM metric_rows AS m
    JOIN {trips} AS t
      ON btrim(t.registration) = m.registration
     AND t.start_timestamp = m.start_ts
     AND t.end_timestamp = m.end_ts
),
exact_match_totals AS (
    SELECT report_ctid, COUNT(*) AS match_count
    FROM exact_trip_matches
    GROUP BY report_ctid
),
exact_matches AS (
    SELECT tm.*
    FROM exact_trip_matches AS tm
    JOIN exact_match_totals AS mt ON mt.report_ctid = tm.report_ctid
    WHERE mt.match_count = 1
),
minute_trip_matches AS (
    SELECT
        m.report_ctid,
        m.speed_140_160_count,
        m.speed_160_170_count,
        m.speed_170_plus_count,
        m.overrev_count,
        t.client_id,
        t.provider_trip_id,
        COALESCE(NULLIF(t.record_id::text, ''), t.provider_trip_id::text) AS trip_id_text,
        'minute' AS match_strategy
    FROM metric_rows AS m
    LEFT JOIN exact_match_totals AS emt ON emt.report_ctid = m.report_ctid
    JOIN {trips} AS t
      ON btrim(t.registration) = m.registration
     AND date_trunc('minute', t.start_timestamp) = date_trunc('minute', m.start_ts)
     AND date_trunc('minute', t.end_timestamp) = date_trunc('minute', m.end_ts)
    WHERE COALESCE(emt.match_count, 0) = 0
),
minute_match_totals AS (
    SELECT report_ctid, COUNT(*) AS match_count
    FROM minute_trip_matches
    GROUP BY report_ctid
),
minute_fallback_matches AS (
    SELECT mtm.*
    FROM minute_trip_matches AS mtm
    JOIN minute_match_totals AS mmt ON mmt.report_ctid = mtm.report_ctid
    WHERE mmt.match_count = 1
),
rounded_trip_matches AS (
    SELECT
        m.report_ctid,
        m.speed_140_160_count,
        m.speed_160_170_count,
        m.speed_170_plus_count,
        m.overrev_count,
        t.client_id,
        t.provider_trip_id,
        COALESCE(NULLIF(t.record_id::text, ''), t.provider_trip_id::text) AS trip_id_text,
        'rounded' AS match_strategy
    FROM metric_rows AS m
    LEFT JOIN exact_match_totals AS emt ON emt.report_ctid = m.report_ctid
    LEFT JOIN minute_match_totals AS mmt ON mmt.report_ctid = m.report_ctid
    JOIN {trips} AS t
      ON btrim(t.registration) = m.registration
     AND abs(EXTRACT(EPOCH FROM (t.start_timestamp - m.start_ts))) <= 60
     AND abs(EXTRACT(EPOCH FROM (t.end_timestamp - m.end_ts))) <= 60
    WHERE COALESCE(emt.match_count, 0) = 0
      AND COALESCE(mmt.match_count, 0) = 0
),
rounded_match_totals AS (
    SELECT report_ctid, COUNT(*) AS match_count
    FROM rounded_trip_matches
    GROUP BY report_ctid
),
rounded_fallback_matches AS (
    SELECT rtm.*
    FROM rounded_trip_matches AS rtm
    JOIN rounded_match_totals AS rmt ON rmt.report_ctid = rtm.report_ctid
    WHERE rmt.match_count = 1
),
selected_matches AS (
    SELECT * FROM exact_matches
    UNION ALL
    SELECT * FROM minute_fallback_matches
    UNION ALL
    SELECT * FROM rounded_fallback_matches
),
unmatched_rows AS (
    SELECT m.report_ctid, '{RETRYABLE_ERROR_CODE_NO_MATCHING_TRIP}' AS error_code
    FROM metric_rows AS m
    LEFT JOIN exact_match_totals AS emt ON emt.report_ctid = m.report_ctid
    LEFT JOIN minute_match_totals AS mmt ON mmt.report_ctid = m.report_ctid
    LEFT JOIN rounded_match_totals AS rmt ON rmt.report_ctid = m.report_ctid
    WHERE COALESCE(emt.match_count, 0) = 0
      AND COALESCE(mmt.match_count, 0) = 0
      AND COALESCE(rmt.match_count, 0) = 0
),
ambiguous_rows AS (
    SELECT mt.report_ctid, '{ERROR_CODE_AMBIGUOUS_TRIP_MATCH}' AS error_code
    FROM exact_match_totals AS mt
    WHERE mt.match_count > 1
    UNION ALL
    SELECT mt.report_ctid, '{ERROR_CODE_AMBIGUOUS_TRIP_MATCH}' AS error_code
    FROM minute_match_totals AS mt
    WHERE mt.match_count > 1
    UNION ALL
    SELECT mt.report_ctid, '{ERROR_CODE_AMBIGUOUS_TRIP_MATCH}' AS error_code
    FROM rounded_match_totals AS mt
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
        SUM(speed_140_160_count)::integer AS inc_140_160,
        SUM(speed_160_170_count)::integer AS inc_160_170,
        SUM(speed_170_plus_count)::integer AS inc_170_plus,
        SUM(overrev_count)::integer AS inc_overrev
    FROM selected_matches
    GROUP BY client_id, provider_trip_id
)
"""
    if include_updates:
        ctes += f""",
updated_trips AS (
    UPDATE {trips} AS t
       SET speeding_140_160_count = COALESCE(t.speeding_140_160_count, 0) + i.inc_140_160,
           speeding_160_170_count = COALESCE(t.speeding_160_170_count, 0) + i.inc_160_170,
           speeding_170_plus_count = COALESCE(t.speeding_170_plus_count, 0) + i.inc_170_plus,
           overrev_events_count = COALESCE(t.overrev_events_count, 0) + i.inc_overrev
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
    FROM selected_matches AS em
    WHERE r.ctid = em.report_ctid
    RETURNING 1
),
marked_zero_metric_rows AS (
    UPDATE {report} AS r
       SET migrated_to_client_db = TRUE,
           migrated_to_client_db_at = now(),
           migrated_to_client_trip_id = NULL,
           migrated_to_client_db_error = NULL
    FROM zero_metric_rows AS z
    WHERE r.ctid = z.report_ctid
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
        "((SELECT COUNT(*) FROM marked_migrated) + (SELECT COUNT(*) FROM marked_zero_metric_rows))::integer"
        if include_updates
        else "((SELECT COUNT(*) FROM selected_matches) + (SELECT COUNT(*) FROM zero_metric_rows))::integer"
    )
    zero_marked_expr = (
        "(SELECT COUNT(*) FROM marked_zero_metric_rows)::integer"
        if include_updates
        else "(SELECT COUNT(*) FROM zero_metric_rows)::integer"
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
    (SELECT COUNT(*) FROM metric_rows)::integer AS non_zero_metric_rows,
    (SELECT COUNT(*) FROM zero_metric_rows)::integer AS zero_metric_rows,
    (SELECT COUNT(*) FROM exact_matches)::integer AS exact_match_rows,
    (SELECT COUNT(*) FROM minute_fallback_matches)::integer AS minute_fallback_match_rows,
    (SELECT COUNT(*) FROM rounded_fallback_matches)::integer AS rounded_fallback_match_rows,
    (SELECT COUNT(*) FROM selected_matches)::integer AS matched_rows,
    (SELECT COUNT(*) FROM unmatched_rows)::integer AS unmatched_rows,
    (SELECT COUNT(*) FROM ambiguous_rows)::integer AS ambiguous_rows,
    (SELECT COUNT(*) FROM invalid_rows WHERE invalid_error = '{ERROR_CODE_INVALID_REGISTRATION}')::integer AS invalid_registration_rows,
    (SELECT COUNT(*) FROM invalid_rows WHERE invalid_error = '{ERROR_CODE_INVALID_TIMESTAMP}')::integer AS invalid_timestamp_rows,
    (SELECT COUNT(*) FROM invalid_rows WHERE invalid_error = '{ERROR_CODE_INVALID_METRIC_COUNTS}')::integer AS invalid_metric_rows,
    {migrated_rows_expr} AS migrated_rows,
    {zero_marked_expr} AS zero_metric_rows_marked,
    (SELECT COUNT(*) FROM selected_matches WHERE speed_140_160_count > 0)::integer AS rows_incrementing_140_160,
    (SELECT COUNT(*) FROM selected_matches WHERE speed_160_170_count > 0)::integer AS rows_incrementing_160_170,
    (SELECT COUNT(*) FROM selected_matches WHERE speed_170_plus_count > 0)::integer AS rows_incrementing_170_plus,
    (SELECT COUNT(*) FROM selected_matches WHERE overrev_count > 0)::integer AS rows_incrementing_overrev,
    COALESCE((SELECT SUM(inc_140_160) FROM increments), 0)::integer AS incremented_140_160,
    COALESCE((SELECT SUM(inc_160_170) FROM increments), 0)::integer AS incremented_160_170,
    COALESCE((SELECT SUM(inc_170_plus) FROM increments), 0)::integer AS incremented_170_plus,
    COALESCE((SELECT SUM(inc_overrev) FROM increments), 0)::integer AS incremented_overrev
    {update_reference_exprs}
"""


def _apply_counts(summary: ClientSummary, counts: dict[str, int], *, dry_run: bool) -> None:
    for name in [
        "candidate_rows",
        "non_zero_metric_rows",
        "zero_metric_rows",
        "exact_match_rows",
        "minute_fallback_match_rows",
        "rounded_fallback_match_rows",
        "matched_rows",
        "unmatched_rows",
        "ambiguous_rows",
        "invalid_registration_rows",
        "invalid_timestamp_rows",
        "invalid_metric_rows",
        "migrated_rows",
        "zero_metric_rows_marked",
        "rows_incrementing_140_160",
        "rows_incrementing_160_170",
        "rows_incrementing_170_plus",
        "rows_incrementing_overrev",
        "incremented_140_160",
        "incremented_160_170",
        "incremented_170_plus",
        "incremented_overrev",
    ]:
        setattr(summary, name, int(counts.get(name, 0) or 0))
    if dry_run:
        summary.migrated_rows = 0
        summary.zero_metric_rows_marked = 0
    if summary.errors:
        summary.status = "ERROR"
    elif (
        summary.ambiguous_rows
        or summary.unmatched_rows
        or summary.invalid_registration_rows
        or summary.invalid_timestamp_rows
        or summary.invalid_metric_rows
    ):
        summary.status = "WARNING"
    else:
        summary.status = "OK"


def _ensure_report_tracking_columns(cur, missing_columns: list[str]) -> None:
    if not missing_columns:
        return
    _require_table_owner_for_missing_columns(cur, REPORT_SCHEMA, REPORT_TABLE, missing_columns)
    for column in missing_columns:
        definition = REPORT_TRACKING_COLUMNS[column]
        cur.execute(
            f"ALTER TABLE {_qualified_ident(REPORT_SCHEMA, REPORT_TABLE)} "
            f"ADD COLUMN IF NOT EXISTS {_qi(column)} {definition}"
        )


def _ensure_client_trips_counter_columns(cur, missing_columns: list[str]) -> None:
    if not missing_columns:
        return
    _require_table_owner_for_missing_columns(cur, CLIENT_TRIPS_SCHEMA, CLIENT_TRIPS_TABLE, missing_columns)
    for column in missing_columns:
        definition = CLIENT_TRIPS_COUNTER_COLUMNS[column]
        cur.execute(
            f"ALTER TABLE {_qualified_ident(CLIENT_TRIPS_SCHEMA, CLIENT_TRIPS_TABLE)} "
            f"ADD COLUMN IF NOT EXISTS {_qi(column)} {definition}"
        )


def _require_table_owner_for_missing_columns(
    cur,
    schema_name: str,
    table_name: str,
    missing_columns: list[str],
) -> None:
    if _current_user_owns_table(cur, schema_name, table_name):
        return
    labels = _missing_column_labels(schema_name, table_name, missing_columns)
    raise RuntimeError(
        "Missing required columns require schema bootstrap/admin migration: "
        + ", ".join(labels)
    )


def _current_user_owns_table(cur, schema_name: str, table_name: str) -> bool:
    cur.execute(
        """
        SELECT pg_has_role(c.relowner, 'MEMBER') AS is_owner
        FROM pg_class AS c
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = %s
          AND c.relname = %s
          AND c.relkind IN ('r', 'p')
        """,
        (schema_name, table_name),
    )
    row = cur.fetchone()
    return bool(row and row.get("is_owner"))


def _missing_column_labels(schema_name: str, table_name: str, columns: list[str]) -> list[str]:
    return [f"{schema_name}.{table_name}.{column}" for column in columns]


def _require_report_source_columns(existing_columns: set[str]) -> None:
    missing = [column for column in REPORT_REQUIRED_COLUMNS if column not in existing_columns]
    if missing:
        raise RuntimeError(f"{REPORT_SCHEMA}.{REPORT_TABLE} is missing required columns: {', '.join(missing)}")


def _require_client_trips_match_columns(existing_columns: set[str]) -> None:
    required = {"client_id", "provider_trip_id", "registration", "start_timestamp", "end_timestamp", "record_id"}
    missing = sorted(required - set(existing_columns))
    if missing:
        raise RuntimeError(f"public.client_trips is missing required columns: {', '.join(missing)}")


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
                trip_metrics_population_source,
                client_db_environment,
                client_db_identity_id
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
                client_db_environment=(
                    str(row["client_db_environment"])
                    if row.get("client_db_environment") is not None
                    else None
                ),
                client_db_identity_id=(
                    str(row["client_db_identity_id"])
                    if row.get("client_db_identity_id") is not None
                    else None
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


def _attest_client_environment(
    config: ClientDbConfig,
    *,
    runtime_identity: environment_identity.RuntimeIdentity,
) -> environment_identity.AttestedDatabaseIdentity:
    expectation = environment_identity.ClientIdentityExpectation(
        client_code=config.client_code,
        environment=config.client_db_environment,
        database_identity_id=config.client_db_identity_id,
        database_name=config.client_db_name,
        database_user=config.client_db_user,
    )
    with _client_business_pg_conn(config) as conn:
        return environment_identity.attest_client_identity(
            conn,
            runtime_identity,
            expectation,
        )


def _ensure_stage3_permissions_for_client_db(config: ClientDbConfig) -> None:
    with permissions.admin_pg_conn(
        host=config.client_db_host,
        port=config.client_db_port,
        dbname="postgres",
    ) as admin_conn:
        permissions.ensure_stage3_permissions_for_client(
            admin_conn,
            config.client_code,
            config.client_db_name,
            config.client_db_user,
            role_name=permissions.DEFAULT_STAGE3_LOADER_ROLE,
        )


def _qi(identifier: str) -> str:
    return permissions.quote_ident(identifier)


def _qualified_ident(schema_name: str, table_name: str) -> str:
    return f"{_qi(schema_name)}.{_qi(table_name)}"


def _row_to_counts(row: dict[str, Any]) -> dict[str, int]:
    return {
        name: int(row.get(name) or 0)
        for name in [
            "candidate_rows",
            "non_zero_metric_rows",
            "zero_metric_rows",
            "exact_match_rows",
            "minute_fallback_match_rows",
            "rounded_fallback_match_rows",
            "matched_rows",
            "unmatched_rows",
            "ambiguous_rows",
            "invalid_registration_rows",
            "invalid_timestamp_rows",
            "invalid_metric_rows",
            "migrated_rows",
            "zero_metric_rows_marked",
            "rows_incrementing_140_160",
            "rows_incrementing_160_170",
            "rows_incrementing_170_plus",
            "rows_incrementing_overrev",
            "incremented_140_160",
            "incremented_160_170",
            "incremented_170_plus",
            "incremented_overrev",
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
    return f"D105.2 EcoDriving trip metrics migration failed for {error_count} client(s){suffix}"


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
