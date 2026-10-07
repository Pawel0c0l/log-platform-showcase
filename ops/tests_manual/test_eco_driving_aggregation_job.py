#!/usr/bin/env python3
"""Manual checks for Eco Driving aggregation job helpers.

These checks exercise the deterministic business rules without requiring a
live client database. The job itself keeps DB filtering/aggregation in SQL and
uses these helpers for period, scoring, qualification, and ranking semantics.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
import sys
from types import SimpleNamespace
from zoneinfo import ZoneInfo


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.ecodriving import job_eco_driving_aggregate as aggregate_job  # noqa: E402
from jobs.ecodriving.job_eco_driving_aggregate import (  # noqa: E402
    ASSIGNMENT_DRIVER_RESTRICTIONS,
    ASSIGNMENT_DYSPONENT_ID,
    ASSIGNMENT_SKIPPED_NO_ID,
    DRIVER_CHART_NOT_LOADED,
    DRIVER_CHART_NO_SOURCE_MATCHES,
    EcoDrivingAggregationPreconditionError,
    PRIVATE_EXCLUSION_REASON,
    RankingPeriod,
    _apply_rankings,
    _apply_rating_type_share_percent,
    _assignment_for_values,
    _count_rank_groups,
    _is_private_trip,
    _month_bounded_weekly_periods,
    _qualification_and_calculation,
    _rate_per_100km,
    _rate_scoring_diagnostics_from_aggregate,
    _raw_rate_per_100km,
    _stats_row_from_aggregate,
    _validate_driver_chart_loaded,
    _validate_source_chart_matches,
    _total_kilometers,
)


CLIENT_ID = "00000000-0000-0000-0000-000000000001"


def _period(start: date, end: date) -> RankingPeriod:
    return RankingPeriod(
        period_start_date=start,
        period_end_date=end,
        month_start_date=start.replace(day=1),
        period_sequence_in_month=1,
        period_label=f"{start:%Y-%m}-W1",
        is_partial_period=(end - start).days != 7 or start.isoweekday() != 1,
    )


def _aggregate_row(
    assigned_id: str,
    *,
    meters: int,
    trips: int = 1,
    source_trips: int | None = None,
    skipped: int = 0,
    ranking_included: bool | None = True,
    chart_exists: bool = True,
    month_start: date | None = None,
    month_end: date | None = None,
    **events,
) -> dict:
    row = {
        "client_id": CLIENT_ID,
        "client_code": "TEST",
        "assigned_id": assigned_id,
        "trips_count": trips,
        "source_trips_count": source_trips if source_trips is not None else trips + skipped,
        "skipped_trips_count": skipped,
        "total_distance_meters": meters,
        "driver_id": assigned_id if chart_exists else None,
        "ranking_included": ranking_included,
        "month_start_date": month_start,
        "month_end_date": month_end,
        "overrev_events_count": 0,
        "harsh_braking_events": 0,
        "harsh_acceleration_events": 0,
        "harsh_turning_events": 0,
        "idle_events": 0,
        "speeding_140_160_count": 0,
        "speeding_160_170_count": 0,
        "speeding_170_plus_count": 0,
    }
    row.update(events)
    return row


def test_assignment_priority_and_private_detection() -> None:
    assert _assignment_for_values(" R-123 ", " D-999 ") == (
        "R-123",
        ASSIGNMENT_DRIVER_RESTRICTIONS,
    )
    assert _assignment_for_values(" ", " D-999 ") == (
        "D-999",
        ASSIGNMENT_DYSPONENT_ID,
    )
    assert _assignment_for_values(None, "") == (None, ASSIGNMENT_SKIPPED_NO_ID)

    for value in ("pryw", "PRYW", "Prywatny", "prywatny", "jazda pryw.", "wyjazd PRYWATNY"):
        assert _is_private_trip(value), value
    assert not _is_private_trip("business")
    print("PASS: assignment priority and private-trip contains matching")


def test_month_bounded_weekly_periods() -> None:
    may_2026 = _month_bounded_weekly_periods(date(2026, 5, 1))
    assert may_2026[0].period_start_date == date(2026, 5, 1)
    assert may_2026[0].period_end_date == date(2026, 5, 4)
    assert may_2026[0].is_partial_period is True
    assert may_2026[1].period_start_date == date(2026, 5, 1)
    assert may_2026[1].period_end_date == date(2026, 5, 11)
    assert may_2026[1].is_partial_period is False
    assert may_2026[2].period_start_date == date(2026, 5, 1)
    assert may_2026[2].period_end_date == date(2026, 5, 18)
    assert may_2026[-1].period_start_date == date(2026, 5, 1)
    assert may_2026[-1].period_end_date == date(2026, 6, 1)

    april_2026 = _month_bounded_weekly_periods(date(2026, 4, 1))
    assert april_2026[0].period_start_date == date(2026, 4, 1)
    assert april_2026[0].period_end_date == date(2026, 4, 6)
    assert april_2026[-1].period_start_date == date(2026, 4, 1)
    assert april_2026[-1].period_end_date == date(2026, 5, 1)
    assert april_2026[-1].is_partial_period is True

    june_2026 = _month_bounded_weekly_periods(date(2026, 6, 1))
    assert june_2026[0].period_start_date == date(2026, 6, 1)
    assert june_2026[0].period_end_date == date(2026, 6, 8)
    assert june_2026[0].is_partial_period is False
    print("PASS: cumulative month-to-date weekly report boundaries")


def test_ranking_period_timestamp_contract() -> None:
    warsaw = ZoneInfo("Europe/Warsaw")
    explicit = _period(date(2026, 7, 1), date(2026, 7, 20))
    assert explicit.start_ts == datetime(2026, 7, 1, 0, 0, tzinfo=warsaw)
    assert explicit.end_ts == datetime(2026, 7, 20, 0, 0, tzinfo=warsaw)
    assert explicit.start_ts.tzinfo is not None
    assert explicit.end_ts.tzinfo is not None
    assert explicit.period_end_date - date.resolution == date(2026, 7, 19)

    monthly = _period(date(2026, 5, 1), date(2026, 6, 1))
    assert monthly.start_ts == datetime(2026, 5, 1, 0, 0, tzinfo=warsaw)
    assert monthly.end_ts == datetime(2026, 6, 1, 0, 0, tzinfo=warsaw)

    precondition_error = EcoDrivingAggregationPreconditionError(
        DRIVER_CHART_NOT_LOADED,
        {"driver_chart_rows": 0},
    )
    assert not hasattr(precondition_error, "end_ts")
    print("PASS: RankingPeriod owns timezone-aware inclusive/exclusive timestamp boundaries")


class _EntryPointFakeCursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, object]] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None) -> None:
        self.executed.append((sql, params))

    def fetchone(self) -> dict:
        return {"chart_row_count": 0}


class _EntryPointFakeConnection:
    def __init__(self) -> None:
        self.fake_cursor = _EntryPointFakeCursor()
        self.rollback_count = 0
        self.closed = False

    def cursor(self, **_kwargs):
        return self.fake_cursor

    def rollback(self) -> None:
        self.rollback_count += 1

    def close(self) -> None:
        self.closed = True


class _EntryPointFakeClient:
    def __init__(self) -> None:
        self.logs: list[tuple[tuple, dict]] = []

    def log(self, *args, **kwargs) -> None:
        self.logs.append((args, kwargs))


def test_explicit_period_real_entry_point_reaches_chart_precondition() -> None:
    fake_conn = _EntryPointFakeConnection()
    fake_client = _EntryPointFakeClient()
    originals = {
        "_load_client_account_config": aggregate_job._load_client_account_config,
        "_client_business_pg_conn": aggregate_job._client_business_pg_conn,
        "_dict_row_factory": aggregate_job._dict_row_factory,
    }
    aggregate_job._load_client_account_config = lambda **_kwargs: SimpleNamespace(
        client_db_schema="public"
    )
    aggregate_job._client_business_pg_conn = lambda _cfg: fake_conn
    aggregate_job._dict_row_factory = lambda: None
    try:
        try:
            aggregate_job.run(
                client=fake_client,
                run_id="00000000-0000-0000-0000-000000000099",
                params={
                    "client_id": CLIENT_ID,
                    "period_start_date": "2026-07-01",
                    "period_end_date": "2026-07-20",
                    "include_weekly": True,
                    "include_monthly": False,
                    "dry_run": True,
                },
            )
        except EcoDrivingAggregationPreconditionError as exc:
            assert exc.classification == DRIVER_CHART_NOT_LOADED
        except AttributeError as exc:
            raise AssertionError(
                "real explicit-period entry point must expose RankingPeriod.end_ts"
            ) from exc
        else:
            raise AssertionError("synthetic empty chart must stop at the chart precondition")
    finally:
        for name, value in originals.items():
            setattr(aggregate_job, name, value)

    assert fake_conn.rollback_count >= 1
    assert fake_conn.closed is True
    assert any("count(*)::int AS chart_row_count" in sql for sql, _params in fake_conn.fake_cursor.executed)
    assert any(
        args[3] == DRIVER_CHART_NOT_LOADED
        for args, _kwargs in fake_client.logs
        if len(args) > 3
    )
    print("PASS: real aggregate entry point resolves explicit timestamps before chart precondition")


def _cumulative_weekly_stats(trips: list[dict], period: RankingPeriod) -> dict:
    included = [
        trip
        for trip in trips
        if period.period_start_date <= trip["day"] < period.period_end_date
        and trip.get("assigned_id")
        and not trip.get("is_private_trip", False)
    ]
    skipped = [
        trip
        for trip in trips
        if period.period_start_date <= trip["day"] < period.period_end_date
        and (not trip.get("assigned_id") or trip.get("is_private_trip", False))
    ]
    return _stats_row_from_aggregate(
        _aggregate_row(
            "A",
            meters=sum(trip["meters"] for trip in included),
            trips=len(included),
            source_trips=len(included) + len(skipped),
            skipped=len(skipped),
            overrev_events_count=sum(trip.get("overrev_events_count", 0) for trip in included),
            harsh_braking_events=sum(trip.get("harsh_braking_events", 0) for trip in included),
            speeding_170_plus_count=sum(trip.get("speeding_170_plus_count", 0) for trip in included),
        ),
        period,
        monthly=False,
    )


def test_cumulative_weekly_aggregation_semantics() -> None:
    periods = _month_bounded_weekly_periods(date(2026, 5, 1))
    trips = [
        {"day": date(2026, 5, 2), "assigned_id": "A", "meters": 10_000, "overrev_events_count": 1},
        {"day": date(2026, 5, 5), "assigned_id": "A", "meters": 20_000, "overrev_events_count": 2},
        {"day": date(2026, 5, 12), "assigned_id": "A", "meters": 30_000, "overrev_events_count": 3},
        {"day": date(2026, 5, 26), "assigned_id": "A", "meters": 40_000, "overrev_events_count": 4},
        {"day": date(2026, 5, 3), "assigned_id": "A", "meters": 999_000, "is_private_trip": True},
        {"day": date(2026, 5, 6), "assigned_id": None, "meters": 999_000},
    ]

    w1 = _cumulative_weekly_stats(trips, periods[0])
    w2 = _cumulative_weekly_stats(trips, periods[1])
    w3 = _cumulative_weekly_stats(trips, periods[2])
    final_w = _cumulative_weekly_stats(trips, periods[-1])

    assert w1["period_start_date"] == date(2026, 5, 1)
    assert w1["period_end_date"] == date(2026, 5, 4)
    assert w1["total_distance_meters"] == 10_000
    assert w1["overrev_events_count"] == 1
    assert w1["source_trips_count"] == 2
    assert w1["skipped_trips_count"] == 1

    assert w2["period_start_date"] == date(2026, 5, 1)
    assert w2["period_end_date"] == date(2026, 5, 11)
    assert w2["total_distance_meters"] == 30_000
    assert w2["overrev_events_count"] == 3
    assert w2["source_trips_count"] == 4
    assert w2["skipped_trips_count"] == 2

    assert w3["period_start_date"] == date(2026, 5, 1)
    assert w3["period_end_date"] == date(2026, 5, 18)
    assert w3["total_distance_meters"] == 60_000
    assert w3["overrev_events_count"] == 6

    assert final_w["period_start_date"] == date(2026, 5, 1)
    assert final_w["period_end_date"] == date(2026, 6, 1)
    assert final_w["total_distance_meters"] == 100_000
    assert final_w["total_kilometers"] == Decimal("100.000")
    assert final_w["overrev_events_count"] == 10

    monthly = _stats_row_from_aggregate(
        _aggregate_row(
            "A",
            meters=100_000,
            trips=4,
            source_trips=6,
            skipped=2,
            month_start=date(2026, 5, 1),
            month_end=date(2026, 6, 1),
            overrev_events_count=10,
        ),
        None,
        monthly=True,
    )
    assert final_w["total_distance_meters"] == monthly["total_distance_meters"]
    assert final_w["total_kilometers"] == monthly["total_kilometers"]
    assert final_w["overrev_events_count"] == monthly["overrev_events_count"]
    summed_weekly_distance = sum(
        _cumulative_weekly_stats(trips, period)["total_distance_meters"]
        for period in periods
    )
    assert summed_weekly_distance > monthly["total_distance_meters"]
    print("PASS: W2/W3/final are cumulative snapshots and final raw totals match monthly")


def test_stats_scoring_qualification_and_nulls() -> None:
    period = _period(date(2026, 5, 4), date(2026, 5, 11))
    row = _aggregate_row(
        "A",
        meters=100_000,
        trips=2,
        source_trips=3,
        skipped=1,
        overrev_events_count=2,
        harsh_braking_events=None,
    )
    stats = _stats_row_from_aggregate(row, period, monthly=False)
    assert stats["total_kilometers"] == Decimal("100.000")
    assert stats["overrev_events_per_100km"] == Decimal("2")
    assert stats["harsh_braking_events"] == 0
    assert stats["harsh_braking_events_per_100km"] == Decimal("0")
    assert stats["eco_driving_score_total"] is not None
    assert stats["qualification_status"] == "QUALIFIED"
    assert stats["calculation_status"] == "OK"
    assert stats["overrev_points"] == 11
    assert stats["overrev_maxpoints_subtract"] == -4
    assert stats["harsh_braking_maxpoints_subtract"] == 0
    assert stats["top_1_validation"] == "Nadmierne obroty"
    assert stats["top_2_validation"] is None
    assert stats["ecodriving_rating_type"] == "bezpieczny"
    assert stats["source_trips_count"] == 3
    assert stats["skipped_trips_count"] == 1

    assert _qualification_and_calculation(99_999) == ("LOW_DISTANCE", "OK")
    assert _qualification_and_calculation(0) == ("NO_DISTANCE", "NO_DISTANCE")
    assert _total_kilometers(0) == Decimal("0.000")
    assert _rate_per_100km(5, Decimal("0.000")) is None

    zero_stats = _stats_row_from_aggregate(
        _aggregate_row("Z", meters=0),
        period,
        monthly=False,
    )
    assert zero_stats["total_kilometers"] == Decimal("0.000")
    assert zero_stats["overrev_events_per_100km"] is None
    assert zero_stats["overrev_points"] is None
    assert zero_stats["overrev_maxpoints_subtract"] is None
    assert zero_stats["top_1_validation"] is None
    assert zero_stats["top_2_validation"] is None
    assert zero_stats["eco_driving_score_total"] is None
    assert zero_stats["ecodriving_rating_type"] is None
    assert zero_stats["qualification_status"] == "NO_DISTANCE"
    assert zero_stats["calculation_status"] == "NO_DISTANCE"
    print("PASS: rates, NULL event handling, zero distance, and qualification statuses")


def test_per_100km_rates_are_stored_as_whole_numbers() -> None:
    assert _raw_rate_per_100km(1, Decimal("204.082")) == Decimal("0.4900")
    assert _rate_per_100km(1, Decimal("204.082")) == Decimal("0")
    assert _raw_rate_per_100km(1, Decimal("200.000")) == Decimal("0.5000")
    assert _rate_per_100km(1, Decimal("200.000")) == Decimal("1")
    assert _rate_per_100km(3, Decimal("214.286")) == Decimal("1")
    assert _rate_per_100km(3, Decimal("200.000")) == Decimal("2")

    period = _period(date(2026, 5, 4), date(2026, 5, 11))
    weekly = _stats_row_from_aggregate(
        _aggregate_row(
            "WR",
            meters=200_000,
            overrev_events_count=3,
            harsh_braking_events=1,
        ),
        period,
        monthly=False,
    )
    assert weekly["overrev_events_per_100km"] == Decimal("2")
    assert weekly["harsh_braking_events_per_100km"] == Decimal("1")
    assert weekly["harsh_acceleration_events_per_100km"] == Decimal("0")

    monthly = _stats_row_from_aggregate(
        _aggregate_row(
            "MR",
            meters=200_000,
            month_start=date(2026, 5, 1),
            month_end=date(2026, 6, 1),
            overrev_events_count=3,
            harsh_braking_events=1,
        ),
        None,
        monthly=True,
    )
    assert monthly["overrev_events_per_100km"] == Decimal("2")
    assert monthly["harsh_braking_events_per_100km"] == Decimal("1")
    assert monthly["harsh_acceleration_events_per_100km"] == Decimal("0")

    raw_049 = _stats_row_from_aggregate(_aggregate_row("E049", meters=20_408_200, harsh_braking_events=1), period, monthly=False)
    raw_050 = _stats_row_from_aggregate(_aggregate_row("E050", meters=20_000_000, harsh_braking_events=100), period, monthly=False)
    raw_149 = _stats_row_from_aggregate(_aggregate_row("E149", meters=10_000_000, harsh_braking_events=149), period, monthly=False)
    raw_150 = _stats_row_from_aggregate(_aggregate_row("E150", meters=10_000_000, harsh_braking_events=150), period, monthly=False)
    assert raw_049["harsh_braking_events_per_100km"] == Decimal("0")
    assert raw_049["harsh_braking_maxpoints_subtract"] == 0
    assert raw_050["harsh_braking_events_per_100km"] == Decimal("1")
    assert raw_050["harsh_braking_maxpoints_subtract"] == -2
    assert raw_149["harsh_braking_events_per_100km"] == Decimal("1")
    assert raw_149["harsh_braking_maxpoints_subtract"] == -2
    assert raw_150["harsh_braking_events_per_100km"] == Decimal("2")
    assert raw_150["harsh_braking_maxpoints_subtract"] == -6

    boundary_down = _stats_row_from_aggregate(_aggregate_row("B249", meters=10_000_000, overrev_events_count=249), period, monthly=False)
    boundary_up = _stats_row_from_aggregate(_aggregate_row("B250", meters=10_000_000, overrev_events_count=250), period, monthly=False)
    assert boundary_down["overrev_events_per_100km"] == Decimal("2")
    assert boundary_down["overrev_maxpoints_subtract"] == -4
    assert boundary_up["overrev_events_per_100km"] == Decimal("3")
    assert boundary_up["overrev_maxpoints_subtract"] == -8
    print("PASS: weekly and monthly per-100km stats store whole-number rates and score rounded buckets")


def test_fractional_raw_rates_score_after_display_rounding() -> None:
    period = _period(date(2026, 5, 4), date(2026, 5, 11))
    row = _aggregate_row("FR", meters=10_000_000, idle_events=23)

    weekly = _stats_row_from_aggregate(row, period, monthly=False)
    assert weekly["idle_events_per_100km"] == Decimal("0")
    assert weekly["idle_points"] == 10
    assert weekly["idle_maxpoints_subtract"] == 0
    assert weekly["eco_driving_score_total"] == 100
    assert weekly["top_1_validation"] is None

    weekly_diag = _rate_scoring_diagnostics_from_aggregate(row, period, monthly=False)
    idle_diag = next(item for item in weekly_diag["metric_diagnostics"] if item["metric_key"] == "idle_events")
    assert idle_diag["numerator_value"] == 23
    assert idle_diag["denominator_total_kilometers"] == Decimal("10000.000")
    assert idle_diag["raw_rate_before_rounding"] == Decimal("0.2300")
    assert idle_diag["stored_display_rate"] == Decimal("0")
    assert idle_diag["persisted_subtract"] == 0
    assert weekly_diag["recomputed_score_from_subtract_columns"] == Decimal("100")
    assert weekly_diag["score_difference"] == Decimal("0")

    monthly = _stats_row_from_aggregate(
        _aggregate_row(
            "FR",
            meters=10_000_000,
            idle_events=23,
            month_start=date(2026, 5, 1),
            month_end=date(2026, 6, 1),
        ),
        None,
        monthly=True,
    )
    assert monthly["idle_events_per_100km"] == weekly["idle_events_per_100km"]
    assert monthly["idle_maxpoints_subtract"] == weekly["idle_maxpoints_subtract"]
    assert monthly["eco_driving_score_total"] == weekly["eco_driving_score_total"]
    print("PASS: fractional raw rates are rounded first, then scored from the rounded coefficient")


def test_weekly_and_monthly_low_distance_boundaries() -> None:
    period = _period(date(2026, 5, 4), date(2026, 5, 11))

    weekly_low = _stats_row_from_aggregate(_aggregate_row("WL", meters=99_990), period, monthly=False)
    weekly_qualified = _stats_row_from_aggregate(_aggregate_row("WQ", meters=100_000), period, monthly=False)
    assert weekly_low["total_kilometers"] == Decimal("99.990")
    assert weekly_low["qualification_status"] == "LOW_DISTANCE"
    assert weekly_qualified["total_kilometers"] == Decimal("100.000")
    assert weekly_qualified["qualification_status"] != "LOW_DISTANCE"
    assert weekly_qualified["qualification_status"] == "QUALIFIED"

    monthly_low = _stats_row_from_aggregate(
        _aggregate_row(
            "ML",
            meters=99_990,
            month_start=date(2026, 5, 1),
            month_end=date(2026, 6, 1),
        ),
        None,
        monthly=True,
    )
    monthly_qualified = _stats_row_from_aggregate(
        _aggregate_row(
            "MQ",
            meters=100_000,
            month_start=date(2026, 5, 1),
            month_end=date(2026, 6, 1),
        ),
        None,
        monthly=True,
    )
    assert monthly_low["total_kilometers"] == Decimal("99.990")
    assert monthly_low["qualification_status"] == "LOW_DISTANCE"
    assert monthly_qualified["total_kilometers"] == Decimal("100.000")
    assert monthly_qualified["qualification_status"] != "LOW_DISTANCE"
    assert monthly_qualified["qualification_status"] == "QUALIFIED"
    print("PASS: weekly and monthly LOW_DISTANCE threshold is total_kilometers < 100")


def _share_test_row(
    assigned_id: str,
    rating_type: str | None,
    *,
    monthly: bool = False,
    ranking_included: bool | None = True,
    qualification_status: str = "QUALIFIED",
) -> dict:
    row = {
        "assigned_id": assigned_id,
        "ranking_included": ranking_included,
        "qualification_status": qualification_status,
        "ecodriving_rating_type": rating_type,
    }
    if monthly:
        row["month_start_date"] = date(2026, 5, 1)
    else:
        row["period_start_date"] = date(2026, 5, 1)
        row["period_end_date"] = date(2026, 6, 1)
    return row


def test_rating_type_share_percent_weekly_and_monthly() -> None:
    weekly_rows = (
        [_share_test_row(f"B{i:02d}", "bezpieczny") for i in range(20)]
        + [_share_test_row(f"A{i:02d}", "akceptowalny") for i in range(70)]
        + [_share_test_row(f"N{i:02d}", "niebezpieczny") for i in range(10)]
        + [_share_test_row("EXC", "bezpieczny", ranking_included=False)]
        + [_share_test_row("LOW", "bezpieczny", qualification_status="LOW_DISTANCE")]
        + [_share_test_row("ZERO", None, qualification_status="NO_DISTANCE")]
    )
    _apply_rating_type_share_percent(weekly_rows, monthly=False)
    assert all(row["ecodriving_rating_type_share_percent"] == Decimal("20.00") for row in weekly_rows[:20])
    assert all(row["ecodriving_rating_type_share_percent"] == Decimal("70.00") for row in weekly_rows[20:90])
    assert all(row["ecodriving_rating_type_share_percent"] == Decimal("10.00") for row in weekly_rows[90:100])
    assert weekly_rows[100]["ecodriving_rating_type_share_percent"] is None
    assert weekly_rows[101]["ecodriving_rating_type_share_percent"] is None
    assert weekly_rows[102]["ecodriving_rating_type_share_percent"] is None

    denominator_zero = [
        _share_test_row("E", "bezpieczny", ranking_included=False),
        _share_test_row("L", "akceptowalny", qualification_status="LOW_DISTANCE"),
    ]
    _apply_rating_type_share_percent(denominator_zero, monthly=False)
    assert all(row["ecodriving_rating_type_share_percent"] is None for row in denominator_zero)

    monthly_rows = (
        [_share_test_row("MB1", "bezpieczny", monthly=True), _share_test_row("MB2", "bezpieczny", monthly=True)]
        + [_share_test_row("MN1", "niebezpieczny", monthly=True)]
        + [_share_test_row("MLOW", "bezpieczny", monthly=True, qualification_status="LOW_DISTANCE")]
    )
    _apply_rating_type_share_percent(monthly_rows, monthly=True)
    assert monthly_rows[0]["ecodriving_rating_type_share_percent"] == Decimal("66.67")
    assert monthly_rows[1]["ecodriving_rating_type_share_percent"] == Decimal("66.67")
    assert monthly_rows[2]["ecodriving_rating_type_share_percent"] == Decimal("33.33")
    assert monthly_rows[3]["ecodriving_rating_type_share_percent"] is None
    print("PASS: rating-type share percent uses qualified ranked population for weekly and monthly rows")


def test_private_and_missing_id_exclusion_contract() -> None:
    assigned_id, source = _assignment_for_values(None, None)
    assert assigned_id is None
    assert source == ASSIGNMENT_SKIPPED_NO_ID
    assert PRIVATE_EXCLUSION_REASON == "PRIVATE_DRIVER_TAG"

    period = _period(date(2026, 5, 4), date(2026, 5, 11))
    public_row = _stats_row_from_aggregate(
        _aggregate_row("A", meters=100_000, trips=1, source_trips=2, skipped=1),
        period,
        monthly=False,
    )
    assert public_row["trips_count"] == 1
    assert public_row["source_trips_count"] == 2
    assert public_row["skipped_trips_count"] == 1
    assert public_row["total_distance_meters"] == 100_000
    print("PASS: skipped/private rows are represented as skipped source rows, not metric contributors")


def test_monthly_stats_and_ranking_groups() -> None:
    month_start = date(2026, 5, 1)
    month_end = date(2026, 6, 1)
    monthly = _stats_row_from_aggregate(
        _aggregate_row(
            "A",
            meters=100_000,
            month_start=month_start,
            month_end=month_end,
        ),
        None,
        monthly=True,
    )
    assert monthly["month_start_date"] == month_start
    assert monthly["month_end_date"] == month_end

    included = _stats_row_from_aggregate(
        _aggregate_row("INC", meters=100_000, ranking_included=True),
        _period(date(2026, 5, 4), date(2026, 5, 11)),
        monthly=False,
    )
    excluded = _stats_row_from_aggregate(
        _aggregate_row("EXC", meters=100_000, ranking_included=False),
        _period(date(2026, 5, 4), date(2026, 5, 11)),
        monthly=False,
    )
    unknown = _stats_row_from_aggregate(
        _aggregate_row("UNK", meters=100_000, ranking_included=None, chart_exists=False),
        _period(date(2026, 5, 4), date(2026, 5, 11)),
        monthly=False,
    )
    assert included["ranking_group"] == "INCLUDED"
    assert excluded["ranking_group"] == "EXCLUDED"
    assert unknown["ranking_group"] == "UNKNOWN_DRIVER"
    _apply_rankings([included, excluded, unknown], monthly=False)
    assert included["ranking_position"] == 1
    assert included["ranking_total_participants"] == 1
    assert excluded["ranking_position"] == 1
    assert excluded["ranking_total_participants"] == 1
    assert unknown["ranking_position"] is None
    assert unknown["ranking_total_participants"] is None
    print("PASS: monthly stats and INCLUDED/EXCLUDED/UNKNOWN_DRIVER ranking groups")


def test_driver_chart_preconditions_and_partial_match_diagnostics() -> None:
    period_start = date(2026, 7, 1)
    period_end = date(2026, 7, 20)
    try:
        _validate_driver_chart_loaded(
            0,
            period_start_date=period_start,
            period_end_date=period_end,
        )
    except EcoDrivingAggregationPreconditionError as exc:
        assert exc.classification == DRIVER_CHART_NOT_LOADED
        assert exc.diagnostics["driver_chart_rows"] == 0
    else:
        raise AssertionError("empty driver chart must fail before aggregation persistence")

    no_matches = [
        _aggregate_row("A", meters=100_000, chart_exists=False, ranking_included=None),
        _aggregate_row("B", meters=100_000, chart_exists=False, ranking_included=None),
    ]
    try:
        _validate_source_chart_matches(
            no_matches,
            chart_row_count=5,
            period_start_date=period_start,
            period_end_date=period_end,
            snapshot_type="weekly",
        )
    except EcoDrivingAggregationPreconditionError as exc:
        assert exc.classification == DRIVER_CHART_NO_SOURCE_MATCHES
        assert exc.diagnostics["source_driver_rows"] == 2
        assert exc.diagnostics["matched_driver_rows"] == 0
        assert exc.diagnostics["unmatched_driver_rows"] == 2
        assert exc.diagnostics["unmatched_driver_percentage"] == 100.0
    else:
        raise AssertionError("zero source-to-chart matches must fail")

    partial = [
        _aggregate_row("MATCHED", meters=200_000),
        _aggregate_row("UNKNOWN", meters=100_000, chart_exists=False, ranking_included=None),
    ]
    diagnostics = _validate_source_chart_matches(
        partial,
        chart_row_count=5,
        period_start_date=period_start,
        period_end_date=period_end,
        snapshot_type="weekly",
    )
    assert diagnostics["source_driver_rows"] == 2
    assert diagnostics["matched_driver_rows"] == 1
    assert diagnostics["unmatched_driver_rows"] == 1
    assert diagnostics["unmatched_driver_percentage"] == 50.0
    stats = [
        _stats_row_from_aggregate(row, _period(period_start, period_end), monthly=False)
        for row in partial
    ]
    _apply_rankings(stats, monthly=False)
    _apply_rating_type_share_percent(stats, monthly=False)
    assert stats[0]["ranking_position"] == 1
    assert stats[0]["ecodriving_rating_type_share_percent"] == Decimal("100.00")
    assert stats[1]["ranking_group"] == "UNKNOWN_DRIVER"
    assert stats[1]["ranking_position"] is None
    assert stats[1]["ecodriving_rating_type_share_percent"] is None

    empty = _validate_source_chart_matches(
        [],
        chart_row_count=5,
        period_start_date=period_start,
        period_end_date=period_end,
        snapshot_type="weekly",
    )
    assert empty["source_driver_rows"] == 0
    assert empty["unmatched_driver_percentage"] == 0.0

    source = (REPO_ROOT / "jobs" / "ecodriving" / "job_eco_driving_aggregate.py").read_text()
    run_source = source[source.index("def run(") :]
    assert run_source.index("_fetch_driver_chart_count(") < run_source.index("_upsert_assignments(")
    assert run_source.index("_validate_source_chart_matches(") < run_source.index(
        "_delete_existing_stats("
    )
    assert run_source.index("_validate_source_chart_matches(") < run_source.index(
        "_upsert_weekly_stats("
    )
    print("PASS: driver-chart preconditions fail before snapshot mutation and partial matches remain supported")


def test_ranking_tie_breakers_and_idempotency_contract() -> None:
    period = _period(date(2026, 5, 4), date(2026, 5, 11))
    higher_km = _stats_row_from_aggregate(_aggregate_row("B", meters=200_000), period, monthly=False)
    lower_km = _stats_row_from_aggregate(_aggregate_row("A", meters=100_000), period, monthly=False)
    _apply_rankings([lower_km, higher_km], monthly=False)
    assert higher_km["ranking_position"] == 1
    assert lower_km["ranking_position"] == 2

    a_id = _stats_row_from_aggregate(_aggregate_row("A", meters=100_000), period, monthly=False)
    b_id = _stats_row_from_aggregate(_aggregate_row("B", meters=100_000), period, monthly=False)
    _apply_rankings([b_id, a_id], monthly=False)
    assert a_id["ranking_position"] == 1
    assert b_id["ranking_position"] == 2

    # Every row here is QUALIFIED, so the assertion isolates per-snapshot
    # recalculation from the qualification gate (covered by
    # test_eco_ranking_qualified_only.py).
    periods = _month_bounded_weekly_periods(date(2026, 5, 1))
    w1_a = _stats_row_from_aggregate(_aggregate_row("A", meters=100_000), periods[0], monthly=False)
    w1_b = _stats_row_from_aggregate(_aggregate_row("B", meters=200_000), periods[0], monthly=False)
    w2_a = _stats_row_from_aggregate(_aggregate_row("A", meters=300_000), periods[1], monthly=False)
    w2_b = _stats_row_from_aggregate(_aggregate_row("B", meters=200_000), periods[1], monthly=False)
    _apply_rankings([w1_a, w1_b, w2_a, w2_b], monthly=False)
    assert w1_b["ranking_position"] == 1
    assert w1_a["ranking_position"] == 2
    assert w2_a["ranking_position"] == 1
    assert w2_b["ranking_position"] == 2
    print("PASS: rankings are recalculated independently per cumulative snapshot")


def test_only_qualified_rows_are_ranked() -> None:
    """Qualification gates ranking-group membership and the participant total."""

    period = _period(date(2026, 5, 4), date(2026, 5, 11))
    qualified = _stats_row_from_aggregate(_aggregate_row("Q", meters=200_000), period, monthly=False)
    low = _stats_row_from_aggregate(_aggregate_row("L", meters=50_000), period, monthly=False)
    zero = _stats_row_from_aggregate(_aggregate_row("Z", meters=0), period, monthly=False)
    unmapped_low = _stats_row_from_aggregate(
        _aggregate_row("U", meters=50_000, chart_exists=False, ranking_included=None),
        period,
        monthly=False,
    )
    _apply_rankings([qualified, low, zero, unmapped_low], monthly=False)

    assert qualified["ranking_group"] == "INCLUDED"
    assert qualified["ranking_position"] == 1
    # The two non-qualified chart members must not inflate the denominator.
    assert qualified["ranking_total_participants"] == 1

    for row in (low, zero, unmapped_low):
        assert row["ranking_group"] is None, row["assigned_id"]
        assert row["ranking_position"] is None, row["assigned_id"]
        assert row["ranking_total_participants"] is None, row["assigned_id"]

    # Chart configuration survives on non-qualified rows; it is simply ignored.
    assert low["ranking_included"] is True
    assert _count_rank_groups([qualified, low, zero, unmapped_low]) == (1, 0, 0, 3)
    print("PASS: only QUALIFIED rows join ranking groups, positions, and totals")


def test_job_static_sql_contracts() -> None:
    source = (REPO_ROOT / "jobs" / "ecodriving" / "job_eco_driving_aggregate.py").read_text()
    assert "ON CONFLICT (client_id, provider_trip_id) DO UPDATE" in source
    assert "ON CONFLICT (client_id, assigned_id, period_start_date, period_end_date) DO UPDATE" in source
    assert "ON CONFLICT (client_id, assigned_id, month_start_date) DO UPDATE" in source
    assert "aggregation_included IS TRUE AND a.is_private_trip IS FALSE" in source
    assert "a.assigned_id IS NOT NULL" in source
    assert "ILIKE '%%pryw%%'" in source
    assert "assigned_filter" in source
    assert "conn.rollback()" in source and "dry_run" in source
    assert "month_start_date = %(month_start)s" in source
    assert "weekly_periods if explicit_period_mode else None" in source
    assert "ROW_NUMBER" not in source  # rankings use equivalent deterministic Python row-number semantics

    migration = (REPO_ROOT / "db" / "client_business" / "029_eco_driving_nullable_scores.sql").read_text()
    assert "eco_driving_score_total DROP NOT NULL" in migration

    round_rates_migration = (
        REPO_ROOT / "db" / "client_business" / "036_eco_driving_round_per_100km_stats.sql"
    ).read_text()
    assert "UPDATE public.eco_driver_weekly_stats" in round_rates_migration
    assert "UPDATE public.eco_driver_monthly_stats" in round_rates_migration
    assert "overrev_events_per_100km = ROUND(overrev_events_per_100km)" in round_rates_migration
    assert "overrev_events_per_100km IS NOT NULL" in round_rates_migration
    assert "ALTER TABLE" not in round_rates_migration

    rounded_scoring_migration = (
        REPO_ROOT / "db" / "client_business" / "037_eco_driving_score_from_rounded_per_100km.sql"
    ).read_text(encoding="utf-8")
    assert "UPDATE public.eco_driver_weekly_stats" in rounded_scoring_migration
    assert "UPDATE public.eco_driver_monthly_stats" in rounded_scoring_migration
    assert "ROUND(overrev_events_per_100km) AS overrev_rate" in rounded_scoring_migration
    assert "overrev_maxpoints_subtract = validated.overrev_subtract_new" in rounded_scoring_migration
    assert "eco_driving_score_total = validated.score_total_new" in rounded_scoring_migration
    assert "top_1_validation = validated.top_validations[1]" in rounded_scoring_migration
    assert "ecodriving_rating_type = CASE" in rounded_scoring_migration
    assert "ecodriving_rating_type_share_percent = shares.share_percent" in rounded_scoring_migration
    assert "WHERE COALESCE(total_distance_meters, 0) > 0" in rounded_scoring_migration
    assert "AND overrev_events_per_100km IS NOT NULL" in rounded_scoring_migration
    # PostgreSQL ROUND(numeric) and Python ROUND_HALF_UP agree for non-negative
    # rates; aggregation rejects negative rate inputs before scoring.
    assert _raw_rate_per_100km(1, Decimal("200.000")) == Decimal("0.5000")
    assert _rate_per_100km(1, Decimal("200.000")) == Decimal("1")

    validation_migration = (
        REPO_ROOT / "db" / "client_business" / "031_eco_driving_validation_fields.sql"
    ).read_text()
    assert "overrev_maxpoints_subtract" in validation_migration
    assert "top_1_validation" in validation_migration
    assert "ecodriving_rating_type" in validation_migration
    assert "EcoDriving_rating_type" not in validation_migration

    share_migration = (
        REPO_ROOT / "db" / "client_business" / "034_eco_driving_rating_type_share_percent.sql"
    ).read_text()
    assert "ecodriving_rating_type_share_percent NUMERIC(7, 2)" in share_migration
    assert "eco_driver_weekly_trends_view" in share_migration
    assert "eco_driver_monthly_trends_view" in share_migration
    assert "ecodriving_rating_type_share_percent = EXCLUDED.ecodriving_rating_type_share_percent" in source
    print("PASS: SQL contracts cover idempotent upserts, filters, dry-run, assigned_id filter, and nullable scores")


def main() -> None:
    test_assignment_priority_and_private_detection()
    test_month_bounded_weekly_periods()
    test_ranking_period_timestamp_contract()
    test_explicit_period_real_entry_point_reaches_chart_precondition()
    test_cumulative_weekly_aggregation_semantics()
    test_stats_scoring_qualification_and_nulls()
    test_weekly_and_monthly_low_distance_boundaries()
    test_per_100km_rates_are_stored_as_whole_numbers()
    test_fractional_raw_rates_score_after_display_rounding()
    test_rating_type_share_percent_weekly_and_monthly()
    test_private_and_missing_id_exclusion_contract()
    test_monthly_stats_and_ranking_groups()
    test_driver_chart_preconditions_and_partial_match_diagnostics()
    test_ranking_tie_breakers_and_idempotency_contract()
    test_only_qualified_rows_are_ranked()
    test_job_static_sql_contracts()
    print("OK - Eco Driving aggregation job checks passed")


if __name__ == "__main__":
    main()
