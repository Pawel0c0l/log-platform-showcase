#!/usr/bin/env python3
"""Synthetic fixtures for the Driver Eco Dashboard V1 snapshot contract.

Every value here is invented. No production identity, distance, score, roster
row or e-mail address appears in this file, and nothing in it touches a
database.

Each fixture is expressed as a set of *days*; the period totals are summed from
those days, so the daily/period reconciliation assertions (A8/A9) are exercised
against realistic data rather than hand-tuned aggregates.

Run directly to print a summary, or with `--write-json <dir>` to materialise the
serialised browser payloads for frontend work.
"""

from __future__ import annotations

import argparse
import copy
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sys
from typing import Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.ecodriving.eco_scoring import REQUIRED_METRICS  # noqa: E402
from jobs.ecodriving_dashboard.snapshot_builder import (  # noqa: E402
    DailyInput,
    DashboardSnapshot,
    PeriodIdentity,
    PeriodInput,
    RankingFacts,
    SeriesInput,
    build_driver_snapshot,
)
from jobs.ecodriving_dashboard.snapshot_contract import (  # noqa: E402
    assert_snapshot_document,
    CATEGORY_BY_KEY,
    PERIOD_TYPE_MONTHLY,
    PERIOD_TYPE_WEEKLY,
)


SYNTHETIC_IDENTITY_KEY = "SYNTHETIC-DRIVER-0001"
SYNTHETIC_CLIENT_CODE = "SYNT00001"
GENERATED_AT = datetime(2026, 7, 20, 4, 40, 11, tzinfo=timezone.utc)
UPDATED_AT = datetime(2026, 7, 20, 4, 40, 11, tzinfo=timezone.utc)

# 19 days, 1 July .. 19 July. Deliberately contains 5 km, 20 km, 49 km and
# 99 km days plus three no-driving days: once the reporting period qualifies,
# every one of them is an ordinary Detailed row.
WEEKLY_DAY_KM: tuple[int, ...] = (70, 85, 0, 92, 64, 20, 0, 78, 88, 95, 60, 5, 0, 99, 84, 91, 77, 49, 43)
PREVIOUS_DAY_KM: tuple[int, ...] = WEEKLY_DAY_KM[:12]


def _metric_counts(by_category_key: Mapping[str, int]) -> dict[str, int]:
    counts = {metric: 0 for metric in REQUIRED_METRICS}
    for key, value in by_category_key.items():
        counts[CATEGORY_BY_KEY[key].metric] = int(value)
    return counts


def _allocate(total: int, weights: Sequence[int]) -> list[int]:
    """Largest-remainder allocation of one category total across days."""

    weight_sum = sum(weights)
    if weight_sum <= 0 or total <= 0:
        return [0] * len(weights)
    exact = [Decimal(total) * Decimal(weight) / Decimal(weight_sum) for weight in weights]
    floors = [int(value) for value in exact]
    remainder = total - sum(floors)
    order = sorted(range(len(weights)), key=lambda index: (-(exact[index] - floors[index]), index))
    allocated = list(floors)
    for index in order[:remainder]:
        allocated[index] += 1
    return allocated


def daily_inputs(
    start: date,
    day_kilometers: Sequence[int],
    category_totals: Mapping[str, int],
) -> list[DailyInput]:
    metric_totals = _metric_counts(category_totals)
    allocations = {
        metric: _allocate(total, day_kilometers) for metric, total in metric_totals.items()
    }
    days: list[DailyInput] = []
    for index, kilometers in enumerate(day_kilometers):
        days.append(
            DailyInput(
                day=start + timedelta(days=index),
                total_distance_meters=int(kilometers) * 1000,
                trips_count=0 if kilometers == 0 else max(1, int(kilometers) // 25),
                counts={metric: allocations[metric][index] for metric in REQUIRED_METRICS},
            )
        )
    return days


def period_from_days(
    identity: PeriodIdentity,
    days: Sequence[DailyInput],
    *,
    ranking: RankingFacts | None = None,
    updated_at: datetime = UPDATED_AT,
    unknown_categories: Sequence[str] = (),
) -> PeriodInput:
    counts: dict[str, int | None] = {
        metric: sum(int(day.counts.get(metric, 0)) for day in days) for metric in REQUIRED_METRICS
    }
    for key in unknown_categories:
        counts[CATEGORY_BY_KEY[key].metric] = None
    return PeriodInput(
        identity=identity,
        total_distance_meters=sum(day.total_distance_meters for day in days),
        trips_count=sum(day.trips_count for day in days),
        counts=counts,
        snapshot_updated_at_utc=updated_at,
        ranking=ranking or RankingFacts(),
    )


def weekly_identity(
    *,
    sequence: int,
    end_exclusive: date,
    month_start: date = date(2026, 7, 1),
    closed_periods_in_month: int = 5,
    is_partial: bool = False,
) -> PeriodIdentity:
    return PeriodIdentity(
        period_type=PERIOD_TYPE_WEEKLY,
        period_label=f"{month_start:%Y-%m}-W{sequence}",
        period_start_date=month_start,
        period_end_date_exclusive=end_exclusive,
        month_start_date=month_start,
        period_sequence_in_month=sequence,
        closed_periods_in_month=closed_periods_in_month,
        is_partial_period=is_partial,
    )


CURRENT_WEEKLY = weekly_identity(sequence=3, end_exclusive=date(2026, 7, 20))
PREVIOUS_WEEKLY = weekly_identity(sequence=2, end_exclusive=date(2026, 7, 13))

MONTHLY_IDENTITY = PeriodIdentity(
    period_type=PERIOD_TYPE_MONTHLY,
    period_label="2026-07",
    period_start_date=date(2026, 7, 1),
    period_end_date_exclusive=date(2026, 8, 1),
    month_start_date=date(2026, 7, 1),
    period_sequence_in_month=None,
    closed_periods_in_month=5,
    is_partial_period=False,
)
PREVIOUS_MONTHLY_IDENTITY = PeriodIdentity(
    period_type=PERIOD_TYPE_MONTHLY,
    period_label="2026-06",
    period_start_date=date(2026, 6, 1),
    period_end_date_exclusive=date(2026, 7, 1),
    month_start_date=date(2026, 6, 1),
    period_sequence_in_month=None,
    closed_periods_in_month=5,
    is_partial_period=False,
)

# 1 100 km over the current cumulative period.
ACCEPTABLE_TOTALS = {
    "harsh_braking": 22,
    "harsh_acceleration": 11,
    "harsh_turning": 66,
    "idle": 33,
    "speeding_140_160": 11,
}
SAFE_TOTALS = {
    "harsh_braking": 11,
    "harsh_turning": 44,
    "idle": 11,
}
DANGEROUS_TOTALS = {
    "harsh_braking": 99,
    "harsh_acceleration": 33,
    "harsh_turning": 187,
    "idle": 66,
    "speeding_140_160": 77,
}
# 657 km over the previous cumulative period.
PREVIOUS_TOTALS = {
    "harsh_braking": 20,
    "harsh_acceleration": 7,
    "harsh_turning": 26,
    "idle": 20,
    "speeding_140_160": 7,
}

RANKED_FACTS = RankingFacts(
    ranking_group="INCLUDED",
    ranking_position=18,
    ranking_total_participants=158,
    rating_group_share_percent=Decimal("50.63"),
    rating_group_distribution={
        "safe": Decimal("41.77"),
        "acceptable": Decimal("50.63"),
        "dangerous": Decimal("7.59"),
    },
)
# The same ranked driver in a client whose ranked population contains NOBODY in
# the `dangerous` group. The host groups the rated population, so an empty group
# produces no row and no key: the distribution is SPARSE, and that is a valid
# snapshot rather than a defective one. Kept next to RANKED_FACTS because the
# only difference that matters is which buckets exist.
SPARSE_RANKED_FACTS = RankingFacts(
    ranking_group="INCLUDED",
    ranking_position=18,
    ranking_total_participants=158,
    rating_group_share_percent=Decimal("50.63"),
    rating_group_distribution={
        "safe": Decimal("49.37"),
        "acceptable": Decimal("50.63"),
    },
)
PREVIOUS_RANKED_FACTS = RankingFacts(
    ranking_group="INCLUDED",
    ranking_position=24,
    ranking_total_participants=151,
    rating_group_share_percent=Decimal("48.34"),
)
# A driver configured out of the ranking still carries a persisted position in
# the separate EXCLUDED league. It must never reach the browser.
EXCLUDED_FACTS = RankingFacts(
    ranking_group="EXCLUDED",
    ranking_position=407,
    ranking_total_participants=927,
    rating_group_share_percent=None,
)
UNKNOWN_DRIVER_FACTS = RankingFacts(ranking_group="UNKNOWN_DRIVER")


def weekly_series(labels_and_ends: Sequence[tuple[str, date, int, int]]) -> list[SeriesInput]:
    return [
        SeriesInput(
            period_label=label,
            period_start_date=date(2026, 7, 1),
            period_end_date_exclusive=end_exclusive,
            eco_score_total=score,
            total_distance_meters=meters,
        )
        for label, end_exclusive, score, meters in labels_and_ends
    ]


FULL_SERIES = weekly_series(
    (
        ("2026-07-W1", date(2026, 7, 6), 68, 311_000),
        ("2026-07-W2", date(2026, 7, 13), 71, 657_000),
        ("2026-07-W3", date(2026, 7, 20), 75, 1_100_000),
    )
)
SKIPPED_SERIES = weekly_series(
    (
        ("2026-07-W2", date(2026, 7, 13), 71, 657_000),
        ("2026-07-W3", date(2026, 7, 20), 75, 1_100_000),
    )
)


def _snapshot(
    *,
    period_type: str,
    current: PeriodInput,
    previous: PeriodInput | None,
    days: Sequence[DailyInput],
    series: Sequence[SeriesInput] = (),
    series_reference: SeriesInput | None = None,
    comparable: bool = True,
) -> DashboardSnapshot:
    return build_driver_snapshot(
        generated_at_utc=GENERATED_AT,
        period_type=period_type,
        current=current,
        previous=previous,
        days=days,
        series=series,
        series_reference=series_reference,
        comparable=comparable,
        internal={"client_code": SYNTHETIC_CLIENT_CODE, "identity_key": SYNTHETIC_IDENTITY_KEY},
        banned_values=(SYNTHETIC_IDENTITY_KEY, SYNTHETIC_CLIENT_CODE),
    )


def _weekly_case(
    totals: Mapping[str, int],
    *,
    ranking: RankingFacts,
    previous_ranking: RankingFacts | None = PREVIOUS_RANKED_FACTS,
    with_previous: bool = True,
    series: Sequence[SeriesInput] = FULL_SERIES,
) -> DashboardSnapshot:
    days = daily_inputs(date(2026, 7, 1), WEEKLY_DAY_KM, totals)
    current = period_from_days(CURRENT_WEEKLY, days, ranking=ranking)
    previous = None
    if with_previous:
        previous_days = daily_inputs(date(2026, 7, 1), PREVIOUS_DAY_KM, PREVIOUS_TOTALS)
        previous = period_from_days(PREVIOUS_WEEKLY, previous_days, ranking=previous_ranking or RankingFacts())
    return _snapshot(
        period_type=PERIOD_TYPE_WEEKLY,
        current=current,
        previous=previous,
        days=days,
        series=series,
    )


def _aged_without_series_reference(
    snapshot: DashboardSnapshot, period_type: str
) -> DashboardSnapshot:
    """A copy of `snapshot` with `series_reference` blanked, as if built before
    the reference was derived. Re-validated, so the result is still a document
    the contract gate accepts — an old document, not a malformed one."""
    document = copy.deepcopy(snapshot.document)
    document["periods"][period_type]["series_reference"] = None
    assert_snapshot_document(
        document, banned_values=(SYNTHETIC_IDENTITY_KEY, SYNTHETIC_CLIENT_CODE)
    )
    return DashboardSnapshot(document=document, internal=dict(snapshot.internal))


def build_fixtures() -> dict[str, DashboardSnapshot]:
    fixtures: dict[str, DashboardSnapshot] = {}

    fixtures["ranked_acceptable"] = _weekly_case(ACCEPTABLE_TOTALS, ranking=RANKED_FACTS)
    fixtures["ranked_safe"] = _weekly_case(SAFE_TOTALS, ranking=RANKED_FACTS)
    fixtures["ranked_dangerous"] = _weekly_case(DANGEROUS_TOTALS, ranking=RANKED_FACTS)
    fixtures["not_ranked_by_configuration"] = _weekly_case(ACCEPTABLE_TOTALS, ranking=EXCLUDED_FACTS)
    fixtures["not_on_roster"] = _weekly_case(
        ACCEPTABLE_TOTALS, ranking=UNKNOWN_DRIVER_FACTS, previous_ranking=UNKNOWN_DRIVER_FACTS
    )
    fixtures["newly_ranked"] = _weekly_case(
        ACCEPTABLE_TOTALS, ranking=RANKED_FACTS, previous_ranking=UNKNOWN_DRIVER_FACTS
    )
    fixtures["left_ranking"] = _weekly_case(
        ACCEPTABLE_TOTALS, ranking=UNKNOWN_DRIVER_FACTS, previous_ranking=PREVIOUS_RANKED_FACTS
    )
    fixtures["skipped_period_in_series"] = _weekly_case(
        ACCEPTABLE_TOTALS, ranking=RANKED_FACTS, series=SKIPPED_SERIES
    )

    # First closed cumulative period of the month: no basis at all.
    first_days = daily_inputs(date(2026, 7, 1), WEEKLY_DAY_KM[:5], {"harsh_braking": 8, "idle": 12})
    fixtures["first_closed_period_of_month"] = _snapshot(
        period_type=PERIOD_TYPE_WEEKLY,
        current=period_from_days(
            weekly_identity(sequence=1, end_exclusive=date(2026, 7, 6), is_partial=True),
            first_days,
            ranking=RANKED_FACTS,
        ),
        previous=None,
        days=first_days,
        series=FULL_SERIES[:1],
    )

    # Previous period exists but did not reach the 100 km reporting gate.
    short_previous_days = daily_inputs(date(2026, 7, 1), (30, 29, 0, 40), {"idle": 2})
    current_days = daily_inputs(date(2026, 7, 1), WEEKLY_DAY_KM, ACCEPTABLE_TOTALS)
    fixtures["no_comparison"] = _snapshot(
        period_type=PERIOD_TYPE_WEEKLY,
        current=period_from_days(CURRENT_WEEKLY, current_days, ranking=RANKED_FACTS),
        previous=period_from_days(
            weekly_identity(sequence=1, end_exclusive=date(2026, 7, 5), is_partial=True),
            short_previous_days,
        ),
        days=current_days,
        series=FULL_SERIES,
    )

    # Whole reporting period below 100 km: no Eco payload at all.
    insufficient_days = daily_inputs(date(2026, 7, 1), (30, 29, 0, 40), {"idle": 2})
    fixtures["insufficient_period_distance"] = _snapshot(
        period_type=PERIOD_TYPE_WEEKLY,
        current=period_from_days(
            weekly_identity(sequence=1, end_exclusive=date(2026, 7, 5), is_partial=True),
            insufficient_days,
            ranking=RANKED_FACTS,
        ),
        previous=None,
        days=insufficient_days,
        series=FULL_SERIES,
    )

    # A required scoring category cannot be produced: fail closed.
    not_ready_days = daily_inputs(date(2026, 7, 1), WEEKLY_DAY_KM, ACCEPTABLE_TOTALS)
    fixtures["report_not_ready"] = _snapshot(
        period_type=PERIOD_TYPE_WEEKLY,
        current=period_from_days(
            CURRENT_WEEKLY, not_ready_days, ranking=RANKED_FACTS, unknown_categories=("speeding_170_plus",)
        ),
        previous=None,
        days=not_ready_days,
        series=FULL_SERIES,
    )

    # Final cumulative period of the month == the month itself.
    end_of_month_km = WEEKLY_DAY_KM + (55, 62, 0, 71, 66, 58, 44, 39, 51, 47, 63, 0)
    end_of_month_days = daily_inputs(date(2026, 7, 1), end_of_month_km, ACCEPTABLE_TOTALS)
    fixtures["end_of_month_period"] = _snapshot(
        period_type=PERIOD_TYPE_WEEKLY,
        current=period_from_days(
            weekly_identity(sequence=5, end_exclusive=date(2026, 8, 1), is_partial=True),
            end_of_month_days,
            ranking=RANKED_FACTS,
        ),
        previous=period_from_days(
            PREVIOUS_WEEKLY,
            daily_inputs(date(2026, 7, 1), PREVIOUS_DAY_KM, PREVIOUS_TOTALS),
            ranking=PREVIOUS_RANKED_FACTS,
        ),
        days=end_of_month_days,
        series=FULL_SERIES,
    )

    # Closed month vs previous closed month, 31 materialised days.
    monthly_days = daily_inputs(date(2026, 7, 1), end_of_month_km, ACCEPTABLE_TOTALS)
    previous_month_days = daily_inputs(
        date(2026, 6, 1), tuple(WEEKLY_DAY_KM) + (61, 58, 0, 44, 72, 66, 51, 49, 47, 55, 38), PREVIOUS_TOTALS
    )
    fixtures["monthly_31_days"] = _snapshot(
        period_type=PERIOD_TYPE_MONTHLY,
        current=period_from_days(MONTHLY_IDENTITY, monthly_days, ranking=RANKED_FACTS),
        previous=period_from_days(
            PREVIOUS_MONTHLY_IDENTITY, previous_month_days, ranking=PREVIOUS_RANKED_FACTS
        ),
        days=monthly_days,
        series=FULL_SERIES,
        series_reference=SeriesInput(
            period_label="2026-06",
            period_start_date=date(2026, 6, 1),
            period_end_date_exclusive=date(2026, 7, 1),
            eco_score_total=69,
            total_distance_meters=1_641_000,
        ),
    )

    # The shape every live August 2026 document has: a real PREVIOUS_CLOSED_MONTH
    # comparison, but `series_reference: null` — those documents were built
    # before the reference was derived, and the Worker keeps their bytes. The
    # previous-month bar must come from the comparison in that case, or the page
    # contradicts its own header.
    #
    # It is produced by BLANKING the field on a normally built document, not by
    # a builder switch: the builder now always derives the reference from a
    # usable previous month, which is correct and must not gain a way to skip
    # that. This fixture represents an OLD document, so it is edited the way age
    # made it, then re-validated against the same contract gate.
    fixtures["monthly_previous_from_comparison"] = _aged_without_series_reference(
        fixtures["monthly_31_days"], PERIOD_TYPE_MONTHLY
    )

    # A closed month with NO usable previous month: the first month of a series.
    # The trend card opens on the progress tab, because the comparison tab has
    # only this month to show and says so.
    fixtures["monthly_without_previous_month"] = _snapshot(
        period_type=PERIOD_TYPE_MONTHLY,
        current=period_from_days(MONTHLY_IDENTITY, monthly_days, ranking=RANKED_FACTS),
        previous=None,
        days=monthly_days,
        series=FULL_SERIES,
        series_reference=None,
    )

    # A zero-event category (green) next to a nonzero count that is still green.
    fixtures["zero_event_category"] = fixtures["ranked_safe"]
    # No-driving days are present in every weekly fixture by construction.
    fixtures["no_driving_day"] = fixtures["ranked_acceptable"]

    return fixtures


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-json", type=Path, default=None)
    args = parser.parse_args()

    fixtures = build_fixtures()
    for name, snapshot in sorted(fixtures.items()):
        entry = snapshot.document["periods"][snapshot.internal["period_type"]]
        block = entry["current"]
        summary = entry["status"]
        if block is not None:
            summary = (
                f"{entry['status']} score={block['eco_score_total']} "
                f"rating={block['rating_type']} ranking={block['ranking_state']} "
                f"transition={block['ranking_transition']} days={len(block['days'])} "
                f"coaching={[insight['code'] for insight in block['coaching']]}"
            )
        print(f"{name:32s} {summary}")

    if args.write_json:
        args.write_json.mkdir(parents=True, exist_ok=True)
        for name, snapshot in sorted(fixtures.items()):
            path = args.write_json / f"{name}.json"
            path.write_text(json.dumps(snapshot.document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {len(fixtures)} fixture documents to {args.write_json}")


if __name__ == "__main__":
    main()
