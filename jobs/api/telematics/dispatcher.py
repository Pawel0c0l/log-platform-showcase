"""
Workflow A — dispatcher (v1).

A lightweight, DB-driven scheduler runnable as a standard `ops/runner.py`
job. The intended invocation is via a 5-minute systemd timer:

    PYTHONPATH="$PWD" .venv/bin/python ops/runner.py \\
      jobs.api.telematics.dispatcher \\
      '{}'

Behavior on every tick:

  1. Read enabled schedule rows from `workflow_a_control.client_dataset_schedule`
     (joined with `client_account` and `dataset_registry`). The DB is the
     source of truth — no caching across ticks; configuration changes take
     effect on the very next tick.

  2. Validate every row's `dataset_name` against the **Python** registry
     (`jobs.api.telematics.registry.DATASETS`). Rows whose `job_module` does
     not match the Python registry are skipped with an ERROR log; we never
     execute a job_module value that is not in the allowlist.

  3. For each row, evaluate the latest scheduled fire timestamp at or before
     `now_utc`, in the schedule's local timezone (Python stdlib `zoneinfo`
     + `datetime`; no third-party scheduling lib). Frequencies: daily,
     weekly (`day_of_week` 0=Mon..6=Sun matching Python `weekday()`),
     monthly with either `day_of_month ∈ [1, 28]` or `day_of_month_last`.
     Schedules whose latest fire is in the future are skipped.

  4. Acquire a global Postgres advisory lock before entering the scheduling
     loop. If another dispatcher process already holds it, this tick is a
     clean no-op. Then mark stale `RUNNING` schedule-history rows older than
     the configured timeout as `FAILED`.

  5. Single-job execution under the recommended single systemd unit: count rows
     with `status='RUNNING'` in `client_schedule_run_history`. If any remain,
     this tick is a no-op. Otherwise, sort due schedules by
     `(scheduled_fire_ts ASC, client_code ASC, dataset_name ASC)`, claim
     exactly one (insert a `RUNNING` row; the UNIQUE on
     `(schedule_id, scheduled_fire_ts)` rejects duplicate fires), then run
     the matching Workflow A job once via subprocess to `ops/runner.py`.
     The subprocess receives `LOG_PLATFORM_RUN_ID_FILE`; once the runner
     writes its platform `run_id`, the dispatcher stores it in
     `client_schedule_run_history.platform_run_id`. Eco Driving schedules
     use the same subprocess path but pass a `mode` resolver instead of
     provider API window params.

  6. After the subprocess returns, update the row to `SUCCESS` or `FAILED`
     and end the dispatcher tick. Multiple due jobs naturally queue across
     consecutive ticks.

Telematics `/trips` compatibility mode (delivery-plan C5/C6, `docs/14_…` §7.1):

  * For a `trips_sync` schedule whose client is `trips_pagination_mode =
    'data_invariants_v1'`, steps 3-5 gain a read-only preparation: the coverage
    row is loaded by `schedule_id` and the fail-closed gate of `docs/13_…`
    §5.2.1 is evaluated together with the C4 effective-window arithmetic
    **before** the claim INSERT, because the claim stores the execution window.
  * The claim always happens — a rejected fire claims its *nominal* window, so
    every due fire leaves durable terminal history. The five evidence columns
    (`nominal_window_*`, `stabilization_delay_seconds`, `overlap_seconds`,
    `trips_pagination_mode`) are written once, in that INSERT.
  * The gate is then enforced immediately after the claim and strictly before
    `_build_job_params` and `_launch_job`: a rejected fire is finalized `FAILED`
    with the abort classification, logged at `ERROR`, reported as a
    `suspected_bug`, and launches nothing. No credential is resolved, no socket
    is opened, no provider request is made and no client-business write occurs.
  * A newly disconnected valid `READY` claim is atomically persisted as
    `GAP_DETECTED` with history `FAILED` before launch. Existing or malformed
    gap rows remain non-mutating rejections.
  * C6 coverage writes exist only in `_finalize_compat_gap` and
    `_finalize_compat_success`. Both lock coverage before history, verify the
    retained claim snapshot and history `RUNNING` before mutation, and commit
    the coverage/history decision atomically.
  * `strict_meta` short-circuits all of the above: no coverage read, no gate,
    nominal windows, `NULL` evidence columns and today's launch path unchanged.
    It is no longer the majority configuration — as of 2026-08-05 every enabled
    production trips client is `data_invariants_v1` — so the compatibility path
    below is the normal one, not the exception.
  * After compatibility `rc == 0`, `covered_through_ts` advances monotonically
    to the effective end, or coverage is a true no-op when already covered —
    but only once M3's outcome gate has accepted the child's terminal record.
    `rc == 0` is necessary and never sufficient: the record must be present,
    parse, verify against this exact claim (including platform-run identity)
    and be coverage-eligible. Anything else finalizes the fire `FAILED` and
    leaves the watermark untouched. See `_require_coverage_eligible_outcome`.

This dispatcher does NOT:

  * catch up on multiple historical missed fires (it only ever considers
    the latest one per schedule),
  * run jobs in parallel,
  * understand cron expressions,
  * trigger from email or any source other than the systemd timer.

Failure modes:

  * If the dispatcher process itself is killed mid-subprocess (e.g. OOM),
    the `RUNNING` row stays temporarily. A later tick marks it `FAILED`
    after `stale_running_timeout_minutes` (default 720 minutes), or an
    operator may unblock earlier with a manual UPDATE — see
    `docs/07_operations.md`.
  * Any exception while launching/running the subprocess flips the row
    to `FAILED` with a truncated stderr+stdout tail; the dispatcher does
    not crash, the next tick can pick up the next due schedule.
"""
from __future__ import annotations

import calendar
import json
import os
import subprocess
import sys
import tempfile
import traceback
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError as exc:
    raise RuntimeError("dispatcher requires Python 3.9+ stdlib zoneinfo") from exc

from api.suspected_bug import SuspectedBugEvent
from api.timezone_utils import set_pg_session_timezone
from jobs.api.telematics import manual_recovery_authority
from jobs.api.telematics.schedule_mutation_surfaces import (
    SCHEDULE_RUN_TYPE_BASE,
    validate_schedule_run_type,
)
from jobs.api.telematics import registry
from jobs.api.telematics.execution_outcome import (
    EXECUTION_OUTCOME_FILE_ENV,
    ExecutionOutcome,
    ExecutionOutcomeError,
    is_coverage_eligible,
    read_outcome,
    verify_outcome,
)
from jobs.api.telematics.coverage_finalization import (
    COVERAGE_SOURCE_SCHEDULED_RUN,
    TRIPS_COVERAGE_ADVANCE_CONFLICT,
    CoverageCasConflict,
    CoverageClaimSnapshot,
    advance_covered_through_cas,
    lock_coverage_row_for_update,
    snapshot_matches_row,
)
from jobs.api.telematics.request_evidence import (
    WindowCompleteness,
    WindowCompletenessError,
    verify_window_completeness,
)
from jobs.api.telematics.coverage_windows import (
    COVERAGE_STATUS_READY,
    TRIPS_COVERAGE_BOOTSTRAP_REQUIRED,
    TRIPS_COVERAGE_GAP_DETECTED,
    CoverageGateResult,
    CoverageState,
    evaluate_coverage_gate,
)
from jobs.trips_pagination_mode import (
    TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1,
    TRIPS_PAGINATION_MODE_DEFAULT,
    normalize_trips_pagination_mode,
)
from jobs.trips_stabilization_config import (
    TRIPS_MAX_RECOVERY_SPAN_SECONDS_DEFAULT,
    TRIPS_OVERLAP_SECONDS_DEFAULT,
    TRIPS_STABILIZATION_DELAY_SECONDS_DEFAULT,
    validate_trips_stabilization_config,
)
from jobs.ecodriving.scheduled_mailing_contract import (
    ALLOWED_SCHEDULED_RUNNER_OPTIONS,
    ScheduledMailingInvocation,
    is_mailing_dataset,
    resolve_scheduled_invocation,
)


JOB_SOURCE = "jobs.api.telematics.dispatcher"
DEFERRED_RUN_CREATION = True
DISPATCHER_ADVISORY_LOCK_KEY = 728503746327118001
STALE_RUNNING_TIMEOUT_ENV = "WORKFLOW_A_DISPATCHER_STALE_RUNNING_TIMEOUT_MINUTES"
DEFAULT_STALE_RUNNING_TIMEOUT_MINUTES = 720
TRIPS_SYNC_DATASET_NAME = "trips_sync"
SUPPORTED_EVENT_ENRICHMENT_MODES = {"enabled", "disabled"}

TRIPS_COVERAGE_GAP_DETECTED_PERSISTENCE_CONFLICT = (
    "TRIPS_COVERAGE_GAP_DETECTED_PERSISTENCE_CONFLICT"
)
TRIPS_HISTORY_CLAIM_LOST = "TRIPS_HISTORY_CLAIM_LOST"
TRIPS_COVERAGE_FINALIZATION_COMMIT_FAILED = (
    "TRIPS_COVERAGE_FINALIZATION_COMMIT_FAILED"
)
TRIPS_COVERAGE_ATOMIC_STATE_DIVERGENCE = (
    "TRIPS_COVERAGE_ATOMIC_STATE_DIVERGENCE"
)
TRIPS_COVERAGE_COMMIT_RECONCILIATION_UNAVAILABLE = (
    "TRIPS_COVERAGE_COMMIT_RECONCILIATION_UNAVAILABLE"
)
#: M4. The durable evidence projection failed inside the finalization
#: transaction, so the watermark did not move. Kept distinct from
#: `TRIPS_COVERAGE_FINALIZATION_COMMIT_FAILED` because it says something
#: different: not that the transaction could not be committed, but that the
#: evidence which had to be committed *with* it could not be written.
TRIPS_WINDOW_EVIDENCE_PROJECTION_FAILED = (
    "TRIPS_WINDOW_EVIDENCE_PROJECTION_FAILED"
)
#: A verified proof that projected nothing. Structurally unreachable; kept so
#: that if it ever becomes reachable it refuses rather than commits.
TRIPS_WINDOW_EVIDENCE_PROJECTION_EMPTY = (
    "TRIPS_WINDOW_EVIDENCE_PROJECTION_EMPTY"
)
#: The proof names request identities this platform run did not durably record
#: as PENDING request facts — so the evidence backing the watermark is not what
#: it claims. Distinct from a projection *failure*: nothing went wrong
#: mechanically, the evidence simply is not there to promote.
TRIPS_WINDOW_EVIDENCE_PROJECTION_INCOMPLETE = (
    "TRIPS_WINDOW_EVIDENCE_PROJECTION_INCOMPLETE"
)


class CoverageFinalizationError(RuntimeError):
    """Bounded C6 failure classification; never carries raw DB content."""

    def __init__(
        self,
        code: str,
        *,
        branch: str,
        business_subprocess_succeeded: bool,
        observed_history_status: Optional[str] = None,
        exception_class: Optional[str] = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.branch = branch
        self.business_subprocess_succeeded = business_subprocess_succeeded
        self.observed_history_status = observed_history_status
        self.exception_class = exception_class

ECO_DRIVING_SCHEDULE_MODES = {
    "eco_driving_weekly_snapshot": {
        "mode": "weekly_cumulative_snapshot",
        "include_weekly": True,
        "include_monthly": False,
    },
    "eco_driving_month_end_weekly_snapshot": {
        "mode": "final_month_weekly_snapshot",
        "include_weekly": True,
        "include_monthly": False,
    },
    "eco_driving_monthly_aggregation": {
        "mode": "monthly_full_aggregation",
        "include_weekly": False,
        "include_monthly": True,
    },
    "eco_person_driving_weekly_snapshot": {
        "mode": "weekly_cumulative_snapshot",
        "include_weekly": True,
        "include_monthly": False,
    },
    "eco_person_driving_month_end_weekly_snapshot": {
        "mode": "final_month_weekly_snapshot",
        "include_weekly": True,
        "include_monthly": False,
    },
    "eco_person_driving_monthly_aggregation": {
        "mode": "monthly_full_aggregation",
        "include_weekly": False,
        "include_monthly": True,
    },
    "eco_person_driving_weekly_email_notifications": {},
    "eco_person_driving_monthly_email_notifications": {},
}


# ---------------------------------------------------------------------------
# Helpers — DB
# ---------------------------------------------------------------------------

def _require_dependency(module: str, feature: str):
    try:
        return __import__(module)
    except ImportError as exc:
        raise RuntimeError(f"Missing dependency for {feature}: {module}") from exc


def _platform_pg_conn():
    psycopg = _require_dependency("psycopg", "Postgres connection")
    dsn = (
        f"host={os.getenv('POSTGRES_HOST', '127.0.0.1')} "
        f"port={os.getenv('POSTGRES_PORT', '5432')} "
        f"dbname={os.getenv('POSTGRES_DB', 'logdb')} "
        f"user={os.getenv('POSTGRES_USER', 'loguser')} "
        f"password={os.getenv('POSTGRES_PASSWORD', '')}"
    )
    return set_pg_session_timezone(psycopg.connect(dsn))


def _positive_int(value: Any, *, name: str) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if out <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return out


def _stale_running_timeout_minutes(params: dict) -> int:
    raw = params.get("stale_running_timeout_minutes")
    if raw in (None, ""):
        raw = os.getenv(STALE_RUNNING_TIMEOUT_ENV, str(DEFAULT_STALE_RUNNING_TIMEOUT_MINUTES))
    return _positive_int(raw, name="stale_running_timeout_minutes")


def _record_heartbeat(conn, *, component: str = "workflow_a.dispatcher") -> bool:
    """Stamp proof that this dispatcher tick reached the platform database.

    A successful no-work tick deliberately persists no `public.runs` row
    (`DEFERRED_RUN_CREATION`), so `runs` cannot distinguish an idle dispatcher
    from a dead one. This single-row upsert closes that gap for the missing-run
    watchdog and is intentionally taken *before* the advisory lock, so a tick
    that loses the lock still proves liveness.

    Never fails the tick: the heartbeat is observability, not scheduling.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ops_control.scheduler_heartbeat AS h
                    (component, last_beat_at, last_beat_detail, beat_count, updated_at)
                VALUES (%s, now(), %s::jsonb, 1, now())
                ON CONFLICT (component) DO UPDATE
                   SET last_beat_at = now(),
                       last_beat_detail = EXCLUDED.last_beat_detail,
                       beat_count = h.beat_count + 1,
                       updated_at = now()
                """,
                (component, json.dumps({"pid": os.getpid()})),
            )
        conn.commit()
        return True
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return False


def _try_acquire_dispatcher_lock(conn) -> bool:
    """Acquire a global Postgres advisory lock for the dispatcher session."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (DISPATCHER_ADVISORY_LOCK_KEY,))
        acquired = bool(cur.fetchone()[0])
    conn.commit()
    return acquired


def _release_dispatcher_lock(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_unlock(%s)", (DISPATCHER_ADVISORY_LOCK_KEY,))
    conn.commit()


# ---------------------------------------------------------------------------
# Schedule rows + evaluation (pure stdlib; testable without DB)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ScheduleRow:
    """In-memory view of a row from `client_dataset_schedule` JOIN ed with
    `client_account` and `dataset_registry`.
    """
    schedule_id: str
    client_id: str
    client_code: str
    client_name: str
    dataset_name: str
    job_module: str
    enabled: bool
    frequency: str            # 'daily' | 'weekly' | 'monthly'
    day_of_week: Optional[int]    # 0=Mon..6=Sun (matches Python weekday())
    day_of_month: Optional[int]   # 1..28 or None
    day_of_month_last: bool
    run_time: time            # naive local time in `timezone_name`
    timezone_name: str
    lookback_days: int
    overwrite_existing: bool
    event_enrichment_mode: str
    # Client-account scoped compatibility configuration (migrations 055/056).
    # Defaults reproduce today's behavior for any caller that does not supply
    # them. `strict_meta` remains the default here but is no longer what
    # production carries: every enabled trips client has been
    # `data_invariants_v1` since 2026-08-05.
    trips_pagination_mode: str = TRIPS_PAGINATION_MODE_DEFAULT
    trips_stabilization_delay_seconds: int = TRIPS_STABILIZATION_DELAY_SECONDS_DEFAULT
    trips_overlap_seconds: int = TRIPS_OVERLAP_SECONDS_DEFAULT
    trips_max_recovery_span_seconds: int = TRIPS_MAX_RECOVERY_SPAN_SECONDS_DEFAULT
    # M5 role discriminator (migration 062). `DAILY` is the base ingestion
    # schedule; `WEEKLY_RECONCILIATION`/`MONTHLY_RECONCILIATION` are the additive
    # reconciliation passes M6/M7 will register. Orthogonal to `frequency`: it
    # says what part this schedule plays, not how often it fires.
    #
    # The dispatcher carries it as evidence and deliberately does NOT branch on
    # it. Every enabled row is enumerated and dispatched exactly as before,
    # whatever its role — M5 makes reconciliation rows representable, and
    # filtering them out here would quietly un-make that.
    #
    # Placed LAST on purpose. Existing callers construct `ScheduleRow`
    # positionally, so inserting a field anywhere earlier would silently rebind
    # their arguments — a defaulted field appended at the end is the only
    # addition that cannot change what an existing positional call means.
    run_type: str = SCHEDULE_RUN_TYPE_BASE


@dataclass
class PreparedDispatcherRun:
    conn: Any
    schedule: Optional[ScheduleRow]
    fire_utc: Optional[datetime]
    window_start: Optional[datetime]
    window_end: Optional[datetime]
    run_history_id: Optional[str]
    stale_rows: List[Dict[str, Any]]
    stale_after_minutes: int
    released: bool = False
    # Nominal `[F − L, F]` regardless of mode. In `strict_meta` it equals
    # `window_start`/`window_end`; in compatibility mode those carry the
    # effective execution window instead.
    nominal_window_start: Optional[datetime] = None
    nominal_window_end: Optional[datetime] = None
    # Populated only for a compatibility-mode `trips_sync` fire.
    coverage_state: Optional[CoverageState] = None
    gate_result: Optional[CoverageGateResult] = None


def _last_day_of_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def latest_scheduled_fire_local(
    *, now_local: datetime, sched: ScheduleRow,
) -> Optional[datetime]:
    """Compute the latest scheduled fire time at or before `now_local`.

    Returns a tz-aware datetime in the **same timezone as `now_local`**, or
    None if no fire is computable (unsupported frequency or missing fields).

    The "latest fire <= now" semantics is intentional: the dispatcher does
    not enumerate historical fires it might have missed (no catch-up). It
    only ever considers the most recent one. Re-firing the same logical
    timestamp is prevented by the UNIQUE (schedule_id, scheduled_fire_ts)
    constraint on `client_schedule_run_history`.
    """
    tz = now_local.tzinfo
    today_local = now_local.date()
    rt = sched.run_time

    if sched.frequency == "daily":
        candidate_today = datetime.combine(today_local, rt, tzinfo=tz)
        if now_local >= candidate_today:
            return candidate_today
        return datetime.combine(today_local - timedelta(days=1), rt, tzinfo=tz)

    if sched.frequency == "weekly":
        if sched.day_of_week is None:
            return None
        for offset in range(0, 8):
            d = today_local - timedelta(days=offset)
            if d.weekday() != sched.day_of_week:
                continue
            cand = datetime.combine(d, rt, tzinfo=tz)
            if cand <= now_local:
                return cand
        return None

    if sched.frequency == "monthly":
        for back in range(0, 13):
            year = today_local.year
            month = today_local.month - back
            while month <= 0:
                month += 12
                year -= 1
            if sched.day_of_month_last:
                day = _last_day_of_month(year, month)
            elif sched.day_of_month is not None:
                day = sched.day_of_month
                if day > _last_day_of_month(year, month):
                    continue
            else:
                return None
            cand = datetime.combine(date(year, month, day), rt, tzinfo=tz)
            if cand <= now_local:
                return cand
        return None

    return None


def evaluate_schedule(
    *, now_utc: datetime, sched: ScheduleRow,
) -> Optional[Tuple[datetime, datetime, datetime]]:
    """Return (scheduled_fire_utc, window_start_utc, window_end_utc) when due.

    "Due" means the latest scheduled fire is at or before `now_utc`. The
    caller still has to check the dispatcher run-history before claiming —
    re-fires of the same logical timestamp are prevented by the UNIQUE
    (schedule_id, scheduled_fire_ts) constraint.

    Returns None if:
      * the timezone is unknown,
      * the frequency is unsupported,
      * the latest fire would be in the future (not due yet).
    """
    try:
        tz = ZoneInfo(sched.timezone_name)
    except ZoneInfoNotFoundError:
        return None
    now_local = now_utc.astimezone(tz)
    fire_local = latest_scheduled_fire_local(now_local=now_local, sched=sched)
    if fire_local is None:
        return None
    fire_utc = fire_local.astimezone(timezone.utc)
    if fire_utc > now_utc:
        return None
    window_end = fire_utc
    window_start = window_end - timedelta(days=max(sched.lookback_days, 0))
    return (fire_utc, window_start, window_end)


# ---------------------------------------------------------------------------
# Selection: order due schedules deterministically
# ---------------------------------------------------------------------------

def select_next_due(
    *, now_utc: datetime, schedules: List[ScheduleRow],
) -> List[Tuple[ScheduleRow, datetime, datetime, datetime]]:
    """Return the queue of due schedules, deterministically ordered.

    Pure function — no DB. Useful for testing. The dispatcher applies the
    Python registry allowlist BEFORE calling this; rows the registry does
    not know are filtered out by `_filter_known(...)`.

    Sort key: (scheduled_fire_ts ASC, client_code ASC, dataset_name ASC).
    """
    out: List[Tuple[ScheduleRow, datetime, datetime, datetime]] = []
    for s in schedules:
        ev = evaluate_schedule(now_utc=now_utc, sched=s)
        if ev is None:
            continue
        fire_utc, win_start, win_end = ev
        out.append((s, fire_utc, win_start, win_end))
    out.sort(key=lambda t: (t[1], t[0].client_code or "", t[0].dataset_name))
    return out


# ---------------------------------------------------------------------------
# Registry validation
# ---------------------------------------------------------------------------

def _validate_against_registry(s: ScheduleRow) -> Optional[str]:
    """Return None if the row is registry-valid, else a human-readable reason.

    Two checks:
      * `dataset_name` must exist in the Python registry.
      * The `job_module` stored in the DB must equal the registry's
        `job_module`. We never execute an arbitrary DB-supplied module.
    """
    ds = registry.DATASETS.get(s.dataset_name)
    if ds is None:
        return f"unknown dataset {s.dataset_name!r} (not in jobs/api/telematics/registry.py)"
    if ds.job_module != s.job_module:
        return (
            f"dataset {s.dataset_name!r} job_module mismatch "
            f"(db={s.job_module!r}, py={ds.job_module!r})"
        )
    if s.event_enrichment_mode not in SUPPORTED_EVENT_ENRICHMENT_MODES:
        return (
            f"dataset {s.dataset_name!r} has invalid event_enrichment_mode "
            f"{s.event_enrichment_mode!r}"
        )
    return None


# ---------------------------------------------------------------------------
# DB queries — schedules + run-history
# ---------------------------------------------------------------------------

def _count_running(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM workflow_a_control.client_schedule_run_history "
            "WHERE status = 'RUNNING'"
        )
        return int(cur.fetchone()[0])


def _mark_stale_running(
    conn, *, stale_after_minutes: int, now_utc: datetime,
) -> List[Dict[str, Any]]:
    cutoff = now_utc - timedelta(minutes=stale_after_minutes)
    error_summary = (
        "stale RUNNING auto-failed by dispatcher: "
        f"no completion after {stale_after_minutes} minutes"
    )
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE workflow_a_control.client_schedule_run_history
               SET status='FAILED',
                   finished_at=%s,
                   error_summary=%s
             WHERE status='RUNNING'
               AND COALESCE(started_at, created_at) < %s
             RETURNING
               run_history_id::text,
               schedule_id::text,
               client_id::text,
               client_code,
               dataset_name,
               scheduled_fire_ts,
               started_at,
               created_at
            """,
            (now_utc, error_summary, cutoff),
        )
        rows = cur.fetchall()
    conn.commit()

    out: List[Dict[str, Any]] = []
    for row in rows:
        # psycopg row shape differs between tuple and dict row factories; support both.
        if isinstance(row, dict):
            out.append(dict(row))
        else:
            out.append({
                "run_history_id": row[0],
                "schedule_id": row[1],
                "client_id": row[2],
                "client_code": row[3],
                "dataset_name": row[4],
                "scheduled_fire_ts": row[5],
                "started_at": row[6],
                "created_at": row[7],
            })
    return out


def _load_enabled_schedules(conn) -> List[ScheduleRow]:
    psycopg = _require_dependency("psycopg", "Postgres connection")
    from psycopg.rows import dict_row

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT
              cds.schedule_id::text         AS schedule_id,
              ca.client_id::text            AS client_id,
              COALESCE(cds.client_code, ca.client_code, '') AS client_code,
              ca.client_name                AS client_name,
              cds.dataset_name              AS dataset_name,
              dr.job_module                 AS job_module,
              cds.enabled                   AS enabled,
              cds.frequency                 AS frequency,
              cds.day_of_week               AS day_of_week,
              cds.day_of_month              AS day_of_month,
              COALESCE(cds.day_of_month_last, false) AS day_of_month_last,
              cds.run_type                  AS run_type,
              cds.run_time                  AS run_time,
              cds.timezone                  AS timezone_name,
              cds.lookback_days             AS lookback_days,
              cds.overwrite_existing        AS overwrite_existing,
              cds.event_enrichment_mode     AS event_enrichment_mode,
              ca.trips_pagination_mode      AS trips_pagination_mode,
              ca.trips_stabilization_delay_seconds
                                            AS trips_stabilization_delay_seconds,
              ca.trips_overlap_seconds      AS trips_overlap_seconds,
              ca.trips_max_recovery_span_seconds
                                            AS trips_max_recovery_span_seconds
            FROM workflow_a_control.client_dataset_schedule cds
            JOIN workflow_a_control.client_account ca
              ON ca.client_id = cds.client_id
            JOIN workflow_a_control.dataset_registry dr
              ON dr.dataset_name = cds.dataset_name
            WHERE cds.enabled = true
              AND ca.enabled  = true
            """
        )
        rows: List[ScheduleRow] = []
        for r in cur.fetchall():
            rt = r["run_time"]
            if not isinstance(rt, time):
                rt = time.fromisoformat(str(rt))
            # Fail closed on drift: an unknown mode or an out-of-contract
            # numeric raises here, exactly like an unknown dataset, instead of
            # silently defaulting a client into or out of compatibility mode.
            stabilization = validate_trips_stabilization_config(
                stabilization_delay_seconds=r["trips_stabilization_delay_seconds"],
                overlap_seconds=r["trips_overlap_seconds"],
                max_recovery_span_seconds=r["trips_max_recovery_span_seconds"],
            )
            rows.append(ScheduleRow(
                schedule_id=r["schedule_id"],
                client_id=r["client_id"],
                client_code=r["client_code"] or "",
                client_name=r["client_name"] or "",
                dataset_name=r["dataset_name"],
                job_module=r["job_module"],
                enabled=bool(r["enabled"]),
                frequency=r["frequency"],
                day_of_week=r["day_of_week"],
                day_of_month=r["day_of_month"],
                day_of_month_last=bool(r["day_of_month_last"]),
                run_type=validate_schedule_run_type(r["run_type"]),
                run_time=rt,
                timezone_name=r["timezone_name"],
                lookback_days=int(r["lookback_days"]),
                overwrite_existing=bool(r["overwrite_existing"]),
                event_enrichment_mode=str(r["event_enrichment_mode"] or "enabled").strip().lower(),
                trips_pagination_mode=normalize_trips_pagination_mode(
                    r["trips_pagination_mode"]
                ),
                trips_stabilization_delay_seconds=stabilization[0],
                trips_overlap_seconds=stabilization[1],
                trips_max_recovery_span_seconds=stabilization[2],
            ))
        return rows


def _is_compatibility_trips_fire(schedule: ScheduleRow) -> bool:
    """True only for a `trips_sync` schedule of a `data_invariants_v1` client.

    Every other combination — every non-`trips_sync` dataset and every
    `strict_meta` client — takes the pre-C5 path unchanged: no coverage read, no
    gate, no effective window and no evidence columns (docs/14 §7.1
    strict-mode short-circuit).
    """
    return (
        schedule.dataset_name == TRIPS_SYNC_DATASET_NAME
        and schedule.trips_pagination_mode == TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1
    )


def _load_coverage_state(
    conn, *, client_id: str, dataset_name: str,
) -> Optional[CoverageState]:
    """Read at most one coverage row for exactly this dataset. Read-only.

    Narrow by design (docs/14 §7.1 step 2): keyed on the authoritative coverage
    owner `(client_id, dataset_name)` since M5, explicit column list, name-based
    row mapping, no fallback to another dataset, no row creation and no row
    lock. Zero rows is a normal answer meaning "no claim exists" and is
    reported to the gate, which refuses it.

    **Every cadence over one dataset reads the same row here.** That is the M5
    contract: the watermark is a fact about the dataset, so a base fire and a
    future reconciliation fire must see one truth, not two. The row's own
    `schedule_id` is returned as provenance and is deliberately not compared
    against the firing schedule (see `evaluate_coverage_gate`).

    **Provenance coherence is re-checked here, fail-closed.** The row is joined to
    its anchoring schedule and that schedule must belong to the same owner *and*
    carry the base role. Migration 062's composite foreign key already makes the
    owner half structurally impossible to violate, so this is defence in depth
    rather than the primary control — but the base-role half is enforced at the
    coverage INSERT surface rather than by a constraint (a partial unique index
    cannot be a foreign-key target), and a fire must not run against a watermark
    anchored to a reconciliation cadence. Returning `None` routes that state
    through the existing `TRIPS_COVERAGE_BOOTSTRAP_REQUIRED` refusal, which is
    the correct classification: the coverage claim is not usable and a reviewed
    bootstrap owns fixing it.

    A duplicate row shape cannot exist under
    `uq_client_dataset_coverage_dataset UNIQUE (client_id, dataset_name)`; if the
    database ever returns one, that is corruption and the tick stops rather than
    guessing which row is authoritative.
    """
    _require_dependency("psycopg", "Postgres connection")
    from psycopg.rows import dict_row

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT
              c.schedule_id::text      AS schedule_id,
              c.client_id::text        AS client_id,
              c.client_code            AS client_code,
              c.dataset_name           AS dataset_name,
              c.coverage_start_ts      AS coverage_start_ts,
              c.covered_through_ts     AS covered_through_ts,
              c.bootstrap_status       AS bootstrap_status,
              c.bootstrap_evidence_ref AS bootstrap_evidence_ref,
              c.seeded_at              AS seeded_at,
              c.seeded_by              AS seeded_by,
              c.covered_through_source AS covered_through_source,
              c.last_gap_detected_ts AS last_gap_detected_ts,
              (s.schedule_id IS NOT NULL) AS provenance_is_base_owner
            FROM workflow_a_control.client_dataset_coverage AS c
            LEFT JOIN workflow_a_control.client_dataset_schedule AS s
              ON  s.schedule_id  = c.schedule_id
              AND s.client_id    = c.client_id
              AND s.dataset_name = c.dataset_name
              AND s.run_type     = %s
            WHERE c.client_id = %s
              AND c.dataset_name = %s
            """,
            (SCHEDULE_RUN_TYPE_BASE, client_id, dataset_name),
        )
        rows = cur.fetchall()

    if not rows:
        return None
    if len(rows) > 1:
        raise RuntimeError(
            "workflow_a_control.client_dataset_coverage returned "
            f"{len(rows)} rows for client_id={client_id} "
            f"dataset_name={dataset_name}; expected at most one"
        )
    row = rows[0]
    if not row["provenance_is_base_owner"]:
        # The watermark is not anchored to a base schedule of this owner. Report
        # "no usable claim" rather than running against it — which is the honest
        # classification, not a euphemism: a coverage row whose anchor is foreign
        # or is a reconciliation cadence is not a claim this fire may rely on,
        # and `TRIPS_COVERAGE_BOOTSTRAP_REQUIRED` names exactly the reviewed
        # operation that owns fixing it. This function stays a pure read and
        # raises nothing, because one client's incoherent row must refuse that
        # client's fire, not abort the whole tick.
        return None
    return CoverageState(
        schedule_id=row["schedule_id"],
        client_id=row["client_id"],
        client_code=row["client_code"],
        dataset_name=row["dataset_name"],
        coverage_start_ts=row["coverage_start_ts"],
        covered_through_ts=row["covered_through_ts"],
        bootstrap_status=row["bootstrap_status"],
        bootstrap_evidence_ref=row["bootstrap_evidence_ref"],
        seeded_at=row["seeded_at"],
        seeded_by=row["seeded_by"],
        covered_through_source=row["covered_through_source"],
        last_gap_detected_ts=row["last_gap_detected_ts"],
    )


def _claim_fire(
    conn, *, schedule_id: str, client_id: str, client_code: str, dataset_name: str,
    scheduled_fire_ts: datetime, window_start_ts: datetime,
    window_end_ts: datetime,
    nominal_window_start_ts: Optional[datetime] = None,
    nominal_window_end_ts: Optional[datetime] = None,
    stabilization_delay_seconds: Optional[int] = None,
    overlap_seconds: Optional[int] = None,
    trips_pagination_mode: Optional[str] = None,
) -> Optional[str]:
    """Insert a RUNNING claim for (schedule_id, scheduled_fire_ts).

    Returns `run_history_id` on a fresh claim, or None if the row already
    exists (prior SUCCESS/FAILED, or another RUNNING from a previous tick
    that crashed before finalizing). The UNIQUE constraint is the source
    of truth for de-dup; the count-RUNNING gate above is the strict-queue
    enforcement.

    `window_start_ts` / `window_end_ts` keep their existing meaning: the window
    the job is actually asked to fetch. For a compatibility fire that is the
    effective window; for every strict fire, and for a rejected compatibility
    fire, it is the nominal window (docs/13 §14.1, docs/14 §7.1 step 3).

    The five evidence columns are written **once**, here, in the same INSERT
    that creates the RUNNING row, and are never edited afterwards — the
    configuration they record is mutable and would not be reconstructable from a
    terminal row. They stay NULL for every strict fire and for every historical
    row; nothing backfills them.
    """
    started = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO workflow_a_control.client_schedule_run_history
              (schedule_id, client_id, client_code, dataset_name,
               window_start_ts, window_end_ts, scheduled_fire_ts,
               status, started_at,
               nominal_window_start_ts, nominal_window_end_ts,
               stabilization_delay_seconds, overlap_seconds,
               trips_pagination_mode)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'RUNNING', %s,
                    %s, %s, %s, %s, %s)
            ON CONFLICT (schedule_id, scheduled_fire_ts) DO NOTHING
            RETURNING run_history_id::text
            """,
            (schedule_id, client_id, client_code or None, dataset_name,
             window_start_ts, window_end_ts, scheduled_fire_ts, started,
             nominal_window_start_ts, nominal_window_end_ts,
             stabilization_delay_seconds, overlap_seconds,
             trips_pagination_mode),
        )
        row = cur.fetchone()
    conn.commit()
    return row[0] if row else None


def _finalize_run(
    conn, *, run_history_id: str, status: str, error: Optional[str],
) -> None:
    finished = datetime.now(timezone.utc)
    err_trim = (error or "")[-4000:] if error else None
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE workflow_a_control.client_schedule_run_history
               SET status        = %s,
                   finished_at   = %s,
                   error_summary = %s
             WHERE run_history_id = %s
            """,
            (status, finished, err_trim, run_history_id),
        )
    conn.commit()



# ---------------------------------------------------------------------------
# C6 — compatibility-only coverage/history finalization
# ---------------------------------------------------------------------------

_COVERAGE_TIMESTAMP_FIELDS = frozenset({
    "coverage_start_ts",
    "covered_through_ts",
    "seeded_at",
    "last_gap_detected_ts",
    "updated_at",
})
_COVERAGE_EXACT_FIELDS = (
    "schedule_id",
    "client_id",
    "client_code",
    "dataset_name",
    "coverage_start_ts",
    "covered_through_ts",
    "bootstrap_status",
    "bootstrap_evidence_ref",
    "covered_through_source",
    "seeded_at",
    "seeded_by",
    "last_gap_detected_ts",
    "updated_at",
)


def _utc_preserving_precision(value: object) -> object:
    if value is None:
        return None
    if not isinstance(value, datetime) or value.utcoffset() is None:
        return value
    return value.astimezone(timezone.utc)


def _claim_coverage_params(state: CoverageState) -> Dict[str, Any]:
    """Bind approved CAS values only from the retained pre-claim object."""
    return {
        "claim_schedule_id": state.schedule_id,
        "claim_client_id": state.client_id,
        "claim_dataset_name": state.dataset_name,
        "claim_coverage_start_ts": _utc_preserving_precision(
            state.coverage_start_ts
        ),
        "claim_covered_through_ts": _utc_preserving_precision(
            state.covered_through_ts
        ),
        "claim_bootstrap_status": state.bootstrap_status,
        "claim_bootstrap_evidence_ref": state.bootstrap_evidence_ref,
        "claim_covered_through_source": state.covered_through_source,
        "claim_seeded_at": _utc_preserving_precision(state.seeded_at),
        "claim_seeded_by": state.seeded_by,
        "claim_last_gap_detected_ts": _utc_preserving_precision(
            state.last_gap_detected_ts
        ),
    }


def _coverage_row_matches_claim(
    row: Dict[str, Any], state: CoverageState,
) -> bool:
    expected = {
        "schedule_id": str(state.schedule_id),
        "client_id": str(state.client_id),
        "dataset_name": state.dataset_name,
        "coverage_start_ts": state.coverage_start_ts,
        "covered_through_ts": state.covered_through_ts,
        "bootstrap_status": state.bootstrap_status,
        "bootstrap_evidence_ref": state.bootstrap_evidence_ref,
        "covered_through_source": state.covered_through_source,
        "seeded_at": state.seeded_at,
        "seeded_by": state.seeded_by,
        "last_gap_detected_ts": state.last_gap_detected_ts,
    }
    for field, expected_value in expected.items():
        observed = row.get(field)
        if field in _COVERAGE_TIMESTAMP_FIELDS:
            if _utc_preserving_precision(observed) != _utc_preserving_precision(
                expected_value
            ):
                return False
        elif field in {"schedule_id", "client_id"}:
            if str(observed) != str(expected_value):
                return False
        elif observed != expected_value:
            return False
    return True


def _coverage_rows_equal(
    left: Optional[Dict[str, Any]], right: Optional[Dict[str, Any]],
) -> bool:
    if left is None or right is None:
        return left is right
    for field in _COVERAGE_EXACT_FIELDS:
        left_value = left.get(field)
        right_value = right.get(field)
        if field in _COVERAGE_TIMESTAMP_FIELDS:
            if _utc_preserving_precision(left_value) != _utc_preserving_precision(
                right_value
            ):
                return False
        elif field in {"schedule_id", "client_id"}:
            if str(left_value) != str(right_value):
                return False
        elif left_value != right_value:
            return False
    return True


def _history_row_matches_prepared(
    row: Dict[str, Any], prepared: PreparedDispatcherRun,
) -> bool:
    schedule = prepared.schedule
    return bool(
        schedule is not None
        and str(row.get("run_history_id")) == str(prepared.run_history_id)
        and str(row.get("schedule_id")) == str(schedule.schedule_id)
        and row.get("dataset_name") == schedule.dataset_name
        and _utc_preserving_precision(row.get("window_start_ts"))
        == _utc_preserving_precision(prepared.window_start)
        and _utc_preserving_precision(row.get("window_end_ts"))
        == _utc_preserving_precision(prepared.window_end)
        and _utc_preserving_precision(row.get("scheduled_fire_ts"))
        == _utc_preserving_precision(prepared.fire_utc)
    )


def _bounded_exception_class(exc: BaseException) -> str:
    return type(exc).__name__[:120]


def _new_finalization_error(
    code: str,
    *,
    branch: str,
    business_subprocess_succeeded: bool,
    observed_history_status: Optional[str] = None,
    exception_class: Optional[str] = None,
) -> CoverageFinalizationError:
    return CoverageFinalizationError(
        code,
        branch=branch,
        business_subprocess_succeeded=business_subprocess_succeeded,
        observed_history_status=(
            str(observed_history_status)[:40]
            if observed_history_status is not None else None
        ),
        exception_class=(
            str(exception_class)[:120] if exception_class is not None else None
        ),
    )


def _observed_history_status(conn, *, run_history_id: str) -> Optional[str]:
    """Read bounded claim-loss evidence after the mutating transaction rolls back."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT status
                  FROM workflow_a_control.client_schedule_run_history
                 WHERE run_history_id = %s
                """,
                (run_history_id,),
            )
            row = cur.fetchone()
        conn.rollback()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return None
    if not row:
        return None
    return str(row[0] if not isinstance(row, dict) else row.get("status"))


def _terminal_failure_after_rollback(
    *,
    primary_code: str,
    branch: str,
    run_history_id: str,
    business_subprocess_succeeded: bool,
) -> CoverageFinalizationError:
    """Finalize only a freshly locked RUNNING claim in a separate transaction."""
    conn = None
    try:
        conn = _platform_pg_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT status
                  FROM workflow_a_control.client_schedule_run_history
                 WHERE run_history_id = %s
                 FOR UPDATE
                """,
                (run_history_id,),
            )
            row = cur.fetchone()
            observed = None if not row else str(
                row[0] if not isinstance(row, dict) else row.get("status")
            )
            if observed != "RUNNING":
                conn.rollback()
                return _new_finalization_error(
                    TRIPS_HISTORY_CLAIM_LOST,
                    branch=branch,
                    business_subprocess_succeeded=business_subprocess_succeeded,
                    observed_history_status=observed,
                )
            cur.execute(
                """
                UPDATE workflow_a_control.client_schedule_run_history
                   SET status = 'FAILED',
                       finished_at = %s,
                       error_summary = %s
                 WHERE run_history_id = %s
                   AND status = 'RUNNING'
                """,
                (
                    datetime.now(timezone.utc),
                    primary_code[:4000],
                    run_history_id,
                ),
            )
            if cur.rowcount != 1:
                conn.rollback()
                return _new_finalization_error(
                    TRIPS_HISTORY_CLAIM_LOST,
                    branch=branch,
                    business_subprocess_succeeded=business_subprocess_succeeded,
                )
        try:
            conn.commit()
        except Exception as commit_exc:
            try:
                conn.rollback()
            except Exception:
                pass
            verify = None
            try:
                verify = _platform_pg_conn()
                with verify.cursor() as cur:
                    cur.execute("BEGIN READ ONLY")
                    cur.execute(
                        """
                        SELECT status, error_summary
                          FROM workflow_a_control.client_schedule_run_history
                         WHERE run_history_id = %s
                        """,
                        (run_history_id,),
                    )
                    checked = cur.fetchone()
                verify.rollback()
                if checked:
                    status = checked[0] if not isinstance(checked, dict) else checked.get("status")
                    summary = checked[1] if not isinstance(checked, dict) else checked.get("error_summary")
                    if status == "FAILED" and summary == primary_code:
                        return _new_finalization_error(
                            primary_code,
                            branch=branch,
                            business_subprocess_succeeded=business_subprocess_succeeded,
                        )
                    if status != "RUNNING":
                        return _new_finalization_error(
                            TRIPS_HISTORY_CLAIM_LOST,
                            branch=branch,
                            business_subprocess_succeeded=business_subprocess_succeeded,
                            observed_history_status=str(status),
                        )
            except Exception:
                pass
            finally:
                if verify is not None:
                    try:
                        verify.close()
                    except Exception:
                        pass
            return _new_finalization_error(
                TRIPS_COVERAGE_COMMIT_RECONCILIATION_UNAVAILABLE,
                branch=branch,
                business_subprocess_succeeded=business_subprocess_succeeded,
                exception_class=_bounded_exception_class(commit_exc),
            )
        return _new_finalization_error(
            primary_code,
            branch=branch,
            business_subprocess_succeeded=business_subprocess_succeeded,
        )
    except CoverageFinalizationError:
        raise
    except Exception as exc:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        return _new_finalization_error(
            TRIPS_COVERAGE_COMMIT_RECONCILIATION_UNAVAILABLE,
            branch=branch,
            business_subprocess_succeeded=business_subprocess_succeeded,
            exception_class=_bounded_exception_class(exc),
        )
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _coverage_from_reconciliation(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if row.get("coverage_schedule_id") is None:
        return None
    return {
        "schedule_id": row.get("coverage_schedule_id"),
        "client_id": row.get("coverage_client_id"),
        "client_code": row.get("coverage_client_code"),
        "dataset_name": row.get("coverage_dataset_name"),
        "coverage_start_ts": row.get("coverage_start_ts"),
        "covered_through_ts": row.get("covered_through_ts"),
        "bootstrap_status": row.get("bootstrap_status"),
        "bootstrap_evidence_ref": row.get("bootstrap_evidence_ref"),
        "covered_through_source": row.get("covered_through_source"),
        "seeded_at": row.get("seeded_at"),
        "seeded_by": row.get("seeded_by"),
        "last_gap_detected_ts": row.get("last_gap_detected_ts"),
        "updated_at": row.get("coverage_updated_at"),
    }


def _reconcile_compat_commit(
    *,
    branch: str,
    run_history_id: str,
    original_coverage: Dict[str, Any],
    expected_coverage: Dict[str, Any],
    original_history: Dict[str, Any],
    expected_history_status: str,
    expected_error_summary: Optional[str],
    business_subprocess_succeeded: bool,
) -> None:
    """Classify an uncertain atomic COMMIT from one fresh read-only snapshot."""
    conn = None
    try:
        conn = _platform_pg_conn()
        from psycopg.rows import dict_row

        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            cur.execute(
                """
                SELECT h.run_history_id::text AS run_history_id,
                       h.status, h.error_summary, h.finished_at,
                       h.schedule_id::text AS schedule_id,
                       h.dataset_name, h.window_start_ts, h.window_end_ts,
                       h.scheduled_fire_ts,
                       c.schedule_id::text AS coverage_schedule_id,
                       c.client_id::text AS coverage_client_id,
                       c.client_code AS coverage_client_code,
                       c.dataset_name AS coverage_dataset_name,
                       c.coverage_start_ts, c.covered_through_ts,
                       c.bootstrap_status, c.bootstrap_evidence_ref,
                       c.covered_through_source, c.seeded_at, c.seeded_by,
                       c.last_gap_detected_ts,
                       c.updated_at AS coverage_updated_at
                  FROM workflow_a_control.client_schedule_run_history AS h
                  LEFT JOIN workflow_a_control.client_dataset_coverage AS c
                    ON c.client_id = h.client_id
                   AND c.dataset_name = h.dataset_name
                 WHERE h.run_history_id = %s
                """,
                (run_history_id,),
            )
            rows = cur.fetchall()
        conn.rollback()
    except Exception as exc:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        raise _new_finalization_error(
            TRIPS_COVERAGE_COMMIT_RECONCILIATION_UNAVAILABLE,
            branch=branch,
            business_subprocess_succeeded=business_subprocess_succeeded,
            exception_class=_bounded_exception_class(exc),
        ) from exc
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    if len(rows) > 1:
        raise _new_finalization_error(
            TRIPS_COVERAGE_ATOMIC_STATE_DIVERGENCE,
            branch=branch,
            business_subprocess_succeeded=business_subprocess_succeeded,
        )
    if not rows:
        raise _new_finalization_error(
            TRIPS_HISTORY_CLAIM_LOST,
            branch=branch,
            business_subprocess_succeeded=business_subprocess_succeeded,
        )

    row = dict(rows[0])
    coverage = _coverage_from_reconciliation(row)
    history_identity_matches = all((
        str(row.get("run_history_id")) == str(original_history.get("run_history_id")),
        str(row.get("schedule_id")) == str(original_history.get("schedule_id")),
        row.get("dataset_name") == original_history.get("dataset_name"),
        _utc_preserving_precision(row.get("window_start_ts"))
        == _utc_preserving_precision(original_history.get("window_start_ts")),
        _utc_preserving_precision(row.get("window_end_ts"))
        == _utc_preserving_precision(original_history.get("window_end_ts")),
        _utc_preserving_precision(row.get("scheduled_fire_ts"))
        == _utc_preserving_precision(original_history.get("scheduled_fire_ts")),
    ))
    if (
        history_identity_matches
        and row.get("status") == expected_history_status
        and row.get("error_summary") == expected_error_summary
        and _coverage_rows_equal(coverage, expected_coverage)
    ):
        return

    coverage_is_original = _coverage_rows_equal(coverage, original_coverage)
    if history_identity_matches and row.get("status") == "RUNNING" and coverage_is_original:
        raise _terminal_failure_after_rollback(
            primary_code=TRIPS_COVERAGE_FINALIZATION_COMMIT_FAILED,
            branch=branch,
            run_history_id=run_history_id,
            business_subprocess_succeeded=business_subprocess_succeeded,
        )
    if history_identity_matches and row.get("status") != "RUNNING" and coverage_is_original:
        raise _new_finalization_error(
            TRIPS_HISTORY_CLAIM_LOST,
            branch=branch,
            business_subprocess_succeeded=business_subprocess_succeeded,
            observed_history_status=str(row.get("status")),
        )
    raise _new_finalization_error(
        TRIPS_COVERAGE_ATOMIC_STATE_DIVERGENCE,
        branch=branch,
        business_subprocess_succeeded=business_subprocess_succeeded,
        observed_history_status=str(row.get("status")),
    )


def _project_provider_request_log(
    cur,
    *,
    prepared: PreparedDispatcherRun,
    schedule: ScheduleRow,
    platform_run_id: str,
    completeness: WindowCompleteness,
) -> int:
    """Promote this execution's request facts to coverage evidence.

    Hinge 2 of the two-hinge model (`docs/20` §4.3a D3): this runs on the
    caller's cursor, inside `_finalize_compat_success`'s existing transaction,
    alongside the coverage CAS — one connection, one commit, one rollback. The
    invariant that produces is the point of M4:

        the watermark cannot commit without the durable completeness evidence
        that supports it, and that evidence cannot commit without the watermark.

    **It promotes; it does not insert.** The rows already exist, written PENDING
    by the child before its business transaction committed — which is what makes
    an immutable `client_trips.first_seen_request_id` resolvable even when this
    step fails (`docs/20` §22.4). What happens here is the transition from *this
    request happened* to *this request belongs to a fire whose window was
    verified exactly tiled and complete*.

    That distinction is load-bearing, so it is enforced three ways: the UPDATE
    matches `status = 'PENDING'` only, migration 061 forbids a PENDING row from
    carrying any fire attribution or completeness claim, and the row count must
    equal the proof exactly.

    **Every identity column is bound from the dispatcher's own claim**, never
    from the child's payload: `client_id`, `client_code`, `schedule_id`,
    `dataset_name` and `run_history_id` come from `prepared` and `schedule`, and
    are written *here*, at promotion. The payload contributed only
    provider-observed facts, whose window has already been cross-checked against
    this same claim by `verify_outcome` and `verify_window_completeness`. A
    forged payload therefore cannot attribute evidence to another client,
    schedule or fire.

    A mismatch — a missing PENDING row, a row already finalized by another fire,
    a duplicate — yields a row count other than the expected one and refuses.
    Refusing is what makes the promotion idempotent in the direction that
    matters: a second attempt on already-finalized rows promotes nothing and
    cannot silently re-advance coverage.

    It never commits and never rolls back: the caller owns the boundary, exactly
    as `advance_covered_through_cas` does.
    """
    expected: Dict[str, Tuple[Any, ...]] = {}
    for sub in completeness.subwindows:
        for page in sub.pages:
            expected[page.request_id] = (
                sub.complete, sub.termination_reason, sub.total_reconciliation,
            )
    if not expected:
        # Unreachable: `verify_window_completeness` refuses a tiling with no
        # unit, and a COMPLETE unit must carry at least one page. Kept as a
        # refusal rather than an assertion so a future edit cannot commit a
        # watermark backed by an empty projection.
        raise ValueError(
            f"{TRIPS_WINDOW_EVIDENCE_PROJECTION_EMPTY}: the verified "
            "completeness proof projected no durable evidence rows; a "
            "watermark must not commit without them"
        )

    finalized_at = datetime.now(timezone.utc).replace(microsecond=0)
    promoted = 0
    for request_id, (complete, termination, reconciliation) in expected.items():
        cur.execute(
            """
            UPDATE workflow_a_control.provider_request_log
               SET status = 'FINALIZED',
                   run_history_id = %(run_history_id)s,
                   client_id = %(client_id)s,
                   client_code = %(client_code)s,
                   schedule_id = %(schedule_id)s,
                   dataset_name = %(dataset_name)s,
                   subwindow_complete = %(subwindow_complete)s,
                   termination_reason = %(termination_reason)s,
                   total_reconciliation = %(total_reconciliation)s,
                   finalized_at = %(finalized_at)s
             WHERE request_id = %(request_id)s
               AND status = 'PENDING'
               AND platform_run_id = %(platform_run_id)s
            """,
            {
                "request_id": request_id,
                "platform_run_id": str(platform_run_id),
                "run_history_id": prepared.run_history_id,
                "client_id": schedule.client_id,
                "client_code": schedule.client_code or None,
                "schedule_id": schedule.schedule_id,
                "dataset_name": schedule.dataset_name,
                "subwindow_complete": complete,
                "termination_reason": termination,
                "total_reconciliation": reconciliation,
                "finalized_at": finalized_at,
            },
        )
        promoted += cur.rowcount

    if promoted != len(expected):
        # The proof names request identities this platform run did not durably
        # record as PENDING — a child that never wrote them, a payload naming
        # another run's requests, or a replay against rows already finalized.
        # Every one of those means the evidence backing this watermark is not
        # what it claims, so none of it may commit.
        raise ValueError(
            f"{TRIPS_WINDOW_EVIDENCE_PROJECTION_INCOMPLETE}: promoted "
            f"{promoted} of {len(expected)} request fact(s) for platform run "
            f"{platform_run_id}; a watermark must not commit on evidence that "
            "was not durably recorded by this execution"
        )
    return promoted


def _finalize_compat_success(
    conn, *, prepared: PreparedDispatcherRun,
    completeness: Optional[WindowCompleteness] = None,
    platform_run_id: Optional[str] = None,
) -> bool:
    """Atomically validate coverage and finalize an allowed rc==0 fire."""
    schedule = prepared.schedule
    state = prepared.coverage_state
    gate = prepared.gate_result
    if not (
        schedule is not None
        and _is_compatibility_trips_fire(schedule)
        and state is not None
        and gate is not None
        and gate.allowed
        and prepared.run_history_id
        and prepared.window_end is not None
        # M4: the finalizer will not open a transaction it cannot back with
        # durable evidence. Both are established by the gate above it, so a
        # missing one means a caller reached here without passing condition 6 —
        # refused before any coverage SQL, never defaulted away.
        and completeness is not None
        and platform_run_id
    ):
        raise ValueError("_finalize_compat_success requires an allowed compatibility claim")

    branch = "success"
    mutation_ts = datetime.now(timezone.utc).replace(microsecond=0)
    # The coverage CAS parameters are bound inside the shared finalizer from
    # this same retained claim-time snapshot; only the history statements below
    # still take parameters here.
    snapshot = CoverageClaimSnapshot.from_state(state)
    params = {
        "run_history_id": prepared.run_history_id,
        "finished_at": mutation_ts,
    }
    original_coverage: Optional[Dict[str, Any]] = None
    original_history: Optional[Dict[str, Any]] = None
    expected_coverage: Optional[Dict[str, Any]] = None
    moved = False
    try:
        from psycopg.rows import dict_row

        with conn.cursor(row_factory=dict_row) as cur:
            coverage_rows = lock_coverage_row_for_update(
                cur,
                client_id=snapshot.client_id,
                dataset_name=snapshot.dataset_name,
            )
            if len(coverage_rows) > 1:
                conn.rollback()
                raise _new_finalization_error(
                    TRIPS_COVERAGE_ATOMIC_STATE_DIVERGENCE,
                    branch=branch,
                    business_subprocess_succeeded=True,
                )
            if not coverage_rows or not snapshot_matches_row(
                dict(coverage_rows[0]), snapshot
            ):
                conn.rollback()
                raise _terminal_failure_after_rollback(
                    primary_code=TRIPS_COVERAGE_ADVANCE_CONFLICT,
                    branch=branch,
                    run_history_id=prepared.run_history_id,
                    business_subprocess_succeeded=True,
                )
            original_coverage = dict(coverage_rows[0])

            cur.execute(
                """
                SELECT run_history_id::text AS run_history_id, status,
                       schedule_id::text AS schedule_id, dataset_name,
                       window_start_ts, window_end_ts, scheduled_fire_ts
                  FROM workflow_a_control.client_schedule_run_history
                 WHERE run_history_id = %(run_history_id)s
                   AND status = 'RUNNING'
                 FOR UPDATE
                """,
                params,
            )
            history_rows = cur.fetchall()
            if len(history_rows) != 1:
                conn.rollback()
                observed = _observed_history_status(
                    conn, run_history_id=prepared.run_history_id,
                )
                raise _new_finalization_error(
                    TRIPS_HISTORY_CLAIM_LOST,
                    branch=branch,
                    business_subprocess_succeeded=True,
                    observed_history_status=observed,
                )
            original_history = dict(history_rows[0])
            if not _history_row_matches_prepared(original_history, prepared):
                conn.rollback()
                raise _new_finalization_error(
                    TRIPS_COVERAGE_ATOMIC_STATE_DIVERGENCE,
                    branch=branch,
                    business_subprocess_succeeded=True,
                )

            # M4 hinge 2. The durable evidence is written *before* the CAS in
            # the same transaction, so there is no ordering in which a watermark
            # becomes visible without it: either both commit or neither does.
            # A failure here raises and reaches the outer handler, which rolls
            # back and finalizes the fire FAILED with the watermark untouched.
            try:
                _project_provider_request_log(
                    cur,
                    prepared=prepared,
                    schedule=schedule,
                    platform_run_id=str(platform_run_id),
                    completeness=completeness,
                )
            except Exception as projection_exc:  # noqa: BLE001
                # Anything at all — a missing relation, a constraint violation
                # from a duplicate request identity, a serialization failure —
                # means the evidence is not durable, and the watermark must not
                # move. Classified distinctly so the refusal names the evidence
                # rather than the commit.
                conn.rollback()
                raise _terminal_failure_after_rollback(
                    primary_code=TRIPS_WINDOW_EVIDENCE_PROJECTION_FAILED,
                    branch=branch,
                    run_history_id=prepared.run_history_id,
                    business_subprocess_succeeded=True,
                ) from projection_exc

            effective_end = _utc_preserving_precision(prepared.window_end)
            expected_coverage = dict(original_coverage)
            try:
                advance = advance_covered_through_cas(
                    cur,
                    snapshot=snapshot,
                    candidate_covered_through_ts=effective_end,
                    source=COVERAGE_SOURCE_SCHEDULED_RUN,
                    mutation_ts=mutation_ts,
                )
            except CoverageCasConflict:
                conn.rollback()
                raise _terminal_failure_after_rollback(
                    primary_code=TRIPS_COVERAGE_ADVANCE_CONFLICT,
                    branch=branch,
                    run_history_id=prepared.run_history_id,
                    business_subprocess_succeeded=True,
                )
            moved = advance.moved
            if moved:
                expected_coverage["covered_through_ts"] = effective_end
                expected_coverage["covered_through_source"] = (
                    COVERAGE_SOURCE_SCHEDULED_RUN
                )
                expected_coverage["updated_at"] = mutation_ts

            cur.execute(
                """
                UPDATE workflow_a_control.client_schedule_run_history
                   SET status = 'SUCCESS',
                       finished_at = %(finished_at)s,
                       error_summary = NULL
                 WHERE run_history_id = %(run_history_id)s
                   AND status = 'RUNNING'
                """,
                params,
            )
            if cur.rowcount != 1:
                conn.rollback()
                raise _new_finalization_error(
                    TRIPS_HISTORY_CLAIM_LOST,
                    branch=branch,
                    business_subprocess_succeeded=True,
                )
        try:
            conn.commit()
        except Exception as commit_exc:
            try:
                conn.rollback()
            except Exception:
                pass
            assert original_coverage is not None
            assert original_history is not None
            assert expected_coverage is not None
            try:
                _reconcile_compat_commit(
                    branch=branch,
                    run_history_id=prepared.run_history_id,
                    original_coverage=original_coverage,
                    expected_coverage=expected_coverage,
                    original_history=original_history,
                    expected_history_status="SUCCESS",
                    expected_error_summary=None,
                    business_subprocess_succeeded=True,
                )
            except CoverageFinalizationError:
                raise
            except Exception as reconcile_exc:
                raise _new_finalization_error(
                    TRIPS_COVERAGE_COMMIT_RECONCILIATION_UNAVAILABLE,
                    branch=branch,
                    business_subprocess_succeeded=True,
                    exception_class=_bounded_exception_class(reconcile_exc),
                ) from reconcile_exc
            return moved
        return moved
    except CoverageFinalizationError:
        raise
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        raise _terminal_failure_after_rollback(
            primary_code=TRIPS_COVERAGE_FINALIZATION_COMMIT_FAILED,
            branch=branch,
            run_history_id=prepared.run_history_id,
            business_subprocess_succeeded=True,
        ) from exc


def _finalize_compat_gap(
    conn, *, prepared: PreparedDispatcherRun,
) -> None:
    """Persist a newly detected READY gap atomically with history FAILED."""
    schedule = prepared.schedule
    state = prepared.coverage_state
    gate = prepared.gate_result
    if not (
        schedule is not None
        and _is_compatibility_trips_fire(schedule)
        and state is not None
        and state.bootstrap_status == COVERAGE_STATUS_READY
        and gate is not None
        and not gate.allowed
        and gate.abort_code == TRIPS_COVERAGE_GAP_DETECTED
        and gate.requires_gap_persistence
        and prepared.run_history_id
    ):
        raise ValueError("_finalize_compat_gap requires a newly detected READY gap")

    branch = "gap"
    mutation_ts = datetime.now(timezone.utc).replace(microsecond=0)
    params = {
        **_claim_coverage_params(state),
        "run_history_id": prepared.run_history_id,
        "mutation_ts": mutation_ts,
        "finished_at": mutation_ts,
        "error_summary": TRIPS_COVERAGE_GAP_DETECTED,
    }
    original_coverage: Optional[Dict[str, Any]] = None
    original_history: Optional[Dict[str, Any]] = None
    expected_coverage: Optional[Dict[str, Any]] = None
    try:
        from psycopg.rows import dict_row

        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT schedule_id::text AS schedule_id,
                       client_id::text AS client_id, client_code, dataset_name,
                       coverage_start_ts, covered_through_ts, bootstrap_status,
                       bootstrap_evidence_ref, covered_through_source,
                       seeded_at, seeded_by, last_gap_detected_ts, updated_at
                  FROM workflow_a_control.client_dataset_coverage
                 WHERE client_id = %(claim_client_id)s
                   AND dataset_name = %(claim_dataset_name)s
                 FOR UPDATE
                """,
                params,
            )
            coverage_rows = cur.fetchall()
            if len(coverage_rows) > 1:
                conn.rollback()
                raise _new_finalization_error(
                    TRIPS_COVERAGE_ATOMIC_STATE_DIVERGENCE,
                    branch=branch,
                    business_subprocess_succeeded=False,
                )
            if not coverage_rows or not _coverage_row_matches_claim(
                dict(coverage_rows[0]), state
            ):
                conn.rollback()
                raise _terminal_failure_after_rollback(
                    primary_code=TRIPS_COVERAGE_GAP_DETECTED_PERSISTENCE_CONFLICT,
                    branch=branch,
                    run_history_id=prepared.run_history_id,
                    business_subprocess_succeeded=False,
                )
            original_coverage = dict(coverage_rows[0])

            cur.execute(
                """
                SELECT run_history_id::text AS run_history_id, status,
                       schedule_id::text AS schedule_id, dataset_name,
                       window_start_ts, window_end_ts, scheduled_fire_ts
                  FROM workflow_a_control.client_schedule_run_history
                 WHERE run_history_id = %(run_history_id)s
                   AND status = 'RUNNING'
                 FOR UPDATE
                """,
                params,
            )
            history_rows = cur.fetchall()
            if len(history_rows) != 1:
                conn.rollback()
                observed = _observed_history_status(
                    conn, run_history_id=prepared.run_history_id,
                )
                raise _new_finalization_error(
                    TRIPS_HISTORY_CLAIM_LOST,
                    branch=branch,
                    business_subprocess_succeeded=False,
                    observed_history_status=observed,
                )
            original_history = dict(history_rows[0])
            if not _history_row_matches_prepared(original_history, prepared):
                conn.rollback()
                raise _new_finalization_error(
                    TRIPS_COVERAGE_ATOMIC_STATE_DIVERGENCE,
                    branch=branch,
                    business_subprocess_succeeded=False,
                )

            cur.execute(
                """
                UPDATE workflow_a_control.client_dataset_coverage
                   SET bootstrap_status = 'GAP_DETECTED',
                       last_gap_detected_ts = %(mutation_ts)s,
                       updated_at = %(mutation_ts)s
                 WHERE client_id = %(claim_client_id)s
                   AND dataset_name = %(claim_dataset_name)s
                   AND schedule_id = %(claim_schedule_id)s
                   AND bootstrap_status = %(claim_bootstrap_status)s
                   AND coverage_start_ts
                       IS NOT DISTINCT FROM %(claim_coverage_start_ts)s
                   AND covered_through_ts
                       IS NOT DISTINCT FROM %(claim_covered_through_ts)s
                   AND bootstrap_evidence_ref
                       IS NOT DISTINCT FROM %(claim_bootstrap_evidence_ref)s
                   AND covered_through_source = %(claim_covered_through_source)s
                   AND seeded_at IS NOT DISTINCT FROM %(claim_seeded_at)s
                   AND seeded_by IS NOT DISTINCT FROM %(claim_seeded_by)s
                   AND last_gap_detected_ts
                       IS NOT DISTINCT FROM %(claim_last_gap_detected_ts)s
                """,
                params,
            )
            if cur.rowcount != 1:
                conn.rollback()
                raise _terminal_failure_after_rollback(
                    primary_code=TRIPS_COVERAGE_GAP_DETECTED_PERSISTENCE_CONFLICT,
                    branch=branch,
                    run_history_id=prepared.run_history_id,
                    business_subprocess_succeeded=False,
                )
            expected_coverage = dict(original_coverage)
            expected_coverage["bootstrap_status"] = "GAP_DETECTED"
            expected_coverage["last_gap_detected_ts"] = mutation_ts
            expected_coverage["updated_at"] = mutation_ts

            cur.execute(
                """
                UPDATE workflow_a_control.client_schedule_run_history
                   SET status = 'FAILED',
                       finished_at = %(finished_at)s,
                       error_summary = %(error_summary)s
                 WHERE run_history_id = %(run_history_id)s
                   AND status = 'RUNNING'
                """,
                params,
            )
            if cur.rowcount != 1:
                conn.rollback()
                raise _new_finalization_error(
                    TRIPS_HISTORY_CLAIM_LOST,
                    branch=branch,
                    business_subprocess_succeeded=False,
                )
        try:
            conn.commit()
        except Exception as commit_exc:
            try:
                conn.rollback()
            except Exception:
                pass
            assert original_coverage is not None
            assert original_history is not None
            assert expected_coverage is not None
            try:
                _reconcile_compat_commit(
                    branch=branch,
                    run_history_id=prepared.run_history_id,
                    original_coverage=original_coverage,
                    expected_coverage=expected_coverage,
                    original_history=original_history,
                    expected_history_status="FAILED",
                    expected_error_summary=TRIPS_COVERAGE_GAP_DETECTED,
                    business_subprocess_succeeded=False,
                )
            except CoverageFinalizationError:
                raise
            except Exception as reconcile_exc:
                raise _new_finalization_error(
                    TRIPS_COVERAGE_COMMIT_RECONCILIATION_UNAVAILABLE,
                    branch=branch,
                    business_subprocess_succeeded=False,
                    exception_class=_bounded_exception_class(reconcile_exc),
                ) from reconcile_exc
            return
    except CoverageFinalizationError:
        raise
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        raise _terminal_failure_after_rollback(
            primary_code=TRIPS_COVERAGE_FINALIZATION_COMMIT_FAILED,
            branch=branch,
            run_history_id=prepared.run_history_id,
            business_subprocess_succeeded=False,
        ) from exc


def _set_platform_run_id(
    conn, *, run_history_id: str, platform_run_id: str,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE workflow_a_control.client_schedule_run_history
               SET platform_run_id = %s
             WHERE run_history_id = %s
            """,
            (platform_run_id, run_history_id),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Subprocess launcher
# ---------------------------------------------------------------------------

def _repo_root() -> Path:
    # jobs/api/telematics/dispatcher.py -> repo root is 3 parents up.
    return Path(__file__).resolve().parents[3]


def _iso_z(dt: datetime) -> str:
    """ISO-8601 UTC with trailing 'Z' (matches sync_trips_and_speeding parser)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _jsonable_row(row: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, datetime):
            out[key] = _iso_z(value)
        elif value is None or isinstance(value, (str, int, float, bool)):
            out[key] = value
        else:
            out[key] = str(value)
    return out


def _build_job_params(
    *,
    client_id: str,
    client_code: str,
    dataset_name: str,
    event_enrichment_mode: str,
    schedule_run_type: str = SCHEDULE_RUN_TYPE_BASE,
    schedule_id: Optional[str] = None,
    window_start_ts: datetime,
    window_end_ts: datetime,
    scheduled_fire_ts: Optional[datetime] = None,
    nominal_window_start_ts: Optional[datetime] = None,
    nominal_window_end_ts: Optional[datetime] = None,
    trips_stabilization_delay_seconds: Optional[int] = None,
    trips_overlap_seconds: Optional[int] = None,
    trips_pagination_mode: Optional[str] = None,
    scheduled_mailing: Optional[ScheduledMailingInvocation] = None,
) -> Dict[str, Any]:
    """Build the runner params for one claimed fire.

    Pure: it resolves no secret, opens no connection and reads no environment,
    which is what lets the coverage gate be enforced immediately before it.

    `scheduled_mailing` is the already-resolved Eco mailing execution contract
    for this fire, or `None`. It is RESOLVED BY THE CALLER precisely so this
    function stays pure: reading the declaration is I/O, and the coverage gate
    is enforced immediately before this call. `None` — which is every
    non-mailing dataset and every mailing pair that is not declared scheduled
    production — contributes no parameter at all, so the job falls back to its
    own render-only default.

    `window_start_ts` / `window_end_ts` are always the window the job must
    fetch, already effective when the dispatcher derived one. The job never
    re-derives or shifts a window it is given (docs/13 §12).

    `scheduled_fire_ts` reaches `trips_sync` in both modes, closing the
    `docs/13_…` R6 evidence gap; the nominal window and the D/O/mode scalars are
    added only when the caller derived them, i.e. only for a compatibility fire.
    All are non-secret scalars and are ignored by today's job.

    `schedule_run_type` is carried the same way: as evidence about the fire, not
    as a decision about it. The dispatcher still enumerates every enabled row
    and still refuses to branch on the role — it only tells the job which role
    claimed this fire, and the job decides what that means for event scope. Put
    the other way round: the dispatcher is not allowed to know that a
    reconciliation buys fewer events, only that it is a reconciliation.
    """
    if scheduled_mailing is not None and scheduled_mailing.dataset_name != dataset_name:
        # Checked on every branch, not only the one that would consume it: a
        # contract resolved for another dataset must fail the fire, never be
        # silently dropped into a run that then looks ordinary.
        raise ValueError(
            "scheduled mailing contract does not describe this dataset: "
            f"{scheduled_mailing.dataset_name} != {dataset_name}"
        )
    params: Dict[str, Any] = {
        "client_id": client_id,
        "trigger": "SCHEDULED",
    }
    if client_code:
        params["client_code"] = client_code
    if dataset_name in ECO_DRIVING_SCHEDULE_MODES:
        # Eco Driving datasets carry a mode instead of an explicit window. This
        # branch must fall through to the guard below rather than return: the
        # dataset modes are module data a future edit can extend, so they are
        # exactly the kind of input the deny-by-default check exists for.
        params.update(ECO_DRIVING_SCHEDULE_MODES[dataset_name])
        params["scheduled_fire_ts"] = _iso_z(window_end_ts)
        if scheduled_mailing is not None:
            # The ONE parameter the scheduled execution contract contributes:
            # the explicit `execution_mode`, identical to the one an operator
            # types. The dashboard opt-in is NOT smuggled in here — it travels
            # as the runner option `_launch_job` appends, so it keeps passing
            # through the runner's own option validation.
            params.update(scheduled_mailing.params_overrides())
    else:
        params["window_start_ts"] = _iso_z(window_start_ts)
        params["window_end_ts"] = _iso_z(window_end_ts)
        if dataset_name == TRIPS_SYNC_DATASET_NAME:
            if event_enrichment_mode not in SUPPORTED_EVENT_ENRICHMENT_MODES:
                raise ValueError(
                    "event_enrichment_mode must be one of: "
                    f"{', '.join(sorted(SUPPORTED_EVENT_ENRICHMENT_MODES))}"
                )
            params["event_enrichment_mode"] = event_enrichment_mode
            # Only `trips_sync` has an event scope to decide, so only
            # `trips_sync` is told which role claimed the fire. Widening the
            # param set of every dataset for one dataset's benefit is exactly
            # what docs/13 R6 exists to prevent.
            params["schedule_run_type"] = schedule_run_type
            # The identity of the schedule row that CLAIMED this fire, which is
            # not the same thing as the client's base configuration once a
            # reconciliation cadence exists (M5). The child derives its own
            # configuration from the BASE row on purpose
            # (`control_plane.load_dataset_schedule`), so without this it would
            # stamp the base `schedule_id` on its terminal record while
            # `_require_coverage_eligible_outcome` verifies that record against
            # the FIRING schedule — a guaranteed
            # `EXECUTION_OUTCOME_IDENTITY_MISMATCH` on every reconciliation fire
            # and none at all on a DAILY one, where the two ids coincide.
            #
            # This is telling the child which fire it is executing, never asking
            # it. The dispatcher still verifies the returned record against its
            # own claim, so a forged value can only fail that comparison.
            if schedule_id is not None:
                params["schedule_id"] = str(schedule_id)
            if scheduled_fire_ts is not None:
                params["scheduled_fire_ts"] = _iso_z(scheduled_fire_ts)
            if nominal_window_start_ts is not None:
                params["nominal_window_start_ts"] = _iso_z(
                    nominal_window_start_ts
                )
            if nominal_window_end_ts is not None:
                params["nominal_window_end_ts"] = _iso_z(nominal_window_end_ts)
            if trips_stabilization_delay_seconds is not None:
                params["trips_stabilization_delay_seconds"] = int(
                    trips_stabilization_delay_seconds
                )
            if trips_overlap_seconds is not None:
                params["trips_overlap_seconds"] = int(trips_overlap_seconds)
            if trips_pagination_mode is not None:
                params["trips_pagination_mode"] = trips_pagination_mode
    # Deny by default, on the single exit shared by every dataset branch. The
    # dispatcher must never be able to construct — or be tricked into
    # constructing — a manual-recovery authority, so the finished parameter set
    # is checked rather than merely "not written above". A future edit that adds
    # one of those keys — in this function or in ECO_DRIVING_SCHEDULE_MODES —
    # fails here instead of silently gaining the ability to run a disabled
    # schedule. This is the only successful return: no branch may add one.
    manual_recovery_authority.reject_authority_params(
        params, surface="jobs.api.telematics.dispatcher._build_job_params",
    )
    return params


def _resolve_python_executable() -> str:
    return sys.executable or "python3"


def _read_platform_run_id_file(path: Path) -> Optional[str]:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError:
        return None
    if not raw:
        return None
    try:
        return str(uuid.UUID(raw))
    except ValueError:
        return None


#: Refusal codes this dispatcher owns. Every one of them means the same thing —
#: the watermark did not move — and they differ only in what the evidence said.
#: `EXECUTION_OUTCOME_*` codes raised by the contract module are passed through
#: unchanged rather than remapped, so a refusal always names its real cause.
TRIPS_OUTCOME_NOT_COLLECTED = "TRIPS_OUTCOME_NOT_COLLECTED"
TRIPS_OUTCOME_NOT_COVERAGE_ELIGIBLE = "TRIPS_OUTCOME_NOT_COVERAGE_ELIGIBLE"
#: M4 (§6 condition 6). The `TRIPS_WINDOW_*` / `TRIPS_SUBWINDOW_*` codes are
#: owned by `request_evidence` and passed through unchanged, so a refusal names
#: its real cause: a tiling with a hole reads differently from a sub-window that
#: never terminated, and both read differently from evidence that was never
#: written at all.
TRIPS_WINDOW_EVIDENCE_GATE_FAILED = "TRIPS_WINDOW_EVIDENCE_GATE_FAILED"
#: A defect in the gate itself. Distinct from every evidence-based refusal
#: above, because it says nothing about what the child did — only that the
#: dispatcher could not decide, which must still fail closed.
TRIPS_OUTCOME_GATE_FAILED = "TRIPS_OUTCOME_GATE_FAILED"

#: Mirrors `coverage_windows.COVERAGE_GATE_REASON_MAX_CHARS` rather than
#: importing a private helper across the module boundary.
OUTCOME_REFUSAL_REASON_MAX_CHARS = 500


def _bounded_refusal_reason(text: str) -> str:
    reason = " ".join(str(text).split())
    if len(reason) > OUTCOME_REFUSAL_REASON_MAX_CHARS:
        return reason[: OUTCOME_REFUSAL_REASON_MAX_CHARS - 1] + "…"
    return reason


class ScheduledOutcomeRefused(RuntimeError):
    """A verified-or-not terminal record that must not advance coverage.

    Carries the classification and a bounded reason so the refusal reaches
    `client_schedule_run_history.error_summary` as a deterministic string rather
    than as a stack trace.
    """

    def __init__(self, code: str, reason: str) -> None:
        # Bounded because `_finalize_run` keeps the *tail* of `error_summary`:
        # an unbounded reason (a corrupt record naming many unknown fields, say)
        # would push the leading classification off the front and persist a
        # refusal whose deterministic code is no longer readable.
        reason = _bounded_refusal_reason(reason)
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


@dataclass(frozen=True)
class ScheduledExecutionOutcome:
    """What the launcher managed to learn about the child's terminal record.

    Three genuinely different states, and collapsing any two of them would
    reintroduce the defect this exists to close:

    * `requested = False` — this launch never asked for a record (`strict_meta`,
      a non-trips dataset). Nothing may be concluded, and nothing that could
      advance coverage runs on this branch.
    * `requested = True`, `outcome` set — a record was read and strictly parsed.
      It still has to verify and be eligible.
    * `requested = True`, `error` set — absent, empty, malformed or internally
      contradictory. Fail closed.
    """

    requested: bool
    outcome: Optional[ExecutionOutcome] = None
    error: Optional[ExecutionOutcomeError] = None


def _read_execution_outcome(
    path: Path, *, requested: bool,
) -> ScheduledExecutionOutcome:
    """Total read of the child's terminal record. Never raises."""
    if not requested:
        return ScheduledExecutionOutcome(requested=False)
    try:
        return ScheduledExecutionOutcome(
            requested=True, outcome=read_outcome(path),
        )
    except ExecutionOutcomeError as exc:
        return ScheduledExecutionOutcome(requested=True, error=exc)
    except Exception as exc:  # noqa: BLE001 — an unreadable record is a refusal
        return ScheduledExecutionOutcome(
            requested=True,
            error=ExecutionOutcomeError(
                "EXECUTION_OUTCOME_UNREADABLE",
                f"terminal record could not be read ({type(exc).__name__})",
            ),
        )


def _require_coverage_eligible_outcome(
    *,
    prepared: PreparedDispatcherRun,
    schedule: ScheduleRow,
    collected: ScheduledExecutionOutcome,
    platform_run_id: Optional[str],
) -> ExecutionOutcome:
    """M3 §6 conditions 2–4. Raises `ScheduledOutcomeRefused` or returns.

    Called only after `rc == 0` on a compatibility `trips_sync` fire, and only
    before `_finalize_compat_success`. It reads no coverage row, issues no SQL
    and mutates nothing: a refusal here must leave the watermark exactly as the
    claim-time snapshot found it, which is why the decision is made *before* the
    finalizer opens its transaction rather than inside it.

    Condition 1 (`rc == 0`) is the caller's. Condition 5 (CAS + strict
    monotonicity) remains the finalizer's and is untouched. Condition 6
    (`subwindow_complete`) is M4 and is deliberately not evaluated here.
    """
    if not collected.requested:
        # Unreachable from the compatibility branch, which always requests the
        # record. Kept as a fail-closed refusal rather than an assertion so a
        # future edit that forgets to ask cannot silently advance coverage.
        raise ScheduledOutcomeRefused(
            TRIPS_OUTCOME_NOT_COLLECTED,
            "no terminal execution record was requested for a fire whose "
            "success may advance coverage",
        )
    if collected.error is not None:
        raise ScheduledOutcomeRefused(
            collected.error.code, str(collected.error.message),
        )

    outcome = collected.outcome
    if outcome is None:
        raise ScheduledOutcomeRefused(
            "EXECUTION_OUTCOME_ABSENT",
            "the business process wrote no terminal execution record",
        )

    try:
        verify_outcome(
            outcome,
            client_id=schedule.client_id,
            # `ScheduleRow.client_code` is `COALESCE(..., '')`, so a client with
            # no code arrives as `''` while the child's record carries `None`.
            # Passing `''` through would turn "no code" into a positive identity
            # assertion the child can never satisfy, refusing every fire for that
            # client forever. `None` is the contract's "nothing to compare".
            client_code=(schedule.client_code or None),
            schedule_id=schedule.schedule_id,
            dataset_name=schedule.dataset_name,
            # A scheduled fire is never a manual recovery. Asserting `None` is
            # what refuses a recovery record replayed against a scheduled claim.
            recovery_run_id=None,
            window_start_ts=prepared.window_start,
            window_end_ts=prepared.window_end,
            platform_run_id=platform_run_id,
        )
    except ExecutionOutcomeError as exc:
        raise ScheduledOutcomeRefused(exc.code, str(exc.message)) from exc

    if not is_coverage_eligible(outcome):
        raise ScheduledOutcomeRefused(
            TRIPS_OUTCOME_NOT_COVERAGE_ELIGIBLE,
            f"terminal outcome {outcome.outcome} is not coverage-eligible "
            f"(skipped={outcome.skipped}, "
            f"provider_execution_entered={outcome.provider_execution_entered}, "
            f"business_transaction_entered="
            f"{outcome.business_transaction_entered}, "
            f"transaction_status={outcome.transaction_status})",
        )
    return outcome


def _require_complete_window_evidence(
    *,
    prepared: PreparedDispatcherRun,
    outcome: ExecutionOutcome,
) -> WindowCompleteness:
    """M4 §6 condition 6. Raises `ScheduledOutcomeRefused` or returns the proof.

    Called only after `_require_coverage_eligible_outcome` has already proved
    the record belongs to this execution, so identity is settled before this
    reads anything: what is left to establish is that the window was *covered*,
    not whose window it was.

    Like the M3 gate, this is pure. It reads no coverage row, issues no SQL and
    mutates nothing, and it runs before `_finalize_compat_success` opens its
    transaction — so a refusal here performs no coverage SQL at all and leaves
    the watermark exactly where the claim-time snapshot found it.

    The window it verifies against is `prepared`'s own claim, never the
    record's. `verify_outcome` has already established that the record's
    requested window equals that claim, which is what makes it safe to treat
    the tiling as a statement about *this* fire.
    """
    try:
        return verify_window_completeness(
            outcome.window_completeness,
            window_start_ts=prepared.window_start,
            window_end_ts=prepared.window_end,
        )
    except WindowCompletenessError as exc:
        raise ScheduledOutcomeRefused(exc.code, str(exc.message)) from exc


def _launch_job(
    *, job_module: str, job_params: Dict[str, Any], log_fn,
    on_platform_run_id=None, collect_execution_outcome: bool = False,
    runner_options: Tuple[str, ...] = (),
) -> Tuple[int, str, str, Optional[str], ScheduledExecutionOutcome]:
    """Run `python ops/runner.py <job_module> <params_json>` synchronously.

    Returns (returncode, stdout, stderr, platform_run_id, execution_outcome).
    Does NOT raise on non-zero exit; the dispatcher inspects the return code and
    the terminal record and decides SUCCESS vs FAILED.

    `runner_options` are appended to that argv verbatim, and are non-empty only
    for a declared Eco mailing fire (`jobs.ecodriving.scheduled_mailing_contract`
    resolved them). They are the SAME options an operator types, validated by
    the SAME `ops/runner.py` contract — which is the point: the scheduled path
    is the manual command, not a parallel one. Anything outside the declared
    allowlist is refused above before a process is started.

    `collect_execution_outcome` is set only for a compatibility `trips_sync`
    fire — the one path whose success may move the coverage watermark. The
    record's destination is a fresh file inside this launch's own temporary
    directory, so it is created per process launch, cannot be a leftover from
    another run, and is removed when this function returns. That is also why
    the record is read *inside* the `with` block: outside it the directory is
    already gone.

    Reading is deliberately total. A missing or unparseable record is returned
    as data, never raised, because the caller — not this launcher — owns the
    fail-closed decision and must still be able to finalize the claim.
    """
    unknown_options = [
        option for option in runner_options
        if option not in ALLOWED_SCHEDULED_RUNNER_OPTIONS
    ]
    if unknown_options:
        # Deny by default. `ops/runner.py` owns the option contract and would
        # refuse an unknown option anyway; refusing here as well means a
        # scheduled fire cannot even ATTEMPT to carry one this dispatcher has
        # not declared schedulable.
        raise ValueError(
            "scheduled fire may not carry option(s): "
            + ", ".join(unknown_options)
        )
    cwd = _repo_root()
    with tempfile.TemporaryDirectory(prefix="log-platform-run-id-") as tmpdir:
        run_id_file = Path(tmpdir) / "platform_run_id.txt"
        outcome_file = Path(tmpdir) / "execution_outcome.json"
        cmd = [
            _resolve_python_executable(),
            "ops/runner.py",
            job_module,
            json.dumps(job_params),
            *runner_options,
        ]
        env = os.environ.copy()
        pythonpath = env.get("PYTHONPATH")
        env["PYTHONPATH"] = str(cwd) if not pythonpath else f"{cwd}{os.pathsep}{pythonpath}"
        env["LOG_PLATFORM_RUN_ID_FILE"] = str(run_id_file)
        # A manual-recovery authority present in the dispatcher's own
        # environment is never inherited by a scheduled fire. Stripping it here
        # is independent of the parameter guard above: either one alone is
        # sufficient to keep the dispatcher out of the disabled-schedule path.
        manual_recovery_authority.strip_authority_from_env(env)
        # Strip unconditionally *first*, then opt in. An inherited path can
        # therefore never survive into the child on any branch: a scheduled fire
        # either writes to this launch's own fresh file or writes nothing.
        env.pop(EXECUTION_OUTCOME_FILE_ENV, None)
        if collect_execution_outcome:
            env[EXECUTION_OUTCOME_FILE_ENV] = str(outcome_file)
        log_fn("INFO",
               f"Launching subprocess: {cmd[0]} {cmd[1]} {cmd[2]} <params>"
               + ("".join(f" {option}" for option in runner_options)),
               {"cwd": str(cwd), "job_module": job_module,
                "runner_options": list(runner_options)})
        proc = subprocess.Popen(
            cmd, cwd=str(cwd), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )

        platform_run_id: Optional[str] = None
        while True:
            if platform_run_id is None:
                maybe_run_id = _read_platform_run_id_file(run_id_file)
                if maybe_run_id:
                    platform_run_id = maybe_run_id
                    if on_platform_run_id is not None:
                        on_platform_run_id(platform_run_id)
            try:
                out, err = proc.communicate(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                continue

        if platform_run_id is None:
            platform_run_id = _read_platform_run_id_file(run_id_file)
            if platform_run_id and on_platform_run_id is not None:
                on_platform_run_id(platform_run_id)

        execution_outcome = _read_execution_outcome(
            outcome_file, requested=collect_execution_outcome,
        )
        return (
            proc.returncode, out or "", err or "", platform_run_id,
            execution_outcome,
        )


# ---------------------------------------------------------------------------
# run() — runner contract
# ---------------------------------------------------------------------------

def _close_prepared(prepared: PreparedDispatcherRun) -> None:
    if prepared.released:
        return
    prepared.released = True
    try:
        _release_dispatcher_lock(prepared.conn)
    except Exception:
        pass
    try:
        prepared.conn.close()
    except Exception:
        pass


def prepare_run(params: dict) -> Optional[PreparedDispatcherRun]:
    """Atomically plan and claim one fire before platform run persistence.

    ``None`` is the only successful no-work result. The advisory lock is held
    from planning through claimed-work execution, so deferred run creation does
    not add a read/check/claim race.
    """
    if not isinstance(params, dict):
        raise ValueError("params must be a dict")

    stale_after_minutes = _stale_running_timeout_minutes(params)
    conn = _platform_pg_conn()
    lock_acquired = False
    try:
        _record_heartbeat(conn)
        lock_acquired = _try_acquire_dispatcher_lock(conn)
        if not lock_acquired:
            conn.close()
            return None

        now_utc = datetime.now(timezone.utc).replace(microsecond=0)
        stale_rows = _mark_stale_running(
            conn, stale_after_minutes=stale_after_minutes, now_utc=now_utc,
        )
        prepared_base = dict(
            conn=conn, stale_rows=stale_rows,
            stale_after_minutes=stale_after_minutes,
        )

        if _count_running(conn) > 0:
            if stale_rows:
                return PreparedDispatcherRun(
                    **prepared_base, schedule=None, fire_utc=None,
                    window_start=None, window_end=None, run_history_id=None,
                )
            _release_dispatcher_lock(conn)
            conn.close()
            return None

        schedules = _load_enabled_schedules(conn)
        validated: List[ScheduleRow] = []
        invalid: List[str] = []
        for schedule in schedules:
            reason = _validate_against_registry(schedule)
            if reason is None:
                validated.append(schedule)
            else:
                invalid.append(
                    f"client_id={schedule.client_id} schedule_id={schedule.schedule_id}: {reason}"
                )
        if invalid:
            raise ValueError("invalid enabled dispatcher schedule(s): " + "; ".join(invalid))

        now_utc = datetime.now(timezone.utc).replace(microsecond=0)
        due = select_next_due(now_utc=now_utc, schedules=validated)
        for schedule, fire_utc, window_start, window_end in due:
            # docs/14 §7.1 steps 2-3: for a compatibility fire the mode, the
            # coverage row and the gate decision must all be known *before* the
            # claim INSERT, because `_claim_fire` writes the execution window.
            # This is read-only preparation: no lock is taken on the coverage
            # row, no coverage state is written, and a rejection here does not
            # skip the claim — it only fixes the claim window at nominal.
            coverage_state: Optional[CoverageState] = None
            gate_result: Optional[CoverageGateResult] = None
            claim_start, claim_end = window_start, window_end
            evidence: Dict[str, Any] = {
                "nominal_window_start_ts": None,
                "nominal_window_end_ts": None,
                "stabilization_delay_seconds": None,
                "overlap_seconds": None,
                "trips_pagination_mode": None,
            }
            if _is_compatibility_trips_fire(schedule):
                coverage_state = _load_coverage_state(
                    conn,
                    client_id=schedule.client_id,
                    dataset_name=schedule.dataset_name,
                )
                gate_result = evaluate_coverage_gate(
                    schedule_id=schedule.schedule_id,
                    client_id=schedule.client_id,
                    client_code=schedule.client_code,
                    dataset_name=schedule.dataset_name,
                    scheduled_fire_ts=fire_utc,
                    lookback_days=max(schedule.lookback_days, 0),
                    stabilization_delay_seconds=(
                        schedule.trips_stabilization_delay_seconds
                    ),
                    overlap_seconds=schedule.trips_overlap_seconds,
                    max_recovery_span_seconds=(
                        schedule.trips_max_recovery_span_seconds
                    ),
                    coverage_state=coverage_state,
                    now_utc=now_utc,
                )
                if gate_result.allowed:
                    effective = gate_result.effective_window
                    claim_start = effective.effective_window_start_ts
                    claim_end = effective.effective_window_end_ts
                evidence = {
                    "nominal_window_start_ts": window_start,
                    "nominal_window_end_ts": window_end,
                    "stabilization_delay_seconds": (
                        schedule.trips_stabilization_delay_seconds
                    ),
                    "overlap_seconds": schedule.trips_overlap_seconds,
                    "trips_pagination_mode": schedule.trips_pagination_mode,
                }

            run_history_id = _claim_fire(
                conn, schedule_id=schedule.schedule_id,
                client_id=schedule.client_id, client_code=schedule.client_code,
                dataset_name=schedule.dataset_name,
                scheduled_fire_ts=fire_utc, window_start_ts=claim_start,
                window_end_ts=claim_end,
                **evidence,
            )
            if run_history_id is not None:
                return PreparedDispatcherRun(
                    **prepared_base, schedule=schedule, fire_utc=fire_utc,
                    window_start=claim_start, window_end=claim_end,
                    run_history_id=run_history_id,
                    nominal_window_start=window_start,
                    nominal_window_end=window_end,
                    coverage_state=coverage_state,
                    gate_result=gate_result,
                )

        if stale_rows:
            return PreparedDispatcherRun(
                **prepared_base, schedule=None, fire_utc=None,
                window_start=None, window_end=None, run_history_id=None,
            )
        _release_dispatcher_lock(conn)
        conn.close()
        return None
    except Exception:
        if lock_acquired:
            try:
                _release_dispatcher_lock(conn)
            except Exception:
                pass
        try:
            conn.close()
        except Exception:
            pass
        raise


def discard_prepared(prepared: PreparedDispatcherRun) -> None:
    if prepared.released:
        return
    if prepared.run_history_id is not None:
        try:
            _finalize_run(
                prepared.conn, run_history_id=prepared.run_history_id,
                status="FAILED",
                error="dispatcher technical run creation failed after claim",
            )
        except Exception:
            pass
    _close_prepared(prepared)


def _coverage_gate_context(
    *,
    schedule: ScheduleRow,
    prepared: PreparedDispatcherRun,
    gate: CoverageGateResult,
) -> Dict[str, Any]:
    """Flat, scalar, non-sensitive context for a coverage-gate rejection.

    Deliberately excluded (`docs/06_security.md`, `CONVENTIONS.md` §8): the
    bootstrap evidence reference itself, any evidence-bundle content, any trip
    or personal field, any secret ref and any DSN. Only its presence is
    reported, because presence is what §5.2.1 checks.
    """
    state = prepared.coverage_state
    return {
        "abort_code": gate.abort_code,
        "coverage_gate_classification": gate.classification,
        "coverage_gate_reason": gate.reason,
        # Starts false for every gate decision. The pre-launch C6 gap finalizer
        # flips the emitted context to true only after its atomic commit.
        "requires_gap_persistence": gate.requires_gap_persistence,
        "coverage_mutation_performed": False,
        "trips_pagination_mode": schedule.trips_pagination_mode,
        "bootstrap_status": gate.bootstrap_status,
        "coverage_row_present": state is not None,
        "bootstrap_evidence_ref_present": bool(
            state is not None
            and isinstance(state.bootstrap_evidence_ref, str)
            and state.bootstrap_evidence_ref.strip()
        ),
        "coverage_start_ts": (
            _iso_z(gate.coverage_start_ts) if gate.coverage_start_ts else None
        ),
        "covered_through_ts": (
            _iso_z(gate.covered_through_ts) if gate.covered_through_ts else None
        ),
        "nominal_window_start_ts": (
            _iso_z(prepared.nominal_window_start)
            if prepared.nominal_window_start else None
        ),
        "nominal_window_end_ts": (
            _iso_z(prepared.nominal_window_end)
            if prepared.nominal_window_end else None
        ),
        "stabilization_delay_seconds": schedule.trips_stabilization_delay_seconds,
        "overlap_seconds": schedule.trips_overlap_seconds,
        "max_recovery_span_seconds": schedule.trips_max_recovery_span_seconds,
    }


def _report_coverage_gate_suspected_bug(
    client,
    *,
    run_id: str,
    schedule: ScheduleRow,
    incident_code: str,
    gate_context: Dict[str, Any],
    log,
) -> None:
    """Report the rejection through the existing suspected_bug mechanism.

    The fingerprint identity deliberately excludes `scheduled_fire_ts` and the
    run/history ids, so repeated fires of the same unresolved condition
    aggregate as occurrences of one incident instead of alerting per fire —
    the platform's existing grouping semantics, not a new one. Reporting can
    never mask the abort: a transport failure is logged and swallowed.
    """
    event = SuspectedBugEvent(
        incident_code=incident_code,
        title=(
            f"{incident_code} for {schedule.dataset_name} "
            f"({schedule.client_code or schedule.client_id})"
        ),
        summary=(
            f"Scheduled {schedule.dataset_name} fire was refused before launch: "
            f"{gate_context.get('coverage_gate_reason')}. No subprocess was "
            "started, no credential was resolved and no provider request was "
            "issued; the schedule history row is terminal FAILED."
        ),
        occurred_at=datetime.now(timezone.utc),
        environment=(os.getenv("LOG_PLATFORM_TARGET_ENVIRONMENT") or "unknown"),
        severity="error",
        component=JOB_SOURCE,
        workflow_name="workflow_a",
        stage_name="dispatcher",
        job_name=JOB_SOURCE,
        client_id=schedule.client_id,
        client_code=schedule.client_code or None,
        run_id=run_id or None,
        dataset_name=schedule.dataset_name,
        subject_type="workflow_a_schedule",
        subject_key="schedule_id",
        subject_value=schedule.schedule_id,
        processing_outcome=(
            "fire refused before launch; coverage bounds unchanged"
        ),
        rows_modified=0,
        fingerprint_fields={
            "abort_code": gate_context.get("abort_code"),
            "schedule_id": schedule.schedule_id,
            "dataset_name": schedule.dataset_name,
            "trips_pagination_mode": schedule.trips_pagination_mode,
            "bootstrap_status": gate_context.get("bootstrap_status"),
        },
        details=dict(gate_context),
        suggested_action=(
            "Run the reviewed coverage bootstrap procedure. Do not enable, "
            "seed or advance coverage state from this alert."
        ),
    )
    try:
        client.report_suspected_bug(event)
    except Exception as exc:  # reporting must never replace the gate abort
        log(
            "WARNING",
            "Failed to report coverage gate suspected_bug",
            {**gate_context, "error": str(exc)},
        )


def _report_coverage_finalization_failure(
    client,
    *,
    run_id: str,
    schedule: ScheduleRow,
    error: CoverageFinalizationError,
    log,
) -> None:
    """Emit bounded C6 telemetry without allowing reporting to mask failure."""
    severity = (
        "critical"
        if error.code in {
            TRIPS_COVERAGE_ATOMIC_STATE_DIVERGENCE,
            TRIPS_COVERAGE_COMMIT_RECONCILIATION_UNAVAILABLE,
        }
        else "error"
    )
    context = {
        "code": error.code,
        "schedule_id": schedule.schedule_id,
        "dataset_name": schedule.dataset_name,
        "trips_pagination_mode": schedule.trips_pagination_mode,
        "branch": error.branch,
        "business_subprocess_succeeded": error.business_subprocess_succeeded,
        "observed_history_status": error.observed_history_status,
        "exception_class": error.exception_class,
    }
    log(
        "CRITICAL" if severity == "critical" else "ERROR",
        f"Compatibility coverage finalization failed ({error.code})",
        context,
    )
    event = SuspectedBugEvent(
        incident_code=error.code,
        title=(
            f"{error.code} for {schedule.dataset_name} "
            f"({schedule.client_code or schedule.client_id})"
        ),
        summary=(
            f"Compatibility coverage finalization failed on the {error.branch} "
            "branch. No automatic replay or coverage inference was attempted."
        ),
        occurred_at=datetime.now(timezone.utc),
        environment=(os.getenv("LOG_PLATFORM_TARGET_ENVIRONMENT") or "unknown"),
        severity=severity,
        component=JOB_SOURCE,
        workflow_name="workflow_a",
        stage_name="dispatcher",
        job_name=JOB_SOURCE,
        client_id=schedule.client_id,
        client_code=schedule.client_code or None,
        run_id=run_id or None,
        dataset_name=schedule.dataset_name,
        subject_type="workflow_a_schedule",
        subject_key="schedule_id",
        subject_value=schedule.schedule_id,
        processing_outcome=(
            "compatibility finalization stopped; no automatic replay"
        ),
        rows_modified=None,
        fingerprint_fields={
            "code": error.code,
            "schedule_id": schedule.schedule_id,
            "dataset_name": schedule.dataset_name,
            "trips_pagination_mode": schedule.trips_pagination_mode,
            "branch": error.branch,
        },
        details=context,
        suggested_action=(
            "Inspect coverage and schedule history read-only. Do not hand-advance "
            "coverage or restore RUNNING; follow the documented C6 triage."
        ),
        exception_type=error.exception_class,
    )
    try:
        client.report_suspected_bug(event)
    except Exception as report_exc:
        log(
            "WARNING",
            "Failed to report coverage finalization suspected_bug",
            {**context, "reporting_exception_class": type(report_exc).__name__[:120]},
        )


def _enforce_coverage_gate(
    client,
    run_id: str,
    prepared: PreparedDispatcherRun,
    schedule: ScheduleRow,
    gate: CoverageGateResult,
    ctx: Dict[str, Any],
    log,
) -> None:
    """Terminate a rejected fire durably, before any launch side effect.

    Runs immediately after a successful uniqueness claim and strictly before
    '_build_job_params', '_launch_job', 'subprocess.Popen', any credential
    resolution, any socket and any client-business access (docs/14 §7.1
    step 5). It never falls back to strict mode or retries. C6 persists only a
    newly disconnected valid READY row; every other rejection remains
    coverage-free.
    """
    gate_ctx = {**ctx, **_coverage_gate_context(
        schedule=schedule, prepared=prepared, gate=gate,
    )}
    incident_code = gate.abort_code or TRIPS_COVERAGE_BOOTSTRAP_REQUIRED

    if gate.requires_gap_persistence:
        try:
            _finalize_compat_gap(prepared.conn, prepared=prepared)
            gate_ctx["coverage_mutation_performed"] = True
        except CoverageFinalizationError as finalization_error:
            _report_coverage_finalization_failure(
                client, run_id=run_id, schedule=schedule,
                error=finalization_error, log=log,
            )
            raise
    else:
        _finalize_run(
            prepared.conn, run_history_id=prepared.run_history_id,
            status="FAILED", error=incident_code,
        )

    log(
        "ERROR",
        f"Coverage gate refused {schedule.dataset_name} before launch "
        f"({incident_code})",
        gate_ctx,
    )
    _report_coverage_gate_suspected_bug(
        client, run_id=run_id, schedule=schedule,
        incident_code=incident_code, gate_context=gate_ctx, log=log,
    )
    raise RuntimeError(
        f"{incident_code}: dataset={schedule.dataset_name} "
        f"schedule_id={schedule.schedule_id}"
    )


def run_prepared(
    client, run_id: str, params: dict, prepared: PreparedDispatcherRun,
) -> None:
    def log(level: str, message: str, ctx: Optional[Dict[str, Any]] = None) -> None:
        client.log(
            level, "SCRIPT", JOB_SOURCE, message, run_id=run_id, context=ctx or {},
        )

    try:
        log(
            "INFO", "Dispatcher tick starting",
            {
                "trigger": params.get("trigger", "SCHEDULED"),
                "stale_running_timeout_minutes": prepared.stale_after_minutes,
            },
        )
        if prepared.stale_rows:
            log(
                "WARNING",
                f"Marked {len(prepared.stale_rows)} stale RUNNING schedule history row(s) as FAILED",
                {
                    "stale_running_timeout_minutes": prepared.stale_after_minutes,
                    "stale_count": len(prepared.stale_rows),
                    "stale_rows": [
                        _jsonable_row(row) for row in prepared.stale_rows[:20]
                    ],
                },
            )

        schedule = prepared.schedule
        if schedule is None:
            return
        if not all(
            (prepared.fire_utc, prepared.window_start, prepared.window_end,
             prepared.run_history_id)
        ):
            raise RuntimeError("claimed dispatcher preparation is incomplete")

        fire_utc = prepared.fire_utc
        window_start = prepared.window_start
        window_end = prepared.window_end
        run_history_id = prepared.run_history_id
        ctx = {
            "schedule_id": schedule.schedule_id,
            "client_id": schedule.client_id,
            "client_code": schedule.client_code,
            "dataset_name": schedule.dataset_name,
            "job_module": schedule.job_module,
            "scheduled_fire_ts": _iso_z(fire_utc),
            "window_start_ts": _iso_z(window_start),
            "window_end_ts": _iso_z(window_end),
            "run_history_id": run_history_id,
            "event_enrichment_mode": schedule.event_enrichment_mode,
        }
        gate = prepared.gate_result
        if gate is not None:
            ctx["trips_pagination_mode"] = schedule.trips_pagination_mode
            ctx["nominal_window_start_ts"] = (
                _iso_z(prepared.nominal_window_start)
                if prepared.nominal_window_start else None
            )
            ctx["nominal_window_end_ts"] = (
                _iso_z(prepared.nominal_window_end)
                if prepared.nominal_window_end else None
            )
        # Gate enforcement — after the durable claim, before every side effect
        # and before any log that would claim a dispatch actually happened.
        if gate is not None and not gate.allowed:
            _enforce_coverage_gate(
                client, run_id, prepared, schedule, gate, ctx, log,
            )
            return  # unreachable: _enforce_coverage_gate always raises

        log(
            "INFO",
            f"Dispatching {schedule.dataset_name} for {schedule.client_name} "
            f"(fire={_iso_z(fire_utc)}, "
            f"window=[{_iso_z(window_start)}, {_iso_z(window_end)}])",
            ctx,
        )

        try:
            # THE canonical scheduled execution contract for an Eco mailing
            # fire, resolved once, here, and used for BOTH the parameters and
            # the runner options below so the two cannot disagree. Resolution
            # reads two declarations and nothing else; a non-mailing dataset
            # reads neither. An undeclared pair resolves to None and the fire
            # keeps today's behaviour exactly: no execution_mode, no option,
            # render-only. A declaration that cannot be trusted raises, and
            # this fire finalizes FAILED without launching anything — the
            # fail-closed branch, inside the try for exactly that reason.
            scheduled_mailing: Optional[ScheduledMailingInvocation] = None
            if is_mailing_dataset(schedule.dataset_name):
                scheduled_mailing = resolve_scheduled_invocation(
                    dataset_name=schedule.dataset_name,
                    client_code=schedule.client_code,
                )
                ctx["scheduled_mailing_contract"] = (
                    scheduled_mailing.audit() if scheduled_mailing is not None
                    else {"declared": False, "execution_mode": None,
                          "dashboard_enabled": False}
                )
                log(
                    "INFO",
                    "Resolved Eco mailing scheduled execution contract",
                    {**ctx, "dataset_name": schedule.dataset_name},
                )

            job_params = _build_job_params(
                client_id=schedule.client_id, client_code=schedule.client_code,
                dataset_name=schedule.dataset_name,
                event_enrichment_mode=schedule.event_enrichment_mode,
                schedule_run_type=schedule.run_type,
                schedule_id=schedule.schedule_id,
                window_start_ts=window_start, window_end_ts=window_end,
                scheduled_fire_ts=fire_utc,
                nominal_window_start_ts=(
                    prepared.nominal_window_start if gate is not None else None
                ),
                nominal_window_end_ts=(
                    prepared.nominal_window_end if gate is not None else None
                ),
                trips_stabilization_delay_seconds=(
                    schedule.trips_stabilization_delay_seconds
                    if gate is not None else None
                ),
                trips_overlap_seconds=(
                    schedule.trips_overlap_seconds if gate is not None else None
                ),
                trips_pagination_mode=(
                    schedule.trips_pagination_mode if gate is not None else None
                ),
                scheduled_mailing=scheduled_mailing,
            )
            platform_run_id_seen: Optional[str] = None

            def _on_platform_run_id(platform_run_id: str) -> None:
                nonlocal platform_run_id_seen
                if platform_run_id_seen == platform_run_id:
                    return
                platform_run_id_seen = platform_run_id
                try:
                    _set_platform_run_id(
                        prepared.conn, run_history_id=run_history_id,
                        platform_run_id=platform_run_id,
                    )
                    log(
                        "INFO", "Linked schedule history row to platform run",
                        {**ctx, "platform_run_id": platform_run_id},
                    )
                except Exception as link_exc:
                    log(
                        "ERROR", "Failed to link schedule history row to platform run",
                        {**ctx, "platform_run_id": platform_run_id,
                         "error": str(link_exc)},
                    )

            rc, out, err, platform_run_id, collected_outcome = _launch_job(
                job_module=schedule.job_module, job_params=job_params,
                log_fn=lambda lvl, msg, extra: log(lvl, msg, {**ctx, **extra}),
                on_platform_run_id=_on_platform_run_id,
                # Only a compatibility trips fire can move the watermark, and
                # only it needs the terminal record. Every other branch keeps
                # today's launch environment byte-for-byte.
                collect_execution_outcome=(gate is not None),
                runner_options=(
                    scheduled_mailing.runner_options
                    if scheduled_mailing is not None else ()
                ),
            )
            if platform_run_id:
                ctx["platform_run_id"] = platform_run_id
        except Exception as exc:
            err_text = f"subprocess launch failed: {exc}\n{traceback.format_exc()}"
            log("ERROR", "Job subprocess launch failed", {**ctx, "error": str(exc)})
            _finalize_run(
                prepared.conn, run_history_id=run_history_id,
                status="FAILED", error=err_text,
            )
            raise

        tail_ctx = {
            **ctx, "rc": rc, "stdout_tail": (out or "")[-500:],
            "stderr_tail": (err or "")[-500:],
        }
        if "platform_run_id" not in tail_ctx:
            tail_ctx["platform_run_id"] = None
            log("WARNING", "Dispatched job did not expose a platform_run_id", tail_ctx)
        if rc == 0:
            if gate is not None:
                # M3 conditions 2–4. `rc == 0` got us here and is not enough:
                # unless the child left a record that verifies against this
                # exact claim and is coverage-eligible, this fire finalizes
                # FAILED and the watermark stays where it was. The refusal is
                # evaluated before the finalizer opens its transaction, so a
                # refused fire performs no coverage SQL at all.
                try:
                    verified_outcome = _require_coverage_eligible_outcome(
                        prepared=prepared, schedule=schedule,
                        collected=collected_outcome,
                        platform_run_id=platform_run_id,
                    )
                    # M4 condition 6, evaluated in the same pure phase and on
                    # the same terms: a window that was not exactly tiled, or a
                    # sub-window that never completed, refuses here — before the
                    # finalizer opens its transaction — so the refusal costs the
                    # watermark nothing and performs no coverage SQL.
                    verified_completeness = _require_complete_window_evidence(
                        prepared=prepared, outcome=verified_outcome,
                    )
                except Exception as gate_error:  # noqa: BLE001 — see below
                    # Deliberately wider than `ScheduledOutcomeRefused`. The gate
                    # is pure and should raise nothing else, but an exception
                    # escaping here would skip `_finalize_run` and leave the row
                    # `RUNNING` — and `_count_running() > 0` then makes every
                    # later tick a no-op for *every* client until the 12-hour
                    # stale sweep. A defect in the gate must cost one fire, not
                    # the whole workflow.
                    if isinstance(gate_error, ScheduledOutcomeRefused):
                        code, detail = gate_error.code, str(gate_error)
                    else:
                        code = TRIPS_OUTCOME_GATE_FAILED
                        detail = (
                            f"{TRIPS_OUTCOME_GATE_FAILED}: coverage outcome gate "
                            f"raised {type(gate_error).__name__}"
                        )
                    refusal_ctx = {
                        **tail_ctx,
                        "outcome_refusal_code": code,
                        "outcome_refusal_reason": _bounded_refusal_reason(detail),
                        "coverage_advanced": False,
                    }
                    _finalize_run(
                        prepared.conn, run_history_id=run_history_id,
                        status="FAILED", error=detail,
                    )
                    log(
                        "ERROR",
                        "Job returned rc=0 but its terminal execution record "
                        f"cannot advance coverage ({code})",
                        refusal_ctx,
                    )
                    # The identity belongs in the message, not only in the log:
                    # this exception is what `ops/runner.py` turns into the
                    # terminal-failure incident, and that incident reads the
                    # dispatcher tick's params, which name no client.
                    raise RuntimeError(
                        "dispatched job returned rc=0 without a "
                        "coverage-eligible execution outcome: "
                        f"client_code={schedule.client_code or schedule.client_id} "
                        f"schedule_id={schedule.schedule_id} "
                        f"dataset={schedule.dataset_name} {detail}"
                    ) from gate_error
                tail_ctx["execution_outcome"] = verified_outcome.outcome
                tail_ctx["execution_outcome_upserted_count"] = (
                    verified_outcome.upserted_count
                )
                tail_ctx["window_subwindow_count"] = len(
                    verified_completeness.subwindows
                )
                tail_ctx["window_request_count"] = verified_completeness.page_count
                try:
                    moved = _finalize_compat_success(
                        prepared.conn, prepared=prepared,
                        completeness=verified_completeness,
                        platform_run_id=platform_run_id,
                    )
                    tail_ctx["coverage_advanced"] = moved
                except CoverageFinalizationError as finalization_error:
                    _report_coverage_finalization_failure(
                        client, run_id=run_id, schedule=schedule,
                        error=finalization_error, log=log,
                    )
                    raise
            else:
                _finalize_run(
                    prepared.conn, run_history_id=run_history_id,
                    status="SUCCESS", error=None,
                )
            log("INFO", f"Job finished SUCCESS (rc={rc})", tail_ctx)
        else:
            error_summary = ((err or "") + "\n" + (out or "")).strip()
            _finalize_run(
                prepared.conn, run_history_id=run_history_id,
                status="FAILED", error=error_summary,
            )
            log("ERROR", f"Job finished FAILED (rc={rc})", tail_ctx)
            raise RuntimeError(
                f"dispatched job failed: dataset={schedule.dataset_name} rc={rc}"
            )
    finally:
        _close_prepared(prepared)


def run(client, run_id: str, params: dict) -> None:
    """Compatibility entry point for direct callers outside ``ops/runner.py``."""
    prepared = prepare_run(params)
    if prepared is not None:
        run_prepared(client, run_id, params, prepared)
