"""Eco Driving score calculation for rounded per-100km event rates.

The scoring input is a rate, not a raw event count. Each coefficient is
rounded to a whole number with ROUND_HALF_UP before bucket selection. Negative
rates are rejected because event-per-distance coefficients are physically
non-negative; upstream aggregation should correct bad source data before scoring.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Mapping

RateValue = Decimal | float | int | str | None

REQUIRED_METRICS: tuple[str, ...] = (
    "overrev_events_count",
    "harsh_braking_events",
    "harsh_acceleration_events",
    "harsh_turning_events",
    "idle_events",
    "speeding_140_160_count",
    "speeding_160_170_count",
    "speeding_170_plus_count",
)

MAX_POSSIBLE_SCORE = 100
MIN_POSSIBLE_SCORE = -100
RATE_SCORE_QUANTUM = Decimal("1")
MIN_QUALIFYING_DISTANCE_METERS = 100_000
RATING_THRESHOLDS: tuple[tuple[Decimal, str], ...] = (
    (Decimal("85"), "bezpieczny"),
    (Decimal("40"), "akceptowalny"),
)
RATING_FALLBACK_LABEL = "niebezpieczny"


@dataclass(frozen=True)
class ScoringBucket:
    upper_bound: Decimal
    points: int


@dataclass(frozen=True)
class MetricScoringRules:
    buckets: tuple[ScoringBucket, ...]
    final_points: int


SCORING_RULES: dict[str, MetricScoringRules] = {
    "overrev_events_count": MetricScoringRules(
        buckets=(
            ScoringBucket(Decimal("0"), 15),
            ScoringBucket(Decimal("2"), 11),
            ScoringBucket(Decimal("5"), 7),
            ScoringBucket(Decimal("9"), 4),
            ScoringBucket(Decimal("13"), 0),
            ScoringBucket(Decimal("20"), -7),
        ),
        final_points=-15,
    ),
    "harsh_braking_events": MetricScoringRules(
        buckets=(
            ScoringBucket(Decimal("0"), 10),
            ScoringBucket(Decimal("1"), 8),
            ScoringBucket(Decimal("4"), 4),
            ScoringBucket(Decimal("6"), 2),
            ScoringBucket(Decimal("8"), 0),
            ScoringBucket(Decimal("10"), -5),
        ),
        final_points=-10,
    ),
    "harsh_acceleration_events": MetricScoringRules(
        buckets=(
            ScoringBucket(Decimal("0"), 10),
            ScoringBucket(Decimal("1"), 5),
            ScoringBucket(Decimal("2"), 0),
            ScoringBucket(Decimal("4"), -5),
        ),
        final_points=-10,
    ),
    "harsh_turning_events": MetricScoringRules(
        buckets=(
            ScoringBucket(Decimal("4"), 10),
            ScoringBucket(Decimal("7"), 7),
            ScoringBucket(Decimal("12"), 4),
            ScoringBucket(Decimal("16"), 0),
            ScoringBucket(Decimal("23"), -4),
            ScoringBucket(Decimal("30"), -8),
        ),
        final_points=-10,
    ),
    "idle_events": MetricScoringRules(
        buckets=(
            ScoringBucket(Decimal("0"), 10),
            ScoringBucket(Decimal("2"), 7),
            ScoringBucket(Decimal("4"), 4),
            ScoringBucket(Decimal("5"), 0),
            ScoringBucket(Decimal("6"), -5),
            ScoringBucket(Decimal("8"), -7),
        ),
        final_points=-10,
    ),
    "speeding_140_160_count": MetricScoringRules(
        buckets=(
            ScoringBucket(Decimal("0"), 15),
            ScoringBucket(Decimal("2"), 10),
            ScoringBucket(Decimal("4"), 5),
            ScoringBucket(Decimal("6"), 0),
            ScoringBucket(Decimal("8"), -5),
            ScoringBucket(Decimal("10"), -10),
        ),
        final_points=-15,
    ),
    "speeding_160_170_count": MetricScoringRules(
        buckets=(
            ScoringBucket(Decimal("0"), 15),
            ScoringBucket(Decimal("1"), 10),
            ScoringBucket(Decimal("2"), 5),
            ScoringBucket(Decimal("3"), 0),
            ScoringBucket(Decimal("4"), -5),
            ScoringBucket(Decimal("5"), -10),
        ),
        final_points=-15,
    ),
    "speeding_170_plus_count": MetricScoringRules(
        buckets=(
            ScoringBucket(Decimal("0"), 15),
            ScoringBucket(Decimal("1"), 0),
            ScoringBucket(Decimal("2"), -7),
        ),
        final_points=-15,
    ),
}


METRIC_MAX_POINTS: dict[str, int] = {
    metric: max([bucket.points for bucket in rules.buckets] + [rules.final_points])
    for metric, rules in SCORING_RULES.items()
}

VALIDATION_LABELS: dict[str, str] = {
    "overrev_events_count": "Nadmierne obroty",
    "harsh_braking_events": "Gwałtowne hamowania",
    "harsh_acceleration_events": "Gwałtowne przyspieszenia",
    "harsh_turning_events": "Ostre skręty",
    "idle_events": "Nadmierny postój",
    "speeding_140_160_count": "Przekroczenia prędkości między 140-160",
    "speeding_160_170_count": "Przekroczenia prędkości między 160-170",
    "speeding_170_plus_count": "Przekroczenia prędkości powyżej 170",
}


def _to_decimal(value: Decimal | float | int | str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("Eco Driving rate must be numeric, not boolean")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, (int, float)):
        result = Decimal(str(value))
    elif isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            raise ValueError("Eco Driving rate cannot be an empty string")
        try:
            result = Decimal(stripped)
        except InvalidOperation as exc:
            raise ValueError(f"Eco Driving rate is not a valid decimal: {value!r}") from exc
    else:
        raise ValueError(f"Unsupported Eco Driving rate type: {type(value).__name__}")

    if not result.is_finite():
        raise ValueError("Eco Driving rate must be finite")
    if result < 0:
        raise ValueError("Eco Driving rate cannot be negative")
    return result


def round_rate_for_scoring(value: RateValue) -> Decimal | None:
    """Return the whole-number per-100km coefficient used for scoring."""

    if value is None:
        return None
    return _to_decimal(value).quantize(RATE_SCORE_QUANTUM, rounding=ROUND_HALF_UP)


def score_metric(metric_name: str, value: RateValue) -> int | None:
    """Return Eco Driving points for one rounded per-100km coefficient."""

    rules = SCORING_RULES.get(metric_name)
    if rules is None:
        raise ValueError(f"Unknown Eco Driving metric: {metric_name!r}")

    rounded_value = round_rate_for_scoring(value)
    if rounded_value is None:
        return None

    for bucket in rules.buckets:
        if rounded_value <= bucket.upper_bound:
            return bucket.points
    return rules.final_points


def calculate_eco_score(rates: Mapping[str, RateValue]) -> dict:
    """Calculate total Eco Driving score from rounded per-100km rates."""

    unknown_metrics = [metric for metric in rates if metric not in SCORING_RULES]
    missing_metrics = [
        metric
        for metric in REQUIRED_METRICS
        if metric not in rates or rates[metric] is None
    ]

    metric_points: dict[str, int | None] = {}
    for metric in REQUIRED_METRICS:
        metric_points[metric] = score_metric(metric, rates.get(metric))

    scoring_complete = not missing_metrics
    total = sum(point for point in metric_points.values() if point is not None)
    return {
        "metric_points": metric_points,
        "eco_driving_score_total": total if scoring_complete else None,
        "scoring_complete": scoring_complete,
        "missing_metrics": missing_metrics,
        "unknown_metrics": unknown_metrics,
        "max_possible_score": MAX_POSSIBLE_SCORE,
        "min_possible_score": MIN_POSSIBLE_SCORE,
    }


def calculate_maxpoints_subtractions(scoring_result: Mapping[str, Any]) -> dict[str, int | None]:
    """Return points lost versus each metric's maximum possible points."""

    metric_points = scoring_result.get("metric_points") or {}
    subtractions: dict[str, int | None] = {}
    for metric in REQUIRED_METRICS:
        points = metric_points.get(metric)
        subtractions[metric] = (
            None if points is None else min(int(points) - METRIC_MAX_POINTS[metric], 0)
        )
    return subtractions


def calculate_top_validations(subtractions: Mapping[str, int | None]) -> tuple[str | None, str | None]:
    """Return labels for the two metrics with the largest point losses."""

    ranked_losses = sorted(
        (
            (subtractions[metric], index, metric)
            for index, metric in enumerate(REQUIRED_METRICS)
            if subtractions.get(metric) is not None and subtractions[metric] < 0
        ),
        key=lambda item: (item[0], item[1]),
    )
    labels = [VALIDATION_LABELS[metric] for _loss, _index, metric in ranked_losses[:2]]
    if len(labels) == 0:
        return None, None
    if len(labels) == 1:
        return labels[0], None
    return labels[0], labels[1]


def classify_ecodriving_rating_type(score_total: Decimal | float | int | str | None) -> str | None:
    """Classify the total Eco Driving score into the business rating label."""

    if score_total is None:
        return None
    if isinstance(score_total, bool):
        raise ValueError("Eco Driving score must be numeric, not boolean")
    if isinstance(score_total, Decimal):
        score = score_total
    elif isinstance(score_total, (int, float)):
        score = Decimal(str(score_total))
    elif isinstance(score_total, str):
        stripped = score_total.strip()
        if not stripped:
            raise ValueError("Eco Driving score cannot be an empty string")
        try:
            score = Decimal(stripped)
        except InvalidOperation as exc:
            raise ValueError(f"Eco Driving score is not a valid decimal: {score_total!r}") from exc
    else:
        raise ValueError(f"Unsupported Eco Driving score type: {type(score_total).__name__}")
    if not score.is_finite():
        raise ValueError("Eco Driving score must be finite")
    for minimum_score, label in RATING_THRESHOLDS:
        if score >= minimum_score:
            return label
    return RATING_FALLBACK_LABEL

