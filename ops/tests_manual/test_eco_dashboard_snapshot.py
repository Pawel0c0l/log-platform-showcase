#!/usr/bin/env python3
"""Deterministic checks for the Driver Eco Dashboard V1 snapshot contract.

Run:
    python3 ops/tests_manual/test_eco_dashboard_snapshot.py

No database, no network, no production data. Every input is synthetic.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.ecodriving import job_eco_driving_aggregate as driver_job  # noqa: E402
from jobs.ecodriving_person import job_eco_driving_person_aggregate as person_job  # noqa: E402
from jobs.ecodriving.eco_scoring import REQUIRED_METRICS  # noqa: E402
from jobs.ecodriving_dashboard import sources  # noqa: E402
from jobs.ecodriving_dashboard.snapshot_builder import (  # noqa: E402
    DailyInput,
    PeriodIdentity,
    PeriodInput,
    RankingFacts,
    SeriesInput,
    build_driver_snapshot,
    build_period_entry,
)
from jobs.ecodriving_dashboard.snapshot_contract import (  # noqa: E402
    BANDS_BY_CATEGORY,
    CATEGORY_BY_KEY,
    CATEGORY_KEYS,
    MIN_QUALIFYING_DISTANCE_KM,
    PERIOD_TYPE_MONTHLY,
    PERIOD_TYPE_WEEKLY,
    RANKING_STATE_LEFT_RANKING,
    RANKING_STATE_NOT_ON_ROSTER,
    RANKING_STATE_NOT_RANKED_BY_CONFIGURATION,
    RANKING_STATE_RANKED,
    RANKING_TRANSITION_LEFT_RANKING,
    RANKING_TRANSITION_NEWLY_RANKED,
    RANKING_TRANSITION_NO_BASIS,
    RANKING_TRANSITION_NOT_RANKED,
    RANKING_TRANSITION_RANKED_TO_RANKED,
    SCHEMA_VERSION,
    SNAPSHOT_CONTRACT_ID,
    SNAPSHOT_STATUS_INSUFFICIENT_DISTANCE,
    SNAPSHOT_STATUS_OK,
    SNAPSHOT_STATUS_REPORT_NOT_READY,
    MAX_BROWSER_PAYLOAD_BYTES,
    SnapshotContractError,
    assert_snapshot_document,
    serialize_document,
    coefficient_per_100km,
    qualification_status_for_meters,
    total_kilometers_from_meters,
)

import eco_dashboard_fixtures as fx  # noqa: E402


GENERATED_AT = datetime(2026, 8, 17, 3, 15, 0, tzinfo=timezone.utc)


def _identity(
    *,
    period_type: str = PERIOD_TYPE_WEEKLY,
    label: str = "2026-07-W3",
    start: date = date(2026, 7, 1),
    end_exclusive: date = date(2026, 7, 20),
    sequence: int | None = 3,
) -> PeriodIdentity:
    return PeriodIdentity(
        period_type=period_type,
        period_label=label,
        period_start_date=start,
        period_end_date_exclusive=end_exclusive,
        month_start_date=start.replace(day=1),
        period_sequence_in_month=sequence,
        closed_periods_in_month=5,
        is_partial_period=False,
    )


def _period(
    *,
    identity: PeriodIdentity,
    meters: int,
    counts: dict[str, int | None] | None = None,
    ranking: RankingFacts | None = None,
    trips: int = 10,
) -> PeriodInput:
    metric_counts: dict[str, int | None] = {metric: 0 for metric in REQUIRED_METRICS}
    for key, value in (counts or {}).items():
        metric_counts[CATEGORY_BY_KEY[key].metric] = value
    return PeriodInput(
        identity=identity,
        total_distance_meters=meters,
        trips_count=trips,
        counts=metric_counts,
        snapshot_updated_at_utc=GENERATED_AT,
        ranking=ranking or RankingFacts(),
    )


def _category(block: dict, key: str) -> dict:
    for category in block["categories"]:
        if category["key"] == key:
            return category
    raise AssertionError(f"category {key} missing")


# --- 1/2. period-level 100 km gate --------------------------------------------


def test_period_eligibility_boundary() -> None:
    # 99.99 km — below the reporting-period gate.
    below = build_period_entry(current=_period(identity=_identity(), meters=99_990))
    assert below.status == SNAPSHOT_STATUS_INSUFFICIENT_DISTANCE, below.status
    entry = below.entry
    assert entry["current"] is None
    assert entry["previous"] is None
    assert entry["series"] == []
    assert entry["series_reference"] is None
    # No Eco result may be inferable from the payload at all.
    serialized = json.dumps(entry, ensure_ascii=False)
    for forbidden in (
        "eco_score_total",
        "rating_type",
        "ranking_position",
        "rating_group_share_percent",
        "categories",
        "coefficient",
        "coaching",
        "total_kilometers",
        "days",
    ):
        assert forbidden not in serialized, forbidden
    assert entry["period_identity"]["period_label"] == "2026-07-W3"
    assert entry["period_identity"]["snapshot_updated_at_utc"].endswith("Z")

    # Exactly 100.00 km — the existing gate is inclusive.
    at_gate = build_period_entry(current=_period(identity=_identity(), meters=100_000))
    assert at_gate.status == SNAPSHOT_STATUS_OK, at_gate.status
    assert at_gate.entry["current"]["qualification_status"] == "QUALIFIED"
    assert at_gate.entry["current"]["total_kilometers"] == MIN_QUALIFYING_DISTANCE_KM

    # Boundary semantics match the existing aggregation job exactly.
    for meters in (0, 1, 99_999, 100_000, 100_001):
        expected = driver_job._qualification_and_calculation(meters)[0]
        assert qualification_status_for_meters(meters) == expected, meters
        assert person_job._qualification_and_calculation(meters)[0] == expected, meters
    print("PASS test_period_eligibility_boundary")


# --- 3. days below 100 km stay visible ----------------------------------------


def test_no_daily_distance_threshold() -> None:
    day_km = (0, 5, 20, 49, 75, 99, 120, 140)
    days = fx.daily_inputs(date(2026, 7, 1), day_km, {"idle": 8})
    current = fx.period_from_days(
        _identity(end_exclusive=date(2026, 7, 9)), days, ranking=fx.RANKED_FACTS
    )
    result = build_period_entry(current=current, days=days)
    assert result.status == SNAPSHOT_STATUS_OK
    block = result.entry["current"]

    assert len(block["days"]) == len(day_km)
    by_date = {day["date"]: day for day in block["days"]}
    # Every sub-100 km day is an ordinary Detailed row with its own distance.
    assert by_date["2026-07-02"]["kilometers"] == 5
    assert by_date["2026-07-03"]["kilometers"] == 20
    assert by_date["2026-07-04"]["kilometers"] == 49
    assert by_date["2026-07-06"]["kilometers"] == 99

    # And they carry a real per-category coefficient derived from that day.
    twenty_km_day = by_date["2026-07-03"]
    idle_cell = next(cell for cell in twenty_km_day["categories"] if cell["key"] == "idle")
    assert idle_cell["coefficient_per_100km"] is not None
    assert idle_cell["status"] != "neutral"

    # The no-driving day is neutral, never a fabricated zero evaluation.
    no_driving = by_date["2026-07-01"]
    assert no_driving["kilometers"] == 0
    assert all(cell["coefficient_per_100km"] is None for cell in no_driving["categories"])
    assert all(cell["status"] == "neutral" for cell in no_driving["categories"])

    # No daily threshold constant exists anywhere in the payload.
    serialized = json.dumps(result.entry, ensure_ascii=False)
    assert "min_daily_evaluation_km" not in serialized
    assert "day_status" not in serialized

    # A8/A9 reconciliation.
    assert sum(day["kilometers"] for day in block["days"]) == block["total_kilometers"]
    for key in CATEGORY_KEYS:
        daily_total = sum(
            cell["count"]
            for day in block["days"]
            for cell in day["categories"]
            if cell["key"] == key
        )
        assert daily_total == _category(block, key)["count"], key
    print("PASS test_no_daily_distance_threshold")


# --- 4. identical raw counts, different semantic status -----------------------


def test_same_count_different_status() -> None:
    low_exposure = build_period_entry(
        current=_period(identity=_identity(), meters=1_100_000, counts={"harsh_braking": 22})
    ).entry["current"]
    high_exposure = build_period_entry(
        current=_period(identity=_identity(), meters=11_000_000, counts={"harsh_braking": 22})
    ).entry["current"]

    low = _category(low_exposure, "harsh_braking")
    high = _category(high_exposure, "harsh_braking")
    assert low["count"] == high["count"] == 22
    assert low["coefficient_per_100km"] == 2 and high["coefficient_per_100km"] == 0
    assert low["status"] == "yellow" and high["status"] == "green"
    assert low["points"] == 4 and high["points"] == 10

    # The same rule holds at day level: exposure, not the count, drives status.
    day_km = (5, 99, 500, 600)
    days = fx.daily_inputs(date(2026, 7, 1), day_km, {})
    days = [
        DailyInput(
            day=day.day,
            total_distance_meters=day.total_distance_meters,
            trips_count=day.trips_count,
            counts={**day.counts, "idle_events": 1},
        )
        for day in days
    ]
    current = fx.period_from_days(_identity(end_exclusive=date(2026, 7, 5)), days)
    block = build_period_entry(current=current, days=days).entry["current"]
    cells = {
        day["date"]: next(cell for cell in day["categories"] if cell["key"] == "idle")
        for day in block["days"]
    }
    assert cells["2026-07-01"]["count"] == cells["2026-07-02"]["count"] == 1
    assert cells["2026-07-01"]["coefficient_per_100km"] == 20
    assert cells["2026-07-02"]["coefficient_per_100km"] == 1
    assert cells["2026-07-01"]["status"] == "red"
    assert cells["2026-07-02"]["status"] == "yellow"
    print("PASS test_same_count_different_status")


# --- 5. EXCLUDED drivers -------------------------------------------------------


def test_excluded_driver_keeps_eco_content_without_ranking() -> None:
    snapshot = fx.build_fixtures()["not_ranked_by_configuration"]
    block = snapshot.document["periods"][PERIOD_TYPE_WEEKLY]["current"]

    assert block["ranking_state"] == RANKING_STATE_NOT_RANKED_BY_CONFIGURATION
    assert block["eco_score_total"] == 75
    assert block["rating_type"] == "acceptable"
    assert len(block["categories"]) == 8
    assert block["coaching"], "EXCLUDED drivers keep deterministic coaching"
    assert block["days"], "EXCLUDED drivers keep daily detail"
    assert block["comparison"] is not None

    assert "ranking_position" not in block
    assert "ranking_total_participants" not in block
    assert "rating_group_share_percent" not in block
    assert block["rating_group_distribution"] is None
    assert block["comparison"]["previous_ranking_position"] is None
    assert block["comparison"]["ranking_position_delta_places"] is None

    serialized = json.dumps(snapshot.document, ensure_ascii=False)
    for forbidden in ("EXCLUDED", "INCLUDED", "UNKNOWN_DRIVER", "ranking_included", "ranking_group"):
        assert forbidden not in serialized, forbidden
    # The persisted shadow-league position never reaches the browser.
    assert "407" not in serialized
    assert snapshot.internal["ranking_group"] == "EXCLUDED"
    print("PASS test_excluded_driver_keeps_eco_content_without_ranking")


# --- 6. per-client private-trip policy ----------------------------------------


def test_private_trip_policy_is_preserved_per_client() -> None:
    alpha = sources.resolve_pipeline_family("ALPHA00001")
    bravo = sources.resolve_pipeline_family("BRAVO00016")
    assert alpha is sources.DRIVER_FAMILY and bravo is sources.PERSON_FAMILY
    assert alpha.include_private_trips is False
    assert bravo.include_private_trips is True
    assert alpha.identity_column == "assigned_id"
    assert bravo.identity_column == "person_name_group_key"

    # A private-designated, otherwise-eligible trip.
    assert sources.is_trip_included(alpha, aggregation_included=True, is_private_trip=True) is False
    assert sources.is_trip_included(bravo, aggregation_included=True, is_private_trip=True) is True
    # A non-private trip behaves identically for both.
    assert sources.is_trip_included(alpha, aggregation_included=True, is_private_trip=False) is True
    assert sources.is_trip_included(bravo, aggregation_included=True, is_private_trip=False) is True
    # An excluded assignment is never counted.
    assert sources.is_trip_included(alpha, aggregation_included=False, is_private_trip=False) is False
    assert sources.is_trip_included(bravo, aggregation_included=False, is_private_trip=True) is False

    # The SQL predicate matches the predicate the existing aggregation jobs use.
    assert (
        sources.trip_inclusion_predicate_sql(alpha)
        == "a.aggregation_included IS TRUE AND a.is_private_trip IS FALSE"
    )
    assert sources.trip_inclusion_predicate_sql(bravo) == "a.aggregation_included IS TRUE"
    assert "is_private_trip" in sources.period_aggregate_sql(alpha, "public")
    assert "is_private_trip" not in sources.period_aggregate_sql(bravo, "public")
    assert "eco_trip_assignments" in sources.daily_aggregate_sql(alpha, "public")
    assert "eco_person_trip_assignments" in sources.daily_aggregate_sql(bravo, "public")

    # Unknown clients fail closed rather than defaulting to either policy.
    try:
        sources.resolve_pipeline_family("NOPE00001")
    except sources.PipelineFamilyError:
        pass
    else:
        raise AssertionError("expected PipelineFamilyError")

    # No roster/identity column is ever projected by a snapshot query.
    for family in (alpha, bravo):
        for sql in (
            sources.period_aggregate_sql(family, "public"),
            sources.daily_aggregate_sql(family, "public"),
            sources.stats_row_sql(family, "public", PERIOD_TYPE_WEEKLY),
            sources.rating_group_distribution_sql(family, "public", PERIOD_TYPE_MONTHLY),
            sources.weekly_series_sql(family, "public"),
        ):
            lowered = sql.lower()
            for forbidden in ("driver_name", "person_name ", "email", "phone", "registration"):
                assert forbidden not in lowered, (family.name, forbidden)
    print("PASS test_private_trip_policy_is_preserved_per_client")


# --- 7. weekly comparison is cumulative MTD -----------------------------------


def test_weekly_comparison_is_cumulative_month_to_date() -> None:
    current_identity = _identity(
        label="2026-08-W2", start=date(2026, 8, 1), end_exclusive=date(2026, 8, 15), sequence=2
    )
    previous_identity = _identity(
        label="2026-08-W1", start=date(2026, 8, 1), end_exclusive=date(2026, 8, 8), sequence=1
    )
    current = _period(
        identity=current_identity,
        meters=1_400_000,
        counts={"idle": 28},
        ranking=RankingFacts("INCLUDED", 12, 150, Decimal("44.00")),
    )
    previous = _period(
        identity=previous_identity,
        meters=500_000,
        counts={"idle": 21},
        ranking=RankingFacts("INCLUDED", 19, 148, Decimal("41.00")),
    )
    block = build_period_entry(current=current, previous=previous).entry["current"]
    comparison = block["comparison"]

    assert comparison["kind"] == "PREVIOUS_CUMULATIVE_PERIOD"
    # The basis is 01–07 August cumulative, never the isolated 08–14 segment.
    assert comparison["basis_start_date"] == "2026-08-01"
    assert comparison["basis_end_date_display"] == "2026-08-07"
    assert comparison["previous_total_kilometers"] == 500
    assert block["period_start_date"] == "2026-08-01"
    assert block["period_end_date_display"] == "2026-08-14"
    assert block["total_kilometers"] == 1400
    # A cumulative comparison, not a comparison of isolated weekly segments:
    # the isolated 08–14 segment would be 900 km, the cumulative basis is 500 km.
    assert comparison["previous_total_kilometers"] == 500
    assert block["total_kilometers"] - comparison["previous_total_kilometers"] == 900
    assert comparison["ranking_position_delta_places"] == 7

    # The authoritative period generator agrees: weekly rows are cumulative and
    # always start on the first day of the month.
    periods = driver_job._month_bounded_weekly_periods(date(2026, 8, 1))
    assert all(period.period_start_date == date(2026, 8, 1) for period in periods)
    assert [period.period_end_date for period in periods] == sorted(
        period.period_end_date for period in periods
    )
    person_periods = person_job._month_bounded_weekly_periods(date(2026, 8, 1))
    assert [
        (p.period_start_date, p.period_end_date, p.period_label, p.period_sequence_in_month)
        for p in person_periods
    ] == [
        (p.period_start_date, p.period_end_date, p.period_label, p.period_sequence_in_month)
        for p in periods
    ]
    # The series never crosses the month boundary.
    assert periods[-1].period_end_date == date(2026, 9, 1)
    print("PASS test_weekly_comparison_is_cumulative_month_to_date")


def test_monthly_comparison_is_previous_closed_month() -> None:
    snapshot = fx.build_fixtures()["monthly_31_days"]
    block = snapshot.document["periods"][PERIOD_TYPE_MONTHLY]["current"]
    comparison = block["comparison"]
    assert comparison["kind"] == "PREVIOUS_CLOSED_MONTH"
    assert comparison["basis_start_date"] == "2026-06-01"
    assert comparison["basis_end_date_display"] == "2026-06-30"
    assert block["period_start_date"] == "2026-07-01"
    assert block["period_end_date_display"] == "2026-07-31"
    assert len(block["days"]) == 31
    assert snapshot.document["periods"][PERIOD_TYPE_WEEKLY] is None
    print("PASS test_monthly_comparison_is_previous_closed_month")


# --- 8/9. deterministic coaching ----------------------------------------------


def test_coaching_uses_coefficient_movement_not_bucket_movement() -> None:
    snapshot = fx.build_fixtures()["ranked_acceptable"]
    block = snapshot.document["periods"][PERIOD_TYPE_WEEKLY]["current"]
    insights = {insight["code"]: insight for insight in block["coaching"]}
    assert set(insights) == {
        "LARGEST_LOSS",
        "MOST_IMPROVED",
        "MOST_DETERIORATED",
        "BEST_OPPORTUNITY",
    }

    improved = insights["MOST_IMPROVED"]
    assert improved["selected_by"] == "coefficient_delta"
    assert improved["category_key"] == "harsh_braking"
    braking = _category(block, "harsh_braking")
    assert braking["previous_coefficient_per_100km"] == 3
    assert braking["coefficient_per_100km"] == 2
    # The scoring bucket did not move — the coefficient did. That is enough.
    assert braking["previous_points_lost"] == braking["points_lost"] == -6
    assert improved["value"]["coefficient_delta"] == -1
    assert improved["value"]["points_delta"] == 0

    deteriorated = insights["MOST_DETERIORATED"]
    assert deteriorated["selected_by"] == "coefficient_delta"
    assert deteriorated["category_key"] == "harsh_turning"
    assert deteriorated["value"]["coefficient_delta"] > 0

    largest = insights["LARGEST_LOSS"]
    assert largest["selected_by"] == "points_lost"
    assert largest["value"]["points"] == min(
        category["points_lost"] for category in block["categories"] if not category["deemphasize"]
    )

    # Over-rev never appears in coaching, even though it is a scored category.
    assert all(insight["category_key"] != "overrev" for insight in block["coaching"])
    # No "allowed event budget" language or field anywhere.
    serialized = json.dumps(snapshot.document, ensure_ascii=False)
    for forbidden in ("budget", "max_events", "allowed_events", "events_left"):
        assert forbidden not in serialized, forbidden
    print("PASS test_coaching_uses_coefficient_movement_not_bucket_movement")


def test_best_opportunity_resolves_from_the_existing_threshold() -> None:
    snapshot = fx.build_fixtures()["ranked_acceptable"]
    block = snapshot.document["periods"][PERIOD_TYPE_WEEKLY]["current"]
    near = block["near_threshold"]
    assert len(near) == 2
    assert [item["rank"] for item in near] == [1, 2]
    assert near[0]["category_key"] == "harsh_acceleration"

    best = next(insight for insight in block["coaching"] if insight["code"] == "BEST_OPPORTUNITY")
    assert best["selected_by"] == "threshold_gain"
    assert best["category_key"] == near[0]["category_key"]

    category = _category(block, "harsh_acceleration")
    bands = BANDS_BY_CATEGORY["harsh_acceleration"]
    marker = category["marker_band_index"]
    target = category["target_band_index"]
    assert target == marker - 1
    assert category["coefficient_per_100km"] == 1
    assert near[0]["target_upper_bound"] == bands[target].upper_bound == 0
    assert near[0]["coefficient_distance"] == 1
    # The gain is arithmetic on the existing scoring table, never an estimate.
    assert near[0]["points_gain"] == bands[target].points_lost - bands[marker].points_lost == 5
    assert best["value"]["points_gain"] == 5
    assert "coefficient_distance" in best["value"]
    print("PASS test_best_opportunity_resolves_from_the_existing_threshold")


# --- 10. privacy ---------------------------------------------------------------


def test_serialized_snapshot_contains_no_forbidden_data() -> None:
    forbidden_fragments = (
        "driver_key",
        "client_code",
        "client_id",
        "driver_name",
        "person_name",
        "assigned_id",
        "person_name_group_key",
        "source_person_id",
        "email",
        "phone",
        "employee",
        "registration",
        "latitude",
        "longitude",
        "geofence",
        "odometer",
        "provider_trip_id",
        "record_id",
        "trip_start_ts",
        "trip_end_ts",
        "driver_tag_description",
        "trip_mode",
        "ranking_included",
        "ranking_group",
        "day_status",
        "chassis",
        "smtp",
        "password",
        "secret",
    )
    for name, snapshot in fx.build_fixtures().items():
        serialized = json.dumps(snapshot.document, ensure_ascii=False).lower()
        for fragment in forbidden_fragments:
            assert fragment not in serialized, f"{name}: {fragment}"
        assert fx.SYNTHETIC_IDENTITY_KEY.lower() not in serialized, name
        assert fx.SYNTHETIC_CLIENT_CODE.lower() not in serialized, name
        # The internal side keeps what the future publisher needs; the document does not.
        assert snapshot.internal["identity_key"] == fx.SYNTHETIC_IDENTITY_KEY

    # The detector actually detects: a poisoned document must be rejected.
    poisoned = fx.build_fixtures()["ranked_acceptable"].document
    poisoned["periods"][PERIOD_TYPE_WEEKLY]["current"]["driver_name"] = "Jan Kowalski"
    try:
        assert_snapshot_document(poisoned, banned_values=(fx.SYNTHETIC_IDENTITY_KEY,))
    except SnapshotContractError as exc:
        assert exc.assertion == "A12", exc.assertion
    else:
        raise AssertionError("forbidden field was not detected")
    print("PASS test_serialized_snapshot_contains_no_forbidden_data")


# --- fail-closed completeness --------------------------------------------------


def test_incomplete_scoring_fails_closed() -> None:
    identity = _identity()
    counts: dict[str, int | None] = {"idle": 20, "speeding_170_plus": None}
    result = build_period_entry(current=_period(identity=identity, meters=1_100_000, counts=counts))
    assert result.status == SNAPSHOT_STATUS_REPORT_NOT_READY
    assert "SCORING_INCOMPLETE" in result.reasons
    assert result.entry["current"] is None
    assert result.entry["series"] == []

    # A persisted score that disagrees with the recomputation also fails closed.
    mismatch = PeriodInput(
        identity=identity,
        total_distance_meters=1_100_000,
        trips_count=10,
        counts={metric: 0 for metric in REQUIRED_METRICS},
        snapshot_updated_at_utc=GENERATED_AT,
        persisted_eco_score_total=Decimal("42"),
    )
    mismatch_result = build_period_entry(current=mismatch)
    assert mismatch_result.status == SNAPSHOT_STATUS_REPORT_NOT_READY
    assert "PERSISTED_SCORE_MISMATCH" in mismatch_result.reasons

    # A snapshot whose only "gap" is over-rev at full points is complete data.
    healthy = build_period_entry(
        current=_period(identity=identity, meters=1_100_000, counts={"idle": 33})
    )
    assert healthy.status == SNAPSHOT_STATUS_OK
    overrev = _category(healthy.entry["current"], "overrev")
    assert overrev["count"] == 0
    assert overrev["points"] == overrev["points_max"] == 15
    assert overrev["status"] == "green"
    assert overrev["deemphasize"] is True
    assert healthy.entry["current"]["scoring_complete"] is True
    print("PASS test_incomplete_scoring_fails_closed")


# --- scoring model preservation -----------------------------------------------


def test_scoring_model_is_the_existing_one_hundred_point_model() -> None:
    snapshot = fx.build_fixtures()["ranked_acceptable"]
    block = snapshot.document["periods"][PERIOD_TYPE_WEEKLY]["current"]
    assert snapshot.document["constants"]["score_max"] == 100
    assert snapshot.document["constants"]["rating_thresholds"] == {"safe": 85, "acceptable": 40}
    assert snapshot.document["constants"]["min_qualifying_distance_km"] == 100
    assert sum(category["points_max"] for category in block["categories"]) == 100

    # A1/A2/A3/A4 hold for every category.
    assert block["eco_score_total"] == 100 + sum(
        category["points_lost"] for category in block["categories"]
    )
    for category in block["categories"]:
        assert category["points"] == category["points_max"] + category["points_lost"]
        bands = BANDS_BY_CATEGORY[category["key"]]
        assert bands[category["marker_band_index"]].points_lost == category["points_lost"]
        expected_status = (
            "green"
            if category["points"] == category["points_max"]
            else "yellow"
            if category["points"] >= 0
            else "red"
        )
        assert category["status"] == expected_status, category["key"]

    # Over-rev keeps its 15 points inside the 100-point model.
    assert CATEGORY_BY_KEY["overrev"].points_max == 15
    assert _category(block, "overrev")["points_max"] == 15
    print("PASS test_scoring_model_is_the_existing_one_hundred_point_model")


def test_coefficient_helpers_match_the_aggregation_jobs() -> None:
    cases = [(0, 0), (1, 100_000), (22, 1_100_000), (7, 657_000), (1, 5_000), (3, 99_000)]
    for count, meters in cases:
        expected_km = driver_job._total_kilometers(meters)
        assert total_kilometers_from_meters(meters) == expected_km, meters
        assert person_job._total_kilometers(meters) == expected_km, meters
        expected_rate = driver_job._rate_per_100km(count, expected_km)
        actual = coefficient_per_100km(count, total_kilometers_from_meters(meters))
        assert (actual is None) == (expected_rate is None), (count, meters)
        if expected_rate is not None:
            assert Decimal(actual) == expected_rate, (count, meters)
            assert person_job._rate_per_100km(count, expected_km) == expected_rate, (count, meters)
    print("PASS test_coefficient_helpers_match_the_aggregation_jobs")


# --- ranking transitions -------------------------------------------------------


def test_ranking_transitions_never_fabricate_a_delta() -> None:
    fixtures = fx.build_fixtures()

    ranked = fixtures["ranked_acceptable"].document["periods"][PERIOD_TYPE_WEEKLY]["current"]
    assert ranked["ranking_state"] == RANKING_STATE_RANKED
    assert ranked["ranking_transition"] == RANKING_TRANSITION_RANKED_TO_RANKED
    assert ranked["ranking_position"] == 18
    assert ranked["ranking_total_participants"] == 158
    assert ranked["comparison"]["previous_ranking_position"] == 24
    assert ranked["comparison"]["ranking_position_delta_places"] == 6

    newly = fixtures["newly_ranked"].document["periods"][PERIOD_TYPE_WEEKLY]["current"]
    assert newly["ranking_state"] == RANKING_STATE_RANKED
    assert newly["ranking_transition"] == RANKING_TRANSITION_NEWLY_RANKED
    assert newly["ranking_position"] == 18
    assert newly["comparison"]["previous_ranking_position"] is None
    assert newly["comparison"]["ranking_position_delta_places"] is None

    left = fixtures["left_ranking"].document["periods"][PERIOD_TYPE_WEEKLY]["current"]
    assert left["ranking_state"] == RANKING_STATE_LEFT_RANKING
    assert left["ranking_transition"] == RANKING_TRANSITION_LEFT_RANKING
    assert "ranking_position" not in left
    assert left["comparison"]["previous_ranking_position"] == 24
    assert left["comparison"]["ranking_position_delta_places"] is None
    assert left["comparison"]["basis_end_date_display"] == "2026-07-12"

    unranked = fixtures["not_on_roster"].document["periods"][PERIOD_TYPE_WEEKLY]["current"]
    assert unranked["ranking_state"] == RANKING_STATE_NOT_ON_ROSTER
    assert unranked["ranking_transition"] == RANKING_TRANSITION_NOT_RANKED
    assert "ranking_position" not in unranked
    assert unranked["comparison"]["previous_ranking_position"] is None

    first = fixtures["first_closed_period_of_month"].document["periods"][PERIOD_TYPE_WEEKLY]["current"]
    assert first["ranking_transition"] == RANKING_TRANSITION_NO_BASIS
    assert first["comparison"] is None
    assert all(
        category["previous_coefficient_per_100km"] is None for category in first["categories"]
    )
    print("PASS test_ranking_transitions_never_fabricate_a_delta")


# --- contract identity and fixtures -------------------------------------------


def test_all_fixtures_satisfy_the_contract() -> None:
    fixtures = fx.build_fixtures()
    expected = {
        "ranked_safe",
        "ranked_acceptable",
        "ranked_dangerous",
        "not_ranked_by_configuration",
        "not_on_roster",
        "newly_ranked",
        "left_ranking",
        "insufficient_period_distance",
        "first_closed_period_of_month",
        "end_of_month_period",
        "no_comparison",
        "report_not_ready",
        "zero_event_category",
        "no_driving_day",
        "skipped_period_in_series",
        "monthly_31_days",
    }
    assert expected <= set(fixtures), sorted(expected - set(fixtures))

    for name, snapshot in fixtures.items():
        document = snapshot.document
        assert document["schema_version"] == SCHEMA_VERSION, name
        assert document["contract_id"] == SNAPSHOT_CONTRACT_ID, name
        assert set(document["periods"]) == {PERIOD_TYPE_WEEKLY, PERIOD_TYPE_MONTHLY}, name
        assert_snapshot_document(document, banned_values=(fx.SYNTHETIC_IDENTITY_KEY,))
        json.dumps(document, ensure_ascii=False)

    zero_event = fixtures["zero_event_category"].document["periods"][PERIOD_TYPE_WEEKLY]["current"]
    assert _category(zero_event, "speeding_170_plus")["count"] == 0
    assert _category(zero_event, "speeding_170_plus")["status"] == "green"
    # The teaching case: a nonzero count that is still green because of exposure.
    turning = _category(zero_event, "harsh_turning")
    assert turning["count"] > 0 and turning["status"] == "green"

    skipped = fixtures["skipped_period_in_series"].document["periods"][PERIOD_TYPE_WEEKLY]
    assert [point["period_label"] for point in skipped["series"]] == ["2026-07-W2", "2026-07-W3"]
    assert skipped["series"][-1]["is_current"] is True

    no_comparison = fixtures["no_comparison"].document["periods"][PERIOD_TYPE_WEEKLY]["current"]
    assert no_comparison["comparison"] is None

    end_of_month = fixtures["end_of_month_period"].document["periods"][PERIOD_TYPE_WEEKLY]["current"]
    assert end_of_month["period_end_date_exclusive"] == "2026-08-01"
    assert end_of_month["period_end_date_display"] == "2026-07-31"
    assert end_of_month["period_sequence_in_month"] == 5
    assert end_of_month["closed_periods_in_month"] == 5
    print("PASS test_all_fixtures_satisfy_the_contract")


def test_series_never_exposes_an_unqualified_period() -> None:
    series = [
        SeriesInput("2026-07-W1", date(2026, 7, 1), date(2026, 7, 6), 91, 40_000),
        SeriesInput("2026-07-W2", date(2026, 7, 1), date(2026, 7, 13), 71, 657_000),
        SeriesInput("2026-07-W3", date(2026, 7, 1), date(2026, 7, 20), 75, 1_100_000),
    ]
    entry = build_period_entry(
        current=_period(identity=_identity(), meters=1_100_000, counts={"idle": 33}),
        series=series,
    ).entry
    labels = [point["period_label"] for point in entry["series"]]
    assert labels == ["2026-07-W2", "2026-07-W3"], labels
    assert entry["series"][-1]["is_current"] is True
    print("PASS test_series_never_exposes_an_unqualified_period")


def _monthly_pair(*, previous_meters: int = 1_100_000,
                  previous_counts: dict | None = None):
    """A closed August with the July before it, as the delivery path loads them."""
    august = _identity(period_type=PERIOD_TYPE_MONTHLY, label="2026-08",
                       start=date(2026, 8, 1), end_exclusive=date(2026, 9, 1),
                       sequence=None)
    july = _identity(period_type=PERIOD_TYPE_MONTHLY, label="2026-07",
                     start=date(2026, 7, 1), end_exclusive=date(2026, 8, 1),
                     sequence=None)
    current = _period(identity=august, meters=1_200_000, counts={"idle": 20})
    previous = _period(identity=july, meters=previous_meters,
                       counts=previous_counts if previous_counts is not None else {"idle": 40})
    return current, previous


def test_a_monthly_period_references_the_month_before_it() -> None:
    """The trend card's horizontal line: "poprzedni miesiąc: N pkt"."""
    current, previous = _monthly_pair()
    entry = build_period_entry(current=current, previous=previous).entry
    reference = entry["series_reference"]
    assert reference is not None, entry
    assert reference["period_label"] == "2026-07", reference
    assert reference["start_date"] == "2026-07-01", reference
    assert reference["end_date_display"] == "2026-07-31", reference
    assert reference["is_current"] is False, reference

    # THE POINT OF THE TEST: it is the comparison's own previous score, not a
    # second reading of the same month. A document that showed one number under
    # the bars and a different one in the comparison would be worse than showing
    # nothing.
    assert reference["eco_score_total"] == entry["current"]["comparison"]["previous_eco_score_total"], (
        reference, entry["current"]["comparison"])
    print("PASS test_a_monthly_period_references_the_month_before_it")


def test_an_unqualified_or_missing_previous_month_references_nothing() -> None:
    """Same gate as the comparison, because it IS the comparison's gate."""
    current, below_gate = _monthly_pair(previous_meters=99_990)   # under 100 km
    entry = build_period_entry(current=current, previous=below_gate).entry
    assert entry["series_reference"] is None, entry["series_reference"]
    assert entry["current"]["comparison"] is None, "the gate must agree with the comparison"

    incomplete = {"idle": None}                                    # score incomplete
    current, unscored = _monthly_pair(previous_counts=incomplete)
    entry = build_period_entry(current=current, previous=unscored).entry
    assert entry["series_reference"] is None, entry["series_reference"]

    current, _ = _monthly_pair()
    entry = build_period_entry(current=current).entry              # no previous at all
    assert entry["series_reference"] is None, entry["series_reference"]
    print("PASS test_an_unqualified_or_missing_previous_month_references_nothing")


def test_a_weekly_period_references_nothing() -> None:
    """The weekly trend already shows the month's own cumulative buckets, and the
    design fixtures for weekly carry no reference line."""
    current = _period(identity=_identity(), meters=1_100_000, counts={"idle": 20})
    previous = _period(identity=_identity(label="2026-07-W2",
                                          end_exclusive=date(2026, 7, 13), sequence=2),
                       meters=1_000_000, counts={"idle": 40})
    entry = build_period_entry(current=current, previous=previous).entry
    assert entry["series_reference"] is None, entry["series_reference"]
    # …while the comparison itself is still made, so this is the reference line
    # being absent, not the previous period being unavailable.
    assert entry["current"]["comparison"] is not None
    print("PASS test_a_weekly_period_references_nothing")


def test_an_explicit_series_reference_still_wins() -> None:
    """The fixture generator names its own; it must not be second-guessed."""
    current, previous = _monthly_pair()
    explicit = SeriesInput("2026-06", date(2026, 6, 1), date(2026, 7, 1), 55, 900_000)
    entry = build_period_entry(current=current, previous=previous,
                               series_reference=explicit).entry
    assert entry["series_reference"]["period_label"] == "2026-06", entry["series_reference"]
    assert entry["series_reference"]["eco_score_total"] == 55, entry["series_reference"]
    print("PASS test_an_explicit_series_reference_still_wins")


def test_browser_payload_stays_within_budget() -> None:
    fixtures = fx.build_fixtures()
    largest = 0
    for name, snapshot in fixtures.items():
        payload = serialize_document(snapshot.document)
        largest = max(largest, len(payload))
        assert json.loads(payload.decode("utf-8"))["contract_id"] == SNAPSHOT_CONTRACT_ID, name
    # The worst case in the fixture set is the 31-day monthly Detailed payload.
    monthly = serialize_document(fixtures["monthly_31_days"].document)
    assert len(monthly) == largest
    assert largest <= MAX_BROWSER_PAYLOAD_BYTES, largest
    print(f"PASS test_browser_payload_stays_within_budget (worst case {largest} B)")


def main() -> None:
    test_period_eligibility_boundary()
    test_no_daily_distance_threshold()
    test_same_count_different_status()
    test_excluded_driver_keeps_eco_content_without_ranking()
    test_private_trip_policy_is_preserved_per_client()
    test_weekly_comparison_is_cumulative_month_to_date()
    test_monthly_comparison_is_previous_closed_month()
    test_coaching_uses_coefficient_movement_not_bucket_movement()
    test_best_opportunity_resolves_from_the_existing_threshold()
    test_serialized_snapshot_contains_no_forbidden_data()
    test_incomplete_scoring_fails_closed()
    test_scoring_model_is_the_existing_one_hundred_point_model()
    test_coefficient_helpers_match_the_aggregation_jobs()
    test_ranking_transitions_never_fabricate_a_delta()
    test_all_fixtures_satisfy_the_contract()
    test_series_never_exposes_an_unqualified_period()
    test_a_monthly_period_references_the_month_before_it()
    test_an_unqualified_or_missing_previous_month_references_nothing()
    test_a_weekly_period_references_nothing()
    test_an_explicit_series_reference_still_wins()
    test_browser_payload_stays_within_budget()
    print("Driver Eco Dashboard snapshot contract tests passed")


if __name__ == "__main__":
    main()
