#!/usr/bin/env python3
"""M2 — the DAILY Telematics `/trips` reconciliation horizon is L = 3.

Specification of record:
  docs/20_telematics_ingestion_permanent_repair_plan.md §3.1, §3.6, §14 (M2), §15
  docs/13_telematics_trips_stabilization_windows.md §16.1 (window arithmetic)
  docs/18_telematics_trips_request_time_contract.md (the wire-time boundary)

Pure: stdlib only, no network, no database, no secrets, no production access.
The provider HTTP layer is exercised through the real
`TelematicsFleetProviderClient` with a stubbed `requests.Session`, so the
production pagination state machine, the safety budgets and the `/trips`
Europe/Warsaw wire-time boundary all run for real. The window arithmetic runs
through the real `derive_effective_window` / `evaluate_coverage_gate`, and the
fire-time arithmetic through the real `dispatcher.evaluate_schedule`. Nothing
here re-implements a second time-window or DST model.

What this proves, in the order docs/20 §15's M2 rows ask for it:

  1. L lives in exactly one place — the control-plane row — and the M2 migration
     moves that one place from 1 to 3 without touching cadence, timezone,
     run_time or any other client.
  2. `evaluate_schedule` turns L = 3 into a nominal window of exactly
     3 x 86400 absolute seconds, and `derive_effective_window` turns it into
     `E_start == F - 3*86400 - D - O` against the in-force ALPHA D/O/R.
  3. The Europe/Warsaw wall-clock contract is preserved across both DST
     transitions: the wire numerals stay local wall clock and the effective
     window keeps its absolute-duration arithmetic.
  4. Two consecutive DAILY fires overlap by design, and the overlap is safe
     because the `client_trips` conflict target is the provider trip identity.
  5. Late arrival: a trip absent from run N's provider response and published
     before run N+1 is fetched by run N+1 under L = 3 — and would NOT have been
     under the historical L = 1. This is the behavioural point of M2.
  6. Explicit manual/backfill windows are untouched: that path takes an explicit
     window and never derives a lookback at all.
  7. A multi-page DAILY response is consumed completely under L = 3.
  7b. Under ALPHA's ACTUAL production configuration — `event_enrichment_mode =
     'disabled'`, `trip_metrics_population_source = 'report_207_migration'` —
     widening L = 1 to L = 3 issues zero `/vehicles/events` requests before and
     after. 0 -> 0, proven by counting real HTTP requests, not by comment.
  7c. Under a HYPOTHETICAL API-owned-enrichment configuration that ALPHA does not
     run, the same widening costs 7 -> 19 four-hour event chunks. Conditional
     analysis, retained because it is the one way L = 3 could be worse than
     L = 1 for a client configured that way.
  8. Configuration precedence is deterministic and the stale L = 1 defaults are
     provably not on the scheduled trips path.

Run from repo root:

    PYTHONDONTWRITEBYTECODE=1 python3 \
        ops/tests_manual/test_telematics_daily_lookback_l3.py
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.api.telematics.coverage_finalization import (  # noqa: E402
    COVERAGE_SOURCE_SCHEDULED_RUN,
)
from jobs.api.telematics.coverage_windows import (  # noqa: E402
    COVERAGE_GATE_ALLOWED,
    CoverageState,
    derive_effective_window,
    evaluate_coverage_gate,
)
from jobs.api.telematics.dispatcher import (  # noqa: E402
    ScheduleRow,
    _build_job_params,
    evaluate_schedule,
)
from jobs.api.telematics.provider_client import (  # noqa: E402
    TRIPS_MAX_SUB_WINDOW_DAYS,
    TelematicsFleetProviderClient,
    trips_wire_window,
)
from jobs.api.telematics.sync_trips_and_speeding import (  # noqa: E402
    TRIPS_DEFAULT_CHUNK_DAYS,
    VEHICLE_EVENTS_DEFAULT_CHUNK_HOURS,
    VEHICLE_EVENTS_ENRICHMENT_MODE_DISABLED,
    VEHICLE_EVENTS_ENRICHMENT_MODE_ENABLED,
    RegistrationFallbackConfig,
    _event_enrichment_mode,
    _event_fetch_strategy,
    _iter_fetch_vehicle_events_fleet_adaptive,
    _vehicle_events_chunk_delta,
    _vehicle_events_min_chunk_delta,
)
from jobs.trip_metrics_population_source import (  # noqa: E402
    TRIP_METRICS_SOURCE_API,
    TRIP_METRICS_SOURCE_REPORT_207,
    is_required_trip_metrics_source,
)
from jobs.api.telematics.provider_safety import (  # noqa: E402
    ProviderRunBudget,
    SafetyLimits,
)

WARSAW = ZoneInfo("Europe/Warsaw")
UTC = timezone.utc
WIRE_FMT = "%Y-%m-%d %H:%M:%S"
SECONDS_PER_DAY = 86_400

# The approved M2 horizon (docs/20 §3.1, §3.6, §14) and the horizon it replaces.
L_DAILY_APPROVED = 3
L_DAILY_HISTORICAL = 1

# ALPHA00001 production stabilization configuration (control plane, read-only —
# migration 056 / docs/20 §1.3).
ALPHA_DELAY_S = 10_800          # trips_stabilization_delay_seconds (D)
ALPHA_OVERLAP_S = 3_600         # trips_overlap_seconds (O)
ALPHA_MAX_RECOVERY_S = 2_678_400  # trips_max_recovery_span_seconds (R, 31 days)

ALPHA_SCHEDULE_ID = "eb099f69-4876-4c7e-8f60-a2bad0c35b5b"
ALPHA_CLIENT_ID = "9536f715-2fd0-4ffd-86ed-ba06f5490c5e"

# ALPHA00001's ACTUAL production event-enrichment configuration, read from the
# control plane (`client_dataset_schedule.event_enrichment_mode` and
# `client_account.trip_metrics_population_source`):
#
#   ALPHA00001 | trips_sync | daily | disabled | report_207_migration
#
# Both halves matter. `disabled` alone would skip the fetch; `report_207_migration`
# alone would also skip it, because the events fetch is gated on the metrics
# source FIRST. ALPHA satisfies both, which is why L = 3 costs it no provider
# events traffic at all. This is a fixture of record: if the control-plane row
# changes, the 0 -> 0 assertions below become false and must be re-derived, not
# re-labelled.
ALPHA_EVENT_ENRICHMENT_MODE = VEHICLE_EVENTS_ENRICHMENT_MODE_DISABLED
ALPHA_TRIP_METRICS_SOURCE = TRIP_METRICS_SOURCE_REPORT_207

# A configuration ALPHA does NOT run: API-owned trip metrics with enrichment
# enabled. Retained deliberately — it is the one shape in which widening the
# horizon carries a real provider cost, so the regression stays covered for any
# client configured this way (FOXTROT00001, DELTA00001 and ECHO00001 are).
HYPOTHETICAL_EVENT_ENRICHMENT_MODE = VEHICLE_EVENTS_ENRICHMENT_MODE_ENABLED
HYPOTHETICAL_TRIP_METRICS_SOURCE = TRIP_METRICS_SOURCE_API

M2_MIGRATION = REPO_ROOT / "db/migrations/060_workflow_a_daily_trips_lookback_l3.sql"

FAILURES: List[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"PASS  {name}")
    else:
        FAILURES.append(name)
        print(f"FAIL  {name}" + (f"  --  {detail}" if detail else ""))


def coverage_state(*, covered_through: datetime, coverage_start: datetime) -> CoverageState:
    return CoverageState(
        schedule_id=ALPHA_SCHEDULE_ID,
        client_id=ALPHA_CLIENT_ID,
        client_code="ALPHA00001",
        dataset_name="trips_sync",
        coverage_start_ts=coverage_start,
        covered_through_ts=covered_through,
        bootstrap_status="READY",
        bootstrap_evidence_ref="telematics-coverage-bootstrap/1:sha256=test",
        seeded_at=coverage_start,
        seeded_by="test",
        covered_through_source=COVERAGE_SOURCE_SCHEDULED_RUN,
        last_gap_detected_ts=None,
    )


def alpha_daily_row(
    *,
    lookback_days: int,
    timezone_name: str = "UTC",
    event_enrichment_mode: str = ALPHA_EVENT_ENRICHMENT_MODE,
) -> ScheduleRow:
    """The live ALPHA00001 daily trips schedule shape (docs/20 §1.2).

    Every default here is the row's ACTUAL production value, not a recommended
    or convenient one:

      * `timezone_name` — M2 does not change it; docs/20 §3.6 pairs timezone with
        run_time and §16 decision 3 leaves that pair open to the approver.
      * `event_enrichment_mode` — ALPHA runs `disabled`. An earlier revision of
        this suite defaulted it to `enabled` while calling the fixture live,
        which made the event-cost section describe a configuration ALPHA does not
        run. Pass the argument explicitly to model a different client.
    """
    return ScheduleRow(
        schedule_id=ALPHA_SCHEDULE_ID,
        client_id=ALPHA_CLIENT_ID,
        client_code="ALPHA00001",
        client_name="ALPHA00001",
        dataset_name="trips_sync",
        job_module="jobs.api.telematics.sync_trips_and_speeding",
        enabled=True,
        frequency="daily",
        day_of_week=None,
        day_of_month=None,
        day_of_month_last=False,
        run_time=time(2, 0),
        timezone_name=timezone_name,
        lookback_days=lookback_days,
        overwrite_existing=True,
        event_enrichment_mode=event_enrichment_mode,
        trips_pagination_mode="data_invariants_v1",
        trips_stabilization_delay_seconds=ALPHA_DELAY_S,
        trips_overlap_seconds=ALPHA_OVERLAP_S,
        trips_max_recovery_span_seconds=ALPHA_MAX_RECOVERY_S,
    )


def derive(*, fire: datetime, lookback_days: int, covered_through: datetime,
           coverage_start: datetime) -> Any:
    return derive_effective_window(
        scheduled_fire_ts=fire,
        lookback_days=lookback_days,
        stabilization_delay_seconds=ALPHA_DELAY_S,
        overlap_seconds=ALPHA_OVERLAP_S,
        max_recovery_span_seconds=ALPHA_MAX_RECOVERY_S,
        coverage_start_ts=coverage_start,
        covered_through_ts=covered_through,
    )


# ---------------------------------------------------------------------------
# 1. Where L lives, and what the M2 migration does to it.
#
# The claim under test is structural: there is exactly ONE production reader of
# `lookback_days` on the scheduled trips path, so M2 is a control-plane change
# and no code default needs flipping. If a second reader ever appears, these
# assertions fail and M2's premise must be re-derived.
# ---------------------------------------------------------------------------

dispatcher_src = (REPO_ROOT / "jobs/api/telematics/dispatcher.py").read_text(encoding="utf-8")
sync_src = (REPO_ROOT / "jobs/api/telematics/sync_trips_and_speeding.py").read_text(encoding="utf-8")
control_plane_src = (REPO_ROOT / "jobs/api/telematics/control_plane.py").read_text(encoding="utf-8")

check(
    "the dispatcher derives the nominal window from the schedule row's lookback_days",
    "window_start = window_end - timedelta(days=max(sched.lookback_days, 0))" in dispatcher_src,
)
check(
    "the trips sync job never derives a lookback of its own",
    "lookback" not in sync_src,
    "sync_trips_and_speeding mentions a lookback; L would no longer live in one place",
)
check(
    "the trips sync job REQUIRES an explicit window instead of defaulting one",
    'raise ValueError("Missing required params: window_start_ts, window_end_ts")' in sync_src,
)
check(
    "control_plane's lookback_days=1 is only the documented no-row placeholder",
    "`lookback_days=1` is a placeholder" in control_plane_src,
)

check("the M2 migration exists", M2_MIGRATION.is_file())
migration_src = M2_MIGRATION.read_text(encoding="utf-8") if M2_MIGRATION.is_file() else ""

check(
    "the M2 migration moves the approved horizon to 3",
    "approved_lookback   CONSTANT INTEGER := 3" in migration_src,
)
check(
    "the M2 migration requires the pre-M2 value to be 1 rather than repairing anything",
    "expected_lookback   CONSTANT INTEGER := 1" in migration_src
    and "M2 precondition failed" in migration_src,
)
check(
    "the M2 migration targets exactly one client and one dataset",
    "'ALPHA00001'" in migration_src and "'trips_sync'" in migration_src
    and "M2 target ambiguous" in migration_src,
)
check(
    "the M2 migration refuses a non-daily target",
    "M2 applies to the DAILY schedule only" in migration_src,
)
check(
    "the M2 migration asserts exactly one affected row",
    "M2 write anomaly" in migration_src and "GET DIAGNOSTICS affected = ROW_COUNT" in migration_src,
)
check(
    "the M2 migration is idempotent once converged",
    "M2 already converged" in migration_src,
)

# The columns M2 must not touch. `updated_at` is the one deliberate exception
# (configuration provenance), so it is excluded from the prohibition.
migration_set_clause = "\n".join(
    re.findall(r"\n\s*SET (.*?)\n\s*WHERE", migration_src, flags=re.S)
)
for forbidden in (
    "timezone", "run_time", "frequency", "day_of_week", "day_of_month",
    "enabled", "overwrite_existing", "event_enrichment_mode",
):
    check(
        f"the M2 migration does not write `{forbidden}`",
        forbidden not in migration_set_clause,
        f"found {forbidden} in the UPDATE SET list",
    )
check(
    "the M2 migration writes lookback_days",
    "lookback_days = approved_lookback" in migration_set_clause,
)

# ---------------------------------------------------------------------------
# 2. Nominal and effective window boundaries under L = 3.
#
# docs/20 §15, M2 row: `E_start == F - 3*86400 - D - O`.
# ---------------------------------------------------------------------------

# A normal, DST-quiet date. `now` is a minute past the fire so the schedule is
# due; the ALPHA row fires at 02:00 in its own `UTC` timezone.
fire_normal = datetime(2026, 8, 20, 2, 0, tzinfo=UTC)
now_normal = fire_normal + timedelta(minutes=1)

evaluated = evaluate_schedule(
    now_utc=now_normal, sched=alpha_daily_row(lookback_days=L_DAILY_APPROVED)
)
check("the DAILY schedule is due at its fire time", evaluated is not None)
if evaluated is not None:
    fire_utc, nominal_start, nominal_end = evaluated
    check(
        "the DAILY fire is the 02:00 boundary itself",
        fire_utc == fire_normal,
        f"got {fire_utc}",
    )
    check(
        "the nominal DAILY window ends at the fire",
        nominal_end == fire_utc,
    )
    check(
        "the nominal DAILY window spans exactly 3 x 86400 absolute seconds",
        nominal_end - nominal_start == timedelta(seconds=L_DAILY_APPROVED * SECONDS_PER_DAY),
        f"got {nominal_end - nominal_start}",
    )
    check(
        "the L=3 nominal window reaches strictly further back than the historical L=1",
        nominal_start
        < evaluate_schedule(
            now_utc=now_normal, sched=alpha_daily_row(lookback_days=L_DAILY_HISTORICAL)
        )[1],
    )

coverage_start = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
covered_through = fire_normal - timedelta(seconds=ALPHA_DELAY_S)  # previous fire's frontier

w3 = derive(
    fire=fire_normal, lookback_days=L_DAILY_APPROVED,
    covered_through=covered_through, coverage_start=coverage_start,
)
w1 = derive(
    fire=fire_normal, lookback_days=L_DAILY_HISTORICAL,
    covered_through=covered_through, coverage_start=coverage_start,
)

check(
    "E_start == F - 3*86400 - D - O exactly (docs/20 §15 M2 row)",
    w3.effective_window_start_ts
    == fire_normal
    - timedelta(seconds=L_DAILY_APPROVED * SECONDS_PER_DAY)
    - timedelta(seconds=ALPHA_DELAY_S)
    - timedelta(seconds=ALPHA_OVERLAP_S),
    f"got {w3.effective_window_start_ts}",
)
check(
    "E_end == F - D, unchanged by the wider lookback",
    w3.effective_window_end_ts == fire_normal - timedelta(seconds=ALPHA_DELAY_S)
    and w3.effective_window_end_ts == w1.effective_window_end_ts,
)
check(
    "L=3 reaches exactly two more days back than L=1",
    w1.effective_window_start_ts - w3.effective_window_start_ts
    == timedelta(days=L_DAILY_APPROVED - L_DAILY_HISTORICAL),
)
check(
    "the L=3 window starts strictly behind the coverage watermark (a real re-request)",
    w3.effective_window_start_ts < covered_through,
)
check(
    "the L=3 window stays connected — no gap is declared by widening the horizon",
    w3.is_connected,
)
check(
    "R does not clamp at L=3 (raising R is M7's business, not M2's)",
    w3.effective_window_start_ts
    > w3.effective_window_end_ts - timedelta(seconds=ALPHA_MAX_RECOVERY_S),
)
# Two separate limits, and the applied one is NOT the provider ceiling. The
# dispatcher passes no `chunk_days`, so the job chunks at `TRIPS_DEFAULT_CHUNK_DAYS`
# and a 3 d + O window becomes 2 requests-worth of chunks; the 30-day
# `TRIPS_MAX_SUB_WINDOW_DAYS` is the provider client's own ceiling on top of that.
l3_span = w3.effective_window_end_ts - w3.effective_window_start_ts
check(
    "the dispatcher passes no chunk_days, so the job's own default applies",
    "chunk_days" not in dispatcher_src,
)
check(
    "L=3 chunks into exactly 2 trips sub-windows at the applied chunk size",
    -(-l3_span // timedelta(days=TRIPS_DEFAULT_CHUNK_DAYS)) == 2,
    f"span {l3_span} at chunk_days={TRIPS_DEFAULT_CHUNK_DAYS}",
)
check(
    "each chunk stays far inside the provider sub-window ceiling",
    timedelta(days=TRIPS_DEFAULT_CHUNK_DAYS) < timedelta(days=TRIPS_MAX_SUB_WINDOW_DAYS),
)

gate = evaluate_coverage_gate(
    schedule_id=ALPHA_SCHEDULE_ID,
    client_id=ALPHA_CLIENT_ID,
    client_code="ALPHA00001",
    dataset_name="trips_sync",
    scheduled_fire_ts=fire_normal,
    lookback_days=L_DAILY_APPROVED,
    stabilization_delay_seconds=ALPHA_DELAY_S,
    overlap_seconds=ALPHA_OVERLAP_S,
    max_recovery_span_seconds=ALPHA_MAX_RECOVERY_S,
    coverage_state=coverage_state(
        covered_through=covered_through, coverage_start=coverage_start
    ),
    now_utc=now_normal,
)
check("the ordinary scheduled gate ALLOWS the L=3 daily fire", gate.allowed)
check(
    "the L=3 daily fire needs no manual recovery authority",
    gate.classification == COVERAGE_GATE_ALLOWED,
)
check(
    "the gate echoes the watermark unchanged rather than proposing to move it back",
    gate.covered_through_ts == covered_through,
)

# ---------------------------------------------------------------------------
# 3. DST safety — the M0 wall-clock/wire-window contract is not regressed.
#
# Both Europe/Warsaw transitions in 2026: spring forward 2026-03-29 02:00 ->
# 03:00 (CET->CEST), autumn back 2026-10-25 03:00 -> 02:00 (CEST->CET). The
# effective window is absolute-duration arithmetic, so the UTC span must stay
# exactly 3 days + D + O across both; only the Warsaw wall-clock numerals on the
# wire shift, and they shift because the provider contract says they must.
# ---------------------------------------------------------------------------

# E_end = F - D and E_start = F - L*86400 - D - O, so the span is L*86400 + O.
# D cancels: the stabilization delay shifts both bounds equally.
expected_span = timedelta(seconds=L_DAILY_APPROVED * SECONDS_PER_DAY + ALPHA_OVERLAP_S)

dst_fires = {
    "spring-forward (2026-03-29, CET->CEST)": datetime(2026, 3, 30, 2, 0, tzinfo=UTC),
    "autumn-back (2026-10-25, CEST->CET)": datetime(2026, 10, 26, 2, 0, tzinfo=UTC),
    "DST-quiet control": fire_normal,
}

for label, fire in dst_fires.items():
    # Each fixture carries its own coverage interval: `coverage_start` must
    # precede the watermark, and these fires span three different months.
    w = derive(
        fire=fire, lookback_days=L_DAILY_APPROVED,
        covered_through=fire - timedelta(seconds=ALPHA_DELAY_S),
        coverage_start=fire - timedelta(days=30),
    )
    reach = w.effective_window_end_ts - w.effective_window_start_ts
    check(
        f"{label}: the L=3 effective span is exactly 3d + O in absolute seconds",
        reach == expected_span,
        f"got {reach}",
    )
    check(
        f"{label}: E_start == F - 3*86400 - D - O regardless of the transition",
        w.effective_window_start_ts
        == fire - timedelta(seconds=L_DAILY_APPROVED * SECONDS_PER_DAY + ALPHA_DELAY_S + ALPHA_OVERLAP_S),
    )

    # The wire numerals come from the single existing boundary
    # (`_wall_clock_wire_window`), never re-derived here. They are the Warsaw
    # wall clock of the *effective* instants — which the boundary may widen
    # outwards across an ambiguous transition, and may only ever widen outwards.
    wire_start, wire_end, diag = trips_wire_window(
        w.effective_window_start_ts, w.effective_window_end_ts
    )
    widened_start = timedelta(seconds=diag["dst_widened_start_seconds"])
    widened_end = timedelta(seconds=diag["dst_widened_end_seconds"])

    check(
        f"{label}: DST widening only ever over-fetches, never under-fetches",
        widened_start >= timedelta(0) and widened_end >= timedelta(0),
        f"widened start {widened_start}, end {widened_end}",
    )
    check(
        f"{label}: the wire start is Warsaw wall clock, not the UTC projection",
        wire_start
        == (w.effective_window_start_ts - widened_start)
        .astimezone(WARSAW).replace(tzinfo=None).strftime(WIRE_FMT),
        f"wire {wire_start}",
    )
    check(
        f"{label}: the wire end is Warsaw wall clock, not the UTC projection",
        wire_end
        == (w.effective_window_end_ts + widened_end)
        .astimezone(WARSAW).replace(tzinfo=None).strftime(WIRE_FMT),
        f"wire {wire_end}",
    )
    check(
        f"{label}: the request stays far inside the provider sub-window limit",
        (w.effective_window_end_ts + widened_end)
        - (w.effective_window_start_ts - widened_start)
        < timedelta(days=TRIPS_MAX_SUB_WINDOW_DAYS),
    )

check(
    "the DST-quiet control needs no widening at all",
    trips_wire_window(
        w3.effective_window_start_ts, w3.effective_window_end_ts
    )[2]["dst_widened_seconds"] == 0,
)

# Across the autumn transition the Warsaw wall-clock span is one hour SHORTER
# than the absolute span (the hour repeats), and across spring one hour LONGER.
# The provider is asked in wall clock, so this is the widening the existing
# infrastructure already handles; M2 must not "fix" it.
autumn_fire = dst_fires["autumn-back (2026-10-25, CEST->CET)"]
autumn = derive(
    fire=autumn_fire,
    lookback_days=L_DAILY_APPROVED,
    covered_through=autumn_fire - timedelta(seconds=ALPHA_DELAY_S),
    coverage_start=autumn_fire - timedelta(days=30),
)
a_start, a_end, _a_diag = trips_wire_window(
    autumn.effective_window_start_ts, autumn.effective_window_end_ts
)
wall_span = datetime.strptime(a_end, WIRE_FMT) - datetime.strptime(a_start, WIRE_FMT)
check(
    "autumn-back: the Warsaw wall-clock request span is 1 h shorter than the absolute span",
    wall_span == expected_span - timedelta(hours=1),
    f"wall {wall_span} vs absolute {expected_span}",
)
# No second DST implementation: exactly one module in the executable tree defines
# the wall-clock converter, and this suite is a caller of it, not a reimplementation.
wire_definers = sorted(
    str(path.relative_to(REPO_ROOT))
    for path in REPO_ROOT.rglob("*.py")
    if ".venv" not in path.parts
    and path.name != Path(__file__).name
    and "def _wall_clock_wire_window" in path.read_text(encoding="utf-8", errors="replace")
)
check(
    "M2 introduces no second DST implementation — exactly one module defines the converter",
    wire_definers == ["jobs/api/telematics/provider_client.py"],
    f"definers: {wire_definers}",
)

# ---------------------------------------------------------------------------
# 4. Consecutive DAILY fires overlap by design, and the overlap is idempotent.
# ---------------------------------------------------------------------------

fire_n = fire_normal
fire_n1 = fire_normal + timedelta(days=1)

w_n = derive(
    fire=fire_n, lookback_days=L_DAILY_APPROVED,
    covered_through=fire_n - timedelta(seconds=ALPHA_DELAY_S),
    coverage_start=coverage_start,
)
w_n1 = derive(
    fire=fire_n1, lookback_days=L_DAILY_APPROVED,
    covered_through=w_n.effective_window_end_ts,
    coverage_start=coverage_start,
)

check(
    "consecutive DAILY fires overlap (the overlap is intentional, not a defect)",
    w_n1.effective_window_start_ts < w_n.effective_window_end_ts,
    f"N+1 start {w_n1.effective_window_start_ts} vs N end {w_n.effective_window_end_ts}",
)
check(
    "the overlap between consecutive L=3 fires is 2 days + O",
    w_n.effective_window_end_ts - w_n1.effective_window_start_ts
    == timedelta(days=L_DAILY_APPROVED - 1, seconds=ALPHA_OVERLAP_S),
    f"got {w_n.effective_window_end_ts - w_n1.effective_window_start_ts}",
)
check(
    "the window is never shortened on the basis of a previous success",
    w_n1.effective_window_start_ts
    == fire_n1 - timedelta(seconds=L_DAILY_APPROVED * SECONDS_PER_DAY + ALPHA_DELAY_S + ALPHA_OVERLAP_S),
)
check(
    "the watermark still advances forward across the overlapping fire",
    w_n1.effective_window_end_ts > w_n.effective_window_end_ts,
)

# Persistence identity: an overlapping re-request cannot create a second logical
# trip, because the conflict target is the provider trip identity.
conflict_blocks = re.findall(
    r"ON CONFLICT \(client_id, provider_trip_id\) DO UPDATE SET(.*?)\n\s*\"\"\"",
    sync_src,
    flags=re.S,
)
check(
    "the client_trips conflict target is (client_id, provider_trip_id)",
    bool(conflict_blocks),
)
check(
    "no other conflict target is used for client_trips",
    "ON CONFLICT" not in sync_src.replace("ON CONFLICT (client_id, provider_trip_id)", ""),
)
check(
    "the non-overwrite path is DO NOTHING, so it also cannot duplicate",
    "ON CONFLICT (client_id, provider_trip_id) DO NOTHING" in sync_src,
)
check(
    "Dysponent_ID is never written by the trips sync job, so re-upsert cannot clear it",
    '"Dysponent_ID"' not in sync_src,
)

# ---------------------------------------------------------------------------
# 5. Late arrival within L = 3 — the behavioural purpose of M2.
#
# TRIP_LATE starts 2 days before run N+1's fire. It is NOT in the provider's
# catalogue when run N executes, and IS by the time run N+1 executes. Under
# L = 3 run N+1's window reaches it; under the historical L = 1 it does not.
# ---------------------------------------------------------------------------


def trip(trip_id: int, reg: str, start_utc: datetime, distance_m: int) -> Dict[str, Any]:
    return {
        "trip_id": trip_id,
        "registration": reg,
        "start_timestamp": start_utc.strftime(WIRE_FMT),
        "end_timestamp": (start_utc + timedelta(minutes=20)).strftime(WIRE_FMT),
        "trip_distance": distance_m,
    }


class StubResponse:
    def __init__(self, payload: Dict[str, Any]) -> None:
        self._payload = payload
        self.status_code = 200
        self.headers: Dict[str, str] = {}
        self.content = b"{}"
        self.text = "{}"

    def json(self) -> Dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        return None


class StubSession:
    """Serves catalogue trips whose Warsaw wall clock falls in the request window.

    Genuinely paginated: `page_limit` rows per page, so a full page means
    "continue" and a short page means "end of data" — the shape
    `data_invariants_v1` reads control flow from.
    """

    def __init__(self, catalogue: List[Dict[str, Any]]) -> None:
        self.catalogue = catalogue
        self.requests: List[Dict[str, Any]] = []
        self.auth = None

    def request(self, method: str, url: str, **kwargs: Any) -> StubResponse:
        params = kwargs.get("params") or {}
        self.requests.append({"method": method, "url": url, "params": dict(params)})
        assert method.upper() == "GET", "the trips client must never issue a write request"
        start = datetime.strptime(params["start_timestamp"], WIRE_FMT)
        end = datetime.strptime(params["end_timestamp"], WIRE_FMT)
        limit = int(params.get("limit", 1000))
        page = int(params.get("page", 1))
        selected = [
            row
            for row in self.catalogue
            if start
            <= datetime.strptime(row["start_timestamp"], WIRE_FMT)
            .replace(tzinfo=UTC)
            .astimezone(WARSAW)
            .replace(tzinfo=None)
            <= end
        ]
        offset = (page - 1) * limit
        data = selected[offset:offset + limit]
        return StubResponse(
            {
                "data": data,
                "meta": {
                    "current_page": page,
                    "per_page": limit,
                    # Advisory only: `data_invariants_v1` must not steer on it.
                    "last_page": 1,
                },
            }
        )

    def get(self, url: str, **kwargs: Any) -> StubResponse:
        return self.request("GET", url, **kwargs)


def build_client(session: StubSession, *, page_limit: int = 1000) -> TelematicsFleetProviderClient:
    limits = SafetyLimits(
        max_requests_per_run=40,
        max_requests_per_endpoint=40,
        max_requests_per_subwindow=12,
        max_pages_per_subwindow=10,
        max_retries_per_http_call=0,
        timeout_s=30,
    )
    client = TelematicsFleetProviderClient(
        base_url="https://provider.invalid/rest",
        basic_auth_username="stub",
        basic_auth_password="stub",
        page_limit=page_limit,
        safety_limits=limits,
        budget=ProviderRunBudget(limits=limits),
        trips_pagination_mode="data_invariants_v1",
    )
    client._session = session
    return client


# The trip that arrives late: it happened 2 days before run N+1's fire, which is
# inside L=3's reach and outside L=1's.
LATE_TRIP_START = fire_n1 - timedelta(days=2)
TRIP_ON_TIME = trip(600_000_001, "XE10645", fire_n - timedelta(hours=6), 4211)
TRIP_LATE = trip(600_000_900, "XE10645", LATE_TRIP_START, 2534)

# Run N — the provider does not have the late trip yet.
session_n = StubSession([TRIP_ON_TIME])
run_n = build_client(session_n).fetch_trips(
    window_start_ts=w_n.effective_window_start_ts,
    window_end_ts=w_n.effective_window_end_ts,
)
run_n_ids = {int(r["trip_id"]) for r in run_n}
check(
    "run N cannot see the not-yet-published trip",
    600_000_900 not in run_n_ids,
    f"got {sorted(run_n_ids)}",
)
check("run N issued GET only", all(r["method"].upper() == "GET" for r in session_n.requests))

# Run N+1 — the trip has since been published, inside already-covered time.
session_n1 = StubSession([TRIP_ON_TIME, TRIP_LATE])
run_n1 = build_client(session_n1).fetch_trips(
    window_start_ts=w_n1.effective_window_start_ts,
    window_end_ts=w_n1.effective_window_end_ts,
)
run_n1_ids = [int(r["trip_id"]) for r in run_n1]

check(
    "the L=3 window of run N+1 reaches back over the late trip's start",
    w_n1.effective_window_start_ts <= LATE_TRIP_START <= w_n1.effective_window_end_ts,
    f"E=[{w_n1.effective_window_start_ts}, {w_n1.effective_window_end_ts}] trip {LATE_TRIP_START}",
)
check(
    "run N+1 ingests the late-published trip under L=3 — the point of M2",
    600_000_900 in run_n1_ids,
    f"got {sorted(run_n1_ids)}",
)
check(
    "run N+1 returns no duplicate provider trip ids",
    len(run_n1_ids) == len(set(run_n1_ids)),
)
check(
    "run N+1 re-fetches the already-seen trip too (overlap is real, and idempotent by identity)",
    600_000_001 in run_n1_ids,
)

# The counterfactual that makes the improvement load-bearing rather than
# incidental: under the historical L = 1 the same run misses the same trip.
w_n1_l1 = derive(
    fire=fire_n1, lookback_days=L_DAILY_HISTORICAL,
    covered_through=w_n.effective_window_end_ts,
    coverage_start=coverage_start,
)
check(
    "under the historical L=1 the same fire's window does NOT reach the late trip",
    w_n1_l1.effective_window_start_ts > LATE_TRIP_START,
    f"L=1 E_start {w_n1_l1.effective_window_start_ts} vs trip {LATE_TRIP_START}",
)
session_n1_l1 = StubSession([TRIP_ON_TIME, TRIP_LATE])
run_n1_l1 = build_client(session_n1_l1).fetch_trips(
    window_start_ts=w_n1_l1.effective_window_start_ts,
    window_end_ts=w_n1_l1.effective_window_end_ts,
)
check(
    "under the historical L=1 the late trip is structurally unreachable (the confirmed RC-1 state)",
    600_000_900 not in {int(r["trip_id"]) for r in run_n1_l1},
)

check(
    "run N+1's provider request carries Warsaw wall clock, not the UTC projection",
    session_n1.requests[0]["params"]["start_timestamp"]
    == trips_wire_window(
        w_n1.effective_window_start_ts, w_n1.effective_window_end_ts
    )[0],
)

# ---------------------------------------------------------------------------
# 6. Explicit manual / backfill windows are NOT converted into DAILY L=3.
# ---------------------------------------------------------------------------

recover_src = (REPO_ROOT / "ops/recover_telematics_trips_window.py").read_text(encoding="utf-8")
backfill_src = (REPO_ROOT / "jobs/api/telematics/backfill_trips_insert_only.py").read_text(encoding="utf-8")

check(
    "the manual recovery tool takes an explicit --window-start rather than a lookback",
    "--window-start" in recover_src,
)
check(
    "the manual recovery tool never converts lookback_days into a window",
    not re.search(r"timedelta\(\s*days\s*=\s*[a-z_]*lookback", recover_src),
    "recovery derives a window from a lookback; M2 could leak into it",
)
check(
    "the insert-only backfill takes an explicit window and never a lookback",
    "lookback" not in backfill_src,
    "the backfill mentions a lookback; the explicit-window contract would be at risk",
)
# Stronger than a comment scan: look at the statements themselves. M2 must be
# one UPDATE against one control-plane table, with no DDL and no other DML that
# could reach a recovery, backfill or coverage structure.
migration_statements = "\n".join(
    line for line in migration_src.splitlines() if not line.lstrip().startswith("--")
)
check(
    "the M2 migration issues exactly one UPDATE",
    migration_statements.count("UPDATE ") == 1,
    f"found {migration_statements.count('UPDATE ')}",
)
check(
    "the M2 migration's only UPDATE targets client_dataset_schedule",
    "UPDATE workflow_a_control.client_dataset_schedule" in migration_statements,
)
for verb in ("INSERT", "DELETE", "DROP", "TRUNCATE", "ALTER TABLE", "CREATE TABLE"):
    check(
        f"the M2 migration issues no {verb}",
        verb not in migration_statements,
    )
for untouched in (
    "client_dataset_coverage", "client_trips", "telematics_trips_recovery",
    "client_schedule_run_history",
):
    check(
        f"the M2 migration never references `{untouched}`",
        untouched not in migration_statements,
    )

# Behavioural: an explicitly supplied window is honoured byte-for-byte by the
# provider layer, with no lookback arithmetic anywhere near it.
explicit_start = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
explicit_end = datetime(2026, 7, 2, 0, 0, tzinfo=UTC)
session_explicit = StubSession([TRIP_ON_TIME, TRIP_LATE])
build_client(session_explicit).fetch_trips(
    window_start_ts=explicit_start, window_end_ts=explicit_end,
)
exp_wire_start, exp_wire_end, _exp_diag = trips_wire_window(explicit_start, explicit_end)
check(
    "an explicit window is requested exactly as given, unwidened by L=3",
    session_explicit.requests[0]["params"]["start_timestamp"] == exp_wire_start
    and session_explicit.requests[0]["params"]["end_timestamp"] == exp_wire_end,
    f"got {session_explicit.requests[0]['params']}",
)

# ---------------------------------------------------------------------------
# 7. Pagination is fully consumed under L = 3.
# ---------------------------------------------------------------------------

MULTI_PAGE_TRIPS = [
    trip(700_000_000 + i, "XE10645",
         w_n1.effective_window_start_ts + timedelta(hours=1 + i), 1000 + i)
    for i in range(7)
]
session_pages = StubSession(MULTI_PAGE_TRIPS)
paged = build_client(session_pages, page_limit=2).fetch_trips(
    window_start_ts=w_n1.effective_window_start_ts,
    window_end_ts=w_n1.effective_window_end_ts,
)
check(
    "a multi-page DAILY response is consumed completely under L=3",
    len(paged) == len(MULTI_PAGE_TRIPS),
    f"got {len(paged)} of {len(MULTI_PAGE_TRIPS)}",
)
check(
    "every page was actually requested — L=3 does not stop after page 1",
    len({r["params"].get("page") for r in session_pages.requests}) >= 4,
    f"pages requested: {sorted({r['params'].get('page') for r in session_pages.requests})}",
)
check(
    "no trip is lost or duplicated across pages",
    sorted(int(r["trip_id"]) for r in paged)
    == sorted(int(r["trip_id"]) for r in MULTI_PAGE_TRIPS),
)
check(
    "the advisory `last_page: 1` did not truncate the run (data drives control flow)",
    len(paged) > 2,
)

# ---------------------------------------------------------------------------
# 7b. What L=3 costs ALPHA in provider events traffic: nothing. Behaviourally.
#
# `/trips` is cheap. `/vehicles/events` is not — it chunks at 4 h, so tripling
# the window roughly triples the number of independent provider calls. Whether
# M2 pays that price is a property of the CLIENT's configuration, and ALPHA's is:
#
#     event_enrichment_mode          = disabled
#     trip_metrics_population_source = report_207_migration
#
# Either value alone already short-circuits the fetch; ALPHA has both. So the
# honest statement of M2's cost to ALPHA is 0 -> 0 requests, and that is asserted
# here by counting real HTTP requests through the real provider client rather
# than by asserting the arithmetic of a path that never runs.
#
# The chain below is deliberately not re-implemented: the dispatcher's own
# `_build_job_params` produces the runner params, the job's own
# `_event_enrichment_mode` / `_event_fetch_strategy` parse them back, the gate is
# `run()`'s gate verbatim, and the fetch is the real adaptive fleet iterator.
# ---------------------------------------------------------------------------


class EventsStubSession:
    """Records every provider request; answers with a well-formed empty page.

    One request per chunk: `data: []` with consistent `current_page`/`last_page`
    meta is the provider's end-of-data shape, so the fleet fetcher stops after
    page 1 and the request count IS the chunk count.
    """

    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []
        self.auth = None

    def request(self, method: str, url: str, **kwargs: Any) -> StubResponse:
        params = kwargs.get("params") or {}
        self.requests.append({"method": method, "url": url, "params": dict(params)})
        assert method.upper() == "GET", "the events client must never issue a write request"
        page = int(params.get("page", 1))
        return StubResponse(
            {"data": [], "meta": {"current_page": page, "last_page": 1, "total": 0}}
        )

    def get(self, url: str, **kwargs: Any) -> StubResponse:
        return self.request("GET", url, **kwargs)


# The registration fallback is a failure-recovery path (a chunk that errored),
# and no chunk errors here. Disabled explicitly so a fallback request can never
# be miscounted as a fleet chunk.
NO_REGISTRATION_FALLBACK = RegistrationFallbackConfig(
    enabled=False,
    rps=1.0,
    max_registrations=0,
    max_chunks=0,
    max_requests_per_run=0,
    max_requests_per_run_explicit=False,
    min_chunk_delta=timedelta(minutes=15),
)


def events_requests_for_fire(
    *, row: ScheduleRow, trip_metrics_population_source: str, window: Any
) -> List[Dict[str, Any]]:
    """Every `/vehicles/events` HTTP request one claimed fire would issue."""
    params = _build_job_params(
        client_id=row.client_id,
        client_code=row.client_code,
        dataset_name=row.dataset_name,
        event_enrichment_mode=row.event_enrichment_mode,
        window_start_ts=window.effective_window_start_ts,
        window_end_ts=window.effective_window_end_ts,
    )
    event_enrichment_mode = _event_enrichment_mode(params)
    event_fetch_strategy = _event_fetch_strategy(params)
    event_enrichment_disabled = (
        event_enrichment_mode == VEHICLE_EVENTS_ENRICHMENT_MODE_DISABLED
    )
    api_owns_trip_metrics = is_required_trip_metrics_source(
        trip_metrics_population_source, TRIP_METRICS_SOURCE_API
    )

    session = EventsStubSession()
    provider = build_client(session)

    # `sync_trips_and_speeding.run()`, verbatim:
    #     if not api_owns_trip_metrics or event_enrichment_disabled:
    #         pass
    #     elif trip_registrations_norm:
    #         ... fetch ...
    if not api_owns_trip_metrics or event_enrichment_disabled:
        pass
    else:
        for _batch in _iter_fetch_vehicle_events_fleet_adaptive(
            provider=provider,
            window_start_ts=window.effective_window_start_ts,
            window_end_ts=window.effective_window_end_ts,
            initial_chunk_delta=_vehicle_events_chunk_delta(),
            min_chunk_delta=_vehicle_events_min_chunk_delta(),
            fallback_config=NO_REGISTRATION_FALLBACK,
            fallback_registrations=[],
            limit=1000,
            max_pages=5,
            timeout_s=30,
            log_fn=lambda level, message, context: None,
        ):
            pass
    return [
        request
        for request in session.requests
        if str(request["url"]).rstrip("/").endswith("/vehicles/events")
    ]


check(
    "the driven gate is `run()`'s gate, not a paraphrase of it",
    "if not api_owns_trip_metrics or event_enrichment_disabled:" in sync_src,
    "the run() gate changed shape; this section's driver no longer mirrors it",
)
check(
    "the events chunk size in force is the 4 h default (no env override in play)",
    _vehicle_events_chunk_delta() == timedelta(hours=VEHICLE_EVENTS_DEFAULT_CHUNK_HOURS)
    and VEHICLE_EVENTS_DEFAULT_CHUNK_HOURS == 4,
    f"got {_vehicle_events_chunk_delta()}",
)

# The fixture of record: ALPHA's actual production pair.
alpha_l1 = events_requests_for_fire(
    row=alpha_daily_row(lookback_days=L_DAILY_HISTORICAL),
    trip_metrics_population_source=ALPHA_TRIP_METRICS_SOURCE,
    window=w1,
)
alpha_l3 = events_requests_for_fire(
    row=alpha_daily_row(lookback_days=L_DAILY_APPROVED),
    trip_metrics_population_source=ALPHA_TRIP_METRICS_SOURCE,
    window=w3,
)

check(
    "the current-production ALPHA fixture is enrichment=disabled, metrics=report_207_migration",
    alpha_daily_row(lookback_days=L_DAILY_APPROVED).event_enrichment_mode == "disabled"
    and ALPHA_TRIP_METRICS_SOURCE == "report_207_migration",
    f"mode {alpha_daily_row(lookback_days=L_DAILY_APPROVED).event_enrichment_mode}, "
    f"source {ALPHA_TRIP_METRICS_SOURCE}",
)
check(
    "current ALPHA at L=1 issues ZERO /vehicles/events requests",
    len(alpha_l1) == 0,
    f"got {len(alpha_l1)}",
)
check(
    "current ALPHA at L=3 issues ZERO /vehicles/events requests — 0 -> 0, M2 costs ALPHA no events traffic",
    len(alpha_l3) == 0,
    f"got {len(alpha_l3)}",
)
check(
    "neither horizon issues ANY provider request on the enrichment path for ALPHA",
    (len(alpha_l1), len(alpha_l3)) == (0, 0),
)
# Each half of the pair is independently sufficient. Asserted separately so a
# future control-plane change that flips only one of them is not mistaken for a
# behaviour-preserving edit.
check(
    "metrics ownership alone already skips the fetch for ALPHA's report_207_migration",
    not is_required_trip_metrics_source(ALPHA_TRIP_METRICS_SOURCE, TRIP_METRICS_SOURCE_API),
)
check(
    "enrichment mode alone already skips the fetch for ALPHA's `disabled`",
    _event_enrichment_mode({"event_enrichment_mode": ALPHA_EVENT_ENRICHMENT_MODE})
    == VEHICLE_EVENTS_ENRICHMENT_MODE_DISABLED,
)
check(
    "the disabled-mode skip is a logged decision, not an accident of empty inventory",
    "Vehicle event enrichment disabled by event_enrichment_mode; skipping /vehicles/events fetch"
    in sync_src
    and "Event-derived trip metric population skipped by trip_metrics_population_source"
    in sync_src,
)

# ---------------------------------------------------------------------------
# 7c. CONDITIONAL — the cost L=3 would impose on a client ALPHA is not.
#
# HYPOTHETICAL fixture: API-owned trip metrics with enrichment enabled. ALPHA does
# NOT run this. It is retained because it is the one configuration in which L=3
# could be WORSE than L=1: enrichment is fail-loud, so INCOMPLETE enrichment
# aborts the whole run and skips the trips upsert entirely, and a wider window
# means more chunks, each another opportunity for that abort.
#
# docs/20 §19.4 carries the arithmetic and labels it conditional. Nothing here
# describes current ALPHA production behaviour.
# ---------------------------------------------------------------------------

hypothetical_row_l1 = alpha_daily_row(
    lookback_days=L_DAILY_HISTORICAL,
    event_enrichment_mode=HYPOTHETICAL_EVENT_ENRICHMENT_MODE,
)
hypothetical_row_l3 = alpha_daily_row(
    lookback_days=L_DAILY_APPROVED,
    event_enrichment_mode=HYPOTHETICAL_EVENT_ENRICHMENT_MODE,
)
hypothetical_l1 = events_requests_for_fire(
    row=hypothetical_row_l1,
    trip_metrics_population_source=HYPOTHETICAL_TRIP_METRICS_SOURCE,
    window=w1,
)
hypothetical_l3 = events_requests_for_fire(
    row=hypothetical_row_l3,
    trip_metrics_population_source=HYPOTHETICAL_TRIP_METRICS_SOURCE,
    window=w3,
)

check(
    "the conditional fixture is explicitly NOT ALPHA's configuration",
    HYPOTHETICAL_EVENT_ENRICHMENT_MODE != ALPHA_EVENT_ENRICHMENT_MODE
    and HYPOTHETICAL_TRIP_METRICS_SOURCE != ALPHA_TRIP_METRICS_SOURCE,
)
check(
    "CONDITIONAL (not ALPHA): 25 h at L=1 costs 7 four-hour event chunks",
    len(hypothetical_l1) == 7,
    f"got {len(hypothetical_l1)} for span {w1.effective_window_end_ts - w1.effective_window_start_ts}",
)
check(
    "CONDITIONAL (not ALPHA): 73 h at L=3 costs 19 four-hour event chunks",
    len(hypothetical_l3) == 19,
    f"got {len(hypothetical_l3)} for span {l3_span}",
)
check(
    "CONDITIONAL: the growth factor is bounded by ~3x, matching the window ratio",
    len(hypothetical_l3) <= 3 * len(hypothetical_l1),
    f"{len(hypothetical_l3)} vs 3 x {len(hypothetical_l1)}",
)
check(
    "CONDITIONAL: every counted request really went to /vehicles/events, one per chunk",
    all(r["method"].upper() == "GET" for r in hypothetical_l3)
    and len({tuple(sorted(r["params"].items())) for r in hypothetical_l3}) == 19,
    f"distinct request params: "
    f"{len({tuple(sorted(r['params'].items())) for r in hypothetical_l3})}",
)
# The arithmetic the documentation quotes, kept alongside the measurement so a
# divergence between them is visible rather than silently resolved.
events_chunk = timedelta(hours=VEHICLE_EVENTS_DEFAULT_CHUNK_HOURS)
span_l1 = w1.effective_window_end_ts - w1.effective_window_start_ts
check(
    "CONDITIONAL: the measured chunk counts match the documented ceil(span / 4 h)",
    (-(-span_l1 // events_chunk), -(-l3_span // events_chunk))
    == (len(hypothetical_l1), len(hypothetical_l3)),
)
check(
    "incomplete event enrichment still aborts the run rather than writing partial trips",
    "DB upsert skipped due to incomplete event enrichment" in sync_src
    and "_raise_incomplete_event_enrichment" in sync_src,
    "the fail-loud enrichment contract changed; the conditional cost analysis is stale",
)
check(
    "provider budget exhaustion is in the enrichment abort set, not silently tolerated",
    "MAX_REQUESTS_PER_SUBWINDOW" in sync_src and "MAX_REQUESTS_PER_RUN" in sync_src,
)
check(
    "skip_vehicle_events cannot quietly produce incomplete enrichment for an API-owned client",
    "skip_vehicle_events=true is incompatible with strict complete event enrichment" in sync_src,
)

# ---------------------------------------------------------------------------
# 8. Configuration precedence.
#
# Ordered, and each layer proven to be the layer it claims to be:
#   1. explicit `window_start_ts`/`window_end_ts` params — wins outright and
#      bypasses lookback entirely (manual recovery, backfill, replay);
#   2. `client_dataset_schedule.lookback_days` — the authoritative scheduled
#      DAILY horizon; M2 sets it to 3;
#   3. `client_dataset_schedule.lookback_days DEFAULT 7` (migration 012) — new
#      rows only; never rewrites an existing row;
#   4. `control_plane._default_schedule(lookback_days=1)` — the no-row fallback,
#      not on the scheduled trips path at all.
# ---------------------------------------------------------------------------

schedule_migration = (
    REPO_ROOT / "db/migrations/012_workflow_a_client_dataset_schedule.sql"
).read_text(encoding="utf-8")
check(
    "layer 3: the column default is 7 and applies to new rows only",
    "lookback_days INTEGER NOT NULL DEFAULT 7" in schedule_migration,
)
check(
    "layer 3: M2 writes the row explicitly rather than relying on the column default",
    "SET lookback_days = approved_lookback" in migration_src.replace("\n", " ").replace("  ", " ")
    or "lookback_days = approved_lookback" in migration_src,
)
check(
    "layer 4: the no-row fallback is reached only when no schedule row exists",
    "exists=False" in control_plane_src and "_default_schedule" in control_plane_src,
)
check(
    "layer 4: the trips job's use of the loaded schedule is enabled/overwrite/identity, never lookback",
    "schedule.lookback_days" not in sync_src,
)

# Layer 2 beats layer 3 and layer 4 for the real row: whatever the defaults say,
# the dispatcher's window comes from the row it was handed.
for candidate in (0, 1, 3, 7):
    ev = evaluate_schedule(
        now_utc=now_normal, sched=alpha_daily_row(lookback_days=candidate)
    )
    check(
        f"layer 2: the row value {candidate} — not any default — decides the window",
        ev is not None and ev[2] - ev[1] == timedelta(days=candidate),
        f"got {None if ev is None else ev[2] - ev[1]}",
    )

check(
    "layer 1: an explicit window is not reachable from the lookback layer at all",
    "window_start_ts" in sync_src and "lookback" not in sync_src,
)

# M2 does not touch cadence: same fire, same timezone, before and after.
before = evaluate_schedule(now_utc=now_normal, sched=alpha_daily_row(lookback_days=L_DAILY_HISTORICAL))
after = evaluate_schedule(now_utc=now_normal, sched=alpha_daily_row(lookback_days=L_DAILY_APPROVED))
check(
    "M2 moves the horizon without moving the fire (cadence and timezone unchanged)",
    before is not None and after is not None and before[0] == after[0],
)

print()
if FAILURES:
    print(f"FAILED ({len(FAILURES)}): " + ", ".join(FAILURES))
    sys.exit(1)
print("ALL M2 DAILY L=3 CHECKS PASSED")
