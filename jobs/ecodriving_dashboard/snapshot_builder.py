"""Driver Eco Dashboard V1 — deterministic snapshot construction.

Pure derivation: given normalised per-driver period inputs (distance, raw
violation counts, ranking facts) plus per-day inputs, produce the
privacy-minimised presentation document defined by `snapshot_contract`.

The module has no database, network or configuration dependency, so every
business invariant is testable from synthetic fixtures. Scoring itself is never
reimplemented — `jobs.ecodriving.eco_scoring` remains the only source of truth
for coefficients, buckets, points and rating classification.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_FLOOR
from typing import Any, Mapping, Sequence

from jobs.ecodriving.eco_scoring import (
    METRIC_MAX_POINTS,
    REQUIRED_METRICS,
    calculate_eco_score,
    calculate_maxpoints_subtractions,
    classify_ecodriving_rating_type,
    score_metric,
)
from jobs.ecodriving_dashboard.snapshot_contract import (
    BANDS_BY_CATEGORY,
    CATEGORY_BY_KEY,
    CATEGORY_DECLARATION_INDEX,
    CATEGORY_SPECS,
    COMPARISON_KIND_PREVIOUS_CLOSED_MONTH,
    COMPARISON_KIND_PREVIOUS_CUMULATIVE_PERIOD,
    INSIGHT_BEST_OPPORTUNITY,
    INSIGHT_LARGEST_LOSS,
    INSIGHT_MOST_DETERIORATED,
    INSIGHT_MOST_IMPROVED,
    MAX_NEAR_THRESHOLD_CATEGORIES,
    MIN_QUALIFYING_DISTANCE_KM,
    PERIOD_TYPE_MONTHLY,
    PERIOD_TYPE_WEEKLY,
    QUALIFICATION_QUALIFIED,
    RANKING_STATE_LEFT_RANKING,
    RANKING_STATE_RANKED,
    RANKING_TRANSITION_LEFT_RANKING,
    RANKING_TRANSITION_NEWLY_RANKED,
    RANKING_TRANSITION_NO_BASIS,
    RANKING_TRANSITION_NOT_RANKED,
    RANKING_TRANSITION_RANKED_TO_RANKED,
    RATING_THRESHOLDS,
    RATING_TYPE_BY_STORED_LABEL,
    SCHEMA_VERSION,
    SCORE_MAX,
    SELECTED_BY_COEFFICIENT_DELTA,
    SELECTED_BY_POINTS_LOST,
    SELECTED_BY_THRESHOLD_GAIN,
    SNAPSHOT_CONTRACT_ID,
    SNAPSHOT_LOCALE,
    SNAPSHOT_STATUS_INSUFFICIENT_DISTANCE,
    SNAPSHOT_STATUS_OK,
    SNAPSHOT_STATUS_REPORT_NOT_READY,
    STATUS_NEUTRAL,
    SnapshotContractError,
    assert_snapshot_document,
    band_index_for_points_lost,
    coefficient_per_100km,
    display_kilometers,
    qualification_status_for_meters,
    ranking_state_for_group,
    status_for_points,
    to_utc_iso,
    total_kilometers_from_meters,
    weekday_short_label,
)


REASON_INSUFFICIENT_PERIOD_DISTANCE = "INSUFFICIENT_PERIOD_DISTANCE"
REASON_SCORING_INCOMPLETE = "SCORING_INCOMPLETE"
REASON_PERSISTED_SCORE_MISMATCH = "PERSISTED_SCORE_MISMATCH"


# --- inputs --------------------------------------------------------------------


@dataclass(frozen=True)
class PeriodIdentity:
    """Reporting-period identity carried explicitly so the frontend never infers it."""

    period_type: str
    period_label: str
    period_start_date: date
    period_end_date_exclusive: date
    month_start_date: date
    period_sequence_in_month: int | None = None
    closed_periods_in_month: int | None = None
    is_partial_period: bool | None = None

    @property
    def period_end_date_display(self) -> date:
        return self.period_end_date_exclusive - timedelta(days=1)

    def as_public_dict(self) -> dict:
        return {
            "period_type": self.period_type,
            "period_label": self.period_label,
            "period_start_date": self.period_start_date.isoformat(),
            "period_end_date_exclusive": self.period_end_date_exclusive.isoformat(),
            "period_end_date_display": self.period_end_date_display.isoformat(),
            "period_sequence_in_month": self.period_sequence_in_month,
            "closed_periods_in_month": self.closed_periods_in_month,
            "is_partial_period": self.is_partial_period,
        }


@dataclass(frozen=True)
class RankingFacts:
    """Ranking values produced by the existing aggregation job.

    `ranking_group` is an internal value. It never reaches the document; only
    the derived `ranking_state` does.
    """

    ranking_group: str | None = None
    ranking_position: int | None = None
    ranking_total_participants: int | None = None
    rating_group_share_percent: Decimal | None = None
    rating_group_distribution: Mapping[str, Decimal] | None = None


@dataclass(frozen=True)
class PeriodInput:
    identity: PeriodIdentity
    total_distance_meters: int
    trips_count: int
    counts: Mapping[str, int | None]
    snapshot_updated_at_utc: datetime
    ranking: RankingFacts = field(default_factory=RankingFacts)
    persisted_eco_score_total: Decimal | None = None


@dataclass(frozen=True)
class DailyInput:
    day: date
    total_distance_meters: int
    trips_count: int
    counts: Mapping[str, int]


@dataclass(frozen=True)
class SeriesInput:
    period_label: str
    period_start_date: date
    period_end_date_exclusive: date
    eco_score_total: Decimal | int | None
    total_distance_meters: int


@dataclass(frozen=True)
class PeriodEntryResult:
    entry: dict
    status: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class DashboardSnapshot:
    """Public document plus the server-side-only facts a publisher needs."""

    document: dict
    internal: dict


# --- category computation ------------------------------------------------------


@dataclass(frozen=True)
class _CategoryComputation:
    key: str
    metric: str
    count: int | None
    coefficient: int | None
    points: int | None
    points_max: int
    points_lost: int | None
    status: str
    marker_band_index: int | None
    target_band_index: int | None
    target_points_gain: int | None
    deemphasize: bool


def _compute_categories(counts: Mapping[str, int | None], total_kilometers: Decimal) -> tuple[list[_CategoryComputation], dict]:
    coefficients: dict[str, int | None] = {}
    for spec in CATEGORY_SPECS:
        coefficients[spec.metric] = coefficient_per_100km(counts.get(spec.metric), total_kilometers)

    score = calculate_eco_score(coefficients)
    subtractions = calculate_maxpoints_subtractions(score)

    computations: list[_CategoryComputation] = []
    for spec in CATEGORY_SPECS:
        points = score["metric_points"][spec.metric]
        points_max = METRIC_MAX_POINTS[spec.metric]
        points_lost = subtractions[spec.metric]
        marker_index = band_index_for_points_lost(spec.key, points_lost)
        target_index = marker_index - 1 if marker_index is not None and marker_index > 0 else None
        bands = BANDS_BY_CATEGORY[spec.key]
        target_gain = (
            bands[target_index].points_lost - bands[marker_index].points_lost
            if target_index is not None
            else None
        )
        computations.append(
            _CategoryComputation(
                key=spec.key,
                metric=spec.metric,
                count=None if counts.get(spec.metric) is None else int(counts[spec.metric]),
                coefficient=coefficients[spec.metric],
                points=None if points is None else int(points),
                points_max=points_max,
                points_lost=None if points_lost is None else int(points_lost),
                status=status_for_points(points, points_max),
                marker_band_index=marker_index,
                target_band_index=target_index,
                target_points_gain=None if target_gain is None else int(target_gain),
                deemphasize=spec.deemphasize,
            )
        )
    return computations, score


def _category_public_dict(
    computation: _CategoryComputation,
    previous: _CategoryComputation | None,
) -> dict:
    spec = CATEGORY_BY_KEY[computation.key]
    bands = BANDS_BY_CATEGORY[computation.key]
    band_label = (
        bands[computation.marker_band_index].label if computation.marker_band_index is not None else None
    )
    return {
        "key": computation.key,
        "label": spec.label,
        "short_label": spec.short_label,
        "deemphasize": computation.deemphasize,
        "count": computation.count,
        "coefficient_per_100km": computation.coefficient,
        "points": computation.points,
        "points_max": computation.points_max,
        "points_lost": computation.points_lost,
        "status": computation.status,
        "band_label": band_label,
        "bands": [band.as_public_dict() for band in bands],
        "marker_band_index": computation.marker_band_index,
        "target_band_index": computation.target_band_index,
        "target_band_label": (
            bands[computation.target_band_index].label if computation.target_band_index is not None else None
        ),
        "target_upper_bound": (
            bands[computation.target_band_index].upper_bound
            if computation.target_band_index is not None
            else None
        ),
        "target_points_gain": computation.target_points_gain,
        "previous_count": None if previous is None else previous.count,
        "previous_coefficient_per_100km": None if previous is None else previous.coefficient,
        "previous_points_lost": None if previous is None else previous.points_lost,
    }


# --- daily detail --------------------------------------------------------------


def _allocate_display_kilometers(exact_values: Sequence[Decimal], target_total: int) -> list[int]:
    """Largest-remainder allocation so daily kilometres sum to the period total."""

    floors = [int(value.to_integral_value(rounding=ROUND_FLOOR)) for value in exact_values]
    remainder = target_total - sum(floors)
    if remainder <= 0:
        return floors
    order = sorted(
        range(len(exact_values)),
        key=lambda index: (-(exact_values[index] - floors[index]), index),
    )
    allocated = list(floors)
    for index in order[:remainder]:
        allocated[index] += 1
    return allocated


def _build_days(
    identity: PeriodIdentity,
    daily_inputs: Sequence[DailyInput],
    *,
    period_display_kilometers: int,
) -> list[dict]:
    """Materialise every calendar day of the reporting period.

    There is no daily distance gate of any kind: once the reporting period is
    qualified, a 5 km day and a 99 km day are ordinary Detailed rows. A day is
    neutral only where the existing scoring contract yields no coefficient
    (`kilometers == 0`).
    """

    by_day = {daily.day: daily for daily in daily_inputs}
    unexpected = sorted(
        day.isoformat()
        for day in by_day
        if not (identity.period_start_date <= day < identity.period_end_date_exclusive)
    )
    if unexpected:
        raise SnapshotContractError("A8", f"daily rows outside the reporting period: {unexpected}")

    calendar_days: list[date] = []
    cursor = identity.period_start_date
    while cursor < identity.period_end_date_exclusive:
        calendar_days.append(cursor)
        cursor += timedelta(days=1)

    exact_kilometers = [
        total_kilometers_from_meters(by_day[day].total_distance_meters if day in by_day else 0)
        for day in calendar_days
    ]
    display = _allocate_display_kilometers(exact_kilometers, period_display_kilometers)

    days: list[dict] = []
    for index, day in enumerate(calendar_days):
        daily = by_day.get(day)
        day_kilometers = exact_kilometers[index]
        categories: list[dict] = []
        for spec in CATEGORY_SPECS:
            count = 0 if daily is None else int(daily.counts.get(spec.metric, 0) or 0)
            coefficient = coefficient_per_100km(count, day_kilometers)
            if coefficient is None:
                categories.append(
                    {
                        "key": spec.key,
                        "count": count,
                        "coefficient_per_100km": None,
                        "band_label": None,
                        "status": STATUS_NEUTRAL,
                    }
                )
                continue
            points = score_metric(spec.metric, coefficient)
            points_lost = min(int(points) - METRIC_MAX_POINTS[spec.metric], 0)
            band_index = band_index_for_points_lost(spec.key, points_lost)
            categories.append(
                {
                    "key": spec.key,
                    "count": count,
                    "coefficient_per_100km": coefficient,
                    "band_label": (
                        BANDS_BY_CATEGORY[spec.key][band_index].label if band_index is not None else None
                    ),
                    "status": status_for_points(points, METRIC_MAX_POINTS[spec.metric]),
                }
            )
        days.append(
            {
                "date": day.isoformat(),
                "weekday_short": weekday_short_label(day),
                "kilometers": display[index],
                "trips_count": 0 if daily is None else int(daily.trips_count),
                "categories": categories,
            }
        )
    return days


# --- near-threshold and coaching ----------------------------------------------


def _near_threshold(computations: Sequence[_CategoryComputation]) -> list[dict]:
    candidates = [
        computation
        for computation in computations
        if not computation.deemphasize
        and computation.points_lost is not None
        and computation.points_lost < 0
        and computation.marker_band_index is not None
        and computation.marker_band_index > 0
        and computation.coefficient is not None
    ]

    def sort_key(computation: _CategoryComputation) -> tuple:
        bands = BANDS_BY_CATEGORY[computation.key]
        target = bands[computation.target_band_index]
        distance = computation.coefficient - int(target.upper_bound)
        return (distance, -(computation.target_points_gain or 0), CATEGORY_DECLARATION_INDEX[computation.key])

    ordered = sorted(candidates, key=sort_key)[:MAX_NEAR_THRESHOLD_CATEGORIES]
    near: list[dict] = []
    for rank, computation in enumerate(ordered, start=1):
        bands = BANDS_BY_CATEGORY[computation.key]
        target = bands[computation.target_band_index]
        near.append(
            {
                "category_key": computation.key,
                "rank": rank,
                "coefficient_now": computation.coefficient,
                "target_upper_bound": target.upper_bound,
                "target_band_label": target.label,
                "points_gain": computation.target_points_gain,
                "coefficient_distance": computation.coefficient - int(target.upper_bound),
            }
        )
    return near


def _insight_inputs(
    computation: _CategoryComputation,
    previous: _CategoryComputation | None,
    *,
    kilometers: int,
    previous_kilometers: int | None,
) -> dict:
    bands = BANDS_BY_CATEGORY[computation.key]
    band_label = bands[computation.marker_band_index].label if computation.marker_band_index is not None else None
    target = bands[computation.target_band_index] if computation.target_band_index is not None else None
    return {
        "coefficient": computation.coefficient,
        "previous_coefficient": None if previous is None else previous.coefficient,
        "band_label": band_label,
        "points_max": computation.points_max,
        "points_lost": computation.points_lost,
        "previous_points_lost": None if previous is None else previous.points_lost,
        "kilometers": kilometers,
        "previous_kilometers": previous_kilometers,
        "target_band_label": None if target is None else target.label,
        "target_upper_bound": None if target is None else target.upper_bound,
        "target_points_gain": computation.target_points_gain,
    }


def _build_coaching(
    computations: Sequence[_CategoryComputation],
    previous_by_key: Mapping[str, _CategoryComputation],
    near: Sequence[dict],
    *,
    kilometers: int,
    previous_kilometers: int | None,
) -> list[dict]:
    """The four approved deterministic insights. Never padded to four."""

    by_key = {computation.key: computation for computation in computations}
    candidates = [
        computation
        for computation in computations
        if not computation.deemphasize and computation.points_lost is not None
    ]
    insights: list[dict] = []

    def emit(code: str, computation: _CategoryComputation, selected_by: str, value: dict) -> None:
        insights.append(
            {
                "code": code,
                "category_key": computation.key,
                "selected_by": selected_by,
                "value": value,
                "inputs": _insight_inputs(
                    computation,
                    previous_by_key.get(computation.key),
                    kilometers=kilometers,
                    previous_kilometers=previous_kilometers,
                ),
            }
        )

    losing = [computation for computation in candidates if computation.points_lost < 0]
    if losing:
        largest = min(losing, key=lambda item: (item.points_lost, CATEGORY_DECLARATION_INDEX[item.key]))
        emit(INSIGHT_LARGEST_LOSS, largest, SELECTED_BY_POINTS_LOST, {"points": largest.points_lost})

    # MOST_IMPROVED / MOST_DETERIORATED are selected on coefficient movement.
    # A bucket crossing is never required; the points consequence is reported
    # when one exists but never used for selection.
    comparable = [
        computation
        for computation in candidates
        if computation.coefficient is not None
        and previous_by_key.get(computation.key) is not None
        and previous_by_key[computation.key].coefficient is not None
    ]
    if comparable:
        improved = max(
            comparable,
            key=lambda item: (
                previous_by_key[item.key].coefficient - item.coefficient,
                -CATEGORY_DECLARATION_INDEX[item.key],
            ),
        )
        delta = previous_by_key[improved.key].coefficient - improved.coefficient
        if delta > 0:
            emit(
                INSIGHT_MOST_IMPROVED,
                improved,
                SELECTED_BY_COEFFICIENT_DELTA,
                {"coefficient_delta": -delta, "points_delta": _points_delta(improved, previous_by_key)},
            )

        deteriorated = max(
            comparable,
            key=lambda item: (
                item.coefficient - previous_by_key[item.key].coefficient,
                -CATEGORY_DECLARATION_INDEX[item.key],
            ),
        )
        delta = deteriorated.coefficient - previous_by_key[deteriorated.key].coefficient
        if delta > 0:
            emit(
                INSIGHT_MOST_DETERIORATED,
                deteriorated,
                SELECTED_BY_COEFFICIENT_DELTA,
                {"coefficient_delta": delta, "points_delta": _points_delta(deteriorated, previous_by_key)},
            )

    if near:
        best = by_key[near[0]["category_key"]]
        emit(
            INSIGHT_BEST_OPPORTUNITY,
            best,
            SELECTED_BY_THRESHOLD_GAIN,
            {"points_gain": near[0]["points_gain"], "coefficient_distance": near[0]["coefficient_distance"]},
        )

    order = {
        INSIGHT_LARGEST_LOSS: 0,
        INSIGHT_MOST_IMPROVED: 1,
        INSIGHT_MOST_DETERIORATED: 2,
        INSIGHT_BEST_OPPORTUNITY: 3,
    }
    insights.sort(key=lambda insight: order[insight["code"]])
    return insights


def _points_delta(
    computation: _CategoryComputation,
    previous_by_key: Mapping[str, _CategoryComputation],
) -> int | None:
    previous = previous_by_key.get(computation.key)
    if previous is None or previous.points_lost is None or computation.points_lost is None:
        return None
    return computation.points_lost - previous.points_lost


# --- period entry --------------------------------------------------------------


def _decimal_to_float(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _unavailable_entry(identity: PeriodIdentity, snapshot_updated_at_utc: datetime, status: str) -> dict:
    """A controlled non-publishable entry.

    It carries only what identifies the closed report plus its freshness. No
    distance, score, rating, ranking, category, trend, coaching or daily data.
    """

    return {
        "status": status,
        "period_identity": {
            **identity.as_public_dict(),
            "snapshot_updated_at_utc": to_utc_iso(snapshot_updated_at_utc),
        },
        "current": None,
        "previous": None,
        "series": [],
        "series_reference": None,
    }


def build_period_entry(
    *,
    current: PeriodInput,
    previous: PeriodInput | None = None,
    days: Sequence[DailyInput] = (),
    series: Sequence[SeriesInput] = (),
    series_reference: SeriesInput | None = None,
    comparable: bool = True,
) -> PeriodEntryResult:
    identity = current.identity
    reasons: list[str] = []

    qualification = qualification_status_for_meters(current.total_distance_meters)
    if qualification != QUALIFICATION_QUALIFIED:
        # Period-level 100 km gate. Fail closed: no Eco payload at all.
        return PeriodEntryResult(
            entry=_unavailable_entry(
                identity, current.snapshot_updated_at_utc, SNAPSHOT_STATUS_INSUFFICIENT_DISTANCE
            ),
            status=SNAPSHOT_STATUS_INSUFFICIENT_DISTANCE,
            reasons=(REASON_INSUFFICIENT_PERIOD_DISTANCE,),
        )

    total_kilometers = total_kilometers_from_meters(current.total_distance_meters)
    computations, score = _compute_categories(current.counts, total_kilometers)
    if not score["scoring_complete"]:
        reasons.append(REASON_SCORING_INCOMPLETE)
    else:
        eco_score_total = int(score["eco_driving_score_total"])
        if current.persisted_eco_score_total is not None and int(
            Decimal(current.persisted_eco_score_total)
        ) != eco_score_total:
            reasons.append(REASON_PERSISTED_SCORE_MISMATCH)

    if reasons:
        return PeriodEntryResult(
            entry=_unavailable_entry(
                identity, current.snapshot_updated_at_utc, SNAPSHOT_STATUS_REPORT_NOT_READY
            ),
            status=SNAPSHOT_STATUS_REPORT_NOT_READY,
            reasons=tuple(reasons),
        )

    eco_score_total = int(score["eco_driving_score_total"])
    rating_label = classify_ecodriving_rating_type(eco_score_total)
    rating_type = RATING_TYPE_BY_STORED_LABEL.get(rating_label) if rating_label else None

    # A previous period is a valid comparison basis only when it satisfies the
    # same period gate and produced a complete score.
    previous_usable = False
    previous_computations: list[_CategoryComputation] = []
    previous_kilometers_display: int | None = None
    previous_score_total: int | None = None
    if previous is not None and comparable:
        previous_qualification = qualification_status_for_meters(previous.total_distance_meters)
        if previous_qualification == QUALIFICATION_QUALIFIED:
            previous_km = total_kilometers_from_meters(previous.total_distance_meters)
            previous_computations, previous_score = _compute_categories(previous.counts, previous_km)
            if previous_score["scoring_complete"]:
                previous_usable = True
                previous_kilometers_display = display_kilometers(previous_km)
                previous_score_total = int(previous_score["eco_driving_score_total"])

    previous_by_key = (
        {computation.key: computation for computation in previous_computations} if previous_usable else {}
    )

    previously_ranked = bool(
        previous_usable
        and previous is not None
        and previous.ranking.ranking_group == "INCLUDED"
        and previous.ranking.ranking_position is not None
    )
    ranking_state = ranking_state_for_group(
        current.ranking.ranking_group, previously_ranked=previously_ranked
    )
    ranked = ranking_state == RANKING_STATE_RANKED

    # The transition is derived from the *driver-facing* ranking state, so an
    # EXCLUDED driver never even reveals that they used to be ranked.
    if not previous_usable:
        ranking_transition = RANKING_TRANSITION_NO_BASIS
    elif ranked and previously_ranked:
        ranking_transition = RANKING_TRANSITION_RANKED_TO_RANKED
    elif ranked:
        ranking_transition = RANKING_TRANSITION_NEWLY_RANKED
    elif ranking_state == RANKING_STATE_LEFT_RANKING:
        ranking_transition = RANKING_TRANSITION_LEFT_RANKING
    else:
        ranking_transition = RANKING_TRANSITION_NOT_RANKED

    # A previous ranking position is exposed only where it is permitted:
    # for a ranked driver (movement) or for a driver who left the ranking
    # (dated context). A driver in the EXCLUDED population never receives any
    # ranking position, current or historical.
    expose_previous_rank = previously_ranked and ranking_state in (
        RANKING_STATE_RANKED,
        RANKING_STATE_LEFT_RANKING,
    )
    previous_ranking_position = (
        int(previous.ranking.ranking_position) if expose_previous_rank and previous is not None else None
    )

    period_display_kilometers = display_kilometers(total_kilometers)

    block: dict[str, Any] = {
        **identity.as_public_dict(),
        "snapshot_updated_at_utc": to_utc_iso(current.snapshot_updated_at_utc),
        "qualification_status": qualification,
        "scoring_complete": True,
        "ranking_state": ranking_state,
        "ranking_transition": ranking_transition,
        "eco_score_total": eco_score_total,
        "rating_type": rating_type,
        "total_kilometers": period_display_kilometers,
        "trips_count": int(current.trips_count),
    }

    if ranked:
        block["ranking_position"] = (
            None if current.ranking.ranking_position is None else int(current.ranking.ranking_position)
        )
        block["ranking_total_participants"] = (
            None
            if current.ranking.ranking_total_participants is None
            else int(current.ranking.ranking_total_participants)
        )
        block["rating_group_share_percent"] = _decimal_to_float(current.ranking.rating_group_share_percent)
        block["rating_group_distribution"] = (
            None
            if current.ranking.rating_group_distribution is None
            else {key: _decimal_to_float(Decimal(value)) for key, value in current.ranking.rating_group_distribution.items()}
        )
    else:
        block["rating_group_distribution"] = None

    if previous_usable and previous is not None:
        comparison_kind = (
            COMPARISON_KIND_PREVIOUS_CUMULATIVE_PERIOD
            if identity.period_type == PERIOD_TYPE_WEEKLY
            else COMPARISON_KIND_PREVIOUS_CLOSED_MONTH
        )
        block["comparison"] = {
            "kind": comparison_kind,
            "comparable": True,
            "basis_period_label": previous.identity.period_label,
            "basis_start_date": previous.identity.period_start_date.isoformat(),
            "basis_end_date_exclusive": previous.identity.period_end_date_exclusive.isoformat(),
            "basis_end_date_display": previous.identity.period_end_date_display.isoformat(),
            "previous_eco_score_total": previous_score_total,
            "previous_total_kilometers": previous_kilometers_display,
            "previous_ranking_position": previous_ranking_position,
            "eco_score_delta": eco_score_total - previous_score_total,
            "ranking_position_delta_places": (
                previous_ranking_position - int(current.ranking.ranking_position)
                if ranking_transition == RANKING_TRANSITION_RANKED_TO_RANKED
                and previous_ranking_position is not None
                and current.ranking.ranking_position is not None
                else None
            ),
        }
    else:
        block["comparison"] = None

    block["categories"] = [
        _category_public_dict(computation, previous_by_key.get(computation.key))
        for computation in computations
    ]
    near = _near_threshold(computations)
    block["near_threshold"] = near
    block["coaching"] = _build_coaching(
        computations,
        previous_by_key,
        near,
        kilometers=period_display_kilometers,
        previous_kilometers=previous_kilometers_display,
    )
    block["days"] = _build_days(identity, days, period_display_kilometers=period_display_kilometers)

    entry = {
        "status": SNAPSHOT_STATUS_OK,
        "period_identity": {
            **identity.as_public_dict(),
            "snapshot_updated_at_utc": to_utc_iso(current.snapshot_updated_at_utc),
        },
        "current": block,
        "previous": (
            {
                "period_label": previous.identity.period_label,
                "period_start_date": previous.identity.period_start_date.isoformat(),
                "period_end_date_exclusive": previous.identity.period_end_date_exclusive.isoformat(),
                "period_end_date_display": previous.identity.period_end_date_display.isoformat(),
                "eco_score_total": previous_score_total,
                "total_kilometers": previous_kilometers_display,
                "trips_count": int(previous.trips_count),
            }
            if previous_usable and previous is not None
            else None
        ),
        "series": _build_series(series, current_label=identity.period_label),
        "series_reference": _resolve_series_reference(
            series_reference,
            identity=identity,
            previous=previous,
            previous_usable=previous_usable,
            previous_score_total=previous_score_total,
        ),
    }
    return PeriodEntryResult(entry=entry, status=SNAPSHOT_STATUS_OK, reasons=())


def _resolve_series_reference(
    explicit: SeriesInput | None,
    *,
    identity: PeriodIdentity,
    previous: PeriodInput | None,
    previous_usable: bool,
    previous_score_total: int | None,
) -> dict | None:
    """The horizontal reference line under the trend: the previous month.

    A caller that names one — the fixture generator does — is obeyed. Otherwise a
    MONTHLY period references the month before it, and a weekly one references
    nothing: the weekly trend already shows the month's own cumulative buckets,
    and the design fixtures for weekly carry no reference.

    IT IS DERIVED FROM THE COMPARISON'S OWN PREVIOUS PERIOD, deliberately, and
    not from the persisted score column. The comparison recomputes the previous
    month from its stored counts and requires `scoring_complete`; the persisted
    total is only ever a cross-check, and the builder treats a divergence between
    the two as REASON_PERSISTED_SCORE_MISMATCH. Sourcing this line from the
    persisted column would therefore let the note under the bars disagree with
    the comparison block three lines above it in the same document. One notion of
    "previous month", not two.

    `previous_usable` is already strictly stronger than the series gate — it
    requires the same 100 km qualification AND a complete score — so the point
    below cannot be dropped by it. It is rendered through the same function as
    every other series point so the shape cannot drift.
    """
    if explicit is not None:
        return _series_point(explicit, is_current=False)
    if identity.period_type != PERIOD_TYPE_MONTHLY:
        return None
    if not previous_usable or previous is None or previous_score_total is None:
        return None
    return _series_point(
        SeriesInput(
            period_label=previous.identity.period_label,
            period_start_date=previous.identity.period_start_date,
            period_end_date_exclusive=previous.identity.period_end_date_exclusive,
            eco_score_total=previous_score_total,
            total_distance_meters=previous.total_distance_meters,
        ),
        is_current=False,
    )


def _series_point(point: SeriesInput, *, is_current: bool) -> dict | None:
    # A period below the 100 km reporting gate never contributes a visible Eco
    # value, so it is omitted rather than shown as a zero or interpolated.
    if qualification_status_for_meters(point.total_distance_meters) != QUALIFICATION_QUALIFIED:
        return None
    if point.eco_score_total is None:
        return None
    return {
        "period_label": point.period_label,
        "start_date": point.period_start_date.isoformat(),
        "end_date_display": (point.period_end_date_exclusive - timedelta(days=1)).isoformat(),
        "eco_score_total": int(Decimal(point.eco_score_total)),
        "is_current": is_current,
    }


def _build_series(series: Sequence[SeriesInput], *, current_label: str) -> list[dict]:
    points: list[dict] = []
    for point in series:
        rendered = _series_point(point, is_current=point.period_label == current_label)
        if rendered is not None:
            points.append(rendered)
    return points


# --- document ------------------------------------------------------------------


def build_snapshot_document(
    *,
    generated_at_utc: datetime,
    weekly: PeriodEntryResult | None = None,
    monthly: PeriodEntryResult | None = None,
    business_timezone: str = "Europe/Warsaw",
) -> dict:
    if weekly is None and monthly is None:
        raise ValueError("a snapshot must carry at least one period type")
    return {
        "schema_version": SCHEMA_VERSION,
        "contract_id": SNAPSHOT_CONTRACT_ID,
        "generated_at_utc": to_utc_iso(generated_at_utc),
        "timezone": business_timezone,
        "locale": SNAPSHOT_LOCALE,
        "constants": {
            "min_qualifying_distance_km": MIN_QUALIFYING_DISTANCE_KM,
            "rating_thresholds": {
                "safe": int(RATING_THRESHOLDS[0][0]),
                "acceptable": int(RATING_THRESHOLDS[1][0]),
            },
            "score_max": SCORE_MAX,
        },
        "periods": {
            PERIOD_TYPE_WEEKLY: None if weekly is None else weekly.entry,
            PERIOD_TYPE_MONTHLY: None if monthly is None else monthly.entry,
        },
    }


def build_driver_snapshot(
    *,
    generated_at_utc: datetime,
    period_type: str,
    current: PeriodInput,
    previous: PeriodInput | None = None,
    days: Sequence[DailyInput] = (),
    series: Sequence[SeriesInput] = (),
    series_reference: SeriesInput | None = None,
    comparable: bool = True,
    internal: Mapping[str, Any] | None = None,
    banned_values: Sequence[str],
    business_timezone: str = "Europe/Warsaw",
) -> DashboardSnapshot:
    """Build and gate one driver's snapshot for one period type.

    `banned_values` is REQUIRED, with no default. The review found a
    person-like value surviving publication precisely because the value-level
    privacy sweep degraded silently to a no-op when the caller forgot the
    argument; a required parameter cannot be forgotten. The supported publisher
    entry point never passes it by hand at all — see
    `jobs.ecodriving_dashboard.publication.build_publishable_snapshot`, which
    derives it from a mandatory `PrivacyContext`.
    """

    if period_type not in (PERIOD_TYPE_WEEKLY, PERIOD_TYPE_MONTHLY):
        raise ValueError(f"Unknown period_type: {period_type!r}")
    if current.identity.period_type != period_type:
        raise ValueError("period_type does not match the current period identity")

    result = build_period_entry(
        current=current,
        previous=previous,
        days=days,
        series=series,
        series_reference=series_reference,
        comparable=comparable,
    )
    document = build_snapshot_document(
        generated_at_utc=generated_at_utc,
        weekly=result if period_type == PERIOD_TYPE_WEEKLY else None,
        monthly=result if period_type == PERIOD_TYPE_MONTHLY else None,
        business_timezone=business_timezone,
    )
    assert_snapshot_document(document, banned_values=banned_values)

    server_side = dict(internal or {})
    server_side.update(
        {
            "period_type": period_type,
            "period_label": current.identity.period_label,
            "period_start_date": current.identity.period_start_date.isoformat(),
            "period_end_date_exclusive": current.identity.period_end_date_exclusive.isoformat(),
            "snapshot_status": result.status,
            "snapshot_status_reasons": list(result.reasons),
            "ranking_group": current.ranking.ranking_group,
        }
    )
    return DashboardSnapshot(document=document, internal=server_side)
