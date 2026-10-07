#!/usr/bin/env python3
"""Manual recovery for Report 207 speed violation data.

The tool is dry-run-first and exists for the historical Report 207 identity bug
fixed by ae53716d. It rebuilds Report 207 rows with the fixed Stage 2 identity
logic, then recalculates Report-207-derived speeding counters in client_trips.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api.client import LogPlatformClient
from jobs.common.environment_identity import (
    ClientIdentityExpectation,
    EnvironmentIdentityError,
    attest_client_identity,
    attest_platform_identity,
    load_runtime_identity,
    require_clean_production_worktree,
    require_production_write_confirmation,
)
from jobs.reports.postprocess import job_report_207_speeding_migration as speed_migration
from jobs.reports.stage2 import job_stage2
from jobs.reports.stage3 import job_stage3
from jobs.trip_metrics_population_source import TRIP_METRICS_SOURCE_REPORT_207

REQUIRED_FIX_COMMIT = "ae53716d89e2fdd9d57352a3c8217c30c5ab4ea6"
REPORT_TYPE = "report_207"
REPORT_SCHEMA = "telematics_reports"
REPORT_TABLE = "report_207"
CLIENT_TRIPS_SCHEMA = "public"
CLIENT_TRIPS_TABLE = "client_trips"
REPORT_EVENT_TIMEZONE = "Europe/Warsaw"
OPERATION_NAME = "report_207_speed_violation_recovery"
REPORT_REQUIRED_COLUMNS = (
    "record_id",
    "Data i czas",
    "Nr rejestracyjny",
    "Prędkość",
    "_raw_file_id",
    "_source_artifact_id",
)
REPORT_TRACKING_COLUMNS = (
    "migrated_to_client_db",
    "migrated_to_client_db_at",
    "migrated_to_client_trip_id",
    "migrated_to_client_db_error",
)
CLIENT_TRIPS_REQUIRED_COLUMNS = (
    "client_id",
    "client_code",
    "provider_trip_id",
    "registration",
    "start_timestamp",
    "end_timestamp",
    "record_id",
    "speeding_140_160_count",
    "speeding_160_170_count",
    "speeding_170_plus_count",
)
UUIDISH_RE = re.compile(r"^[0-9a-fA-F-]{8,}$")


@dataclass(frozen=True)
class RecoveryScope:
    client_code: str
    date_from: date
    date_to: date
    raw_file_ids: tuple[str, ...] = ()
    source_artifact_ids: tuple[str, ...] = ()

    @property
    def backup_confirmation_token(self) -> str:
        return f"BACKUP_CONFIRMED:{self.client_code}:{self.date_from}:{self.date_to}"

    @property
    def counter_recalc_confirmation_token(self) -> str:
        return f"CLIENT_TRIPS_DATE_RECALC:{self.client_code}:{self.date_from}:{self.date_to}"


@dataclass(frozen=True)
class LocalCleanedCsv:
    raw_file_id: str
    path: Path
    source_artifact_id: str
    source_filename: str | None


@dataclass(frozen=True)
class PlannedSql:
    name: str
    sql: str
    params: tuple[Any, ...]
    writes: bool


class RecoverySafetyError(RuntimeError):
    pass


class NullClient:
    def log(self, *args, **kwargs):
        return None


def _load_dotenv_if_present() -> None:
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv
    except Exception:
        return
    load_dotenv(env_path, override=False)


def _qi(identifier: str) -> str:
    return speed_migration._qi(identifier)


def _qname(schema: str, table: str) -> str:
    return f"{_qi(schema)}.{_qi(table)}"


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD date, got {value!r}") from exc


def _dedupe(values: Iterable[str]) -> tuple[str, ...]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return tuple(out)


def _parse_local_cleaned_csv_specs(values: Iterable[str]) -> list[LocalCleanedCsv]:
    parsed: list[LocalCleanedCsv] = []
    for value in values:
        raw = str(value or "").strip()
        if "=" not in raw:
            raise RecoverySafetyError(
                "--cleaned-csv must use RAW_FILE_ID=/path/to/cleaned.csv so reload lineage is explicit"
            )
        raw_file_id, path_text = raw.split("=", 1)
        raw_file_id = raw_file_id.strip()
        path = Path(path_text.strip()).expanduser()
        if not raw_file_id:
            raise RecoverySafetyError("--cleaned-csv RAW_FILE_ID cannot be empty")
        if not path.exists() or not path.is_file():
            raise RecoverySafetyError(f"cleaned CSV does not exist: {path}")
        parsed.append(
            LocalCleanedCsv(
                raw_file_id=raw_file_id,
                path=path,
                source_artifact_id=f"manual-recovery:{raw_file_id[:12]}",
                source_filename=path.name,
            )
        )
    return parsed


def build_scope(args: argparse.Namespace) -> RecoveryScope:
    date_from = args.date_from
    date_to = args.date_to
    if date_to < date_from:
        raise RecoverySafetyError("--date-to must be on or after --date-from")
    client_code = str(args.client_code or "").strip().upper()
    if not re.match(r"^[A-Z0-9_]{3,32}$", client_code):
        raise RecoverySafetyError(f"unsafe client_code: {args.client_code!r}")
    return RecoveryScope(
        client_code=client_code,
        date_from=date_from,
        date_to=date_to,
        raw_file_ids=_dedupe(args.raw_file_id or ()),
        source_artifact_ids=_dedupe(args.source_artifact_id or ()),
    )


def validate_execute_guards(
    *,
    scope: RecoveryScope,
    execute: bool,
    require_env_name: str | None,
    backup_confirmation_token: str | None,
    counter_recalc_confirmation_token: str | None,
    cleaned_csvs: list[LocalCleanedCsv],
) -> None:
    if not execute:
        return
    if not require_env_name:
        raise RecoverySafetyError("execute mode requires --require-env-name")
    if backup_confirmation_token != scope.backup_confirmation_token:
        raise RecoverySafetyError(
            "execute mode requires exact --backup-confirmation-token "
            f"{scope.backup_confirmation_token!r}"
        )
    if counter_recalc_confirmation_token != scope.counter_recalc_confirmation_token:
        raise RecoverySafetyError(
            "client_trips does not store raw_file_id/source_artifact_id lineage for aggregate "
            "speeding counters; execute mode requires exact "
            f"--counter-recalc-confirmation-token {scope.counter_recalc_confirmation_token!r}"
        )
    if not scope.raw_file_ids and not scope.source_artifact_ids and not cleaned_csvs:
        raise RecoverySafetyError(
            "execute mode requires at least one --raw-file-id, --source-artifact-id, "
            "or --cleaned-csv RAW_FILE_ID=path scope"
        )


def verify_required_commit(repo_root: Path = REPO_ROOT) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", REQUIRED_FIX_COMMIT, "HEAD"],
        cwd=repo_root,
        text=True,
        capture_output=True,
        timeout=15,
    )
    return result.returncode == 0


def _report_event_ts_expr(alias: str) -> str:
    ts = f"{alias}.{_qi('Data i czas')}"
    raw = f"btrim(COALESCE({ts}, ''))"
    tz = REPORT_EVENT_TIMEZONE.replace("'", "''")
    return f"""
        CASE
            WHEN {raw} ~ '^[+-]?[0-9]+([.][0-9]+)?$'
                 AND ({raw})::numeric >= {speed_migration.EXCEL_SERIAL_MIN}
                 AND ({raw})::numeric < {speed_migration.EXCEL_SERIAL_MAX_EXCLUSIVE}
                THEN (
                    timestamp '{speed_migration.EXCEL_SERIAL_DATE_BASE}'
                    + round(({raw})::numeric * 86400) * interval '1 second'
                ) AT TIME ZONE '{tz}'
            WHEN {raw} ~ '^[0-9]{{4}}-[0-9]{{1,2}}-[0-9]{{1,2}}[ T][0-9]{{1,2}}:[0-9]{{2}}(:[0-9]{{2}})?$'
                THEN replace({raw}, 'T', ' ')::timestamp AT TIME ZONE '{tz}'
            WHEN {raw} ~ '^[0-9]{{1,2}}[.][0-9]{{1,2}}[.][0-9]{{4}}[[:space:]]+[0-9]{{1,2}}:[0-9]{{2}}(:[0-9]{{2}})?$'
                THEN to_timestamp(
                    CASE WHEN {raw} ~ ':[0-9]{{2}}:[0-9]{{2}}$' THEN {raw} ELSE {raw} || ':00' END,
                    'DD.MM.YYYY HH24:MI:SS'
                )::timestamp AT TIME ZONE '{tz}'
            WHEN {raw} ~ '^[0-9]{{1,2}}/[0-9]{{1,2}}/[0-9]{{4}}[[:space:]]+[0-9]{{1,2}}:[0-9]{{2}}(:[0-9]{{2}})?$'
                THEN to_timestamp(
                    CASE WHEN {raw} ~ ':[0-9]{{2}}:[0-9]{{2}}$' THEN {raw} ELSE {raw} || ':00' END,
                    'DD/MM/YYYY HH24:MI:SS'
                )::timestamp AT TIME ZONE '{tz}'
            ELSE NULL
        END
    """


def _speed_value_expr(alias: str) -> str:
    speed_raw = f"btrim(COALESCE({alias}.{_qi('Prędkość')}, ''))"
    speed_clean = f"regexp_replace(replace({speed_raw}, ',', '.'), '[[:space:]]+', '', 'g')"
    return f"CASE WHEN {speed_clean} ~ '^[+-]?[0-9]+([.][0-9]+)?$' THEN ({speed_clean})::numeric ELSE NULL END"


def report_scope_where(scope: RecoveryScope, alias: str = "r") -> tuple[str, tuple[Any, ...]]:
    event_ts = _report_event_ts_expr(alias)
    clauses = [
        f"(({event_ts}) AT TIME ZONE %s)::date >= %s",
        f"(({event_ts}) AT TIME ZONE %s)::date <= %s",
    ]
    params: list[Any] = [REPORT_EVENT_TIMEZONE, scope.date_from, REPORT_EVENT_TIMEZONE, scope.date_to]
    if scope.raw_file_ids:
        clauses.append(f"{alias}.{_qi('_raw_file_id')} = ANY(%s::text[])")
        params.append(list(scope.raw_file_ids))
    if scope.source_artifact_ids:
        clauses.append(f"{alias}.{_qi('_source_artifact_id')} = ANY(%s::text[])")
        params.append(list(scope.source_artifact_ids))
    return " AND ".join(clauses), tuple(params)


def report_date_where(scope: RecoveryScope, alias: str = "r") -> tuple[str, tuple[Any, ...]]:
    event_ts = _report_event_ts_expr(alias)
    return (
        f"(({event_ts}) AT TIME ZONE %s)::date >= %s AND (({event_ts}) AT TIME ZONE %s)::date <= %s",
        (REPORT_EVENT_TIMEZONE, scope.date_from, REPORT_EVENT_TIMEZONE, scope.date_to),
    )


def client_trips_date_where(scope: RecoveryScope, alias: str = "t") -> tuple[str, tuple[Any, ...]]:
    return (
        f"{alias}.{_qi('client_code')} = %s "
        f"AND ({alias}.{_qi('start_timestamp')} AT TIME ZONE %s)::date <= %s "
        f"AND ({alias}.{_qi('end_timestamp')} AT TIME ZONE %s)::date >= %s",
        (scope.client_code, REPORT_EVENT_TIMEZONE, scope.date_to, REPORT_EVENT_TIMEZONE, scope.date_from),
    )


def build_report_counts_sql(scope: RecoveryScope) -> PlannedSql:
    where, params = report_scope_where(scope, "r")
    report = _qname(REPORT_SCHEMA, REPORT_TABLE)
    sql = f"""
WITH scoped AS (
    SELECT NULLIF(btrim(COALESCE(r.{_qi('record_id')}, '')), '') AS record_id
    FROM {report} AS r
    WHERE {where}
), duplicate_groups AS (
    SELECT record_id, COUNT(*) AS row_count
    FROM scoped
    WHERE record_id IS NOT NULL
    GROUP BY record_id
    HAVING COUNT(*) > 1
)
SELECT
    (SELECT COUNT(*) FROM scoped)::integer AS total_rows,
    (SELECT COUNT(DISTINCT record_id) FROM scoped WHERE record_id IS NOT NULL)::integer AS distinct_record_ids,
    COALESCE((SELECT SUM(row_count - 1) FROM duplicate_groups), 0)::integer AS duplicate_record_id_rows,
    (SELECT COUNT(*) FROM duplicate_groups)::integer AS duplicate_record_id_groups,
    COALESCE((SELECT MAX(row_count) FROM duplicate_groups), 0)::integer AS max_duplicate_group_size
"""
    return PlannedSql("report_207_counts", sql, params, writes=False)


def build_report_delete_sql(scope: RecoveryScope) -> PlannedSql:
    where, params = report_scope_where(scope, "r")
    report = _qname(REPORT_SCHEMA, REPORT_TABLE)
    sql = f"DELETE FROM {report} AS r WHERE {where}"
    return PlannedSql("delete_scoped_report_207_rows", sql, params, writes=True)


def build_client_trips_counts_sql(scope: RecoveryScope) -> PlannedSql:
    where, params = client_trips_date_where(scope, "t")
    trips = _qname(CLIENT_TRIPS_SCHEMA, CLIENT_TRIPS_TABLE)
    cols = ["speeding_140_160_count", "speeding_160_170_count", "speeding_170_plus_count"]
    sums = ",\n    ".join(f"COALESCE(SUM(t.{_qi(col)}), 0)::integer AS {col}" for col in cols)
    sql = f"""
SELECT
    COUNT(*)::integer AS trip_rows_in_date_scope,
    {sums},
    COUNT(*) FILTER (
        WHERE COALESCE(t.{_qi('speeding_140_160_count')}, 0) > 0
           OR COALESCE(t.{_qi('speeding_160_170_count')}, 0) > 0
           OR COALESCE(t.{_qi('speeding_170_plus_count')}, 0) > 0
    )::integer AS trips_with_speeding_counts
FROM {trips} AS t
WHERE {where}
"""
    return PlannedSql("client_trips_speeding_counts", sql, params, writes=False)


def build_clear_client_trips_sql(scope: RecoveryScope) -> PlannedSql:
    where, params = client_trips_date_where(scope, "t")
    trips = _qname(CLIENT_TRIPS_SCHEMA, CLIENT_TRIPS_TABLE)
    sql = f"""
UPDATE {trips} AS t
   SET {_qi('speeding_140_160_count')} = 0,
       {_qi('speeding_160_170_count')} = 0,
       {_qi('speeding_170_plus_count')} = 0
WHERE {where}
"""
    return PlannedSql("clear_client_trips_report_207_speeding_counts", sql, params, writes=True)


def build_reset_report_migration_flags_sql(scope: RecoveryScope) -> PlannedSql:
    where, params = report_date_where(scope, "r")
    report = _qname(REPORT_SCHEMA, REPORT_TABLE)
    sql = f"""
UPDATE {report} AS r
   SET {_qi('migrated_to_client_db')} = FALSE,
       {_qi('migrated_to_client_db_at')} = NULL,
       {_qi('migrated_to_client_trip_id')} = NULL,
       {_qi('migrated_to_client_db_error')} = NULL
WHERE {where}
"""
    return PlannedSql("reset_report_207_migration_flags_in_date_scope", sql, params, writes=True)


def build_repopulate_client_trips_sql(scope: RecoveryScope) -> PlannedSql:
    report = _qname(REPORT_SCHEMA, REPORT_TABLE)
    trips = _qname(CLIENT_TRIPS_SCHEMA, CLIENT_TRIPS_TABLE)
    report_where, report_params = report_date_where(scope, "r")
    trip_where, trip_params = client_trips_date_where(scope, "t")
    event_ts = _report_event_ts_expr("r")
    speed_value = _speed_value_expr("r")
    sql = f"""
WITH scoped_report AS (
    SELECT
        r.ctid AS report_ctid,
        btrim(COALESCE(r.{_qi('Nr rejestracyjny')}, '')) AS registration,
        btrim(COALESCE(r.{_qi('Data i czas')}, '')) AS event_ts_raw,
        btrim(COALESCE(r.{_qi('Prędkość')}, '')) AS speed_raw,
        {event_ts} AS event_ts,
        {speed_value} AS speed_value
    FROM {report} AS r
    WHERE {report_where}
), classified AS (
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
    FROM scoped_report
), valid_speeding AS (
    SELECT *
    FROM classified
    WHERE invalid_error IS NULL
      AND bucket IS NOT NULL
      AND event_ts IS NOT NULL
      AND registration <> ''
), trip_matches AS (
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
    WHERE {trip_where}
), match_totals AS (
    SELECT report_ctid, COUNT(*) AS match_count
    FROM trip_matches
    GROUP BY report_ctid
), exact_matches AS (
    SELECT tm.*
    FROM trip_matches AS tm
    JOIN match_totals AS mt ON mt.report_ctid = tm.report_ctid
    WHERE mt.match_count = 1
), invalid_rows AS (
    SELECT report_ctid, invalid_error AS error_code
    FROM classified
    WHERE invalid_error IS NOT NULL
), unmatched_rows AS (
    SELECT v.report_ctid, '{speed_migration.RETRYABLE_ERROR_CODE_NO_MATCHING_TRIP}' AS error_code
    FROM valid_speeding AS v
    LEFT JOIN match_totals AS mt ON mt.report_ctid = v.report_ctid
    WHERE mt.report_ctid IS NULL
), ambiguous_rows AS (
    SELECT mt.report_ctid, 'AMBIGUOUS_TRIP_MATCH' AS error_code
    FROM match_totals AS mt
    WHERE mt.match_count > 1
), error_rows AS (
    SELECT report_ctid, error_code FROM invalid_rows
    UNION ALL SELECT report_ctid, error_code FROM unmatched_rows
    UNION ALL SELECT report_ctid, error_code FROM ambiguous_rows
), trip_counts AS (
    SELECT
        client_id,
        provider_trip_id,
        COUNT(*) FILTER (WHERE bucket = 'speeding_140_160_count')::integer AS count_140_160,
        COUNT(*) FILTER (WHERE bucket = 'speeding_160_170_count')::integer AS count_160_170,
        COUNT(*) FILTER (WHERE bucket = 'speeding_170_plus_count')::integer AS count_170_plus
    FROM exact_matches
    GROUP BY client_id, provider_trip_id
), updated_trips AS (
    UPDATE {trips} AS t
       SET {_qi('speeding_140_160_count')} = tc.count_140_160,
           {_qi('speeding_160_170_count')} = tc.count_160_170,
           {_qi('speeding_170_plus_count')} = tc.count_170_plus
    FROM trip_counts AS tc
    WHERE t.client_id = tc.client_id
      AND t.provider_trip_id = tc.provider_trip_id
    RETURNING 1
), marked_migrated AS (
    UPDATE {report} AS r
       SET {_qi('migrated_to_client_db')} = TRUE,
           {_qi('migrated_to_client_db_at')} = now(),
           {_qi('migrated_to_client_trip_id')} = em.trip_id_text,
           {_qi('migrated_to_client_db_error')} = NULL
    FROM exact_matches AS em
    WHERE r.ctid = em.report_ctid
    RETURNING 1
), marked_errors AS (
    UPDATE {report} AS r
       SET {_qi('migrated_to_client_db')} = FALSE,
           {_qi('migrated_to_client_db_at')} = NULL,
           {_qi('migrated_to_client_trip_id')} = NULL,
           {_qi('migrated_to_client_db_error')} = er.error_code
    FROM error_rows AS er
    WHERE r.ctid = er.report_ctid
    RETURNING 1
)
SELECT
    (SELECT COUNT(*) FROM scoped_report)::integer AS report_rows_in_date_scope,
    (SELECT COUNT(*) FROM valid_speeding)::integer AS valid_speed_rows,
    (SELECT COUNT(*) FROM exact_matches)::integer AS matched_rows,
    (SELECT COUNT(*) FROM unmatched_rows)::integer AS unmatched_rows,
    (SELECT COUNT(*) FROM ambiguous_rows)::integer AS ambiguous_rows,
    (SELECT COUNT(*) FROM invalid_rows)::integer AS invalid_rows,
    (SELECT COUNT(*) FROM classified WHERE invalid_error IS NULL AND bucket IS NULL)::integer AS sub_threshold_rows,
    (SELECT COUNT(*) FROM updated_trips)::integer AS updated_trip_groups,
    (SELECT COUNT(*) FROM marked_migrated)::integer AS marked_migrated_rows,
    (SELECT COUNT(*) FROM marked_errors)::integer AS marked_error_rows,
    COALESCE((SELECT SUM(count_140_160) FROM trip_counts), 0)::integer AS repopulated_140_160,
    COALESCE((SELECT SUM(count_160_170) FROM trip_counts), 0)::integer AS repopulated_160_170,
    COALESCE((SELECT SUM(count_170_plus) FROM trip_counts), 0)::integer AS repopulated_170_plus
"""
    return PlannedSql(
        "repopulate_client_trips_report_207_speeding_counts",
        sql,
        tuple(report_params + trip_params),
        writes=True,
    )


def execute_sql(cur, step: PlannedSql) -> dict[str, Any]:
    cur.execute(step.sql, step.params)
    if cur.description:
        row = cur.fetchone() or {}
        return dict(row)
    return {"rowcount": cur.rowcount}


def _table_columns(cur, schema: str, table: str) -> set[str]:
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        """,
        (schema, table),
    )
    return {str(row["column_name"]) for row in cur.fetchall()}


def _table_exists(cur, schema: str, table: str) -> bool:
    cur.execute(
        """
        SELECT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = %s AND table_name = %s
        ) AS exists
        """,
        (schema, table),
    )
    row = cur.fetchone() or {}
    return bool(row.get("exists"))


def require_client_schema(cur) -> None:
    if not _table_exists(cur, REPORT_SCHEMA, REPORT_TABLE):
        raise RecoverySafetyError(f"{REPORT_SCHEMA}.{REPORT_TABLE} does not exist")
    if not _table_exists(cur, CLIENT_TRIPS_SCHEMA, CLIENT_TRIPS_TABLE):
        raise RecoverySafetyError(f"{CLIENT_TRIPS_SCHEMA}.{CLIENT_TRIPS_TABLE} does not exist")
    report_columns = _table_columns(cur, REPORT_SCHEMA, REPORT_TABLE)
    trip_columns = _table_columns(cur, CLIENT_TRIPS_SCHEMA, CLIENT_TRIPS_TABLE)
    missing_report = sorted(set(REPORT_REQUIRED_COLUMNS + REPORT_TRACKING_COLUMNS) - report_columns)
    missing_trips = sorted(set(CLIENT_TRIPS_REQUIRED_COLUMNS) - trip_columns)
    if missing_report:
        raise RecoverySafetyError(
            f"{REPORT_SCHEMA}.{REPORT_TABLE} is missing required recovery columns: {', '.join(missing_report)}"
        )
    if missing_trips:
        raise RecoverySafetyError(
            f"{CLIENT_TRIPS_SCHEMA}.{CLIENT_TRIPS_TABLE} is missing required recovery columns: {', '.join(missing_trips)}"
        )


def load_client_config(platform_conn, client_code: str) -> job_stage3.ClientDbConfig:
    with platform_conn.cursor() as cur:
        config = job_stage3._load_client_account_by_code(cur, client_code)
        cur.execute(
            """
            SELECT trip_metrics_population_source
            FROM workflow_a_control.client_account
            WHERE enabled IS TRUE AND client_code = %s
            """,
            (client_code,),
        )
        row = cur.fetchone() or {}
    source = str(row.get("trip_metrics_population_source") or "").strip()
    if source != TRIP_METRICS_SOURCE_REPORT_207:
        raise RecoverySafetyError(
            f"client {client_code} has trip_metrics_population_source={source!r}; "
            f"expected {TRIP_METRICS_SOURCE_REPORT_207!r} before Report 207 counter recovery"
        )
    return config


def _artifact_rows_for_scope(platform_conn, scope: RecoveryScope) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with platform_conn.cursor() as cur:
        for raw_file_id in scope.raw_file_ids:
            artifact = job_stage3._find_stage2_cleaned_artifact(
                cur,
                raw_file_id=raw_file_id,
                report_type=REPORT_TYPE,
            )
            rows.append(
                {
                    "raw_file_id": raw_file_id,
                    "artifact_id": artifact.artifact_id,
                    "filename": artifact.filename,
                    "original_filename": artifact.original_filename,
                    "display_filename": artifact.display_filename,
                }
            )
        if scope.source_artifact_ids:
            cur.execute(
                """
                SELECT
                    artifact_id::text AS artifact_id,
                    raw_file_id::text AS raw_file_id,
                    filename,
                    original_filename,
                    display_filename
                FROM artifacts
                WHERE artifact_id::text = ANY(%s::text[])
                  AND workflow_name = %s
                  AND stage_name = %s
                  AND artifact_role = 'cleaned'
                  AND report_type = ANY(%s::text[])
                ORDER BY created_at DESC, artifact_id DESC
                """,
                (
                    list(scope.source_artifact_ids),
                    job_stage3.WORKFLOW_NAME,
                    job_stage3.STAGE2_CLEAN_STAGE,
                    job_stage3._artifact_report_type_candidates(REPORT_TYPE),
                ),
            )
            for row in cur.fetchall():
                rows.append(dict(row))
    deduped: dict[str, dict[str, Any]] = {}
    for row in rows:
        artifact_id = str(row.get("artifact_id") or "")
        if artifact_id:
            deduped[artifact_id] = row
    return list(deduped.values())


def _download_artifact_to_temp(client: LogPlatformClient, artifact_row: dict[str, Any], tmpdir: Path) -> LocalCleanedCsv:
    artifact_id = str(artifact_row["artifact_id"])
    raw_file_id = str(artifact_row["raw_file_id"])
    filename = (
        artifact_row.get("display_filename")
        or artifact_row.get("original_filename")
        or artifact_row.get("filename")
        or f"{artifact_id}.csv"
    )
    dest = tmpdir / str(filename)
    client.download_artifact(artifact_id, str(dest))
    job_stage3._require_nonempty_artifact_file(dest, artifact_id)
    return LocalCleanedCsv(
        raw_file_id=raw_file_id,
        path=dest,
        source_artifact_id=artifact_id,
        source_filename=str(filename),
    )


def _read_and_regenerate_report_207(cleaned_csv: LocalCleanedCsv):
    df = job_stage3._read_cleaned_csv(cleaned_csv.path)
    return job_stage2._add_record_id_column(
        df,
        record_id_ingredients="Data i czas,Nr rejestracyjny",
        client=NullClient(),
        run_id="manual-report-207-recovery",
        context={"report_type": REPORT_TYPE, "raw_file_id": cleaned_csv.raw_file_id},
    )


def _expected_rows_from_cleaned_csvs(cleaned_csvs: list[LocalCleanedCsv]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for item in cleaned_csvs:
        df = _read_and_regenerate_report_207(item)
        record_ids = df["record_id"].astype(str).str.strip()
        summaries.append(
            {
                "raw_file_id": item.raw_file_id,
                "source_artifact_id": item.source_artifact_id,
                "path": str(item.path),
                "rows": int(len(df)),
                "distinct_record_ids": int(record_ids[record_ids != ""].nunique()),
                "duplicate_record_id_rows": int(len(df) - record_ids[record_ids != ""].nunique()),
            }
        )
    return summaries


def _attest_environment(platform_conn, client_conn, config: job_stage3.ClientDbConfig, require_env_name: str | None, production_write_confirmation: str | None, execute: bool) -> dict[str, Any]:
    runtime = load_runtime_identity()
    if require_env_name and runtime.environment != require_env_name:
        raise EnvironmentIdentityError(
            "RUNTIME_ENVIRONMENT_MISMATCH",
            f"runtime environment {runtime.environment!r} does not match required {require_env_name!r}",
        )
    platform = attest_platform_identity(platform_conn, runtime)
    client = attest_client_identity(
        client_conn,
        runtime,
        ClientIdentityExpectation(
            client_code=config.client_code,
            environment=config.client_db_environment,
            database_identity_id=config.client_db_identity_id,
            database_name=config.client_db_name,
            database_user=config.client_db_user,
        ),
    )
    if execute:
        require_production_write_confirmation(
            runtime,
            client_code=config.client_code,
            operation_name=OPERATION_NAME,
            provided=production_write_confirmation,
            dry_run=False,
        )
        require_clean_production_worktree(runtime, repo_root=REPO_ROOT)
    return {"runtime": runtime.context(), "platform": platform.context(), "client": client.context()}


def run_recovery(args: argparse.Namespace) -> dict[str, Any]:
    scope = build_scope(args)
    local_cleaned_csvs = _parse_local_cleaned_csv_specs(args.cleaned_csv or [])
    execute = bool(args.execute)
    validate_execute_guards(
        scope=scope,
        execute=execute,
        require_env_name=args.require_env_name,
        backup_confirmation_token=args.backup_confirmation_token,
        counter_recalc_confirmation_token=args.counter_recalc_confirmation_token,
        cleaned_csvs=local_cleaned_csvs,
    )

    fixed_commit_present = verify_required_commit()
    if not fixed_commit_present:
        raise RecoverySafetyError(f"required fix commit {REQUIRED_FIX_COMMIT} is not an ancestor of HEAD")

    result: dict[str, Any] = {
        "status": "DRY_RUN" if not execute else "EXECUTED",
        "execute": execute,
        "scope": {
            "client_code": scope.client_code,
            "report_type": REPORT_TYPE,
            "date_from": scope.date_from.isoformat(),
            "date_to": scope.date_to.isoformat(),
            "raw_file_ids": list(scope.raw_file_ids),
            "source_artifact_ids": list(scope.source_artifact_ids),
        },
        "required_fix_commit": REQUIRED_FIX_COMMIT,
        "fixed_commit_present": fixed_commit_present,
        "client_trips_scope_note": (
            "client_trips stores aggregate Report-207-derived speeding counters without "
            "raw_file_id/source_artifact_id lineage; cleanup is date-scope for this client."
        ),
        "writes_performed": False,
    }

    platform_conn = job_stage3._platform_pg_conn()
    try:
        config = load_client_config(platform_conn, scope.client_code)
        result["destination"] = {
            "client_db_name": config.client_db_name,
            "report_table": f"{REPORT_SCHEMA}.{REPORT_TABLE}",
            "client_trips_table": f"{CLIENT_TRIPS_SCHEMA}.{CLIENT_TRIPS_TABLE}",
        }
        artifact_rows = _artifact_rows_for_scope(platform_conn, scope)
        result["stage2_cleaned_artifacts"] = artifact_rows
        with job_stage3._client_business_pg_conn(config) as client_conn:
            identity = _attest_environment(
                platform_conn,
                client_conn,
                config,
                args.require_env_name,
                args.production_write_confirmation,
                execute,
            )
            result["environment_identity"] = identity
            with client_conn.cursor() as cur:
                require_client_schema(cur)
                for step in [build_report_counts_sql(scope), build_client_trips_counts_sql(scope)]:
                    result[step.name] = execute_sql(cur, step)
                client_conn.rollback()

            downloaded: list[LocalCleanedCsv] = list(local_cleaned_csvs)
            with tempfile.TemporaryDirectory(prefix="report-207-recovery-") as tmp:
                if artifact_rows:
                    api_client = LogPlatformClient.from_env()
                    tmpdir = Path(tmp)
                    for row in artifact_rows:
                        downloaded.append(_download_artifact_to_temp(api_client, row, tmpdir))
                result["expected_corrected_cleaned_rows"] = _expected_rows_from_cleaned_csvs(downloaded)

                if not execute:
                    result["planned_write_steps"] = [
                        build_report_delete_sql(scope).name,
                        "reload_corrected_report_207_rows_from_cleaned_artifacts",
                        build_clear_client_trips_sql(scope).name,
                        build_reset_report_migration_flags_sql(scope).name,
                        build_repopulate_client_trips_sql(scope).name,
                    ]
                    result["backup_confirmation_token_required_for_execute"] = scope.backup_confirmation_token
                    result["counter_recalc_confirmation_token_required_for_execute"] = scope.counter_recalc_confirmation_token
                    return result

                if not downloaded:
                    raise RecoverySafetyError("execute mode found no cleaned CSV artifacts to reload")

                with client_conn.cursor() as cur:
                    delete_result = execute_sql(cur, build_report_delete_sql(scope))
                result["delete_scoped_report_207_rows"] = delete_result

                load_results: list[dict[str, Any]] = []
                for item in downloaded:
                    df = _read_and_regenerate_report_207(item)
                    load = job_stage3._load_dataframe_to_destination(
                        client_conn,
                        raw_file_id=item.raw_file_id,
                        run_id=args.run_id,
                        client_code=scope.client_code,
                        report_type=REPORT_TYPE,
                        source_artifact_id=item.source_artifact_id,
                        source_filename=item.source_filename,
                        data_overwrite=False,
                        df=df,
                        cleaned_artifact_id=item.source_artifact_id,
                    )
                    load_results.append(job_stage3._result_context(load))
                result["reload_report_207"] = load_results

                with client_conn.cursor() as cur:
                    clear_result = execute_sql(cur, build_clear_client_trips_sql(scope))
                    reset_result = execute_sql(cur, build_reset_report_migration_flags_sql(scope))
                    repopulate_result = execute_sql(cur, build_repopulate_client_trips_sql(scope))
                    final_report = execute_sql(cur, build_report_counts_sql(scope))
                    final_trips = execute_sql(cur, build_client_trips_counts_sql(scope))
                client_conn.commit()
                result["clear_client_trips_report_207_speeding_counts"] = clear_result
                result["reset_report_207_migration_flags_in_date_scope"] = reset_result
                result["repopulate_client_trips_report_207_speeding_counts"] = repopulate_result
                result["final_report_207_counts"] = final_report
                result["final_client_trips_speeding_counts"] = final_trips
                result["writes_performed"] = True
                return result
    finally:
        platform_conn.close()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Recover Report 207 rows and Report-207-derived client_trips speeding counters."
    )
    parser.add_argument("--client-code", required=True)
    parser.add_argument("--date-from", required=True, type=_parse_date)
    parser.add_argument("--date-to", required=True, type=_parse_date)
    parser.add_argument("--raw-file-id", action="append", default=[])
    parser.add_argument("--source-artifact-id", action="append", default=[])
    parser.add_argument(
        "--cleaned-csv",
        action="append",
        default=[],
        help="Optional local cleaned CSV as RAW_FILE_ID=/path/file.csv. Used when platform artifact download is not available.",
    )
    parser.add_argument("--execute", action="store_true", help="Perform writes. Omit for dry-run.")
    parser.add_argument("--require-env-name", help="Required LOG_PLATFORM_TARGET_ENVIRONMENT value for identity guard.")
    parser.add_argument("--backup-confirmation-token")
    parser.add_argument("--counter-recalc-confirmation-token")
    parser.add_argument(
        "--production-write-confirmation",
        help="Exact production confirmation required by jobs.common.environment_identity in production execute mode.",
    )
    parser.add_argument(
        "--run-id",
        default="manual-report-207-recovery-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    _load_dotenv_if_present()
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        result = run_recovery(args)
    except (RecoverySafetyError, EnvironmentIdentityError) as exc:
        print(json.dumps({"status": "BLOCKED", "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
