#!/usr/bin/env python3
"""Manual sanity test for the Workflow A dispatcher (no DB, no network).

What this checks:

  * Schedule evaluation per frequency (daily / weekly / monthly numeric /
    monthly "last day"), including the timezone conversion:
    ``run_time`` is a local wall-clock time and ``scheduled_fire_ts`` is
    its UTC equivalent.
  * Window calculation: ``window_end_ts == scheduled_fire_ts`` and
    ``window_start_ts == window_end_ts - lookback_days``.
  * Deterministic queue ordering by
    ``(scheduled_fire_ts ASC, client_code ASC, dataset_name ASC)``.
  * Strict "single job per tick" selection in ``select_next_due``.
  * Registry validation: an unknown ``dataset_name`` and a
    ``job_module`` that does not match the Python registry are both
    rejected by ``_validate_against_registry``.

Run:

    cd /opt/log-platform
    PYTHONPATH="$PWD" python3 ops/tests_manual/test_workflow_a_dispatcher.py
"""
from __future__ import annotations

import sys
import tempfile
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.api.telematics import registry  # noqa: E402
from jobs.api.telematics.dispatcher import (  # noqa: E402
    ScheduleRow,
    _build_job_params,
    _claim_fire,
    _finalize_run,
    _is_compatibility_trips_fire,
    _mark_stale_running,
    _read_platform_run_id_file,
    _set_platform_run_id,
    _stale_running_timeout_minutes,
    _try_acquire_dispatcher_lock,
    _validate_against_registry,
    evaluate_schedule,
    latest_scheduled_fire_local,
    select_next_due,
)


FAILURES: list[str] = []


def _check(label: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    line = f"[{status}] {label}"
    if detail:
        line += f"\n        {detail}"
    print(line)
    if not ok:
        FAILURES.append(label)


class _FakeCursor:
    def __init__(self, conn: "_FakeConn"):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append((str(sql), params))

    def fetchone(self):
        return self.conn.fetchone_value

    def fetchall(self):
        return self.conn.fetchall_value


class _FakeConn:
    def __init__(self, *, fetchone_value=None, fetchall_value=None):
        self.fetchone_value = fetchone_value
        self.fetchall_value = fetchall_value or []
        self.executed: list[tuple[str, object]] = []
        self.commits = 0

    def cursor(self, *args, **kwargs):
        return _FakeCursor(self)

    def commit(self):
        self.commits += 1


# ---------------------------------------------------------------------------
# Schedule fixtures
# ---------------------------------------------------------------------------

# Use a real registered (dataset_name, job_module) pair so the dispatcher's
# allowlist check accepts the row in queueing tests below.
TRIPS_DS = "trips_sync"
TRIPS_JOB = registry.DATASETS[TRIPS_DS].job_module
FUEL_DS = "fuel_daily_aggregation"
FUEL_JOB = registry.DATASETS[FUEL_DS].job_module


def _row(
    *, schedule_id="s1", client_id="c1", client_code="C001",
    client_name="Acme", dataset_name=TRIPS_DS, job_module=TRIPS_JOB,
    enabled=True, frequency="daily", day_of_week=None, day_of_month=None,
    day_of_month_last=False, run_time=time(2, 0), timezone_name="UTC",
    lookback_days=1, overwrite_existing=True, event_enrichment_mode="enabled",
    **compat_overrides,
) -> ScheduleRow:
    """Build a ScheduleRow. `compat_overrides` reaches the C5 client fields,
    which default to `strict_meta` and the C2 numerics."""
    return ScheduleRow(
        schedule_id=schedule_id, client_id=client_id, client_code=client_code,
        client_name=client_name, dataset_name=dataset_name, job_module=job_module,
        enabled=enabled, frequency=frequency, day_of_week=day_of_week,
        day_of_month=day_of_month, day_of_month_last=day_of_month_last,
        run_time=run_time, timezone_name=timezone_name,
        lookback_days=lookback_days, overwrite_existing=overwrite_existing,
        event_enrichment_mode=event_enrichment_mode,
        **compat_overrides,
    )


# ---------------------------------------------------------------------------
# 1) Daily evaluation + UTC vs Europe/Warsaw timezone correctness
# ---------------------------------------------------------------------------

print("# Daily + timezone")

# Schedule: every day at 02:00 Europe/Warsaw -> 00:00:00Z (winter; UTC+1).
sched = _row(frequency="daily", run_time=time(2, 0), timezone_name="Europe/Warsaw",
             lookback_days=2)

# now = 01:30Z on Jan 15 2026 -> 02:30 local (winter, UTC+1).
# Today's 02:00 local = 01:00Z is in the past. Latest fire == today's 01:00Z.
now_utc = datetime(2026, 1, 15, 1, 30, tzinfo=timezone.utc)
ev = evaluate_schedule(now_utc=now_utc, sched=sched)
_check(
    "daily / Europe/Warsaw winter — latest fire is today 01:00Z",
    ev is not None and ev[0] == datetime(2026, 1, 15, 1, 0, tzinfo=timezone.utc),
    f"got {ev}",
)

# Window: lookback_days=2 -> [today 01:00Z - 2d, today 01:00Z].
fire_utc, win_start, win_end = ev  # type: ignore[misc]
_check(
    "daily — window_end == scheduled_fire_ts (UTC)",
    win_end == fire_utc,
    f"win_end={win_end} fire_utc={fire_utc}",
)
_check(
    "daily — window_start == fire - lookback_days",
    win_start == datetime(2026, 1, 13, 1, 0, tzinfo=timezone.utc),
    f"win_start={win_start}",
)

# Same schedule, but now is 00:30Z -> 01:30 local (before today's 02:00).
# Latest fire == YESTERDAY's 02:00 local == yesterday's 01:00Z.
now_utc = datetime(2026, 1, 15, 0, 30, tzinfo=timezone.utc)
ev = evaluate_schedule(now_utc=now_utc, sched=sched)
_check(
    "daily / Europe/Warsaw — before today's run_time -> yesterday fire",
    ev is not None and ev[0] == datetime(2026, 1, 14, 1, 0, tzinfo=timezone.utc),
    f"got {ev}",
)

# Summer DST sanity: July 1 2026, UTC+2 -> 02:00 Warsaw == 00:00Z.
sched = _row(frequency="daily", run_time=time(2, 0), timezone_name="Europe/Warsaw",
             lookback_days=1)
now_utc = datetime(2026, 7, 1, 6, 0, tzinfo=timezone.utc)
ev = evaluate_schedule(now_utc=now_utc, sched=sched)
_check(
    "daily / Europe/Warsaw summer (UTC+2) — fire = today 00:00Z",
    ev is not None and ev[0] == datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc),
    f"got {ev}",
)

# Daily / pure UTC: fire at exact `now` should still count as due.
sched = _row(frequency="daily", run_time=time(3, 0), timezone_name="UTC", lookback_days=7)
now_utc = datetime(2026, 4, 27, 3, 0, tzinfo=timezone.utc)
ev = evaluate_schedule(now_utc=now_utc, sched=sched)
_check(
    "daily / UTC — fire == now is treated as due",
    ev is not None and ev[0] == now_utc,
    f"got {ev}",
)


# ---------------------------------------------------------------------------
# 2) Weekly evaluation
# ---------------------------------------------------------------------------

print("\n# Weekly")

# Wednesday in Python = weekday() == 2.
# Sunday 2026-04-26 (UTC), 10:00Z. Most recent Wed at 02:00Z is 2026-04-22.
sched = _row(frequency="weekly", day_of_week=2, run_time=time(2, 0),
             timezone_name="UTC", lookback_days=7)
now_utc = datetime(2026, 4, 26, 10, 0, tzinfo=timezone.utc)
ev = evaluate_schedule(now_utc=now_utc, sched=sched)
_check(
    "weekly / Wednesday 02:00 UTC — latest fire is the most recent Wednesday",
    ev is not None and ev[0] == datetime(2026, 4, 22, 2, 0, tzinfo=timezone.utc),
    f"got {ev}",
)

# It IS Wednesday but BEFORE 02:00Z -> latest fire is last Wednesday.
now_utc = datetime(2026, 4, 22, 1, 0, tzinfo=timezone.utc)
ev = evaluate_schedule(now_utc=now_utc, sched=sched)
_check(
    "weekly / Wed before run_time — fire is previous Wednesday",
    ev is not None and ev[0] == datetime(2026, 4, 15, 2, 0, tzinfo=timezone.utc),
    f"got {ev}",
)


# ---------------------------------------------------------------------------
# 3) Monthly evaluation — numeric day_of_month
# ---------------------------------------------------------------------------

print("\n# Monthly numeric")

# Day 10 of each month at 02:00 UTC. Today = 2026-04-15 12:00Z -> April 10.
sched = _row(frequency="monthly", day_of_month=10, run_time=time(2, 0),
             timezone_name="UTC", lookback_days=30)
now_utc = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
ev = evaluate_schedule(now_utc=now_utc, sched=sched)
_check(
    "monthly / day=10 — same month, day already passed",
    ev is not None and ev[0] == datetime(2026, 4, 10, 2, 0, tzinfo=timezone.utc),
    f"got {ev}",
)

# Today is April 5 -> day=10 hasn't happened yet this month -> March 10.
now_utc = datetime(2026, 4, 5, 12, 0, tzinfo=timezone.utc)
ev = evaluate_schedule(now_utc=now_utc, sched=sched)
_check(
    "monthly / day=10 — earlier in month -> previous month",
    ev is not None and ev[0] == datetime(2026, 3, 10, 2, 0, tzinfo=timezone.utc),
    f"got {ev}",
)


# ---------------------------------------------------------------------------
# 4) Monthly evaluation — "last day"
# ---------------------------------------------------------------------------

print("\n# Monthly last")

# Last day of month at 23:00 UTC. Today = March 30 -> March 31 is in the
# future, so latest fire = February 28.
sched = _row(frequency="monthly", day_of_month=None, day_of_month_last=True,
             run_time=time(23, 0), timezone_name="UTC", lookback_days=30)
now_utc = datetime(2026, 3, 30, 12, 0, tzinfo=timezone.utc)
ev = evaluate_schedule(now_utc=now_utc, sched=sched)
_check(
    "monthly / last — March 30 noon -> February 28 23:00 UTC",
    ev is not None and ev[0] == datetime(2026, 2, 28, 23, 0, tzinfo=timezone.utc),
    f"got {ev}",
)

# Today = March 31 23:30 -> latest fire IS today's March 31 23:00.
now_utc = datetime(2026, 3, 31, 23, 30, tzinfo=timezone.utc)
ev = evaluate_schedule(now_utc=now_utc, sched=sched)
_check(
    "monthly / last — March 31 23:30 -> today",
    ev is not None and ev[0] == datetime(2026, 3, 31, 23, 0, tzinfo=timezone.utc),
    f"got {ev}",
)


# ---------------------------------------------------------------------------
# 5) Window calculation matches the design contract
# ---------------------------------------------------------------------------

print("\n# Window calculation")

# Spec example: scheduled at 02:00 Europe/Warsaw with lookback_days=1
# -> window_end_ts = 00:00:00Z (winter), window_start_ts = -24h.
sched = _row(frequency="daily", run_time=time(2, 0),
             timezone_name="Europe/Warsaw", lookback_days=1)
now_utc = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
ev = evaluate_schedule(now_utc=now_utc, sched=sched)
fire_utc, win_start, win_end = ev  # type: ignore[misc]
_check(
    "window contract — fire == 01:00Z (Warsaw winter)",
    fire_utc == datetime(2026, 1, 15, 1, 0, tzinfo=timezone.utc),
    f"fire={fire_utc}",
)
_check(
    "window contract — window_end == scheduled_fire_ts",
    win_end == fire_utc,
)
_check(
    "window contract — window_start == fire - lookback_days",
    win_start == datetime(2026, 1, 14, 1, 0, tzinfo=timezone.utc),
    f"win_start={win_start}",
)


# ---------------------------------------------------------------------------
# 6) Deterministic ordering + single-job-per-tick selection
# ---------------------------------------------------------------------------

print("\n# Queue ordering")

now_utc = datetime(2026, 4, 27, 5, 0, tzinfo=timezone.utc)

# Three due schedules with different fires/codes/datasets.
s_a_trips = _row(schedule_id="s_a_trips", client_id="ca", client_code="A",
                 client_name="Acme",
                 dataset_name=TRIPS_DS, job_module=TRIPS_JOB,
                 frequency="daily", run_time=time(3, 0))
s_a_fuel = _row(schedule_id="s_a_fuel", client_id="ca", client_code="A",
                client_name="Acme",
                dataset_name=FUEL_DS, job_module=FUEL_JOB,
                frequency="daily", run_time=time(3, 0))
s_b_trips = _row(schedule_id="s_b_trips", client_id="cb", client_code="B",
                 client_name="Beta",
                 dataset_name=TRIPS_DS, job_module=TRIPS_JOB,
                 frequency="daily", run_time=time(2, 0))

queue = select_next_due(now_utc=now_utc, schedules=[s_a_trips, s_a_fuel, s_b_trips])
order_ids = [t[0].schedule_id for t in queue]

# B fires at 02:00Z (earliest), A fires at 03:00Z. Within A: trips_sync
# sorts before fuel_daily_aggregation alphabetically.
_check(
    "queue ordered by (fire ASC, client_code ASC, dataset_name ASC)",
    order_ids == ["s_b_trips", "s_a_fuel", "s_a_trips"],
    f"got {order_ids}",
)
_check(
    "first claim candidate is the earliest fire",
    queue[0][0].schedule_id == "s_b_trips",
    f"first={queue[0][0].schedule_id}",
)


# ---------------------------------------------------------------------------
# 7) Skip-when-RUNNING: tested by the dispatcher's `_count_running` guard,
#    but we can verify the selection helper is otherwise pure (no DB call
#    happens in select_next_due itself).
# ---------------------------------------------------------------------------

print("\n# select_next_due edge cases")

# Empty input -> empty queue.
_check(
    "select_next_due([]) returns []",
    select_next_due(
        now_utc=datetime(2026, 4, 27, 1, 0, tzinfo=timezone.utc),
        schedules=[],
    ) == [],
)

# Unsupported frequency -> filtered out (no fire computable).
_check(
    "select_next_due skips schedules with an unsupported frequency",
    select_next_due(
        now_utc=datetime(2026, 4, 27, 1, 0, tzinfo=timezone.utc),
        schedules=[_row(frequency="hourly", run_time=time(2, 0),
                        timezone_name="UTC")],
    ) == [],
)

# Unknown timezone -> filtered out.
_check(
    "select_next_due skips schedules with an unknown timezone",
    select_next_due(
        now_utc=datetime(2026, 4, 27, 1, 0, tzinfo=timezone.utc),
        schedules=[_row(frequency="daily", run_time=time(2, 0),
                        timezone_name="No/Such_Zone")],
    ) == [],
)


# ---------------------------------------------------------------------------
# 8) Registry validation
# ---------------------------------------------------------------------------

print("\n# Registry validation")

ok_row = _row(dataset_name=TRIPS_DS, job_module=TRIPS_JOB)
_check("registry-valid row -> None",
       _validate_against_registry(ok_row) is None)

bad_dataset = _row(dataset_name="ghost_dataset", job_module="anything")
_check("unknown dataset is rejected",
       _validate_against_registry(bad_dataset) is not None,
       _validate_against_registry(bad_dataset) or "")

bad_module = _row(dataset_name=TRIPS_DS, job_module="evil.module")
_check("dataset/job_module mismatch is rejected",
       _validate_against_registry(bad_module) is not None,
       _validate_against_registry(bad_module) or "")


# ---------------------------------------------------------------------------
# 9) latest_scheduled_fire_local: spot check independent of evaluate_schedule
# ---------------------------------------------------------------------------

print("\n# latest_scheduled_fire_local")

tz = ZoneInfo("UTC")
now_local = datetime(2026, 4, 27, 5, 0, tzinfo=tz)
sched = _row(frequency="daily", run_time=time(3, 0), timezone_name="UTC")
fire = latest_scheduled_fire_local(now_local=now_local, sched=sched)
_check(
    "latest_scheduled_fire_local matches today 03:00 when now=05:00",
    fire == datetime(2026, 4, 27, 3, 0, tzinfo=tz),
    f"got {fire}",
)


# ---------------------------------------------------------------------------
# 10) Production hardening helpers: advisory lock, stale RUNNING, run-id link
# ---------------------------------------------------------------------------

print("\n# Dispatcher hardening helpers")

lock_conn = _FakeConn(fetchone_value=(True,))
_check(
    "advisory lock acquired when pg_try_advisory_lock returns true",
    _try_acquire_dispatcher_lock(lock_conn) is True,
)
_check(
    "advisory lock uses pg_try_advisory_lock",
    lock_conn.executed and "pg_try_advisory_lock" in lock_conn.executed[0][0],
    lock_conn.executed[0][0] if lock_conn.executed else "",
)
_check("advisory lock helper commits after SELECT", lock_conn.commits == 1)

busy_conn = _FakeConn(fetchone_value=(False,))
_check(
    "advisory lock not acquired when another dispatcher holds it",
    _try_acquire_dispatcher_lock(busy_conn) is False,
)

now_for_stale = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)
started_at = datetime(2026, 4, 26, 23, 0, tzinfo=timezone.utc)
stale_conn = _FakeConn(fetchall_value=[
    ("rh1", "s1", "c1", "C001", "trips_sync", now_for_stale, started_at, started_at)
])
stale_rows = _mark_stale_running(
    stale_conn, stale_after_minutes=720, now_utc=now_for_stale,
)
_check(
    "stale RUNNING rows are marked FAILED",
    stale_conn.executed
    and "SET status='FAILED'" in stale_conn.executed[0][0]
    and "COALESCE(started_at, created_at) < %s" in stale_conn.executed[0][0],
    stale_conn.executed[0][0] if stale_conn.executed else "",
)
_check("stale RUNNING helper returns updated rows", stale_rows[0]["run_history_id"] == "rh1")
_check("stale RUNNING helper returns client_code", stale_rows[0]["client_code"] == "C001")
_check("stale RUNNING helper commits update", stale_conn.commits == 1)

claim_conn = _FakeConn(fetchone_value=("f6222a11-06ee-4e4f-8b25-302a9d963cfa",))
claimed = _claim_fire(
    claim_conn,
    schedule_id="b454f82c-5857-4bab-8342-b7258e5cf7de",
    client_id="bd7662a5-eeb4-4614-8720-d477abfcb227",
    client_code="C001",
    dataset_name=TRIPS_DS,
    scheduled_fire_ts=now_for_stale,
    window_start_ts=started_at,
    window_end_ts=now_for_stale,
)
_check("claim_fire returns run_history_id", claimed == "f6222a11-06ee-4e4f-8b25-302a9d963cfa")
_check(
    "claim_fire INSERT includes client_code",
    claim_conn.executed
    and "client_id, client_code, dataset_name" in claim_conn.executed[0][0]
    and claim_conn.executed[0][1][2] == "C001",
    f"sql={claim_conn.executed[0][0] if claim_conn.executed else ''} "
    f"params={claim_conn.executed[0][1] if claim_conn.executed else None}",
)
_check("claim_fire commits insert", claim_conn.commits == 1)

params_with_code = _build_job_params(
    client_id="bd7662a5-eeb4-4614-8720-d477abfcb227",
    client_code="C001",
    dataset_name=TRIPS_DS,
    event_enrichment_mode="enabled",
    window_start_ts=started_at,
    window_end_ts=now_for_stale,
)
_check("dispatcher job params include client_code when available",
       params_with_code.get("client_code") == "C001",
       f"params={params_with_code!r}")
_check("dispatcher job params include default trips event_enrichment_mode",
       params_with_code.get("event_enrichment_mode") == "enabled",
       f"params={params_with_code!r}")

params_disabled_events = _build_job_params(
    client_id="bd7662a5-eeb4-4614-8720-d477abfcb227",
    client_code="C001",
    dataset_name=TRIPS_DS,
    event_enrichment_mode="disabled",
    window_start_ts=started_at,
    window_end_ts=now_for_stale,
)
_check("dispatcher job params pass disabled trips event_enrichment_mode",
       params_disabled_events.get("event_enrichment_mode") == "disabled",
       f"params={params_disabled_events!r}")

params_fuel = _build_job_params(
    client_id="bd7662a5-eeb4-4614-8720-d477abfcb227",
    client_code="C001",
    dataset_name=FUEL_DS,
    event_enrichment_mode="disabled",
    window_start_ts=started_at,
    window_end_ts=now_for_stale,
)
_check("dispatcher does not pass event_enrichment_mode to non-trips datasets",
       "event_enrichment_mode" not in params_fuel,
       f"params={params_fuel!r}")

invalid_event_mode_row = _row(event_enrichment_mode="bogus")
_check("invalid scheduled event_enrichment_mode is rejected",
       _validate_against_registry(invalid_event_mode_row) is not None,
       _validate_against_registry(invalid_event_mode_row) or "")

_check(
    "stale timeout param override is accepted",
    _stale_running_timeout_minutes({"stale_running_timeout_minutes": "30"}) == 30,
)

with tempfile.TemporaryDirectory() as tmpdir:
    run_id_file = Path(tmpdir) / "run_id.txt"
    run_id_file.write_text("bd7662a5-eeb4-4614-8720-d477abfcb227\n", encoding="utf-8")
    _check(
        "platform run id file parser returns UUID string",
        _read_platform_run_id_file(run_id_file) == "bd7662a5-eeb4-4614-8720-d477abfcb227",
    )
    run_id_file.write_text("not-a-uuid\n", encoding="utf-8")
    _check("platform run id file parser rejects invalid UUID", _read_platform_run_id_file(run_id_file) is None)

link_conn = _FakeConn()
_set_platform_run_id(
    link_conn,
    run_history_id="b454f82c-5857-4bab-8342-b7258e5cf7de",
    platform_run_id="bd7662a5-eeb4-4614-8720-d477abfcb227",
)
_check(
    "platform_run_id update writes client_schedule_run_history",
    link_conn.executed
    and "platform_run_id = %s" in link_conn.executed[0][0]
    and "WHERE run_history_id = %s" in link_conn.executed[0][0],
    link_conn.executed[0][0] if link_conn.executed else "",
)
_check("platform_run_id update commits", link_conn.commits == 1)

runner_src = (REPO_ROOT / "ops" / "runner.py").read_text(encoding="utf-8")
_check(
    "runner supports LOG_PLATFORM_RUN_ID_FILE handoff",
    "LOG_PLATFORM_RUN_ID_FILE" in runner_src and "_write_run_id_file_if_requested(run_id)" in runner_src,
)

migration_017 = (
    REPO_ROOT / "db" / "migrations" / "017_workflow_a_add_client_code_to_control_tables.sql"
).read_text(encoding="utf-8")
_check(
    "017 migration adds client_code to client_dataset_schedule",
    "ALTER TABLE workflow_a_control.client_dataset_schedule" in migration_017
    and "ADD COLUMN IF NOT EXISTS client_code TEXT" in migration_017,
)
_check(
    "017 migration adds client_code to client_schedule_run_history",
    "ALTER TABLE workflow_a_control.client_schedule_run_history" in migration_017
    and "ADD COLUMN IF NOT EXISTS client_code TEXT" in migration_017,
)
_check(
    "017 migration adds client_code to client_table_retention",
    "ALTER TABLE workflow_a_control.client_table_retention" in migration_017
    and "ADD COLUMN IF NOT EXISTS client_code TEXT" in migration_017,
)
_check(
    "017 migration backfills client_code from client_account",
    "UPDATE workflow_a_control.client_dataset_schedule cds" in migration_017
    and "UPDATE workflow_a_control.client_table_retention ctr" in migration_017
    and "UPDATE workflow_a_control.client_schedule_run_history h" in migration_017,
)
_check(
    "017 migration installs client_code consistency triggers",
    "set_control_client_code" in migration_017
    and "trg_client_dataset_schedule_client_code" in migration_017
    and "trg_client_schedule_run_history_client_code" in migration_017
    and "trg_client_table_retention_client_code" in migration_017,
)

migration_018 = (
    REPO_ROOT / "db" / "migrations" / "018_workflow_a_schedule_event_enrichment_mode.sql"
).read_text(encoding="utf-8")
_check(
    "018 migration adds schedule event_enrichment_mode column",
    "ALTER TABLE workflow_a_control.client_dataset_schedule" in migration_018
    and "ADD COLUMN IF NOT EXISTS event_enrichment_mode TEXT" in migration_018,
)
_check(
    "018 migration defaults schedule event_enrichment_mode to enabled",
    "ALTER COLUMN event_enrichment_mode SET DEFAULT 'enabled'" in migration_018
    and "SET event_enrichment_mode = 'enabled'" in migration_018,
)
_check(
    "018 migration makes schedule event_enrichment_mode not null",
    "ALTER COLUMN event_enrichment_mode SET NOT NULL" in migration_018,
)
_check(
    "018 migration constrains schedule event_enrichment_mode values",
    "ck_client_dataset_schedule_event_enrichment_mode" in migration_018
    and "event_enrichment_mode IN ('enabled', 'disabled')" in migration_018,
)


# ---------------------------------------------------------------------------
# 11) C5 strict-mode isolation: the coverage gate must not touch strict fires
# ---------------------------------------------------------------------------

print("\n# C5 strict-mode isolation")

default_row = _row()
_check(
    "ScheduleRow defaults to strict_meta with the C2 defaults",
    default_row.trips_pagination_mode == "strict_meta"
    and default_row.trips_stabilization_delay_seconds == 10800
    and default_row.trips_overlap_seconds == 3600
    and default_row.trips_max_recovery_span_seconds == 2678400,
    f"got {default_row}",
)
_check(
    "a strict trips_sync fire is not a compatibility fire",
    _is_compatibility_trips_fire(default_row) is False,
)
_check(
    "a non-trips dataset is never a compatibility fire",
    _is_compatibility_trips_fire(
        _row(dataset_name=FUEL_DS, job_module=FUEL_JOB,
             trips_pagination_mode="data_invariants_v1")
    ) is False,
)
_check(
    "only trips_sync on a data_invariants_v1 client is a compatibility fire",
    _is_compatibility_trips_fire(
        _row(trips_pagination_mode="data_invariants_v1")
    ) is True,
)

strict_claim_conn = _FakeConn(fetchone_value=("44444444-4444-4444-8444-444444444444",))
_claim_fire(
    strict_claim_conn,
    schedule_id="b454f82c-5857-4bab-8342-b7258e5cf7de",
    client_id="bd7662a5-eeb4-4614-8720-d477abfcb227",
    client_code="C001",
    dataset_name=TRIPS_DS,
    scheduled_fire_ts=now_for_stale,
    window_start_ts=started_at,
    window_end_ts=now_for_stale,
)
strict_claim_sql, strict_claim_params = strict_claim_conn.executed[0]
_check(
    "strict claim leaves all five evidence columns NULL",
    strict_claim_params[-5:] == (None, None, None, None, None),
    f"params={strict_claim_params}",
)
_check(
    "strict claim still writes the nominal window into window_start/end_ts",
    strict_claim_params[4] == started_at and strict_claim_params[5] == now_for_stale,
    f"params={strict_claim_params}",
)
_check(
    "claim INSERT never writes trips_max_recovery_span_seconds",
    "trips_max_recovery_span_seconds" not in strict_claim_sql,
)

strict_job_params = _build_job_params(
    client_id="bd7662a5-eeb4-4614-8720-d477abfcb227",
    client_code="C001",
    dataset_name=TRIPS_DS,
    event_enrichment_mode="enabled",
    window_start_ts=started_at,
    window_end_ts=now_for_stale,
    scheduled_fire_ts=now_for_stale,
)
_check(
    "strict trips params keep their pre-C5 values",
    strict_job_params["client_id"] == "bd7662a5-eeb4-4614-8720-d477abfcb227"
    and strict_job_params["trigger"] == "SCHEDULED"
    and strict_job_params["client_code"] == "C001"
    and strict_job_params["window_start_ts"] == "2026-04-26T23:00:00Z"
    and strict_job_params["window_end_ts"] == "2026-04-27T12:00:00Z"
    and strict_job_params["event_enrichment_mode"] == "enabled",
    f"params={strict_job_params!r}",
)
_check(
    "the strict trips params are scheduled_fire_ts and schedule_run_type (docs/13 R6)",
    set(strict_job_params) == {
        "client_id", "trigger", "client_code", "window_start_ts",
        "window_end_ts", "event_enrichment_mode", "scheduled_fire_ts",
        # Added for reconciliation event scope. Non-secret, dataset-scoped to
        # `trips_sync`, and inert for a base fire: it resolves to the historical
        # `window` scope. R6's point is that this set stays deliberate, not that
        # it stays frozen.
        "schedule_run_type",
    },
    f"params={sorted(strict_job_params)}",
)
_check(
    "non-trips datasets receive no scheduled_fire_ts from the trips branch",
    "scheduled_fire_ts" not in _build_job_params(
        client_id="bd7662a5-eeb4-4614-8720-d477abfcb227",
        client_code="C001", dataset_name=FUEL_DS,
        event_enrichment_mode="disabled",
        window_start_ts=started_at, window_end_ts=now_for_stale,
        scheduled_fire_ts=now_for_stale,
    ),
)

_check(
    "_finalize_run still writes only status, finished_at and error_summary",
    (lambda conn: (
        _finalize_run(conn, run_history_id="rh", status="SUCCESS", error=None),
        "SET status" in conn.executed[0][0]
        and "nominal_window" not in conn.executed[0][0]
        and "client_dataset_coverage" not in conn.executed[0][0],
    )[1])(_FakeConn()),
)


# ---------------------------------------------------------------------------

print("")
if FAILURES:
    print(f"FAIL — {len(FAILURES)} check(s) failed:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("OK — dispatcher schedule + queueing logic looks correct.")
sys.exit(0)
