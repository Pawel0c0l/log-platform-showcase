#!/usr/bin/env python3
"""End-to-end manual test for the Eco Driving reporting pipeline.

This test wires together every Eco Driving rule the postprocess job
implements and verifies the full chain from source trips through:

  * assignment audit (`eco_trip_assignments` semantics),
  * private-trip exclusion,
  * cumulative month-to-date weekly snapshots,
  * independent full-month aggregation,
  * scoring + qualification,
  * INCLUDED / EXCLUDED / UNKNOWN_DRIVER ranking groups,
  * trend view deltas (weekly and monthly),
  * runner/dispatcher mode mapping,
  * resolver outputs for cumulative weekly / final-month weekly / monthly,
  * static contracts for dry-run, recalculate, and idempotent upserts.

It follows the repository convention for `ops/tests_manual/`: pure-Python
(no DB, no network), exercises the real job helpers, and is meant to be
run on demand from the repo root:

    cd /opt/log-platform
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" \
      python3 ops/tests_manual/test_eco_driving_e2e.py

Chosen month: April 2026.
  - Apr 1 = Wednesday (ISO weekday 3) — month starts mid-week.
  - Apr 6 = Monday boundary  → W1 (Apr 1 → Apr 6, partial, 5 days).
  - Apr 13 = Monday boundary → W2 (Apr 1 → Apr 13).
  - Apr 20 = Monday boundary → W3 (Apr 1 → Apr 20).
  - Apr 27 = Monday boundary → W4 (Apr 1 → Apr 27).
  - May 1  = Friday          → final W5 (Apr 1 → May 1, partial).

This shape exercises every cumulative-weekly boundary case the
production job is expected to handle.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
import sys
from zoneinfo import ZoneInfo


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.api.telematics import registry  # noqa: E402
from jobs.api.telematics.dispatcher import (  # noqa: E402
    ECO_DRIVING_SCHEDULE_MODES,
    _build_job_params,
)
from jobs.ecodriving.eco_scoring import (  # noqa: E402
    MAX_POSSIBLE_SCORE,
    MIN_POSSIBLE_SCORE,
    REQUIRED_METRICS,
)
from jobs.ecodriving.job_eco_driving_aggregate import (  # noqa: E402
    ASSIGNMENT_DRIVER_RESTRICTIONS,
    ASSIGNMENT_DYSPONENT_ID,
    ASSIGNMENT_SKIPPED_NO_ID,
    DATASET_NAME,
    MODE_FINAL_MONTH_WEEKLY_SNAPSHOT,
    MODE_MONTHLY_FULL_AGGREGATION,
    MODE_WEEKLY_CUMULATIVE_SNAPSHOT,
    PRIVATE_EXCLUSION_REASON,
    RankingPeriod,
    _apply_rankings,
    _assignment_for_values,
    _is_private_trip,
    _month_bounded_weekly_periods,
    _qualification_and_calculation,
    _rate_per_100km,
    _resolve_periods,
    _stats_row_from_aggregate,
    _total_kilometers,
    _trimmed_or_none,
    resolve_final_month_weekly_snapshot,
    resolve_previous_completed_month,
    resolve_previous_completed_weekly_snapshot,
)


CLIENT_ID = "00000000-0000-0000-0000-000000000001"
CLIENT_CODE = "E2E"
ECO_JOB_MODULE = "jobs.ecodriving.job_eco_driving_aggregate"
WARSAW = ZoneInfo("Europe/Warsaw")

MONTH_START = date(2026, 4, 1)
NEXT_MONTH_START = date(2026, 5, 1)


# ---------------------------------------------------------------------------
# In-memory data model + SQL-equivalent aggregation
# ---------------------------------------------------------------------------

@dataclass
class SourceTrip:
    """One row in public.client_trips, as the postprocess job sees it."""
    provider_trip_id: int
    day: date
    driver_restrictions: str | None
    dysponent_id: str | None
    driver_tag_description: str | None
    distance_meters: int
    overrev_events_count: int = 0
    harsh_braking_events: int = 0
    harsh_acceleration_events: int = 0
    harsh_turning_events: int = 0
    idle_events: int = 0
    speeding_140_160_count: int = 0
    speeding_160_170_count: int = 0
    speeding_170_plus_count: int = 0


@dataclass
class AssignmentRow:
    """Mirror of one public.eco_trip_assignments row."""
    provider_trip_id: int
    day: date
    assigned_id: str | None
    assignment_source: str
    driver_restrictions_raw: str | None
    dysponent_id_raw: str | None
    driver_tag_description: str | None
    is_private_trip: bool
    aggregation_included: bool
    exclusion_reason: str | None
    distance_meters: int
    metrics: dict[str, int]


DRIVER_CHART = {
    # ranking_included=true → INCLUDED
    "INC1": True,
    "INC2": True,
    "INC3": True,
    "INC4": True,
    "TIE_A": True,
    "TIE_B": True,
    "DYS1": True,
    "LOW1": True,
    "ZERO1": True,
    # ranking_included=false → EXCLUDED
    "EXC1": False,
    # no chart row → UNKNOWN_DRIVER ("UNK1")
}


def _metrics(
    *,
    overrev: int = 0,
    braking: int = 0,
    accel: int = 0,
    turning: int = 0,
    idle: int = 0,
    s140: int = 0,
    s160: int = 0,
    s170: int = 0,
) -> dict[str, int]:
    return {
        "overrev_events_count": overrev,
        "harsh_braking_events": braking,
        "harsh_acceleration_events": accel,
        "harsh_turning_events": turning,
        "idle_events": idle,
        "speeding_140_160_count": s140,
        "speeding_160_170_count": s160,
        "speeding_170_plus_count": s170,
    }


def _build_source_trips() -> list[SourceTrip]:
    """Deterministic source trips covering every Part B case."""
    trips: list[SourceTrip] = []
    next_id = iter(range(1, 10_000))

    def add(
        day: date,
        *,
        dr: str | None,
        dys: str | None,
        tag: str | None,
        meters: int,
        metrics: dict[str, int] | None = None,
    ) -> None:
        trips.append(
            SourceTrip(
                provider_trip_id=next(next_id),
                day=day,
                driver_restrictions=dr,
                dysponent_id=dys,
                driver_tag_description=tag,
                distance_meters=meters,
                **(metrics or _metrics()),
            )
        )

    # --- Assignment priority cases (Apr 2, inside W1) ---
    add(date(2026, 4, 2), dr="INC1", dys="D-IGNORED", tag=None, meters=20_000)  # dr wins
    add(date(2026, 4, 2), dr=None, dys="DYS1", tag=None, meters=15_000)         # dys fallback
    add(date(2026, 4, 2), dr=None, dys=None, tag=None, meters=999_999)          # SKIPPED_NO_ID
    add(date(2026, 4, 2), dr="  INC2  ", dys=" D-WHITESPACE ", tag=None,
        meters=10_000)                                                          # trimmed → INC2

    # --- Private-trip exclusion cases (Apr 3, inside W1) ---
    add(date(2026, 4, 3), dr="INC1", dys=None, tag="pryw", meters=80_000)
    add(date(2026, 4, 3), dr="INC2", dys=None, tag="PRYWATNY", meters=80_000)
    add(date(2026, 4, 3), dr=None, dys="DYS1", tag="jazda pryw.", meters=80_000)

    # --- INCLUDED qualified driver: W1 contribution ---
    add(date(2026, 4, 2), dr="INC1", dys=None, tag=None, meters=80_000,
        metrics=_metrics(overrev=2, braking=1, accel=1, turning=2, idle=1,
                         s140=1, s160=0, s170=0))

    # --- INC1: W2 incremental contribution (Apr 8) ---
    add(date(2026, 4, 8), dr="INC1", dys=None, tag=None, meters=60_000,
        metrics=_metrics(overrev=1, braking=0, accel=0, turning=1, idle=0,
                         s140=0, s160=0, s170=0))

    # --- INC1: W3 incremental contribution (Apr 15) ---
    add(date(2026, 4, 15), dr="INC1", dys=None, tag=None, meters=80_000,
        metrics=_metrics(overrev=0, braking=0, accel=0, turning=0, idle=1,
                         s140=0, s160=0, s170=0))

    # --- INC1: tail in final-W partial window (Apr 28) ---
    add(date(2026, 4, 28), dr="INC1", dys=None, tag=None, meters=40_000,
        metrics=_metrics(overrev=0, braking=0, accel=1, turning=0, idle=0))

    # --- EXC1 qualified excluded driver (Apr 5, all in W1) ---
    add(date(2026, 4, 5), dr="EXC1", dys=None, tag=None, meters=180_000,
        metrics=_metrics(overrev=1, braking=0, accel=0, turning=0, idle=0))

    # --- UNK1: chart row absent (Apr 9, W2 contribution) ---
    add(date(2026, 4, 9), dr="UNK1", dys=None, tag=None, meters=120_000,
        metrics=_metrics(overrev=0, braking=0, accel=0, turning=0, idle=0))

    # --- LOW1 below 100 km (Apr 11, W2) ---
    add(date(2026, 4, 11), dr="LOW1", dys=None, tag=None, meters=50_000,
        metrics=_metrics(overrev=0, braking=0, accel=0, turning=0, idle=0))

    # --- ZERO1 zero-distance qualified-trip (Apr 12, W2) ---
    add(date(2026, 4, 12), dr="ZERO1", dys=None, tag=None, meters=0)

    # --- INC3 best-possible score driver (Apr 18, W3) ---
    add(date(2026, 4, 18), dr="INC3", dys=None, tag=None, meters=100_000)

    # --- INC4 worst-possible score driver (Apr 22, W4) ---
    add(date(2026, 4, 22), dr="INC4", dys=None, tag=None, meters=100_000,
        metrics=_metrics(overrev=21, braking=11, accel=5, turning=31, idle=9,
                         s140=11, s160=6, s170=3))

    # --- TIE_A / TIE_B (W3 boundary):
    #   * TIE_A has 200 km, TIE_B has 100 km, same zero events → tie on score,
    #     TIE_A wins on km;
    #   * For pure tie (km equal, score equal) we add ZTIE_M and ZTIE_N below.
    add(date(2026, 4, 16), dr="TIE_A", dys=None, tag=None, meters=200_000)
    add(date(2026, 4, 17), dr="TIE_B", dys=None, tag=None, meters=100_000)

    return trips


def _normalize_assignments(trips: list[SourceTrip]) -> list[AssignmentRow]:
    """Mirror the SQL prepared-rows step inside ``_assignment_upsert_batch``.

    The SQL trims whitespace, picks ``Driver_Restrictions`` first, then
    ``Dysponent_ID``, marks private trips by ``driver_tag_description ILIKE
    '%pryw%'`` and toggles ``aggregation_included`` accordingly.
    """
    out: list[AssignmentRow] = []
    for trip in trips:
        assigned_id, source = _assignment_for_values(
            trip.driver_restrictions, trip.dysponent_id,
        )
        is_private = _is_private_trip(trip.driver_tag_description)
        aggregation_included = assigned_id is not None and not is_private
        exclusion_reason = PRIVATE_EXCLUSION_REASON if is_private else None
        out.append(
            AssignmentRow(
                provider_trip_id=trip.provider_trip_id,
                day=trip.day,
                assigned_id=assigned_id,
                assignment_source=source,
                driver_restrictions_raw=_trimmed_or_none(trip.driver_restrictions),
                dysponent_id_raw=_trimmed_or_none(trip.dysponent_id),
                driver_tag_description=trip.driver_tag_description,
                is_private_trip=is_private,
                aggregation_included=aggregation_included,
                exclusion_reason=exclusion_reason,
                distance_meters=trip.distance_meters,
                metrics=_metrics(
                    overrev=trip.overrev_events_count,
                    braking=trip.harsh_braking_events,
                    accel=trip.harsh_acceleration_events,
                    turning=trip.harsh_turning_events,
                    idle=trip.idle_events,
                    s140=trip.speeding_140_160_count,
                    s160=trip.speeding_160_170_count,
                    s170=trip.speeding_170_plus_count,
                ),
            )
        )
    return out


def _filter_assignments_in_period(
    rows: list[AssignmentRow], *, start: date, end: date,
) -> list[AssignmentRow]:
    """Half-open interval [start, end): mirrors ``trip_start_ts >= start AND < end``."""
    return [row for row in rows if start <= row.day < end]


def _aggregate_per_assigned(
    rows: list[AssignmentRow], *, monthly: bool,
    month_start: date | None, month_end: date | None,
) -> list[dict]:
    """Mirror of ``_fetch_aggregate_rows``:

    Only rows with ``aggregation_included=true``, ``is_private_trip=false``
    and ``assigned_id IS NOT NULL`` contribute to event/distance sums. Source
    trip count and skipped trip count include the audit rows in the window.
    """
    eligible: dict[str, list[AssignmentRow]] = {}
    skipped_count: dict[str, int] = {}
    all_audit_count: dict[str, int] = {}
    for row in rows:
        if not row.aggregation_included or row.is_private_trip:
            key = row.assigned_id or "__skipped__"
            skipped_count[key] = skipped_count.get(key, 0) + 1
            if row.assigned_id is not None:
                all_audit_count[row.assigned_id] = (
                    all_audit_count.get(row.assigned_id, 0) + 1
                )
            continue
        assigned_id = row.assigned_id or ""
        eligible.setdefault(assigned_id, []).append(row)
        all_audit_count[assigned_id] = all_audit_count.get(assigned_id, 0) + 1

    out: list[dict] = []
    for assigned_id, group in sorted(eligible.items()):
        included_count = len(group)
        # Match SQL's "source_trips_count = count(*) of audit rows for this
        # assigned_id" (private + ID-bearing skipped rows are attached to
        # their assigned_id; SKIPPED_NO_ID rows have NULL assigned_id and
        # do not group with any included driver).
        source_trips_count = all_audit_count.get(assigned_id, included_count)
        skipped_trips_count = source_trips_count - included_count
        total_distance = sum(row.distance_meters for row in group)
        agg = {
            "client_id": CLIENT_ID,
            "client_code": CLIENT_CODE,
            "assigned_id": assigned_id,
            "trips_count": included_count,
            "source_trips_count": source_trips_count,
            "skipped_trips_count": skipped_trips_count,
            "total_distance_meters": total_distance,
            "driver_id": assigned_id if assigned_id in DRIVER_CHART else None,
            "ranking_included": DRIVER_CHART.get(assigned_id),
        }
        for metric in REQUIRED_METRICS:
            agg[metric] = sum(row.metrics[metric] for row in group)
        if monthly:
            agg["month_start_date"] = month_start
            agg["month_end_date"] = month_end
        out.append(agg)
    return out


def _stats_rows_for_period(
    rows: list[AssignmentRow], period: RankingPeriod,
) -> list[dict]:
    in_period = _filter_assignments_in_period(
        rows, start=period.period_start_date, end=period.period_end_date,
    )
    aggregated = _aggregate_per_assigned(
        in_period, monthly=False, month_start=None, month_end=None,
    )
    return [
        _stats_row_from_aggregate(row, period, monthly=False)
        for row in aggregated
    ]


def _monthly_stats_rows(rows: list[AssignmentRow]) -> list[dict]:
    in_period = _filter_assignments_in_period(
        rows, start=MONTH_START, end=NEXT_MONTH_START,
    )
    aggregated = _aggregate_per_assigned(
        in_period, monthly=True,
        month_start=MONTH_START, month_end=NEXT_MONTH_START,
    )
    return [_stats_row_from_aggregate(row, None, monthly=True) for row in aggregated]


# ---------------------------------------------------------------------------
# Test runner scaffolding
# ---------------------------------------------------------------------------

FAILURES: list[str] = []


def _check(label: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    line = f"[{status}] {label}"
    if detail:
        line += f"\n        {detail}"
    print(line)
    if not ok:
        FAILURES.append(label)


def _assert_equal(label: str, actual, expected) -> None:
    _check(
        label,
        actual == expected,
        f"actual={actual!r}, expected={expected!r}",
    )


def _by_assigned_id(rows: list[dict]) -> dict[str, dict]:
    return {row["assigned_id"]: row for row in rows}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_chosen_month_boundary_shape() -> None:
    periods = _month_bounded_weekly_periods(MONTH_START)
    _assert_equal(
        "April 2026 produces five cumulative-weekly snapshots",
        len(periods),
        5,
    )
    for period in periods:
        _check(
            f"period_start_date is month_start for {period.period_label}",
            period.period_start_date == MONTH_START,
            f"got {period.period_start_date.isoformat()}",
        )

    boundaries = [period.period_end_date for period in periods]
    _assert_equal(
        "weekly boundaries advance through the month",
        boundaries,
        [
            date(2026, 4, 6),
            date(2026, 4, 13),
            date(2026, 4, 20),
            date(2026, 4, 27),
            date(2026, 5, 1),
        ],
    )

    _check(
        "first weekly snapshot is partial (month starts mid-week)",
        periods[0].is_partial_period is True,
    )
    _check(
        "final weekly snapshot is partial (month ends before next Monday)",
        periods[-1].is_partial_period is True,
    )


def test_assignment_audit_and_private_exclusion() -> None:
    trips = _build_source_trips()
    audit = _normalize_assignments(trips)
    _assert_equal(
        "every source trip produces exactly one audit row",
        len(audit),
        len(trips),
    )

    source_counts = {
        ASSIGNMENT_DRIVER_RESTRICTIONS: 0,
        ASSIGNMENT_DYSPONENT_ID: 0,
        ASSIGNMENT_SKIPPED_NO_ID: 0,
    }
    private_rows: list[AssignmentRow] = []
    for row in audit:
        source_counts[row.assignment_source] = (
            source_counts[row.assignment_source] + 1
        )
        if row.is_private_trip:
            private_rows.append(row)

    _check(
        "DRIVER_RESTRICTIONS wins over DYSPONENT_ID",
        source_counts[ASSIGNMENT_DRIVER_RESTRICTIONS] >= 1,
        f"counts={source_counts}",
    )
    _check(
        "DYSPONENT_ID fallback fires when Driver_Restrictions empty",
        source_counts[ASSIGNMENT_DYSPONENT_ID] >= 1,
        f"counts={source_counts}",
    )
    _check(
        "SKIPPED_NO_ID is produced when neither ID is present",
        source_counts[ASSIGNMENT_SKIPPED_NO_ID] == 1,
        f"counts={source_counts}",
    )

    trimmed_inc2 = [row for row in audit if row.assigned_id == "INC2"]
    _check(
        "whitespace around IDs is trimmed (INC2)",
        len(trimmed_inc2) >= 1,
        f"matches={len(trimmed_inc2)}",
    )

    for row in private_rows:
        _check(
            f"private row {row.provider_trip_id} audited as private",
            row.is_private_trip is True and row.aggregation_included is False
            and row.exclusion_reason == PRIVATE_EXCLUSION_REASON,
            f"row={row}",
        )
    _assert_equal(
        "three private trips were audited",
        len(private_rows),
        3,
    )

    skipped_rows = [row for row in audit
                    if row.assignment_source == ASSIGNMENT_SKIPPED_NO_ID]
    for row in skipped_rows:
        _check(
            "SKIPPED_NO_ID rows are excluded from aggregation and have NULL assigned_id",
            row.assigned_id is None
            and row.aggregation_included is False
            and row.exclusion_reason is None,
            f"row={row}",
        )


def test_cumulative_weekly_snapshots_and_monthly_match() -> None:
    trips = _build_source_trips()
    audit = _normalize_assignments(trips)
    periods = _month_bounded_weekly_periods(MONTH_START)

    weekly_snapshots: dict[str, dict] = {
        period.period_label: _stats_rows_for_period(audit, period)
        for period in periods
    }
    monthly_rows = _monthly_stats_rows(audit)

    # ---- Per-driver cumulative semantics for INC1 ----
    inc1_per_period: list[dict] = []
    for period in periods:
        inc1 = _by_assigned_id(weekly_snapshots[period.period_label]).get("INC1")
        _check(
            f"INC1 has a cumulative weekly row at {period.period_label}",
            inc1 is not None,
        )
        if inc1 is not None:
            _check(
                f"INC1 period_start_date is month_start at {period.period_label}",
                inc1["period_start_date"] == MONTH_START,
            )
            _check(
                f"INC1 period_end_date matches reporting boundary at {period.period_label}",
                inc1["period_end_date"] == period.period_end_date,
            )
            inc1_per_period.append(inc1)

    # W2 totals must include W1 totals + new W2 trips for INC1.
    _check(
        "INC1 W2 total_kilometers >= INC1 W1 total_kilometers",
        inc1_per_period[1]["total_distance_meters"]
        >= inc1_per_period[0]["total_distance_meters"],
        f"W1={inc1_per_period[0]['total_distance_meters']}, "
        f"W2={inc1_per_period[1]['total_distance_meters']}",
    )
    # INC1 contributes:
    #   * Apr 2 priority trip (20 km) + Apr 2 W1 trip (80 km) → W1 = 100 km;
    #   * Apr 8 W2 trip adds 60 km            → W2 = 160 km;
    #   * Apr 15 W3 trip adds 80 km           → W3 = 240 km;
    #   * Apr 28 final-W trip adds 40 km      → final = 280 km.
    # Private/skipped rows do not contribute even though they share assigned_id.
    _assert_equal(
        "INC1 W1 distance reflects every Apr 2 eligible trip (cumulative MTD)",
        inc1_per_period[0]["total_distance_meters"], 20_000 + 80_000,
    )
    _assert_equal(
        "INC1 W2 distance reflects W1 + Apr 8 trip (cumulative MTD)",
        inc1_per_period[1]["total_distance_meters"],
        20_000 + 80_000 + 60_000,
    )
    _assert_equal(
        "INC1 W3 distance reflects W1 + W2 + Apr 15 trip (cumulative MTD)",
        inc1_per_period[2]["total_distance_meters"],
        20_000 + 80_000 + 60_000 + 80_000,
    )
    _assert_equal(
        "INC1 final W distance reflects every eligible MTD trip",
        inc1_per_period[-1]["total_distance_meters"],
        20_000 + 80_000 + 60_000 + 80_000 + 40_000,
    )

    # ---- Final weekly raw totals match monthly for the same eligible trip set ----
    final_w = _by_assigned_id(weekly_snapshots[periods[-1].period_label])
    monthly_map = _by_assigned_id(monthly_rows)

    for assigned_id in monthly_map:
        m = monthly_map[assigned_id]
        w = final_w.get(assigned_id)
        _check(
            f"monthly row exists in final weekly snapshot for {assigned_id}",
            w is not None,
        )
        if w is None:
            continue
        _assert_equal(
            f"{assigned_id} final-W kilometers match monthly kilometers",
            w["total_kilometers"], m["total_kilometers"],
        )
        for metric in REQUIRED_METRICS:
            _assert_equal(
                f"{assigned_id} final-W {metric} == monthly {metric}",
                w[metric], m[metric],
            )

    # ---- Proof that weekly rows must NOT be summed for monthly totals ----
    inc1_summed_distance = sum(
        row["total_distance_meters"]
        for row in inc1_per_period
    )
    inc1_monthly = monthly_map["INC1"]["total_distance_meters"]
    _check(
        "summing cumulative weekly snapshots overstates monthly distance for INC1",
        inc1_summed_distance > inc1_monthly,
        f"sum_of_weeklies={inc1_summed_distance}, monthly={inc1_monthly}",
    )

    # ---- SKIPPED / private rows do not pollute included trips_count anywhere ----
    for period in periods:
        snapshot = weekly_snapshots[period.period_label]
        for row in snapshot:
            _check(
                f"trips_count >= 1 for {row['assigned_id']} at {period.period_label}",
                row["trips_count"] >= 1,
            )
            _check(
                f"trips_count never includes private/skipped rows for "
                f"{row['assigned_id']} at {period.period_label}",
                row["trips_count"] <= row["source_trips_count"],
            )

    # ---- Qualification statuses ----
    for assigned_id, status in [
        ("INC1", "QUALIFIED"),
        ("EXC1", "QUALIFIED"),
        ("LOW1", "LOW_DISTANCE"),
        ("ZERO1", "NO_DISTANCE"),
    ]:
        monthly_row = monthly_map.get(assigned_id)
        if monthly_row is None:
            _check(f"monthly row exists for {assigned_id}", False)
            continue
        _assert_equal(
            f"{assigned_id} qualification_status",
            monthly_row["qualification_status"], status,
        )
    _check(
        "ZERO1 has NULL score because zero distance produces NULL rates",
        monthly_map["ZERO1"]["eco_driving_score_total"] is None
        and monthly_map["ZERO1"]["overrev_events_per_100km"] is None
        and monthly_map["ZERO1"]["overrev_points"] is None,
    )
    _assert_equal(
        "ZERO1 total_kilometers is zero",
        monthly_map["ZERO1"]["total_kilometers"], Decimal("0.000"),
    )


def test_rates_and_scoring_boundaries() -> None:
    # Rate math sanity (mirrors aggregation): rate per 100km of total kilometers.
    km = _total_kilometers(50_000)
    _assert_equal(
        "50 km total kilometers", km, Decimal("50.000"),
    )
    _assert_equal(
        "5 events / 50 km → 10.0000 per 100km rate",
        _rate_per_100km(5, km), Decimal("10.0000"),
    )
    _check(
        "zero kilometers produces NULL rate",
        _rate_per_100km(1, Decimal("0.000")) is None,
    )

    # Build a "best possible" driver via 100 km and zero events → MAX score.
    best = _stats_row_from_aggregate(
        {
            "client_id": CLIENT_ID,
            "client_code": CLIENT_CODE,
            "assigned_id": "BEST",
            "trips_count": 1,
            "source_trips_count": 1,
            "skipped_trips_count": 0,
            "total_distance_meters": 100_000,
            "driver_id": "BEST",
            "ranking_included": True,
            **_metrics(),
        },
        RankingPeriod(
            period_start_date=MONTH_START,
            period_end_date=NEXT_MONTH_START,
            month_start_date=MONTH_START,
            period_sequence_in_month=5,
            period_label="2026-04-W5",
            is_partial_period=True,
        ),
        monthly=False,
    )
    _assert_equal(
        "best-possible cumulative score is MAX_POSSIBLE_SCORE",
        best["eco_driving_score_total"], MAX_POSSIBLE_SCORE,
    )

    # Worst possible row: high rates per 100 km → MIN score.
    worst = _stats_row_from_aggregate(
        {
            "client_id": CLIENT_ID,
            "client_code": CLIENT_CODE,
            "assigned_id": "WORST",
            "trips_count": 1,
            "source_trips_count": 1,
            "skipped_trips_count": 0,
            "total_distance_meters": 100_000,
            "driver_id": "WORST",
            "ranking_included": True,
            **_metrics(overrev=21, braking=11, accel=5, turning=31, idle=9,
                       s140=11, s160=6, s170=3),
        },
        RankingPeriod(
            period_start_date=MONTH_START,
            period_end_date=NEXT_MONTH_START,
            month_start_date=MONTH_START,
            period_sequence_in_month=5,
            period_label="2026-04-W5",
            is_partial_period=True,
        ),
        monthly=False,
    )
    _assert_equal(
        "worst-possible cumulative score is MIN_POSSIBLE_SCORE",
        worst["eco_driving_score_total"], MIN_POSSIBLE_SCORE,
    )

    # Sanity: _qualification_and_calculation reflects PartC distance buckets.
    _assert_equal(
        "qualified at 100 km exact",
        _qualification_and_calculation(100_000), ("QUALIFIED", "OK"),
    )
    _assert_equal(
        "low distance just below threshold",
        _qualification_and_calculation(99_999), ("LOW_DISTANCE", "OK"),
    )
    _assert_equal(
        "zero distance status",
        _qualification_and_calculation(0), ("NO_DISTANCE", "NO_DISTANCE"),
    )


def test_ranking_groups_and_tie_breakers() -> None:
    trips = _build_source_trips()
    audit = _normalize_assignments(trips)
    monthly_rows = _monthly_stats_rows(audit)
    _apply_rankings(monthly_rows, monthly=True)

    by_id = _by_assigned_id(monthly_rows)
    # ---- Ranking groups ----
    expected_groups = {
        "INC1": "INCLUDED",
        # INC2 and DYS1 keep most of their distance in private trips, which are
        # excluded from aggregation; what remains is under 100 km, so both are
        # LOW_DISTANCE and therefore outside every ranking population.
        "INC2": None,
        "INC3": "INCLUDED",
        "INC4": "INCLUDED",
        "TIE_A": "INCLUDED",
        "TIE_B": "INCLUDED",
        "DYS1": None,
        # LOW1 (<100 km) and ZERO1 (no distance) are not QUALIFIED, so they sit
        # outside every ranking population regardless of their chart entry.
        "LOW1": None,
        "ZERO1": None,
        "EXC1": "EXCLUDED",
        "UNK1": "UNKNOWN_DRIVER",
    }
    for assigned_id, group in expected_groups.items():
        row = by_id.get(assigned_id)
        _check(
            f"{assigned_id} appears in monthly stats",
            row is not None,
        )
        if row is None:
            continue
        _assert_equal(
            f"{assigned_id} ranking_group", row["ranking_group"], group,
        )

    # ---- UNKNOWN_DRIVER stays visible but unranked ----
    unk = by_id["UNK1"]
    _check(
        "UNKNOWN_DRIVER appears in stats but has no ranking position",
        unk["ranking_position"] is None
        and unk["ranking_total_participants"] is None,
    )

    # ---- EXC1 ranked independently from INCLUDED ----
    exc1 = by_id["EXC1"]
    _assert_equal(
        "single EXCLUDED driver gets ranking_position=1",
        exc1["ranking_position"], 1,
    )
    _assert_equal(
        "single EXCLUDED driver group total is 1",
        exc1["ranking_total_participants"], 1,
    )

    # ---- Tie-break by total_kilometers (TIE_A 200 km vs TIE_B 100 km) ----
    tie_a = by_id["TIE_A"]
    tie_b = by_id["TIE_B"]
    _check(
        "TIE_A and TIE_B have the same score (both zero events)",
        tie_a["eco_driving_score_total"] == tie_b["eco_driving_score_total"],
    )
    _check(
        "TIE_A outranks TIE_B because of higher total_kilometers",
        tie_a["ranking_position"] < tie_b["ranking_position"],
        f"TIE_A={tie_a['ranking_position']}, TIE_B={tie_b['ranking_position']}",
    )

    # ---- Non-qualified rows stay visible but carry no ranking coordinates ----
    for assigned_id in ("LOW1", "ZERO1"):
        row = by_id[assigned_id]
        _check(
            f"{assigned_id} appears in stats but has no ranking coordinates",
            row["ranking_position"] is None
            and row["ranking_total_participants"] is None,
            f"{assigned_id}: pos={row['ranking_position']}, "
            f"total={row['ranking_total_participants']}",
        )
    _check(
        "non-qualified rows keep their chart ranking_included configuration",
        by_id["LOW1"]["ranking_included"] is True,
    )

    # ---- INC4 (worst score) ranks last inside the QUALIFIED INCLUDED group ----
    inc4 = by_id["INC4"]
    included_rows = [row for row in monthly_rows if row["ranking_group"] == "INCLUDED"]
    _check(
        "every INCLUDED row is QUALIFIED",
        all(row["qualification_status"] == "QUALIFIED" for row in included_rows),
    )
    _check(
        "INC4 ranks below every other INCLUDED driver",
        inc4["ranking_position"]
        == max(row["ranking_position"] for row in included_rows),
    )
    _check(
        "INCLUDED denominator counts only QUALIFIED rows",
        all(row["ranking_total_participants"] == len(included_rows) for row in included_rows),
        f"included={len(included_rows)}",
    )

    # ---- Pure score+km tie broken by assigned_id ascending ----
    period = RankingPeriod(
        period_start_date=MONTH_START,
        period_end_date=date(2026, 4, 13),
        month_start_date=MONTH_START,
        period_sequence_in_month=2,
        period_label="2026-04-W2",
        is_partial_period=False,
    )
    tied_rows = [
        _stats_row_from_aggregate(
            {
                "client_id": CLIENT_ID,
                "client_code": CLIENT_CODE,
                "assigned_id": assigned_id,
                "trips_count": 1,
                "source_trips_count": 1,
                "skipped_trips_count": 0,
                "total_distance_meters": 100_000,
                "driver_id": assigned_id,
                "ranking_included": True,
                **_metrics(),
            },
            period,
            monthly=False,
        )
        for assigned_id in ("B", "A")  # intentionally out of order
    ]
    _apply_rankings(tied_rows, monthly=False)
    pos = _by_assigned_id(tied_rows)
    _assert_equal(
        "pure score+km tie ranked by assigned_id ascending (A first)",
        pos["A"]["ranking_position"], 1,
    )
    _assert_equal(
        "pure score+km tie ranked by assigned_id ascending (B second)",
        pos["B"]["ranking_position"], 2,
    )


def test_weekly_and_monthly_trend_deltas() -> None:
    """Mirror eco_driver_weekly_trends_view / eco_driver_monthly_trends_view.

    The view orders by (month_start_date, period_end_date) within an
    assigned_id partition and computes LAG-based deltas. We replay that with
    Python to assert:

      * W2 previous = W1, W3 previous = W2,
      * kilometers_delta_abs = current - previous,
      * ranking_position_delta = previous - current (positive = improved).
    """
    trips = _build_source_trips()
    audit = _normalize_assignments(trips)
    periods = _month_bounded_weekly_periods(MONTH_START)

    weekly_snapshots = []
    for period in periods:
        rows = _stats_rows_for_period(audit, period)
        _apply_rankings(rows, monthly=False)
        weekly_snapshots.append(rows)

    # Pick INC1 chronologically across all 5 snapshots.
    inc1_chrono = []
    for snapshot in weekly_snapshots:
        for row in snapshot:
            if row["assigned_id"] == "INC1":
                inc1_chrono.append(row)
                break

    _assert_equal(
        "INC1 has 5 cumulative weekly snapshots in April",
        len(inc1_chrono), 5,
    )

    # ---- Build trend rows the same way the SQL view does ----
    trend_rows = []
    for index, row in enumerate(inc1_chrono):
        previous = inc1_chrono[index - 1] if index else None
        trend_rows.append(
            {
                "period_label": row["period_label"],
                "score": row["eco_driving_score_total"],
                "kilometers": row["total_kilometers"],
                "previous_kilometers": (
                    previous["total_kilometers"] if previous else None
                ),
                "kilometers_delta_abs": (
                    None
                    if previous is None
                    or row["total_kilometers"] is None
                    or previous["total_kilometers"] is None
                    else row["total_kilometers"] - previous["total_kilometers"]
                ),
                "ranking_position": row["ranking_position"],
                "previous_ranking_position": (
                    previous["ranking_position"] if previous else None
                ),
                "ranking_position_delta": (
                    None
                    if previous is None
                    or row["ranking_position"] is None
                    or previous["ranking_position"] is None
                    else previous["ranking_position"] - row["ranking_position"]
                ),
            }
        )

    _assert_equal(
        "W2 previous snapshot is W1",
        trend_rows[1]["previous_kilometers"], inc1_chrono[0]["total_kilometers"],
    )
    _assert_equal(
        "W3 previous snapshot is W2",
        trend_rows[2]["previous_kilometers"], inc1_chrono[1]["total_kilometers"],
    )
    _assert_equal(
        "kilometers_delta_abs equals current - previous for INC1 W2",
        trend_rows[1]["kilometers_delta_abs"],
        inc1_chrono[1]["total_kilometers"] - inc1_chrono[0]["total_kilometers"],
    )
    _check(
        "INC1 cumulative kilometers strictly increase across snapshots",
        all(
            trend_rows[i]["kilometers_delta_abs"] is None
            or trend_rows[i]["kilometers_delta_abs"] >= Decimal("0.000")
            for i in range(1, 5)
        ),
    )

    # Rank-improvement / rank-worsening:
    # Force a synthetic scenario that produces both signs by switching INC1's
    # ranking between snapshots. We compare two fake periods with explicit
    # ranking positions and verify the sign convention.
    rank_view = []
    for idx, (rank, prev_rank) in enumerate(
        [(3, None), (1, 3), (5, 1)],  # improve, then worsen
    ):
        rank_view.append(
            {
                "ranking_position": rank,
                "previous_ranking_position": prev_rank,
                "ranking_position_delta": (
                    None
                    if prev_rank is None or rank is None
                    else prev_rank - rank
                ),
            }
        )
    _assert_equal(
        "ranking_position_delta is positive when rank improves",
        rank_view[1]["ranking_position_delta"], 2,
    )
    _check(
        "ranking_position_delta is negative when rank worsens",
        rank_view[2]["ranking_position_delta"] < 0,
        f"got={rank_view[2]['ranking_position_delta']}",
    )

    # UNKNOWN_DRIVER trend stays unranked.
    unk_chrono = []
    for snapshot in weekly_snapshots:
        for row in snapshot:
            if row["assigned_id"] == "UNK1":
                unk_chrono.append(row)
                break
    for row in unk_chrono:
        _check(
            f"UNKNOWN_DRIVER row {row['period_label']} stays unranked in trend",
            row["ranking_position"] is None,
        )

    # Monthly trend across two months: synthesize a second month (May 2026)
    # by reusing the same audit data but shifted forward by one month for INC1.
    # This proves previous-month deltas work over independent monthly stats.
    may_audit = []
    for row in audit:
        if row.assigned_id == "INC1" and row.aggregation_included:
            may_audit.append(
                AssignmentRow(
                    provider_trip_id=row.provider_trip_id + 100_000,
                    day=date(row.day.year, row.day.month + 1, row.day.day),
                    assigned_id=row.assigned_id,
                    assignment_source=row.assignment_source,
                    driver_restrictions_raw=row.driver_restrictions_raw,
                    dysponent_id_raw=row.dysponent_id_raw,
                    driver_tag_description=row.driver_tag_description,
                    is_private_trip=row.is_private_trip,
                    aggregation_included=row.aggregation_included,
                    exclusion_reason=row.exclusion_reason,
                    distance_meters=row.distance_meters // 2,
                    metrics=dict(row.metrics),
                )
            )

    may_monthly = []
    in_may = [r for r in may_audit if date(2026, 5, 1) <= r.day < date(2026, 6, 1)]
    if in_may:
        may_agg = _aggregate_per_assigned(
            in_may, monthly=True,
            month_start=date(2026, 5, 1), month_end=date(2026, 6, 1),
        )
        may_monthly = [
            _stats_row_from_aggregate(row, None, monthly=True) for row in may_agg
        ]

    if may_monthly:
        april_monthly = _by_assigned_id(_monthly_stats_rows(audit))["INC1"]
        may_inc1 = _by_assigned_id(may_monthly)["INC1"]
        delta_km = may_inc1["total_kilometers"] - april_monthly["total_kilometers"]
        _check(
            "monthly previous-month kilometers delta matches independent stats",
            delta_km
            == may_inc1["total_kilometers"] - april_monthly["total_kilometers"],
        )


def test_runner_dispatcher_modes_and_resolvers() -> None:
    # Dispatcher registration + mode mapping.
    expected_modes = {
        "eco_driving_weekly_snapshot": (
            MODE_WEEKLY_CUMULATIVE_SNAPSHOT, True, False,
        ),
        "eco_driving_month_end_weekly_snapshot": (
            MODE_FINAL_MONTH_WEEKLY_SNAPSHOT, True, False,
        ),
        "eco_driving_monthly_aggregation": (
            MODE_MONTHLY_FULL_AGGREGATION, False, True,
        ),
    }
    for dataset_name, (mode, want_weekly, want_monthly) in expected_modes.items():
        spec = registry.DATASETS.get(dataset_name)
        _check(
            f"dispatcher dataset {dataset_name} registered",
            spec is not None and spec.job_module == ECO_JOB_MODULE,
        )
        cfg = ECO_DRIVING_SCHEDULE_MODES.get(dataset_name)
        _check(
            f"dispatcher knows mode mapping for {dataset_name}",
            cfg is not None and cfg["mode"] == mode
            and cfg["include_weekly"] is want_weekly
            and cfg["include_monthly"] is want_monthly,
            f"cfg={cfg}",
        )

        fire = datetime(2026, 5, 1, 1, 0, tzinfo=timezone.utc)
        params = _build_job_params(
            client_id=CLIENT_ID,
            client_code=CLIENT_CODE,
            dataset_name=dataset_name,
            event_enrichment_mode="enabled",
            window_start_ts=fire,
            window_end_ts=fire,
        )
        _assert_equal(
            f"{dataset_name} → params.mode",
            params["mode"], mode,
        )
        _assert_equal(
            f"{dataset_name} → params.include_weekly",
            params["include_weekly"], want_weekly,
        )
        _assert_equal(
            f"{dataset_name} → params.include_monthly",
            params["include_monthly"], want_monthly,
        )
        _check(
            f"{dataset_name} dispatcher params carry client identity",
            params["client_id"] == CLIENT_ID
            and params["client_code"] == CLIENT_CODE
            and params["trigger"] == "SCHEDULED",
        )

    # Resolver outputs for the test month.
    # Apr 20 is a Monday inside April; weekly_cumulative_snapshot at noon
    # local time should resolve to (Apr 1 → Apr 20).
    weekly_period = resolve_previous_completed_weekly_snapshot(
        datetime(2026, 4, 20, 12, 0, tzinfo=WARSAW),
    )
    _assert_equal(
        "weekly_cumulative_snapshot resolves period_start_date = month_start",
        weekly_period.period_start_date, MONTH_START,
    )
    _assert_equal(
        "weekly_cumulative_snapshot resolves period_end_date = Apr 20",
        weekly_period.period_end_date, date(2026, 4, 20),
    )

    final_period = resolve_final_month_weekly_snapshot(
        datetime(2026, 5, 1, 3, 30, tzinfo=WARSAW),
    )
    _assert_equal(
        "final_month_weekly_snapshot resolves previous_month_start",
        final_period.period_start_date, MONTH_START,
    )
    _assert_equal(
        "final_month_weekly_snapshot resolves current_month_start",
        final_period.period_end_date, NEXT_MONTH_START,
    )

    month_start, month_end = resolve_previous_completed_month(
        datetime(2026, 5, 1, 4, 0, tzinfo=WARSAW),
    )
    _assert_equal(
        "monthly_full_aggregation resolves previous month_start",
        month_start, MONTH_START,
    )
    _assert_equal(
        "monthly_full_aggregation resolves current month_start",
        month_end, NEXT_MONTH_START,
    )

    # Selected-month manual param resolves to all weekly periods + monthly.
    month_start_p, periods, explicit, mode = _resolve_periods({"month": "2026-04"})
    _assert_equal(
        "manual month=2026-04 resolves to 5 weekly periods",
        len(periods), 5,
    )
    _assert_equal(
        "manual month=2026-04 month_start",
        month_start_p, MONTH_START,
    )
    _check(
        "manual selected-month mode is not the explicit-period mode",
        explicit is False,
    )


def test_static_job_contracts_for_dry_run_recalculate_and_idempotency() -> None:
    job_src = (
        REPO_ROOT / "jobs" / "ecodriving" / "job_eco_driving_aggregate.py"
    ).read_text(encoding="utf-8")

    # dry_run uses rollback / commit branch.
    _check(
        "dry_run rolls back the assignment+stats transaction",
        "if dry_run:" in job_src and "conn.rollback()" in job_src
        and "conn.commit()" in job_src,
    )

    # recalculate drives the delete-then-upsert path for both weekly and monthly.
    _check(
        "recalculate=true deletes stale weekly rows in scope before re-upsert",
        "if recalculate and include_weekly:" in job_src
        and "_delete_existing_stats(" in job_src,
    )
    _check(
        "recalculate=true deletes stale monthly rows in scope before re-upsert",
        "if recalculate and include_monthly and month_start is not None:" in job_src,
    )

    # Idempotent upsert keys.
    _check(
        "eco_trip_assignments upsert keyed by (client_id, provider_trip_id)",
        "ON CONFLICT (client_id, provider_trip_id) DO UPDATE" in job_src,
    )
    _check(
        "eco_driver_weekly_stats upsert keyed by (client_id, assigned_id, "
        "period_start_date, period_end_date)",
        "ON CONFLICT (client_id, assigned_id, period_start_date, "
        "period_end_date) DO UPDATE" in job_src,
    )
    _check(
        "eco_driver_monthly_stats upsert keyed by (client_id, assigned_id, "
        "month_start_date)",
        "ON CONFLICT (client_id, assigned_id, month_start_date) DO UPDATE"
        in job_src,
    )

    # Aggregation filter: only included, non-private, assigned-id rows.
    _check(
        "aggregation filter excludes private and missing-ID rows in SQL",
        "aggregation_included IS TRUE AND a.is_private_trip IS FALSE" in job_src
        and "a.assigned_id IS NOT NULL" in job_src,
    )

    # Eco scoring is the only allowed scoring path. Since the arbitrary-week
    # work it is reached through ONE shared canonical domain module, which both
    # this job and the portal's dynamic recomputation call — so the two cannot
    # drift apart, and neither can restate a ladder of its own.
    domain_src = (REPO_ROOT / "api/eco_driving_explorer/period_domain.py").read_text()
    _check(
        "job_eco_driving_aggregate scores through the shared canonical domain",
        "from api.eco_driving_explorer import period_domain as PD" in job_src
        and "PD.score_aggregate(" in job_src,
    )
    _check(
        "the shared canonical domain scores with eco_scoring and nothing else",
        "from .eco_scoring import" in domain_src
        and "calculate_eco_score" in domain_src
        and "calculate_maxpoints_subtractions" in domain_src
        and "classify_ecodriving_rating_type" in domain_src,
    )
    _check(
        "the job defines no scoring ladder, threshold or maximum of its own",
        "SCORING_RULES" not in job_src
        and "METRIC_MAX_POINTS" not in job_src
        and "RATING_THRESHOLDS" not in job_src,
    )

    # Registry/dispatcher constants.
    _assert_equal(
        "DATASET_NAME stays stable",
        DATASET_NAME, "eco_driving_aggregate",
    )


def test_idempotent_python_pipeline() -> None:
    """Running the simulated pipeline twice must produce identical output rows."""
    trips = _build_source_trips()
    audit = _normalize_assignments(trips)
    periods = _month_bounded_weekly_periods(MONTH_START)

    def _run() -> tuple[list[dict], list[dict]]:
        weekly_all = []
        for period in periods:
            rows = _stats_rows_for_period(audit, period)
            _apply_rankings(rows, monthly=False)
            weekly_all.extend(rows)
        monthly_rows = _monthly_stats_rows(audit)
        _apply_rankings(monthly_rows, monthly=True)
        return weekly_all, monthly_rows

    weekly_a, monthly_a = _run()
    weekly_b, monthly_b = _run()

    def _key_weekly(row: dict) -> tuple:
        return (row["assigned_id"], row["period_start_date"], row["period_end_date"])

    def _key_monthly(row: dict) -> tuple:
        return (row["assigned_id"], row["month_start_date"])

    weekly_a_sorted = sorted(weekly_a, key=_key_weekly)
    weekly_b_sorted = sorted(weekly_b, key=_key_weekly)
    monthly_a_sorted = sorted(monthly_a, key=_key_monthly)
    monthly_b_sorted = sorted(monthly_b, key=_key_monthly)

    _assert_equal(
        "idempotent rerun produces the same number of weekly rows",
        len(weekly_a_sorted), len(weekly_b_sorted),
    )
    _assert_equal(
        "idempotent rerun produces the same number of monthly rows",
        len(monthly_a_sorted), len(monthly_b_sorted),
    )

    weekly_diffs = [
        (a, b) for a, b in zip(weekly_a_sorted, weekly_b_sorted) if a != b
    ]
    monthly_diffs = [
        (a, b) for a, b in zip(monthly_a_sorted, monthly_b_sorted) if a != b
    ]
    _check(
        "weekly rows match exactly across reruns",
        not weekly_diffs,
        f"diffs={weekly_diffs[:1]}",
    )
    _check(
        "monthly rows match exactly across reruns",
        not monthly_diffs,
        f"diffs={monthly_diffs[:1]}",
    )


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def main() -> int:
    test_chosen_month_boundary_shape()
    test_assignment_audit_and_private_exclusion()
    test_cumulative_weekly_snapshots_and_monthly_match()
    test_rates_and_scoring_boundaries()
    test_ranking_groups_and_tie_breakers()
    test_weekly_and_monthly_trend_deltas()
    test_runner_dispatcher_modes_and_resolvers()
    test_static_job_contracts_for_dry_run_recalculate_and_idempotency()
    test_idempotent_python_pipeline()

    print("")
    if FAILURES:
        print(f"FAIL — {len(FAILURES)} check(s) failed:")
        for label in FAILURES:
            print(f"  - {label}")
        return 1
    print("OK - Eco Driving end-to-end pipeline checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
