#!/usr/bin/env python3
"""Read-only Telematics `trips_sync` coverage bootstrap inventory (delivery C10).

Specification of record:
  docs/13_telematics_trips_stabilization_windows.md §13.2 (Gate 1 inventory),
    §13.4 (what "operationally complete" means), §13.5 (evidence bundle)
  docs/14_telematics_trips_compatibility_implementation_plan.md §3/C10, §8/S13

This tool is **permanently read-only**. It has no execute switch, no write
switch and no code path that issues `INSERT`, `UPDATE`, `DELETE` or DDL. It
opens the platform connection read-only, launches no job, starts no subprocess
and issues no provider request.

It is equally deliberate about what it does *not* decide. The tool **reports
facts and never recommends** — it does not choose `A` (`coverage_start_ts`), it
does not choose `W` (`covered_through_ts`), and it never emits a "READY"
verdict. `docs/13_…` §13.3 places that choice with a human who must write down
why the selected instant is the point from which the platform is willing to
claim operational continuity; a tool that suggested one would recreate exactly
the over-optimistic-claim defect the bootstrap correction removed
(`docs/13_…` §17 R9).

Output is a canonical JSON evidence bundle written `0600` inside a `0700`
directory outside the repository tree. It carries counts, identifiers, instants
and hashes — never credentials, DSNs, provider payloads or personal trip data.

Typical use::

    PYTHONPATH="$PWD" python3 ops/audit_telematics_coverage_bootstrap.py \\
        --client-code BRAVO00016 \\
        --dataset trips_sync \\
        --expected-environment production \\
        --expected-platform-uuid 52517750-7438-4558-8490-2736ae4cc629 \\
        --output /var/lib/log-platform/coverage-bootstrap/BRAVO00016.json
"""
from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import os
import subprocess
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import UUID

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.api.telematics.schedule_mutation_surfaces import (  # noqa: E402
    SCHEDULE_RUN_TYPE_BASE,
)

EXIT_OK = 0
EXIT_INVALID_PARAMETERS = 2
EXIT_IDENTITY_NOT_VERIFIED = 3
EXIT_STATE_NOT_INVENTORIABLE = 4
EXIT_RUNTIME_FAILURE = 5

DEFAULT_DATASET = "trips_sync"

# Bundle envelope version. `bootstrap_semantics_version` is the narrower and
# load-bearing one: the bootstrap writer refuses a bundle whose semantics
# version it does not implement, because interval/gap meaning is exactly the
# part of the contract a stale bundle could silently change. A documentation-only
# repository commit deliberately does not invalidate a bundle.
BUNDLE_VERSION = "telematics-coverage-bootstrap-audit/1"
BOOTSTRAP_SEMANTICS_VERSION = "telematics-coverage-bootstrap-semantics/1"

MIGRATION_CEILING = "057_workflow_a_trips_coverage_state.sql"

# Facts, never recommendations. The first three are terminal read-only results
# that still produce a bundle; the last two are refusals that produce none.
CLASSIFICATION_COMPLETE = "COMPLETE_INTERVALS_INVENTORIED"
CLASSIFICATION_UNRESOLVED_GAPS = "UNRESOLVED_GAPS_PRESENT"
CLASSIFICATION_EXISTING_COVERAGE = "EXISTING_COVERAGE_PRESENT"
CLASSIFICATION_AMBIGUOUS_SCHEDULE = "AMBIGUOUS_SCHEDULE"
CLASSIFICATION_INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY_EVIDENCE"
AUDIT_CLASSIFICATIONS = (
    CLASSIFICATION_COMPLETE,
    CLASSIFICATION_UNRESOLVED_GAPS,
    CLASSIFICATION_EXISTING_COVERAGE,
    CLASSIFICATION_AMBIGUOUS_SCHEDULE,
    CLASSIFICATION_INSUFFICIENT_HISTORY,
)

# Terminal history statuses. Anything else is a fire whose interval was never
# proven and is therefore inventoried as unresolved.
STATUS_SUCCESS = "SUCCESS"
STATUS_FAILED = "FAILED"

# Dates of standing operational interest for the whole Workflow A fleet
# (`docs/13_…` §13.8, §15.2: two fires that produced no history row at all and
# one day of terminal `FAILED` fires). Listing them makes those days visible per
# schedule as *facts*; no client-specific interval decision is embedded, and a
# schedule with no fires on these dates simply reports empty inventories.
HISTORICAL_FOCUS_DATES = ("2026-07-30", "2026-07-31", "2026-08-01")

SECONDS_PER_DAY = 86_400
# Bounded enumeration guard: a corrupt cadence must not spin forever.
MAX_ENUMERATED_FIRES = 20_000


class AuditError(RuntimeError):
    """Fail-closed audit refusal carrying a stable exit code."""

    def __init__(self, code: str, message: str, exit_code: int) -> None:
        self.code = code
        self.exit_code = exit_code
        super().__init__(f"{code}: {message}")


# ---------------------------------------------------------------------------
# Canonical serialization
# ---------------------------------------------------------------------------

def canonical_json(value: Any) -> str:
    """Deterministic UTF-8 JSON: sorted keys, no incidental whitespace.

    Two audits of identical state must produce byte-identical output, because
    the bootstrap writer re-derives this hash and refuses on any difference.
    """
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def bundle_sha256(bundle: dict) -> str:
    """SHA-256 over the canonical bundle with `bundle_sha256` excluded."""
    without_hash = {k: v for k, v in bundle.items() if k != "bundle_sha256"}
    return hashlib.sha256(
        canonical_json(without_hash).encode("utf-8")
    ).hexdigest()


def iso_utc(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.utcoffset() is None:
        raise AuditError(
            "NAIVE_TIMESTAMP",
            "a naive timestamp was read where an absolute instant is required",
            EXIT_STATE_NOT_INVENTORIABLE,
        )
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso_utc(raw: str, *, label: str) -> datetime:
    text = str(raw or "").strip()
    if not text:
        raise AuditError(
            "INVALID_PARAMETER", f"{label} is required", EXIT_INVALID_PARAMETERS
        )
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise AuditError(
            "INVALID_PARAMETER",
            f"{label} must be an ISO-8601 instant",
            EXIT_INVALID_PARAMETERS,
        ) from exc
    if parsed.utcoffset() is None:
        raise AuditError(
            "INVALID_PARAMETER",
            f"{label} must be timezone-aware",
            EXIT_INVALID_PARAMETERS,
        )
    return parsed.astimezone(timezone.utc)


def canonical_uuid(value: object, *, label: str) -> str:
    try:
        parsed = UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise AuditError(
            "INVALID_PARAMETER",
            f"{label} must be a canonical UUID",
            EXIT_INVALID_PARAMETERS,
        ) from exc
    return str(parsed)


# ---------------------------------------------------------------------------
# Pure fire enumeration (docs/13 §13.2 item 3)
# ---------------------------------------------------------------------------

def _last_day_of_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def enumerate_expected_fires(
    *,
    frequency: str,
    day_of_week: Optional[int],
    day_of_month: Optional[int],
    day_of_month_last: bool,
    run_time: time,
    timezone_name: str,
    range_start_utc: datetime,
    range_end_utc: datetime,
) -> list[datetime]:
    """Enumerate every fire the schedule definition implies in a closed range.

    Pure: no database, no clock, no environment. This mirrors the cadence
    semantics of `dispatcher.latest_scheduled_fire_local` but *enumerates*
    rather than returning only the newest fire, because a fire that produced no
    `client_schedule_run_history` row at all is invisible any other way
    (`docs/13_…` §13.2 item 3). The local wall-clock `run_time` is combined in
    the schedule's own timezone and then converted to UTC, so DST transitions
    move the absolute instant exactly as the dispatcher moves it.
    """
    from zoneinfo import ZoneInfo

    if range_end_utc < range_start_utc:
        raise ValueError("range_end_utc must not precede range_start_utc")
    try:
        tz = ZoneInfo(str(timezone_name))
    except Exception as exc:  # unknown IANA zone is a schema/state problem
        raise ValueError(f"unknown schedule timezone {timezone_name!r}") from exc

    # Walk local calendar days with a margin so that a fire whose local day sits
    # just outside the UTC range is still considered before filtering on the
    # absolute instant.
    start_local = (range_start_utc.astimezone(tz) - timedelta(days=2)).date()
    end_local = (range_end_utc.astimezone(tz) + timedelta(days=2)).date()

    fires: list[datetime] = []
    current = start_local
    guard = 0
    while current <= end_local:
        guard += 1
        if guard > MAX_ENUMERATED_FIRES:
            raise ValueError("expected-fire enumeration exceeded its bound")
        matches = False
        if frequency == "daily":
            matches = True
        elif frequency == "weekly":
            matches = day_of_week is not None and current.weekday() == day_of_week
        elif frequency == "monthly":
            if day_of_month_last:
                matches = current.day == _last_day_of_month(
                    current.year, current.month
                )
            elif day_of_month is not None:
                matches = current.day == day_of_month
        else:
            raise ValueError(f"unsupported schedule frequency {frequency!r}")

        if matches:
            local_fire = datetime.combine(current, run_time, tzinfo=tz)
            fire_utc = local_fire.astimezone(timezone.utc)
            if range_start_utc <= fire_utc <= range_end_utc:
                fires.append(fire_utc)
        current += timedelta(days=1)

    return sorted(set(fires))


def nominal_window(
    *, fire_utc: datetime, lookback_days: int
) -> tuple[datetime, datetime]:
    """`[F − L, F]` as absolute UTC instants (`docs/13_…` §16.1)."""
    return fire_utc - timedelta(seconds=lookback_days * SECONDS_PER_DAY), fire_utc


# ---------------------------------------------------------------------------
# Read-only database access
# ---------------------------------------------------------------------------

def _load_dotenv() -> None:
    path = REPO_ROOT / ".env"
    if not path.exists():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(path, override=False)


def platform_dsn_from_env() -> str:
    host = os.getenv("POSTGRES_HOST") or "127.0.0.1"
    port = os.getenv("POSTGRES_PORT") or "5432"
    database = os.getenv("POSTGRES_DB")
    user = os.getenv("POSTGRES_USER")
    password = os.getenv("POSTGRES_PASSWORD")
    missing = [
        name
        for name, value in (
            ("POSTGRES_DB", database),
            ("POSTGRES_USER", user),
            ("POSTGRES_PASSWORD", password),
        )
        if not value
    ]
    if missing:
        raise AuditError(
            "PLATFORM_DSN_INCOMPLETE",
            f"missing platform connection variables: {', '.join(missing)}",
            EXIT_INVALID_PARAMETERS,
        )
    return (
        f"host={host} port={port} dbname={database} "
        f"user={user} password={password}"
    )


def open_read_only_connection(dsn: str):
    """Open a connection that the server itself refuses to write through."""
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise AuditError(
            "DEPENDENCY_MISSING",
            "psycopg is required for the read-only inventory",
            EXIT_RUNTIME_FAILURE,
        ) from exc

    conn = psycopg.connect(dsn, autocommit=False, row_factory=dict_row)
    try:
        # Connection-level read-only, so a write is rejected by PostgreSQL
        # rather than only by this module's discipline.
        conn.read_only = True
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute("SET LOCAL statement_timeout = '60s'")
    except Exception:
        conn.close()
        raise
    return conn


def _one(cur, sql: str, params: tuple = ()) -> Optional[dict]:
    cur.execute(sql, params)
    rows = cur.fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        raise AuditError(
            "AMBIGUOUS_ROW",
            "a uniquely keyed lookup returned more than one row",
            EXIT_STATE_NOT_INVENTORIABLE,
        )
    return dict(rows[0])


def verify_platform_identity(
    cur, *, expected_environment: str, expected_platform_uuid: str
) -> dict:
    cur.execute(
        "SELECT to_regclass('ops_control.environment_identity')::text AS marker"
    )
    if not (cur.fetchone() or {}).get("marker"):
        raise AuditError(
            "IDENTITY_MARKER_MISSING",
            "ops_control.environment_identity does not exist",
            EXIT_IDENTITY_NOT_VERIFIED,
        )
    cur.execute(
        """
        SELECT identity_key, environment,
               database_identity_id::text AS database_identity_id,
               database_role, database_name
          FROM ops_control.environment_identity
         ORDER BY identity_key
        """
    )
    rows = [dict(row) for row in cur.fetchall()]
    if len(rows) != 1 or rows[0].get("identity_key") != "primary":
        raise AuditError(
            "IDENTITY_MARKER_AMBIGUOUS",
            "the identity table must hold exactly the primary marker row",
            EXIT_IDENTITY_NOT_VERIFIED,
        )
    marker = rows[0]
    if str(marker.get("environment")) != expected_environment:
        raise AuditError(
            "IDENTITY_ENVIRONMENT_MISMATCH",
            "database environment does not match --expected-environment",
            EXIT_IDENTITY_NOT_VERIFIED,
        )
    if str(marker.get("database_identity_id")) != expected_platform_uuid:
        raise AuditError(
            "IDENTITY_PLATFORM_UUID_MISMATCH",
            "database identity UUID does not match --expected-platform-uuid",
            EXIT_IDENTITY_NOT_VERIFIED,
        )
    if str(marker.get("database_role")) != "platform":
        raise AuditError(
            "IDENTITY_ROLE_MISMATCH",
            "the connected database is not the platform database",
            EXIT_IDENTITY_NOT_VERIFIED,
        )
    return marker


def verify_migration_ceiling(cur) -> list[str]:
    cur.execute(
        "SELECT filename FROM public.schema_migrations ORDER BY filename"
    )
    applied = [str(row["filename"]) for row in cur.fetchall()]
    if MIGRATION_CEILING not in applied:
        raise AuditError(
            "MIGRATION_CEILING_MISSING",
            f"{MIGRATION_CEILING} is not applied to this database",
            EXIT_STATE_NOT_INVENTORIABLE,
        )
    cur.execute(
        "SELECT to_regclass('workflow_a_control.client_dataset_coverage')::text"
        " AS coverage"
    )
    if not (cur.fetchone() or {}).get("coverage"):
        raise AuditError(
            "COVERAGE_TABLE_MISSING",
            "workflow_a_control.client_dataset_coverage does not exist",
            EXIT_STATE_NOT_INVENTORIABLE,
        )
    cur.execute(
        """
        SELECT column_name
          FROM information_schema.columns
         WHERE table_schema = 'workflow_a_control'
           AND table_name = 'client_schedule_run_history'
           AND column_name = ANY(%s)
        """,
        (
            [
                "nominal_window_start_ts",
                "nominal_window_end_ts",
                "stabilization_delay_seconds",
                "overlap_seconds",
                "trips_pagination_mode",
            ],
        ),
    )
    present = {str(row["column_name"]) for row in cur.fetchall()}
    if len(present) != 5:
        raise AuditError(
            "HISTORY_EVIDENCE_COLUMNS_MISSING",
            "migration 057 history evidence columns are incomplete",
            EXIT_STATE_NOT_INVENTORIABLE,
        )
    return applied


def resolve_client(cur, client_code: str) -> dict:
    cur.execute(
        """
        SELECT client_id::text AS client_id, client_code,
               trips_pagination_mode,
               trips_stabilization_delay_seconds,
               trips_overlap_seconds,
               trips_max_recovery_span_seconds
          FROM workflow_a_control.client_account
         WHERE client_code = %s
        """,
        (client_code,),
    )
    rows = [dict(row) for row in cur.fetchall()]
    if not rows:
        raise AuditError(
            "CLIENT_NOT_FOUND",
            f"no client_account row carries client_code {client_code}",
            EXIT_STATE_NOT_INVENTORIABLE,
        )
    if len(rows) > 1:
        raise AuditError(
            CLASSIFICATION_AMBIGUOUS_SCHEDULE,
            f"client_code {client_code} resolves to {len(rows)} clients",
            EXIT_STATE_NOT_INVENTORIABLE,
        )
    return rows[0]


def resolve_schedule(cur, *, client_id: str, dataset_name: str) -> dict:
    """Resolve the one authoritative *enabled BASE* schedule for client × dataset.

    Scoped to `run_type = 'DAILY'` since M5. This audit exists to describe the
    schedule that owns forward coverage; a reconciliation cadence is a valid row
    that shares the watermark without owning it, and counting it here would make
    the ambiguity refusal below fire for a reason unrelated to the client's
    state. Today the scope changes nothing — every schedule row is base.
    """
    cur.execute(
        """
        SELECT schedule_id::text AS schedule_id, client_id::text AS client_id,
               client_code, dataset_name, enabled, frequency, day_of_week,
               day_of_month, day_of_month_last, run_time, timezone,
               lookback_days, overwrite_existing, event_enrichment_mode
          FROM workflow_a_control.client_dataset_schedule
         WHERE client_id = %s AND dataset_name = %s
           AND run_type = %s
         ORDER BY schedule_id
        """,
        (client_id, dataset_name, SCHEDULE_RUN_TYPE_BASE),
    )
    rows = [dict(row) for row in cur.fetchall()]
    if not rows:
        raise AuditError(
            CLASSIFICATION_AMBIGUOUS_SCHEDULE,
            f"no {dataset_name} schedule exists for this client",
            EXIT_STATE_NOT_INVENTORIABLE,
        )
    enabled = [row for row in rows if row["enabled"]]
    if len(enabled) != 1:
        raise AuditError(
            CLASSIFICATION_AMBIGUOUS_SCHEDULE,
            f"{dataset_name} resolves to {len(enabled)} enabled schedules; "
            "exactly one authoritative schedule is required",
            EXIT_STATE_NOT_INVENTORIABLE,
        )
    return enabled[0]


def read_history(cur, *, schedule_id: str) -> list[dict]:
    cur.execute(
        """
        SELECT scheduled_fire_ts, status, window_start_ts, window_end_ts,
               nominal_window_start_ts, nominal_window_end_ts,
               stabilization_delay_seconds, overlap_seconds,
               trips_pagination_mode, error_summary,
               platform_run_id::text AS platform_run_id,
               started_at, finished_at, created_at
          FROM workflow_a_control.client_schedule_run_history
         WHERE schedule_id = %s
         ORDER BY scheduled_fire_ts
        """,
        (schedule_id,),
    )
    return [dict(row) for row in cur.fetchall()]


def read_existing_coverage(
    cur, *, client_id: str, dataset_name: str,
) -> Optional[dict]:
    """Read the coverage row without modifying it. `SELECT` only — no lock."""
    return _one(
        cur,
        """
        SELECT schedule_id::text AS schedule_id, client_id::text AS client_id,
               client_code, dataset_name, coverage_start_ts, covered_through_ts,
               bootstrap_status, bootstrap_evidence_ref, seeded_at, seeded_by,
               covered_through_source, last_gap_detected_ts, updated_at
          FROM workflow_a_control.client_dataset_coverage
         WHERE client_id = %s
           AND dataset_name = %s
        """,
        (client_id, dataset_name),
    )


def repository_head() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            check=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:
        raise AuditError(
            "REPOSITORY_HEAD_UNAVAILABLE",
            "the repository HEAD could not be resolved for the bundle",
            EXIT_RUNTIME_FAILURE,
        ) from exc
    return completed.stdout.strip()


# ---------------------------------------------------------------------------
# Inventory assembly
# ---------------------------------------------------------------------------

def build_bundle(
    *,
    environment_name: str,
    platform_uuid: str,
    repo_head: str,
    generated_at_utc: datetime,
    client: dict,
    schedule: dict,
    history: list[dict],
    existing_coverage: Optional[dict],
    range_start_utc: datetime,
    range_end_utc: datetime,
    migration_ceiling: str,
) -> dict:
    """Assemble the deterministic §13.5 bundle. Facts only, no recommendation."""
    lookback_days = int(schedule["lookback_days"])

    expected_fires = enumerate_expected_fires(
        frequency=str(schedule["frequency"]),
        day_of_week=schedule["day_of_week"],
        day_of_month=schedule["day_of_month"],
        day_of_month_last=bool(schedule["day_of_month_last"]),
        run_time=schedule["run_time"],
        timezone_name=str(schedule["timezone"]),
        range_start_utc=range_start_utc,
        range_end_utc=range_end_utc,
    )

    observed_fires = {
        row["scheduled_fire_ts"].astimezone(timezone.utc) for row in history
    }

    successful_intervals: list[dict] = []
    failed_runs: list[dict] = []
    unresolved: list[dict] = []

    for row in history:
        fire = row["scheduled_fire_ts"].astimezone(timezone.utc)
        status = str(row["status"])
        if status == STATUS_SUCCESS:
            successful_intervals.append({
                "scheduled_fire_ts": iso_utc(fire),
                "interval_start_ts": iso_utc(row["window_start_ts"]),
                "interval_end_ts": iso_utc(row["window_end_ts"]),
                "nominal_window_start_ts": iso_utc(row["nominal_window_start_ts"]),
                "nominal_window_end_ts": iso_utc(row["nominal_window_end_ts"]),
                "trips_pagination_mode": row["trips_pagination_mode"],
                "platform_run_id": row["platform_run_id"],
                "finished_at": iso_utc(row["finished_at"]),
            })
            continue

        entry = {
            "scheduled_fire_ts": iso_utc(fire),
            "status": status,
            "interval_start_ts": iso_utc(row["window_start_ts"]),
            "interval_end_ts": iso_utc(row["window_end_ts"]),
            "error_summary": row["error_summary"],
            "platform_run_id": row["platform_run_id"],
        }
        if status == STATUS_FAILED:
            failed_runs.append(entry)
        unresolved.append({
            "kind": "TERMINAL_FAILED_FIRE" if status == STATUS_FAILED
                    else "NON_TERMINAL_FIRE",
            "scheduled_fire_ts": iso_utc(fire),
            "interval_start_ts": iso_utc(row["window_start_ts"]),
            "interval_end_ts": iso_utc(row["window_end_ts"]),
            "detail": (
                f"fire status {status} did not prove its requested interval"
            ),
        })

    for fire in expected_fires:
        if fire in observed_fires:
            continue
        start, end = nominal_window(fire_utc=fire, lookback_days=lookback_days)
        unresolved.append({
            "kind": "MISSING_FIRE",
            "scheduled_fire_ts": iso_utc(fire),
            "interval_start_ts": iso_utc(start),
            "interval_end_ts": iso_utc(end),
            "detail": (
                "the schedule definition implies this fire but no "
                "client_schedule_run_history row exists"
            ),
        })

    unresolved.sort(key=lambda item: (item["scheduled_fire_ts"], item["kind"]))
    successful_intervals.sort(key=lambda item: item["scheduled_fire_ts"])
    failed_runs.sort(key=lambda item: item["scheduled_fire_ts"])

    focus = []
    for day in HISTORICAL_FOCUS_DATES:
        focus.append({
            "date": day,
            "expected_fires": [
                iso_utc(fire) for fire in expected_fires
                if iso_utc(fire).startswith(day)
            ],
            "history_rows": [
                {
                    "scheduled_fire_ts": iso_utc(
                        row["scheduled_fire_ts"].astimezone(timezone.utc)
                    ),
                    "status": str(row["status"]),
                    "error_summary": row["error_summary"],
                }
                for row in history
                if iso_utc(
                    row["scheduled_fire_ts"].astimezone(timezone.utc)
                ).startswith(day)
            ],
        })

    if existing_coverage is not None:
        classification = CLASSIFICATION_EXISTING_COVERAGE
    elif not history:
        classification = CLASSIFICATION_INSUFFICIENT_HISTORY
    elif unresolved:
        classification = CLASSIFICATION_UNRESOLVED_GAPS
    else:
        classification = CLASSIFICATION_COMPLETE

    coverage_view = None
    if existing_coverage is not None:
        coverage_view = {
            "schedule_id": existing_coverage["schedule_id"],
            "client_id": existing_coverage["client_id"],
            "client_code": existing_coverage["client_code"],
            "dataset_name": existing_coverage["dataset_name"],
            "coverage_start_ts": iso_utc(existing_coverage["coverage_start_ts"]),
            "covered_through_ts": iso_utc(
                existing_coverage["covered_through_ts"]
            ),
            "bootstrap_status": existing_coverage["bootstrap_status"],
            "bootstrap_evidence_ref_present": bool(
                str(existing_coverage["bootstrap_evidence_ref"] or "").strip()
            ),
            "seeded_at": iso_utc(existing_coverage["seeded_at"]),
            "seeded_by": existing_coverage["seeded_by"],
            "covered_through_source": existing_coverage[
                "covered_through_source"
            ],
            "last_gap_detected_ts": iso_utc(
                existing_coverage["last_gap_detected_ts"]
            ),
            "updated_at": iso_utc(existing_coverage["updated_at"]),
        }

    bundle: dict[str, Any] = {
        "bundle_version": BUNDLE_VERSION,
        "bootstrap_semantics_version": BOOTSTRAP_SEMANTICS_VERSION,
        "generated_at_utc": iso_utc(generated_at_utc),
        "environment_name": environment_name,
        "platform_uuid": platform_uuid,
        "repository_head": repo_head,
        "migration_ceiling": migration_ceiling,
        "client_id": client["client_id"],
        "client_code": client["client_code"],
        "dataset_name": str(schedule["dataset_name"]),
        "schedule_id": schedule["schedule_id"],
        "client_mode": client["trips_pagination_mode"],
        "schedule_parameters": {
            "enabled": bool(schedule["enabled"]),
            "frequency": str(schedule["frequency"]),
            "day_of_week": schedule["day_of_week"],
            "day_of_month": schedule["day_of_month"],
            "day_of_month_last": bool(schedule["day_of_month_last"]),
            "run_time": str(schedule["run_time"]),
            "timezone": str(schedule["timezone"]),
            "lookback_days": lookback_days,
            "overwrite_existing": bool(schedule["overwrite_existing"]),
            "event_enrichment_mode": str(schedule["event_enrichment_mode"]),
            "trips_stabilization_delay_seconds": client[
                "trips_stabilization_delay_seconds"
            ],
            "trips_overlap_seconds": client["trips_overlap_seconds"],
            "trips_max_recovery_span_seconds": client[
                "trips_max_recovery_span_seconds"
            ],
        },
        "inventory_range": {
            "range_start_ts": iso_utc(range_start_utc),
            "range_end_ts": iso_utc(range_end_utc),
            "expected_fire_count": len(expected_fires),
            "observed_fire_count": len(history),
        },
        "successful_intervals": successful_intervals,
        "failed_runs": failed_runs,
        "missing_or_unproven_intervals": unresolved,
        "historical_focus_inventory": focus,
        "existing_coverage": coverage_view,
        "audit_classification": classification,
        "evidence_items": [
            {
                "item": "schedule_configuration",
                "source": "workflow_a_control.client_dataset_schedule",
                "count": 1,
            },
            {
                "item": "client_configuration",
                "source": "workflow_a_control.client_account",
                "count": 1,
            },
            {
                "item": "schedule_fire_history",
                "source": "workflow_a_control.client_schedule_run_history",
                "count": len(history),
            },
            {
                "item": "expected_fires",
                "source": "schedule definition enumeration",
                "count": len(expected_fires),
            },
            {
                "item": "successful_intervals",
                "source": "history rows with status SUCCESS",
                "count": len(successful_intervals),
            },
            {
                "item": "missing_or_unproven_intervals",
                "source": "expected fires diffed against history",
                "count": len(unresolved),
            },
            {
                "item": "existing_coverage",
                "source": "workflow_a_control.client_dataset_coverage",
                "count": 0 if existing_coverage is None else 1,
            },
        ],
        # Stated in the bundle so that a reviewer reading only the artifact
        # knows the tool made no interval decision (docs/13 §13.3, §17 R9).
        "decision_contract": {
            "coverage_start_ts_recommended": False,
            "covered_through_ts_recommended": False,
            "note": (
                "This bundle reports inventoried facts only. Selecting A and W "
                "is an explicit reviewed operator decision recorded in the "
                "bootstrap ticket; this tool recommends neither bound and "
                "never asserts that a usable coverage claim exists."
            ),
        },
    }
    bundle["bundle_sha256"] = bundle_sha256(bundle)
    return bundle


def write_bundle(path: Path, bundle: dict) -> None:
    """Write `0600` into a `0700` directory outside the repository tree."""
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(REPO_ROOT)
    except ValueError:
        pass
    else:
        raise AuditError(
            "OUTPUT_INSIDE_REPOSITORY",
            "the evidence bundle must be written outside the repository tree",
            EXIT_INVALID_PARAMETERS,
        )

    parent = resolved.parent
    try:
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(parent, 0o700)
        payload = canonical_json(bundle) + "\n"
        fd = os.open(
            resolved, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
        )
        try:
            os.write(fd, payload.encode("utf-8"))
        finally:
            os.close(fd)
        os.chmod(resolved, 0o600)
    except AuditError:
        raise
    except OSError as exc:
        raise AuditError(
            "OUTPUT_WRITE_FAILED",
            f"the evidence bundle could not be written: {exc.strerror}",
            EXIT_RUNTIME_FAILURE,
        ) from exc


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only Telematics coverage bootstrap inventory. Reports facts; "
            "never recommends A or W; never writes."
        ),
    )
    parser.add_argument("--client-code", required=True)
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--expected-environment",
        required=True,
        help="Environment this database must declare; no default is assumed.",
    )
    parser.add_argument(
        "--expected-platform-uuid",
        required=True,
        help="Platform identity UUID this database must declare.",
    )
    parser.add_argument(
        "--range-start",
        help="Optional ISO-8601 lower bound of the inventory range. "
             "Defaults to the earliest recorded fire for the schedule.",
    )
    parser.add_argument(
        "--range-end",
        help="Optional ISO-8601 upper bound. Defaults to the database clock.",
    )
    parser.add_argument(
        "--dsn",
        help="Platform DSN override. Defaults to the POSTGRES_* environment.",
    )
    return parser


def run_audit(args) -> tuple[dict, Path]:
    expected_uuid = canonical_uuid(
        args.expected_platform_uuid, label="--expected-platform-uuid"
    )
    client_code = str(args.client_code or "").strip()
    dataset = str(args.dataset or "").strip()
    if not client_code or not dataset:
        raise AuditError(
            "INVALID_PARAMETER",
            "--client-code and --dataset must be non-empty",
            EXIT_INVALID_PARAMETERS,
        )

    _load_dotenv()
    dsn = args.dsn or platform_dsn_from_env()
    repo_head = repository_head()

    conn = open_read_only_connection(dsn)
    try:
        with conn.cursor() as cur:
            marker = verify_platform_identity(
                cur,
                expected_environment=str(args.expected_environment),
                expected_platform_uuid=expected_uuid,
            )
            verify_migration_ceiling(cur)

            # Whole seconds, matching the second-precision contract the rest of
            # the scheduled path uses, so that the bootstrap writer can compare
            # bundle age against its own truncated clock without skew.
            cur.execute("SELECT date_trunc('second', now()) AS db_now")
            db_now = (cur.fetchone() or {})["db_now"].astimezone(timezone.utc)

            client = resolve_client(cur, client_code)
            schedule = resolve_schedule(
                cur, client_id=client["client_id"], dataset_name=dataset
            )
            history = read_history(cur, schedule_id=schedule["schedule_id"])
            existing_coverage = read_existing_coverage(
                cur,
                client_id=client["client_id"],
                dataset_name=dataset,
            )

        if args.range_end:
            range_end = parse_iso_utc(args.range_end, label="--range-end")
        else:
            range_end = db_now
        if args.range_start:
            range_start = parse_iso_utc(args.range_start, label="--range-start")
        elif history:
            range_start = history[0]["scheduled_fire_ts"].astimezone(
                timezone.utc
            )
        else:
            range_start = range_end - timedelta(
                seconds=int(schedule["lookback_days"]) * SECONDS_PER_DAY
            )
        if range_end < range_start:
            raise AuditError(
                "INVALID_PARAMETER",
                "--range-end must not precede --range-start",
                EXIT_INVALID_PARAMETERS,
            )

        try:
            bundle = build_bundle(
                environment_name=str(marker["environment"]),
                platform_uuid=expected_uuid,
                repo_head=repo_head,
                generated_at_utc=db_now,
                client=client,
                schedule=schedule,
                history=history,
                existing_coverage=existing_coverage,
                range_start_utc=range_start,
                range_end_utc=range_end,
                migration_ceiling=MIGRATION_CEILING,
            )
        except ValueError as exc:
            raise AuditError(
                "SCHEDULE_DEFINITION_UNUSABLE", str(exc),
                EXIT_STATE_NOT_INVENTORIABLE,
            ) from exc
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()

    output = Path(args.output)
    write_bundle(output, bundle)
    return bundle, output.expanduser().resolve()


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        bundle, output = run_audit(args)
    except AuditError as exc:
        print(f"AUDIT_REFUSED {exc}", file=sys.stderr)
        return exc.exit_code
    except Exception as exc:  # pragma: no cover - unexpected runtime failure
        print(f"AUDIT_FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_RUNTIME_FAILURE

    summary = {
        "audit_classification": bundle["audit_classification"],
        "client_code": bundle["client_code"],
        "client_id": bundle["client_id"],
        "dataset_name": bundle["dataset_name"],
        "schedule_id": bundle["schedule_id"],
        "client_mode": bundle["client_mode"],
        "successful_interval_count": len(bundle["successful_intervals"]),
        "failed_run_count": len(bundle["failed_runs"]),
        "missing_or_unproven_interval_count": len(
            bundle["missing_or_unproven_intervals"]
        ),
        "existing_coverage_present": bundle["existing_coverage"] is not None,
        "bundle_sha256": bundle["bundle_sha256"],
        "output": str(output),
    }
    print(json.dumps(summary, sort_keys=True, indent=2))
    print(
        "READ_ONLY_INVENTORY_COMPLETE — no coverage row was created, no value "
        "was recommended; selecting A and W remains a reviewed operator "
        "decision.",
    )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
