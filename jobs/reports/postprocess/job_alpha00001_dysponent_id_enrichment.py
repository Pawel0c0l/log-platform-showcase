from __future__ import annotations

import json
import os
import re
import time
import traceback
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable
from zoneinfo import ZoneInfo

from api.suspected_bug import SuspectedBugEvent, safe_report_suspected_bug
from api.timezone_utils import set_pg_session_timezone
from jobs.api.telematics.secret_resolver import resolve_secret
from jobs.common.environment_identity import (
    ClientIdentityExpectation,
    EnvironmentIdentityError,
    attest_client_identity,
    attest_platform_identity,
    load_runtime_identity,
)
from jobs.reports.stage3 import permissions

SOURCE = "jobs.reports.postprocess.job_alpha00001_dysponent_id_enrichment"
ALLOWED_CLIENT_CODE = "ALPHA00001"
CLIENT_TRIPS_SCHEMA = "public"
CLIENT_TRIPS_TABLE = "client_trips"
CLIENT_TRIPS_TARGET_COLUMN = "Dysponent_ID"
CLIENT_TRIPS_PK_COLUMNS = ("client_id", "provider_trip_id")
CLIENT_TRIPS_REGISTRATION_COLUMN = "registration"
CLIENT_TRIPS_START_COLUMN = "start_timestamp"
ASSIGNMENT_SCHEMA = "telematics_reports"
ASSIGNMENT_TABLE = "Alpha_GPS_Baza_LOG"
ASSIGNMENT_REQUIRED_COLUMNS = {
    "source_id", "registration", "assignment_date", "imported_at",
    "raw_file_id", "workflow_run_id", "cleaned_artifact_id",
}
WARSAW_TZ_NAME = "Europe/Warsaw"
WARSAW_TZ = ZoneInfo(WARSAW_TZ_NAME)
SAFE_CLIENT_CODE_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
DEFAULT_BATCH_SIZE = 1000
DEFAULT_MAX_SOURCE_AGE_HOURS = 36
DEFAULT_MIN_COVERAGE_PERCENT = Decimal("95.00")
DEFAULT_MAX_AMBIGUOUS_MATCHES = 25

SOURCE_REPORT_NOT_READY = "SOURCE_REPORT_NOT_READY"
SOURCE_REPORT_EMPTY = "SOURCE_REPORT_EMPTY"
UNSUPPORTED_CLIENT = "UNSUPPORTED_CLIENT"
INVALID_DATE_RANGE = "INVALID_DATE_RANGE"
AMBIGUOUS_ENRICHMENT_MATCH = "AMBIGUOUS_ENRICHMENT_MATCH"
COVERAGE_BELOW_THRESHOLD = "COVERAGE_BELOW_THRESHOLD"
ENVIRONMENT_IDENTITY_NOT_VERIFIED = "ENVIRONMENT_IDENTITY_NOT_VERIFIED"

# Suspected-bug reporting for source assignment ambiguity. Detection is pure
# observation: matching stays fail-closed and ambiguous trips are never updated.
ALPHA_DYSPONENT_ASSIGNMENT_CONFLICT = "ALPHA_DYSPONENT_ASSIGNMENT_CONFLICT"
MAX_REPORTED_AMBIGUITY_GROUPS = 50
MAX_EVIDENCE_ITEMS = 20


class EnrichmentPreconditionError(RuntimeError):
    def __init__(self, code: str, message: str, diagnostics: dict[str, Any] | None = None):
        self.code = code
        self.diagnostics = diagnostics or {}
        super().__init__(f"{code}: {message}")


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
    client_db_environment: str | None = None
    client_db_identity_id: str | None = None


@dataclass(frozen=True)
class JobParams:
    client_code: str
    dry_run: bool
    overwrite_existing: bool
    limit: int | None
    batch_size: int
    max_batches: int | None
    process_all: bool
    date_from: date | None
    date_to: date | None
    registration: str | None
    trigger: str | None
    source_raw_file_id: str | None
    source_workflow_run_id: str | None
    source_cleaned_artifact_id: str | None
    max_source_age_hours: int
    min_coverage_percent: Decimal
    min_distance_coverage_percent: Decimal
    max_ambiguous_matches: int
    report_suspected_bugs: bool = True
    require_fresh_source: bool = False

    @property
    def force(self) -> bool:
        return self.overwrite_existing


@dataclass(frozen=True)
class ResolvedWindow:
    requested_start: date
    requested_end_exclusive: date | None
    start: date
    end_exclusive: date
    source_boundary_exclusive: date
    trip_boundary_exclusive: date
    capped_to_source: bool
    capped_to_trips: bool


class ClientProcessingError(RuntimeError):
    def __init__(self, config: ClientDbConfig, phase: str, original: Exception) -> None:
        self.client_code = config.client_code
        self.client_db_name = config.client_db_name
        self.phase = phase
        self.original = original
        self.original_message = str(original)
        super().__init__(self.original_message)


def run(client, run_id: str, params: dict):
    job_params = _parse_params(params or {})
    _validate_client_code_allowed(job_params.client_code)
    _log(client, run_id, "INFO", "ALPHA00001 Dysponent_ID enrichment started", context=_params_context(job_params))
    try:
        runtime = load_runtime_identity()
        with _platform_pg_conn() as platform_conn:
            attest_platform_identity(platform_conn, runtime)
            config = _load_client_config(platform_conn, job_params.client_code)
        summary = _process_client(config, job_params, client=client, run_id=run_id, runtime=runtime)
        level = "INFO" if summary["readiness_passed"] else "WARNING"
        _log(client, run_id, level, "ALPHA00001 Dysponent_ID enrichment finished", context=summary)
        print(json.dumps(summary, sort_keys=True, default=_json_default), flush=True)
        return summary
    except Exception as exc:
        code = _error_code(exc)
        context = {**_params_context(job_params), "status": "ERROR", "error_code": code}
        if isinstance(exc, EnrichmentPreconditionError):
            context.update(exc.diagnostics)
        _log(client, run_id, "ERROR", f"ALPHA00001 Dysponent_ID enrichment failed: {code}",
             context=context, error=traceback.format_exc())
        print(json.dumps(context, sort_keys=True, default=_json_default), flush=True)
        raise


def _process_client(
    config: ClientDbConfig,
    params: JobParams,
    *,
    client=None,
    run_id: str | None = None,
    runtime=None,
    now_fn: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    phase = "connect_client_db"
    conn = None
    now = _aware_now(now_fn)
    try:
        conn = _client_business_pg_conn(config)
        runtime = runtime or load_runtime_identity()
        attest_client_identity(conn, runtime, ClientIdentityExpectation(
            client_code=config.client_code,
            environment=config.client_db_environment,
            database_identity_id=config.client_db_identity_id,
            database_name=config.client_db_name,
            database_user=config.client_db_user,
        ))
        with conn.cursor() as cur:
            phase = "validate_schema"
            source_columns = _existing_columns(cur, ASSIGNMENT_SCHEMA, ASSIGNMENT_TABLE)
            trip_columns = _existing_columns(cur, CLIENT_TRIPS_SCHEMA, CLIENT_TRIPS_TABLE)
            _validate_required_schema(source_columns, trip_columns)
        conn.rollback()

        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute("SELECT set_config('TimeZone', %s, true)", (WARSAW_TZ_NAME,))
            phase = "source_readiness"
            source = _load_source_metadata(cur)
            window = _resolve_window(cur, config=config, params=params, source=source)
            phase = "coverage_analysis"
            metrics = _analyze_scope(cur, config=config, params=params, window=window)
            phase = "ambiguity_inspection"
            ambiguity_groups = _load_ambiguity_groups(
                cur, config=config, params=params, window=window, source_columns=source_columns,
            )
        conn.rollback()

        failures = _readiness_failures(
            params=params, source=source, window=window, metrics=metrics, evaluated_at=now,
        )
        summary = _summary(
            config=config, params=params, source=source, window=window, metrics=metrics,
            failures=failures, evaluated_at=now, rows_updated=0, batches_committed=0,
        )
        if summary["enriched_beyond_source_boundary"]:
            _log(client, run_id or "", "WARNING",
                 "ALPHA00001 Dysponent_ID enrichment uses assignments older than the target window",
                 context={key: summary[key] for key in (
                     "require_fresh_source", "resolved_end_date_exclusive", "source_boundary_exclusive",
                     "source_loaded_at", "source_age_hours", "source_business_date_max",
                 )})
        phase = "suspected_bug_reporting"
        summary["ambiguity_groups_detected"] = len(ambiguity_groups)
        summary["suspected_bug_reports"] = _report_ambiguity_groups(
            groups=ambiguity_groups, config=config, params=params, window=window,
            source=source, runtime=runtime, client=client, run_id=run_id or "", evaluated_at=now,
        )
        if params.dry_run:
            return summary
        if failures:
            first = failures[0]
            raise EnrichmentPreconditionError(first["code"], first["message"], summary)

        phase = "enrich_batches"
        rows_updated, batches = _execute_batches(
            conn, config=config, params=params, window=window,
            expected_source_raw_file_id=str(source["raw_file_id"]),
            client=client, run_id=run_id or "",
        )
        stopped_reason = (
            "reached_limit" if params.limit is not None and rows_updated >= params.limit
            else "reached_max_batches" if params.max_batches is not None and batches >= params.max_batches
            else "single_batch_complete" if not params.process_all
            else "no_more_updates"
        )
        return {
            **summary, "dry_run": False, "rows_updated": rows_updated,
            "total_updated": rows_updated, "batches_committed": batches, "status": "OK",
            "stopped_reason": stopped_reason,
        }
    except Exception as exc:
        if conn is not None:
            _rollback_quietly(conn)
        if isinstance(exc, (ClientProcessingError, EnrichmentPreconditionError, EnvironmentIdentityError)):
            raise
        raise ClientProcessingError(config, phase, exc) from exc
    finally:
        if conn is not None:
            conn.close()


def _load_source_metadata(cur) -> dict[str, Any]:
    cur.execute(f"""
        SELECT count(*)::integer AS source_rows_inspected,
          count(*) FILTER (WHERE NULLIF(btrim(source_id), '') IS NULL)::integer AS blank_source_assignments,
          count(*) FILTER (
            WHERE NULLIF({_registration_sql_expr('registration')}, '') IS NULL OR assignment_date IS NULL
          )::integer AS invalid_source_rows,
          count(DISTINCT raw_file_id)::integer AS raw_file_count,
          count(DISTINCT workflow_run_id)::integer AS workflow_run_count,
          count(DISTINCT cleaned_artifact_id)::integer AS cleaned_artifact_count,
          count(*) FILTER (
            WHERE raw_file_id IS NULL OR workflow_run_id IS NULL OR cleaned_artifact_id IS NULL
          )::integer AS source_rows_missing_provenance,
          min(raw_file_id::text) AS raw_file_id,
          min(workflow_run_id::text) AS workflow_run_id,
          min(cleaned_artifact_id::text) AS cleaned_artifact_id,
          min(imported_at) AS source_loaded_at_min,
          max(imported_at) AS source_loaded_at,
          min(assignment_date) AS source_business_date_min,
          max(assignment_date) AS source_business_date_max
        FROM {_qualified_ident(ASSIGNMENT_SCHEMA, ASSIGNMENT_TABLE)}
    """)
    return dict(cur.fetchone() or {})


def _resolve_window(cur, *, config: ClientDbConfig, params: JobParams, source: dict[str, Any]) -> ResolvedWindow:
    if params.date_from is None:
        raise EnrichmentPreconditionError(
            INVALID_DATE_RANGE,
            "date_from is required; automated callers derive it from the previous committed source load",
        )
    loaded_at = source.get("source_loaded_at")
    if loaded_at is None:
        raise EnrichmentPreconditionError(SOURCE_REPORT_EMPTY, "source assignment table is empty")
    source_boundary = _as_aware(loaded_at).astimezone(WARSAW_TZ).date()
    cur.execute(f"""
        SELECT max({_qi(CLIENT_TRIPS_START_COLUMN)}) AS latest_trip_timestamp
        FROM {_qualified_ident(CLIENT_TRIPS_SCHEMA, CLIENT_TRIPS_TABLE)}
        WHERE client_id = %s
    """, (config.client_id,))
    latest_trip = (cur.fetchone() or {}).get("latest_trip_timestamp")
    if latest_trip is None:
        raise EnrichmentPreconditionError(SOURCE_REPORT_NOT_READY, "target client has no trips")
    trip_boundary = _as_aware(latest_trip).astimezone(WARSAW_TZ).date() + timedelta(days=1)
    requested_end = params.date_to
    candidate_ends = [value for value in (requested_end, trip_boundary) if value is not None]
    if params.require_fresh_source:
        candidate_ends.append(source_boundary)
    end = min(candidate_ends)
    if params.date_from >= end:
        raise EnrichmentPreconditionError(INVALID_DATE_RANGE,
            "resolved date range must contain at least one complete Warsaw-local day", {
                "requested_start_date": params.date_from.isoformat(),
                "requested_end_date_exclusive": requested_end.isoformat() if requested_end else None,
                "source_boundary_exclusive": source_boundary.isoformat(),
                "trip_boundary_exclusive": trip_boundary.isoformat(),
            })
    return ResolvedWindow(
        requested_start=params.date_from, requested_end_exclusive=requested_end,
        start=params.date_from, end_exclusive=end,
        source_boundary_exclusive=source_boundary, trip_boundary_exclusive=trip_boundary,
        capped_to_source=(params.require_fresh_source
                          and (requested_end is None or source_boundary < requested_end)),
        capped_to_trips=requested_end is None or trip_boundary < requested_end,
    )


def _scope_ctes(*, params: JobParams) -> tuple[str, dict[str, Any]]:
    registration_filter = ""
    values: dict[str, Any] = {"timezone": WARSAW_TZ_NAME}
    if params.registration:
        registration_filter = (
            f"AND {_registration_sql_expr('ct.' + _qi(CLIENT_TRIPS_REGISTRATION_COLUMN))} = %(registration)s"
        )
        values["registration"] = _normalize_registration(params.registration)
    sql = f"""
    source_valid AS MATERIALIZED (
      SELECT {_registration_sql_expr('log.registration')} AS registration_norm,
             log.assignment_date, btrim(log.source_id) AS source_id
      FROM {_qualified_ident(ASSIGNMENT_SCHEMA, ASSIGNMENT_TABLE)} AS log
      WHERE NULLIF(btrim(log.source_id), '') IS NOT NULL
        AND NULLIF({_registration_sql_expr('log.registration')}, '') IS NOT NULL
        AND log.assignment_date IS NOT NULL
    ),
    source_groups AS MATERIALIZED (
      SELECT registration_norm, assignment_date,
             array_agg(DISTINCT source_id ORDER BY source_id) AS assignment_ids,
             count(*)::integer AS source_row_count
      FROM source_valid GROUP BY registration_norm, assignment_date
    ),
    scoped AS MATERIALIZED (
      SELECT ct.client_id, ct.provider_trip_id, ct.start_timestamp, ct.trip_distance_meters,
             NULLIF(btrim(ct.{_qi('Driver_Restrictions')}), '') AS driver_restrictions,
             NULLIF(btrim(ct.{_qi(CLIENT_TRIPS_TARGET_COLUMN)}), '') AS current_dysponent_id,
             {_registration_sql_expr('ct.' + _qi(CLIENT_TRIPS_REGISTRATION_COLUMN))} AS registration_norm
      FROM {_qualified_ident(CLIENT_TRIPS_SCHEMA, CLIENT_TRIPS_TABLE)} AS ct
      WHERE ct.client_id = %(client_id)s
        AND ct.{_qi(CLIENT_TRIPS_START_COLUMN)} >= (%(start_date)s::date::timestamp AT TIME ZONE %(timezone)s)
        AND ct.{_qi(CLIENT_TRIPS_START_COLUMN)} < (%(end_date)s::date::timestamp AT TIME ZONE %(timezone)s)
        AND coalesce(ct.driver_tag_description, '') NOT ILIKE '%%pryw%%'
        {registration_filter}
    ),
    matched AS MATERIALIZED (
      SELECT trip.*, chosen.assignment_date AS selected_assignment_date, chosen.assignment_ids,
             CASE WHEN cardinality(chosen.assignment_ids) = 1 THEN chosen.assignment_ids[1] END AS proposed_dysponent_id
      FROM scoped AS trip
      LEFT JOIN LATERAL (
        SELECT sg.assignment_date, sg.assignment_ids
        FROM source_groups AS sg
        WHERE sg.registration_norm = trip.registration_norm
          AND sg.assignment_date <= (trip.start_timestamp AT TIME ZONE %(timezone)s)::date
        ORDER BY sg.assignment_date DESC LIMIT 1
      ) AS chosen ON true
    ),
    classified AS MATERIALIZED (
      SELECT matched.*,
             coalesce(
               driver_restrictions,
               CASE WHEN %(overwrite_existing)s AND proposed_dysponent_id IS NOT NULL
                    THEN proposed_dysponent_id ELSE current_dysponent_id END,
               proposed_dysponent_id
             ) AS predicted_assignment_id,
             assignment_ids IS NOT NULL AND cardinality(assignment_ids) > 1 AS is_ambiguous,
             current_dysponent_id IS NOT NULL AND proposed_dysponent_id IS NOT NULL
               AND current_dysponent_id <> proposed_dysponent_id AS has_existing_conflict
      FROM matched
    ),
    charted AS MATERIALIZED (
      SELECT classified.*, (chart.driver_id IS NOT NULL AND chart.is_active) AS active_chart_match
      FROM classified
      LEFT JOIN public.eco_drivers_id_chart AS chart
        ON chart.client_id = classified.client_id AND chart.driver_id = classified.predicted_assignment_id
    )
    """
    return sql, values


def _analyze_scope(cur, *, config: ClientDbConfig, params: JobParams, window: ResolvedWindow) -> dict[str, Any]:
    ctes, values = _scope_ctes(params=params)
    values.update(client_id=config.client_id, start_date=window.start,
                  end_date=window.end_exclusive, overwrite_existing=params.overwrite_existing)
    cur.execute(f"""
      WITH {ctes},
      used_source_groups AS (
        SELECT DISTINCT registration_norm, selected_assignment_date FROM matched
        WHERE selected_assignment_date IS NOT NULL
      ),
      used_source_rows AS (
        SELECT coalesce(sum(sg.source_row_count), 0)::integer AS row_count
        FROM source_groups sg JOIN used_source_groups used
          ON used.registration_norm = sg.registration_norm AND used.selected_assignment_date = sg.assignment_date
      ),
      target_total AS (
        SELECT
          count(*)::integer AS row_count,
          count(*) FILTER (
            WHERE {_qi(CLIENT_TRIPS_START_COLUMN)} >= (%(start_date)s::date::timestamp AT TIME ZONE %(timezone)s)
              AND {_qi(CLIENT_TRIPS_START_COLUMN)} < (%(end_date)s::date::timestamp AT TIME ZONE %(timezone)s)
          )::integer AS rows_in_period,
          count(*) FILTER (
            WHERE {_qi(CLIENT_TRIPS_START_COLUMN)} >= (%(start_date)s::date::timestamp AT TIME ZONE %(timezone)s)
              AND {_qi(CLIENT_TRIPS_START_COLUMN)} < (%(end_date)s::date::timestamp AT TIME ZONE %(timezone)s)
              AND coalesce(driver_tag_description, '') ILIKE '%%pryw%%'
          )::integer AS private_rows_in_period
        FROM {_qualified_ident(CLIENT_TRIPS_SCHEMA, CLIENT_TRIPS_TABLE)} WHERE client_id = %(client_id)s
      )
      SELECT count(*)::integer AS target_trips_in_scope,
        (SELECT row_count - rows_in_period FROM target_total) AS skipped_outside_requested_period,
        (SELECT private_rows_in_period FROM target_total) AS private_trips_excluded,
        count(*) FILTER (WHERE proposed_dysponent_id IS NOT NULL)::integer AS deterministic_matches,
        count(*) FILTER (WHERE proposed_dysponent_id IS NOT NULL AND
          (current_dysponent_id IS NULL OR
           (%(overwrite_existing)s AND current_dysponent_id <> proposed_dysponent_id)))::integer AS planned_updates,
        count(*) FILTER (WHERE current_dysponent_id IS NOT NULL
          AND current_dysponent_id = proposed_dysponent_id)::integer AS already_correct,
        count(*) FILTER (WHERE assignment_ids IS NULL)::integer AS no_source_match,
        count(*) FILTER (WHERE is_ambiguous)::integer AS ambiguous_source_matches,
        count(*) FILTER (WHERE has_existing_conflict)::integer AS conflicting_existing_target_values,
        count(*) FILTER (WHERE driver_restrictions IS NOT NULL)::integer AS trips_with_driver_restrictions,
        count(*) FILTER (WHERE driver_restrictions IS NULL)::integer AS trips_requiring_dysponent_fallback,
        count(*) FILTER (WHERE driver_restrictions IS NULL AND current_dysponent_id IS NOT NULL)::integer
          AS trips_with_usable_existing_dysponent,
        count(*) FILTER (WHERE driver_restrictions IS NULL AND current_dysponent_id IS NULL
          AND proposed_dysponent_id IS NOT NULL)::integer AS trips_that_would_gain_dysponent,
        count(*) FILTER (WHERE predicted_assignment_id IS NULL)::integer AS trips_remaining_without_assignment,
        count(*) FILTER (WHERE predicted_assignment_id IS NOT NULL AND active_chart_match)::integer
          AS trips_resolving_to_active_driver_chart,
        count(DISTINCT predicted_assignment_id) FILTER
          (WHERE predicted_assignment_id IS NOT NULL AND NOT active_chart_match)::integer
          AS distinct_unmatched_assignment_identifiers,
        coalesce(array_agg(DISTINCT predicted_assignment_id ORDER BY predicted_assignment_id)
          FILTER (WHERE predicted_assignment_id IS NOT NULL AND NOT active_chart_match), ARRAY[]::text[])
          AS unmatched_assignment_identifiers,
        coalesce(sum(coalesce(trip_distance_meters, 0)), 0)::bigint AS total_distance_meters,
        coalesce(sum(coalesce(trip_distance_meters, 0)) FILTER
          (WHERE coalesce(driver_restrictions, current_dysponent_id) IS NOT NULL), 0)::bigint
          AS assigned_distance_before_meters,
        coalesce(sum(coalesce(trip_distance_meters, 0)) FILTER
          (WHERE predicted_assignment_id IS NOT NULL), 0)::bigint AS predicted_assigned_distance_meters,
        round(100.0 * count(*) FILTER
          (WHERE coalesce(driver_restrictions, current_dysponent_id) IS NOT NULL) / nullif(count(*), 0), 2)
          AS coverage_before_trip_percent,
        round(100.0 * count(*) FILTER (WHERE predicted_assignment_id IS NOT NULL) / nullif(count(*), 0), 2)
          AS predicted_trip_coverage_percent,
        round(100.0 * coalesce(sum(coalesce(trip_distance_meters, 0)) FILTER
          (WHERE coalesce(driver_restrictions, current_dysponent_id) IS NOT NULL), 0)
          / nullif(sum(coalesce(trip_distance_meters, 0)), 0), 2) AS coverage_before_distance_percent,
        round(100.0 * coalesce(sum(coalesce(trip_distance_meters, 0)) FILTER
          (WHERE predicted_assignment_id IS NOT NULL), 0)
          / nullif(sum(coalesce(trip_distance_meters, 0)), 0), 2) AS predicted_distance_coverage_percent,
        round(100.0 * count(*) FILTER
          (WHERE predicted_assignment_id IS NOT NULL AND active_chart_match)
          / nullif(count(*) FILTER (WHERE predicted_assignment_id IS NOT NULL), 0), 2)
          AS active_driver_chart_match_percent,
        (SELECT count(*) FROM source_valid)::integer AS valid_source_rows,
        greatest((SELECT count(*) FROM source_valid) - (SELECT row_count FROM used_source_rows), 0)::integer
          AS source_rows_not_used
      FROM charted
    """, values)
    row = dict(cur.fetchone() or {})
    for name, value in list(row.items()):
        if isinstance(value, Decimal):
            row[name] = str(value)
    row["unmatched_assignment_identifiers"] = list(row.get("unmatched_assignment_identifiers") or [])
    return row


def _load_ambiguity_groups(cur, *, config: ClientDbConfig, params: JobParams,
                           window: ResolvedWindow, source_columns: set[str]) -> list[dict[str, Any]]:
    """One row per ambiguity group: a normalized registration whose latest applicable
    assignment date carries more than one distinct source id. Never one row per trip."""
    ctes, values = _scope_ctes(params=params)
    values.update(client_id=config.client_id, start_date=window.start,
                  end_date=window.end_exclusive, overwrite_existing=params.overwrite_existing)
    cur.execute(f"""
      WITH {ctes}
      SELECT registration_norm, selected_assignment_date, assignment_ids,
             count(*)::integer AS affected_trip_count,
             min(start_timestamp) AS first_trip_at,
             max(start_timestamp) AS last_trip_at,
             (array_agg(provider_trip_id ORDER BY start_timestamp, provider_trip_id))
               [1:{MAX_EVIDENCE_ITEMS}] AS sample_trip_ids,
             count(*) FILTER (WHERE current_dysponent_id IS NOT NULL)::integer
               AS trips_with_existing_target_value,
             count(*) FILTER (WHERE driver_restrictions IS NOT NULL)::integer
               AS trips_with_driver_restrictions
      FROM charted
      WHERE is_ambiguous
      GROUP BY registration_norm, selected_assignment_date, assignment_ids
      ORDER BY count(*) DESC, registration_norm, selected_assignment_date
      LIMIT {MAX_REPORTED_AMBIGUITY_GROUPS}
    """, values)
    groups = [dict(row) for row in cur.fetchall()]
    return _attach_source_evidence(cur, groups, source_columns=source_columns) if groups else []


def _attach_source_evidence(cur, groups: list[dict[str, Any]], *,
                            source_columns: set[str]) -> list[dict[str, Any]]:
    """Bounded source-side provenance for each ambiguity group (row numbers, CSV
    filenames, artifact ids). Optional columns that the source lacks stay NULL."""
    def optional_array(column: str, cast: str) -> str:
        if column not in source_columns:
            return f"NULL::{cast}[]"
        return (f"(array_agg(DISTINCT log.{_qi(column)} ORDER BY log.{_qi(column)}) "
                f"FILTER (WHERE log.{_qi(column)} IS NOT NULL))[1:{MAX_EVIDENCE_ITEMS}]")

    def optional_scalar(column: str) -> str:
        return f"min(log.{_qi(column)}::text)" if column in source_columns else "NULL::text"

    cur.execute(f"""
      SELECT {_registration_sql_expr('log.registration')} AS registration_norm,
             log.assignment_date, btrim(log.source_id) AS source_id,
             count(*)::integer AS source_row_count,
             {optional_array('source_row_number', 'integer')} AS source_row_numbers,
             {optional_array('csv_filename', 'text')} AS source_csv_filenames,
             {optional_scalar('raw_file_id')} AS source_raw_file_id,
             {optional_scalar('workflow_run_id')} AS source_workflow_run_id,
             {optional_scalar('source_artifact_id')} AS source_artifact_id,
             {optional_scalar('normalized_artifact_id')} AS normalized_artifact_id,
             {optional_scalar('cleaned_artifact_id')} AS cleaned_artifact_id,
             {optional_scalar('imported_at')} AS source_loaded_at
      FROM {_qualified_ident(ASSIGNMENT_SCHEMA, ASSIGNMENT_TABLE)} AS log
      WHERE {_registration_sql_expr('log.registration')} = ANY(%(registrations)s)
        AND log.assignment_date = ANY(%(assignment_dates)s)
        AND NULLIF(btrim(log.source_id), '') IS NOT NULL
      GROUP BY 1, 2, 3
      ORDER BY 1, 2, 3
    """, {
        "registrations": sorted({str(group["registration_norm"]) for group in groups}),
        "assignment_dates": sorted({group["selected_assignment_date"] for group in groups}),
    })

    evidence: dict[tuple[str, Any], list[dict[str, Any]]] = {}
    for row in cur.fetchall():
        evidence.setdefault((str(row["registration_norm"]), row["assignment_date"]), []).append(dict(row))

    for group in groups:
        key = (str(group["registration_norm"]), group["selected_assignment_date"])
        rows = [row for row in evidence.get(key, [])
                if str(row["source_id"]) in {str(item) for item in (group["assignment_ids"] or [])}]
        group["source_rows"] = rows
        group["source_row_numbers"] = sorted(
            {int(number) for row in rows for number in (row["source_row_numbers"] or [])}
        )[:MAX_EVIDENCE_ITEMS]
        group["source_csv_filenames"] = sorted(
            {str(name) for row in rows for name in (row["source_csv_filenames"] or [])}
        )[:MAX_EVIDENCE_ITEMS]
        group["source_row_counts_by_id"] = {
            str(row["source_id"]): int(row["source_row_count"]) for row in rows
        }
        for name in ("source_raw_file_id", "source_workflow_run_id", "source_artifact_id",
                     "normalized_artifact_id", "cleaned_artifact_id", "source_loaded_at"):
            group[name] = next((row[name] for row in rows if row.get(name)), None)
    return groups


def _report_ambiguity_groups(*, groups: list[dict[str, Any]], config: ClientDbConfig,
                             params: JobParams, window: ResolvedWindow, source: dict[str, Any],
                             runtime, client, run_id: str, evaluated_at: datetime) -> list[dict[str, Any]]:
    """Report one suspected_bug incident per ambiguity group.

    Purely observational: it runs after matching has already decided to skip these
    trips, never raises, and never influences readiness, matching or batch updates.
    """
    if not groups:
        return []
    if not params.report_suspected_bugs:
        _log(client, run_id, "INFO", "ALPHA00001 Dysponent_ID ambiguity durable reporting disabled",
             context={"ambiguity_groups_detected": len(groups),
                      "report_suspected_bugs": False})
        return [_ambiguity_group_context(group, reported=False) for group in groups]

    environment = getattr(runtime, "environment", None) or os.getenv("LOG_PLATFORM_TARGET_ENVIRONMENT")
    reports: list[dict[str, Any]] = []
    for group in groups:
        event = _build_ambiguity_event(
            group=group, config=config, params=params, window=window, source=source,
            environment=str(environment or "unknown"), run_id=run_id, evaluated_at=evaluated_at,
        )
        result = safe_report_suspected_bug(event)
        entry = _ambiguity_group_context(group, reported=True)
        entry.update({
            "fingerprint": result.fingerprint or None,
            "incident_id": result.incident_id,
            "log_id": result.log_id,
            "occurrence_count": result.occurrence_count,
            "email_enqueued": result.email_enqueued,
            "email_suppression_reason": result.suppression_reason,
            "report_error": result.error,
        })
        reports.append(entry)
        _log(client, run_id, "WARNING", "ALPHA00001 Dysponent_ID assignment conflict detected",
             context={**entry, "incident_code": ALPHA_DYSPONENT_ASSIGNMENT_CONFLICT,
                      "classification": "suspected_bug"})
    return reports


def _ambiguity_group_context(group: dict[str, Any], *, reported: bool) -> dict[str, Any]:
    return {
        "incident_code": ALPHA_DYSPONENT_ASSIGNMENT_CONFLICT,
        "durable_report": reported,
        "registration": str(group["registration_norm"]),
        "effective_assignment_date": _iso(group["selected_assignment_date"]),
        "conflicting_assignment_ids": [str(item) for item in (group["assignment_ids"] or [])],
        "affected_trip_count": int(group["affected_trip_count"] or 0),
        "rows_modified": 0,
    }


def _build_ambiguity_event(*, group: dict[str, Any], config: ClientDbConfig, params: JobParams,
                           window: ResolvedWindow, source: dict[str, Any], environment: str,
                           run_id: str, evaluated_at: datetime) -> SuspectedBugEvent:
    registration = str(group["registration_norm"])
    assignment_date = _iso(group["selected_assignment_date"])
    conflicting_ids = sorted(str(item) for item in (group["assignment_ids"] or []))
    affected = int(group["affected_trip_count"] or 0)
    source_table = _qualified_ident(ASSIGNMENT_SCHEMA, ASSIGNMENT_TABLE)
    target_table = _qualified_ident(CLIENT_TRIPS_SCHEMA, CLIENT_TRIPS_TABLE)

    return SuspectedBugEvent(
        incident_code=ALPHA_DYSPONENT_ASSIGNMENT_CONFLICT,
        title=f"Conflicting {CLIENT_TRIPS_TARGET_COLUMN} assignments for {registration}",
        summary=(
            f"{len(conflicting_ids)} distinct source ids ({', '.join(conflicting_ids)}) are assigned to "
            f"registration {registration} on the same latest applicable assignment date {assignment_date} "
            f"in {source_table}. One registration must resolve to exactly one "
            f"{CLIENT_TRIPS_TARGET_COLUMN} for a given date, so matching is fail-closed: "
            f"{affected} trip(s) in the processed window were skipped and no target row was modified."
        ),
        occurred_at=evaluated_at,
        environment=environment,
        severity="error",
        component=SOURCE,
        workflow_name="workflow_b",
        stage_name="postprocess",
        job_name=SOURCE,
        client_id=config.client_id,
        client_code=config.client_code,
        run_id=run_id or None,
        report_type=ASSIGNMENT_TABLE,
        dataset_name=ASSIGNMENT_TABLE,
        database_name=config.client_db_name,
        schema_name=ASSIGNMENT_SCHEMA,
        table_name=ASSIGNMENT_TABLE,
        raw_file_id=group.get("source_raw_file_id") or _text(source.get("raw_file_id")),
        file_id=group.get("cleaned_artifact_id") or _text(source.get("cleaned_artifact_id")),
        raw_artifact_id=group.get("source_artifact_id"),
        normalized_artifact_id=group.get("normalized_artifact_id"),
        cleaned_artifact_id=group.get("cleaned_artifact_id") or _text(source.get("cleaned_artifact_id")),
        stage3_result_artifact_id=None,
        subject_type="vehicle_registration",
        subject_key=CLIENT_TRIPS_REGISTRATION_COLUMN,
        subject_value=registration,
        affected_record_count=affected,
        affected_period_start=_as_aware_or_none(group.get("first_trip_at")),
        affected_period_end=_as_aware_or_none(group.get("last_trip_at")),
        processing_outcome=(
            "ambiguous trips skipped by fail-closed matching; no target rows modified"
        ),
        rows_modified=0,
        fingerprint_fields={
            "source_table": source_table,
            "target_table": target_table,
            "target_column": CLIENT_TRIPS_TARGET_COLUMN,
            "conflicting_assignment_ids": conflicting_ids,
            "effective_assignment_date": assignment_date,
        },
        details={
            "registration": registration,
            "effective_assignment_date": assignment_date,
            "conflicting_assignment_ids": conflicting_ids,
            "affected_trip_count": affected,
            "source_table": source_table,
            "target_table": target_table,
            "target_column": CLIENT_TRIPS_TARGET_COLUMN,
            "rows_modified_for_this_group": 0,
            "trips_skipped": True,
            "trips_with_existing_target_value": int(group.get("trips_with_existing_target_value") or 0),
            "trips_with_driver_restrictions": int(group.get("trips_with_driver_restrictions") or 0),
            "source_workflow_run_id": group.get("source_workflow_run_id") or _text(source.get("workflow_run_id")),
            "source_loaded_at": group.get("source_loaded_at") or _iso(source.get("source_loaded_at")),
            "processed_date_from": window.start.isoformat(),
            "processed_date_to_exclusive": window.end_exclusive.isoformat(),
            "timezone": WARSAW_TZ_NAME,
            "dry_run": params.dry_run,
            "overwrite_existing": params.overwrite_existing,
            "max_ambiguous_matches": params.max_ambiguous_matches,
        },
        evidence={
            "sample_affected_trip_ids": [str(item) for item in (group.get("sample_trip_ids") or [])],
            "first_affected_trip_at": _iso(group.get("first_trip_at")),
            "last_affected_trip_at": _iso(group.get("last_trip_at")),
            "source_row_numbers": group.get("source_row_numbers") or [],
            "source_csv_filenames": group.get("source_csv_filenames") or [],
            "source_row_counts_by_assignment_id": group.get("source_row_counts_by_id") or {},
        },
        suggested_action=(
            f"Inspect {source_table} rows for registration {registration} on {assignment_date} "
            f"(see source row numbers in the evidence). Exactly one source id must remain for that "
            f"registration and date; correct the source workbook and re-run the controlled source "
            f"refresh. Do not pick one id manually in {target_table}."
        ),
    )


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


def _as_aware_or_none(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo and value.utcoffset() is not None else value.replace(tzinfo=timezone.utc)


def _readiness_failures(*, params: JobParams, source: dict[str, Any], window: ResolvedWindow,
                        metrics: dict[str, Any], evaluated_at: datetime) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    if int(source.get("source_rows_inspected") or 0) == 0:
        return [_failure(SOURCE_REPORT_EMPTY, "source assignment table is empty")]
    if (
        int(source.get("raw_file_count") or 0) != 1
        or int(source.get("workflow_run_count") or 0) != 1
        or int(source.get("cleaned_artifact_count") or 0) != 1
        or int(source.get("source_rows_missing_provenance") or 0) != 0
    ):
        failures.append(_failure(SOURCE_REPORT_NOT_READY, "source table does not have one complete committed load identity"))
    if params.source_raw_file_id and str(source.get("raw_file_id") or "") != params.source_raw_file_id:
        failures.append(_failure(SOURCE_REPORT_NOT_READY,
                                 "source raw_file_id does not match the committed Stage 3 dependency"))
    if params.source_workflow_run_id and str(source.get("workflow_run_id") or "") != params.source_workflow_run_id:
        failures.append(_failure(SOURCE_REPORT_NOT_READY,
                                 "source workflow_run_id does not match the requested dependency"))
    if params.source_cleaned_artifact_id and str(source.get("cleaned_artifact_id") or "") != params.source_cleaned_artifact_id:
        failures.append(_failure(SOURCE_REPORT_NOT_READY,
                                 "source cleaned artifact does not match the committed Stage 3 dependency"))
    loaded_at = source.get("source_loaded_at")
    if loaded_at is None:
        failures.append(_failure(SOURCE_REPORT_NOT_READY, "source load timestamp is missing"))
    else:
        age = evaluated_at.astimezone(timezone.utc) - _as_aware(loaded_at).astimezone(timezone.utc)
        if age.total_seconds() < -60:
            failures.append(_failure(SOURCE_REPORT_NOT_READY, "source load timestamp is in the future"))
        elif params.require_fresh_source and age > timedelta(hours=params.max_source_age_hours):
            failures.append(_failure(SOURCE_REPORT_NOT_READY,
                                     "source report exceeds the configured freshness limit"))
    if params.require_fresh_source and window.end_exclusive > window.source_boundary_exclusive:
        failures.append(_failure(SOURCE_REPORT_NOT_READY,
                                 "target window extends beyond the fully available source boundary"))
    target_count = int(metrics.get("target_trips_in_scope") or 0)
    if target_count == 0:
        failures.append(_failure(SOURCE_REPORT_NOT_READY,
                                 "resolved target window contains no non-private trips"))
    fallback = int(metrics.get("trips_requiring_dysponent_fallback") or 0)
    existing = int(metrics.get("trips_with_usable_existing_dysponent") or 0)
    gains = int(metrics.get("trips_that_would_gain_dysponent") or 0)
    if fallback > existing and gains == 0:
        failures.append(_failure(SOURCE_REPORT_NOT_READY,
                                 "fallback trips exist but the source produces no deterministic gains"))
    if int(metrics.get("ambiguous_source_matches") or 0) > params.max_ambiguous_matches:
        failures.append(_failure(AMBIGUOUS_ENRICHMENT_MATCH,
                                 "ambiguous matches exceed the configured allowance"))
    if (_decimal_metric(metrics, "predicted_trip_coverage_percent") < params.min_coverage_percent or
        _decimal_metric(metrics, "predicted_distance_coverage_percent") < params.min_distance_coverage_percent):
        failures.append(_failure(COVERAGE_BELOW_THRESHOLD,
                                 "predicted assignment coverage is below the configured threshold"))
    return failures


def _execute_batches(conn, *, config: ClientDbConfig, params: JobParams, window: ResolvedWindow,
                     expected_source_raw_file_id: str, client, run_id: str) -> tuple[int, int]:
    total = 0
    batches = 0
    while True:
        if params.max_batches is not None and batches >= params.max_batches:
            break
        if params.limit is not None and total >= params.limit:
            break
        batch_size = params.batch_size if params.limit is None else min(params.batch_size, params.limit - total)
        started = time.monotonic()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT set_config('TimeZone', %s, true)", (WARSAW_TZ_NAME,))
                _assert_source_identity(cur, expected_source_raw_file_id)
                updated = _update_batch(cur, config=config, params=params,
                                        window=window, batch_size=batch_size)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        batches += 1
        total += updated
        _log(client, run_id, "INFO", "ALPHA00001 Dysponent_ID enrichment batch committed", context={
            "client_code": config.client_code, "batch_number": batches, "batch_size": batch_size,
            "rows_updated": updated, "total_updated": total,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "source_raw_file_id": expected_source_raw_file_id,
            "date_from": window.start.isoformat(),
            "date_to_exclusive": window.end_exclusive.isoformat(),
            "overwrite_existing": params.overwrite_existing,
        })
        if updated == 0 or not params.process_all:
            break
    return total, batches


def _assert_source_identity(cur, expected_source_raw_file_id: str) -> None:
    cur.execute(f"""
      SELECT count(*)::integer AS rows, count(DISTINCT raw_file_id)::integer AS raw_files,
             min(raw_file_id::text) AS raw_file_id
      FROM {_qualified_ident(ASSIGNMENT_SCHEMA, ASSIGNMENT_TABLE)}
    """)
    row = dict(cur.fetchone() or {})
    if (int(row.get("rows") or 0) == 0 or int(row.get("raw_files") or 0) != 1 or
        str(row.get("raw_file_id") or "") != expected_source_raw_file_id):
        raise EnrichmentPreconditionError(SOURCE_REPORT_NOT_READY,
            "source load identity changed before the batch; no batch update was committed")


def _update_batch(cur, *, config: ClientDbConfig, params: JobParams,
                  window: ResolvedWindow, batch_size: int) -> int:
    registration_filter = ""
    values: dict[str, Any] = {
        "client_id": config.client_id, "start_date": window.start,
        "end_date": window.end_exclusive, "timezone": WARSAW_TZ_NAME,
        "batch_size": batch_size, "overwrite_existing": params.overwrite_existing,
    }
    if params.registration:
        registration_filter = (
            f"AND {_registration_sql_expr('ct.' + _qi(CLIENT_TRIPS_REGISTRATION_COLUMN))} = %(registration)s"
        )
        values["registration"] = _normalize_registration(params.registration)
    cur.execute(f"""
      WITH source_groups AS MATERIALIZED (
        SELECT {_registration_sql_expr('log.registration')} AS registration_norm,
               log.assignment_date,
               array_agg(DISTINCT btrim(log.source_id) ORDER BY btrim(log.source_id))
                 FILTER (WHERE NULLIF(btrim(log.source_id), '') IS NOT NULL) AS assignment_ids
        FROM {_qualified_ident(ASSIGNMENT_SCHEMA, ASSIGNMENT_TABLE)} AS log
        WHERE log.assignment_date IS NOT NULL
          AND NULLIF({_registration_sql_expr('log.registration')}, '') IS NOT NULL
        GROUP BY 1, 2
      ),
      candidates AS MATERIALIZED (
        SELECT ct.client_id, ct.provider_trip_id,
               NULLIF(btrim(ct.{_qi(CLIENT_TRIPS_TARGET_COLUMN)}), '') AS current_dysponent_id,
               chosen.assignment_ids[1] AS proposed_dysponent_id
        FROM {_qualified_ident(CLIENT_TRIPS_SCHEMA, CLIENT_TRIPS_TABLE)} AS ct
        JOIN LATERAL (
          SELECT sg.assignment_ids FROM source_groups AS sg
          WHERE sg.registration_norm = {_registration_sql_expr('ct.' + _qi(CLIENT_TRIPS_REGISTRATION_COLUMN))}
            AND sg.assignment_date <= (ct.{_qi(CLIENT_TRIPS_START_COLUMN)} AT TIME ZONE %(timezone)s)::date
          ORDER BY sg.assignment_date DESC LIMIT 1
        ) AS chosen ON cardinality(chosen.assignment_ids) = 1
        WHERE ct.client_id = %(client_id)s
          AND ct.{_qi(CLIENT_TRIPS_START_COLUMN)} >= (%(start_date)s::date::timestamp AT TIME ZONE %(timezone)s)
          AND ct.{_qi(CLIENT_TRIPS_START_COLUMN)} < (%(end_date)s::date::timestamp AT TIME ZONE %(timezone)s)
          AND coalesce(ct.driver_tag_description, '') NOT ILIKE '%%pryw%%'
          AND (NULLIF(btrim(ct.{_qi(CLIENT_TRIPS_TARGET_COLUMN)}), '') IS NULL OR
               (%(overwrite_existing)s AND
                NULLIF(btrim(ct.{_qi(CLIENT_TRIPS_TARGET_COLUMN)}), '') <> chosen.assignment_ids[1]))
          {registration_filter}
        ORDER BY ct.{_qi(CLIENT_TRIPS_START_COLUMN)}, ct.client_id, ct.provider_trip_id
        LIMIT %(batch_size)s FOR UPDATE OF ct SKIP LOCKED
      ),
      updated AS (
        UPDATE {_qualified_ident(CLIENT_TRIPS_SCHEMA, CLIENT_TRIPS_TABLE)} AS ct
           SET {_qi(CLIENT_TRIPS_TARGET_COLUMN)} = candidates.proposed_dysponent_id
        FROM candidates
        WHERE ct.client_id = candidates.client_id AND ct.provider_trip_id = candidates.provider_trip_id
        RETURNING 1
      )
      SELECT count(*)::integer AS updated_count FROM updated
    """, values)
    return int((cur.fetchone() or {}).get("updated_count") or 0)


def _summary(*, config: ClientDbConfig, params: JobParams, source: dict[str, Any],
             window: ResolvedWindow, metrics: dict[str, Any], failures: list[dict[str, str]],
             evaluated_at: datetime, rows_updated: int, batches_committed: int) -> dict[str, Any]:
    loaded_at = source.get("source_loaded_at")
    age_hours = None
    if loaded_at is not None:
        age_hours = round((evaluated_at.astimezone(timezone.utc) -
                           _as_aware(loaded_at).astimezone(timezone.utc)).total_seconds() / 3600, 3)
    return {
        "status": "OK" if not failures else "NOT_READY", "readiness_passed": not failures,
        "readiness_failures": failures, "client_code": config.client_code,
        "client_id": config.client_id, "client_db_name": config.client_db_name,
        "dry_run": params.dry_run, "overwrite_existing": params.overwrite_existing,
        "trigger": params.trigger, "timezone": WARSAW_TZ_NAME,
        "date_semantics": "start inclusive; end exclusive; Warsaw local-midnight boundaries",
        "requested_start_date": window.requested_start.isoformat(),
        "requested_end_date_exclusive": window.requested_end_exclusive.isoformat() if window.requested_end_exclusive else None,
        "resolved_start_date": window.start.isoformat(),
        "resolved_end_date_exclusive": window.end_exclusive.isoformat(),
        "source_boundary_exclusive": window.source_boundary_exclusive.isoformat(),
        "trip_boundary_exclusive": window.trip_boundary_exclusive.isoformat(),
        "end_capped_to_source": window.capped_to_source, "end_capped_to_trips": window.capped_to_trips,
        "enriched_beyond_source_boundary": window.end_exclusive > window.source_boundary_exclusive,
        "evaluated_at": evaluated_at.astimezone(WARSAW_TZ).isoformat(),
        "source_table": _qualified_ident(ASSIGNMENT_SCHEMA, ASSIGNMENT_TABLE),
        "source_raw_file_id": source.get("raw_file_id"),
        "requested_source_raw_file_id": params.source_raw_file_id,
        "source_workflow_run_id": source.get("workflow_run_id"),
        "requested_source_workflow_run_id": params.source_workflow_run_id,
        "source_cleaned_artifact_id": source.get("cleaned_artifact_id"),
        "requested_source_cleaned_artifact_id": params.source_cleaned_artifact_id,
        "source_loaded_at": _iso(source.get("source_loaded_at")), "source_age_hours": age_hours,
        "source_business_date_min": _iso(source.get("source_business_date_min")),
        "source_business_date_max": _iso(source.get("source_business_date_max")),
        "source_rows_inspected": int(source.get("source_rows_inspected") or 0),
        "blank_source_assignments": int(source.get("blank_source_assignments") or 0),
        "invalid_source_rows": int(source.get("invalid_source_rows") or 0),
        "source_rows_missing_provenance": int(source.get("source_rows_missing_provenance") or 0),
        "max_source_age_hours": params.max_source_age_hours,
        "require_fresh_source": params.require_fresh_source,
        "min_coverage_percent": str(params.min_coverage_percent),
        "min_distance_coverage_percent": str(params.min_distance_coverage_percent),
        "max_ambiguous_matches": params.max_ambiguous_matches,
        **metrics, "rows_updated": rows_updated, "total_updated": rows_updated,
        "batches_committed": batches_committed,
        "stopped_reason": "dry_run" if params.dry_run else None,
    }


def _failure(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _decimal_metric(metrics: dict[str, Any], name: str) -> Decimal:
    value = metrics.get(name)
    return Decimal(str(value)) if value is not None else Decimal("0")


def _existing_columns(cur, schema_name: str, table_name: str) -> set[str]:
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s",
                (schema_name, table_name))
    return {str(row["column_name"]) for row in cur.fetchall()}


def _validate_required_schema(source_columns: set[str], trip_columns: set[str]) -> None:
    missing_source = sorted(ASSIGNMENT_REQUIRED_COLUMNS - source_columns)
    if missing_source:
        raise RuntimeError(f"{_qualified_ident(ASSIGNMENT_SCHEMA, ASSIGNMENT_TABLE)} is missing required columns: "
                           + ", ".join(missing_source))
    required_trips = {*CLIENT_TRIPS_PK_COLUMNS, CLIENT_TRIPS_REGISTRATION_COLUMN,
        CLIENT_TRIPS_START_COLUMN, CLIENT_TRIPS_TARGET_COLUMN, "Driver_Restrictions",
        "driver_tag_description", "trip_distance_meters"}
    missing_trips = sorted(required_trips - trip_columns)
    if missing_trips:
        raise RuntimeError(f"{_qualified_ident(CLIENT_TRIPS_SCHEMA, CLIENT_TRIPS_TABLE)} is missing required columns: "
                           + ", ".join(missing_trips))


def _load_client_config(platform_conn, client_code: str) -> ClientDbConfig:
    with platform_conn.cursor() as cur:
        cur.execute("""SELECT client_id,client_code,client_db_host,client_db_port,client_db_name,
            client_db_user,client_db_password_secret_ref,client_db_environment,client_db_identity_id
            FROM workflow_a_control.client_account WHERE enabled IS TRUE AND client_code=%s""",
            (client_code,))
        row = cur.fetchone()
    platform_conn.rollback()
    if not row:
        raise RuntimeError(f"No enabled client database account found for client_code={client_code}")
    required = ["client_id", "client_db_host", "client_db_name", "client_db_user",
                "client_db_password_secret_ref", "client_db_environment", "client_db_identity_id"]
    missing = [name for name in required if not row.get(name)]
    if missing:
        raise RuntimeError(f"Client database account for client_code={client_code} is missing: {', '.join(missing)}")
    return ClientDbConfig(client_code=str(row["client_code"]), client_id=str(row["client_id"]),
        client_db_host=str(row["client_db_host"]), client_db_port=int(row.get("client_db_port") or 5432),
        client_db_name=str(row["client_db_name"]), client_db_user=str(row["client_db_user"]),
        client_db_password_secret_ref=str(row["client_db_password_secret_ref"]),
        client_db_environment=str(row["client_db_environment"]),
        client_db_identity_id=str(row["client_db_identity_id"]))


def _platform_pg_conn():
    import psycopg
    from psycopg.rows import dict_row
    return set_pg_session_timezone(psycopg.connect(host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
        port=int(os.getenv("POSTGRES_PORT", "5432")), dbname=os.getenv("POSTGRES_DB", "logdb"),
        user=os.getenv("POSTGRES_USER", "loguser"), password=os.getenv("POSTGRES_PASSWORD", ""),
        row_factory=dict_row))


def _client_business_pg_conn(config: ClientDbConfig):
    import psycopg
    from psycopg.rows import dict_row
    return set_pg_session_timezone(psycopg.connect(host=config.client_db_host, port=config.client_db_port,
        dbname=config.client_db_name, user=config.client_db_user,
        password=resolve_secret(config.client_db_password_secret_ref), sslmode=config.client_db_sslmode,
        row_factory=dict_row))


def _parse_params(params: dict[str, Any]) -> JobParams:
    if not isinstance(params, dict):
        raise ValueError("params must be a dict")
    allowed = {"client_code", "dry_run", "overwrite_existing", "force", "limit", "batch_size",
        "max_batches", "process_all", "date_from", "date_to", "registration", "trigger",
        "source_raw_file_id", "source_workflow_run_id", "source_cleaned_artifact_id",
        "max_source_age_hours", "min_coverage_percent", "min_distance_coverage_percent",
        "max_ambiguous_matches", "report_suspected_bugs", "require_fresh_source"}
    unknown = sorted(set(params) - allowed)
    if unknown:
        raise ValueError(f"Unsupported Dysponent enrichment parameters: {unknown}")
    if _strict_bool(params.get("force", False), "force"):
        raise ValueError("force=true is no longer supported; use overwrite_existing=true explicitly")
    min_coverage = params.get("min_coverage_percent", DEFAULT_MIN_COVERAGE_PERCENT)
    parsed = JobParams(
        client_code=_optional_client_code(params.get("client_code")) or ALLOWED_CLIENT_CODE,
        dry_run=_strict_bool(params.get("dry_run", True), "dry_run"),
        overwrite_existing=_strict_bool(params.get("overwrite_existing", False), "overwrite_existing"),
        limit=_optional_positive_int(params.get("limit"), "limit"),
        batch_size=_optional_positive_int(params.get("batch_size"), "batch_size") or DEFAULT_BATCH_SIZE,
        max_batches=_optional_positive_int(params.get("max_batches"), "max_batches"),
        process_all=_strict_bool(params.get("process_all", False), "process_all"),
        date_from=_optional_date(params.get("date_from"), "date_from"),
        date_to=_optional_date(params.get("date_to"), "date_to"),
        registration=_optional_str(params.get("registration")), trigger=_optional_str(params.get("trigger")),
        source_raw_file_id=_optional_str(params.get("source_raw_file_id")),
        source_workflow_run_id=_optional_str(params.get("source_workflow_run_id")),
        source_cleaned_artifact_id=_optional_str(params.get("source_cleaned_artifact_id")),
        max_source_age_hours=_optional_positive_int(params.get("max_source_age_hours"), "max_source_age_hours") or DEFAULT_MAX_SOURCE_AGE_HOURS,
        min_coverage_percent=_percentage(min_coverage, "min_coverage_percent"),
        min_distance_coverage_percent=_percentage(params.get("min_distance_coverage_percent", min_coverage), "min_distance_coverage_percent"),
        max_ambiguous_matches=_non_negative_int(params.get("max_ambiguous_matches", DEFAULT_MAX_AMBIGUOUS_MATCHES), "max_ambiguous_matches"),
        report_suspected_bugs=_strict_bool(params.get("report_suspected_bugs", True), "report_suspected_bugs"),
        require_fresh_source=_strict_bool(params.get("require_fresh_source", False), "require_fresh_source"),
    )
    if parsed.date_from and parsed.date_to and parsed.date_from >= parsed.date_to:
        raise EnrichmentPreconditionError(INVALID_DATE_RANGE, "date_from must be before exclusive date_to")
    return parsed


def _params_context(params: JobParams) -> dict[str, Any]:
    return {"client_code": params.client_code, "dry_run": params.dry_run,
        "overwrite_existing": params.overwrite_existing, "limit": params.limit,
        "batch_size": params.batch_size, "max_batches": params.max_batches,
        "process_all": params.process_all, "date_from": params.date_from.isoformat() if params.date_from else None,
        "date_to_exclusive": params.date_to.isoformat() if params.date_to else None,
        "registration": params.registration, "trigger": params.trigger,
        "source_raw_file_id": params.source_raw_file_id,
        "source_workflow_run_id": params.source_workflow_run_id, "timezone": WARSAW_TZ_NAME,
        "report_suspected_bugs": params.report_suspected_bugs,
        "require_fresh_source": params.require_fresh_source}


def _validate_client_code_allowed(client_code: str) -> None:
    if client_code != ALLOWED_CLIENT_CODE:
        raise EnrichmentPreconditionError(UNSUPPORTED_CLIENT,
            f"job is hard-limited to client_code={ALLOWED_CLIENT_CODE}; got {client_code!r}")


def _strict_bool(value: Any, name: str = "value") -> bool:
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return False
    text = str(value).strip().lower()
    if text in {"true", "1"}:
        return True
    if text in {"false", "0"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _bool_param(value: Any) -> bool:
    return _strict_bool(value)


def _optional_client_code(value: Any) -> str | None:
    text = _optional_str(value)
    if text is not None and not SAFE_CLIENT_CODE_RE.fullmatch(text):
        raise ValueError(f"Unsafe client_code value: {text!r}")
    return text


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_positive_int(value: Any, name: str) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive integer")
    parsed = int(value)
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def _non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a non-negative integer")
    parsed = int(value)
    if parsed < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return parsed


def _percentage(value: Any, name: str) -> Decimal:
    parsed = Decimal(str(value))
    if parsed < 0 or parsed > 100:
        raise ValueError(f"{name} must be between 0 and 100")
    return parsed


def _optional_date(value: Any, name: str) -> date | None:
    if value is None or str(value).strip() == "":
        return None
    if isinstance(value, datetime):
        return value.astimezone(WARSAW_TZ).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO date") from exc


def _normalize_registration(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "").strip()).upper()


def _registration_sql_expr(sql_value: str) -> str:
    return f"regexp_replace(upper(coalesce({sql_value}, '')), '[[:space:]]+', '', 'g')"


def _qi(identifier: str) -> str:
    return permissions.quote_ident(identifier)


def _qualified_ident(schema_name: str, table_name: str) -> str:
    return f"{_qi(schema_name)}.{_qi(table_name)}"


def _aware_now(now_fn: Callable[[], datetime] | None = None) -> datetime:
    value = (now_fn or (lambda: datetime.now(timezone.utc)))()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("injectable clock must return a timezone-aware datetime")
    return value


def _as_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise EnrichmentPreconditionError(SOURCE_REPORT_NOT_READY, "source timestamp is timezone-naive")
    return value


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None and hasattr(value, "isoformat") else None


def _json_default(value: Any):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(type(value).__name__)


def _error_code(exc: Exception) -> str:
    if isinstance(exc, EnrichmentPreconditionError):
        return exc.code
    if isinstance(exc, EnvironmentIdentityError):
        return ENVIRONMENT_IDENTITY_NOT_VERIFIED
    if isinstance(exc, ClientProcessingError):
        return _error_code(exc.original)
    return type(exc).__name__


def _rollback_quietly(conn) -> None:
    try:
        conn.rollback()
    except Exception:
        pass


def _log(client, run_id: str, level: str, message: str, *,
         context: dict[str, Any] | None = None, error: str | None = None) -> None:
    if not client or not hasattr(client, "log"):
        return
    kwargs: dict[str, Any] = {"run_id": run_id, "context": context or {}}
    if error:
        kwargs["error"] = error
    client.log(level, "SCRIPT", SOURCE, message, **kwargs)
