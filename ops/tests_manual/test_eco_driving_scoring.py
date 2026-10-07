#!/usr/bin/env python3
"""Manual unit-style checks for Eco Driving scoring rules."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.ecodriving.eco_scoring import (  # noqa: E402
    MAX_POSSIBLE_SCORE,
    METRIC_MAX_POINTS,
    MIN_POSSIBLE_SCORE,
    REQUIRED_METRICS,
    calculate_eco_score,
    calculate_maxpoints_subtractions,
    calculate_top_validations,
    classify_ecodriving_rating_type,
    score_metric,
)


def assert_raises_value_error(func, expected_text: str) -> None:
    try:
        func()
    except ValueError as exc:
        assert expected_text in str(exc), str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_metric_boundaries() -> None:
    # Decimal rates are rounded with ROUND_HALF_UP before inclusive bucket selection.
    cases = {
        "overrev_events_count": [
            (0, 15),
            (2, 11),
            (Decimal("2.0001"), 11),
            (5, 7),
            (Decimal("5.0001"), 7),
            (9, 4),
            (Decimal("9.0001"), 4),
            (13, 0),
            (Decimal("13.0001"), 0),
            (20, -7),
            (Decimal("20.0001"), -7),
        ],
        "harsh_braking_events": [
            (0, 10),
            (1, 8),
            (Decimal("1.0001"), 8),
            (4, 4),
            (Decimal("4.0001"), 4),
            (6, 2),
            (Decimal("6.0001"), 2),
            (8, 0),
            (Decimal("8.0001"), 0),
            (10, -5),
            (Decimal("10.0001"), -5),
        ],
        "harsh_acceleration_events": [
            (0, 10),
            (1, 5),
            (Decimal("1.0001"), 5),
            (2, 0),
            (Decimal("2.0001"), 0),
            (4, -5),
            (Decimal("4.0001"), -5),
        ],
        "harsh_turning_events": [
            (0, 10),
            (4, 10),
            (Decimal("4.0001"), 10),
            (7, 7),
            (Decimal("7.0001"), 7),
            (12, 4),
            (Decimal("12.0001"), 4),
            (16, 0),
            (Decimal("16.0001"), 0),
            (23, -4),
            (Decimal("23.0001"), -4),
            (30, -8),
            (Decimal("30.0001"), -8),
        ],
        "idle_events": [
            (0, 10),
            (2, 7),
            (Decimal("2.0001"), 7),
            (4, 4),
            (Decimal("4.0001"), 4),
            (5, 0),
            (Decimal("5.0001"), 0),
            (6, -5),
            (Decimal("6.0001"), -5),
            (8, -7),
            (Decimal("8.0001"), -7),
        ],
        "speeding_140_160_count": [
            (0, 15),
            (2, 10),
            (Decimal("2.0001"), 10),
            (4, 5),
            (Decimal("4.0001"), 5),
            (6, 0),
            (Decimal("6.0001"), 0),
            (8, -5),
            (Decimal("8.0001"), -5),
            (10, -10),
            (Decimal("10.0001"), -10),
        ],
        "speeding_160_170_count": [
            (0, 15),
            (1, 10),
            (Decimal("1.0001"), 10),
            (2, 5),
            (Decimal("2.0001"), 5),
            (3, 0),
            (Decimal("3.0001"), 0),
            (4, -5),
            (Decimal("4.0001"), -5),
            (5, -10),
            (Decimal("5.0001"), -10),
        ],
        "speeding_170_plus_count": [
            (0, 15),
            (1, 0),
            (Decimal("1.0001"), 0),
            (2, -7),
            (Decimal("2.0001"), -7),
        ],
    }

    for metric, metric_cases in cases.items():
        for value, expected in metric_cases:
            actual = score_metric(metric, value)
            assert actual == expected, (metric, value, actual, expected)

    assert score_metric("overrev_events_count", "2.0001") == 11
    assert score_metric("harsh_braking_events", Decimal("0.5")) == 8
    assert score_metric("idle_events", None) is None


def test_invalid_metric_values() -> None:
    assert_raises_value_error(
        lambda: score_metric("unknown_metric", 0),
        "Unknown Eco Driving metric",
    )
    assert_raises_value_error(
        lambda: score_metric("idle_events", "-0.1"),
        "cannot be negative",
    )
    assert_raises_value_error(
        lambda: score_metric("idle_events", ""),
        "empty string",
    )
    assert_raises_value_error(
        lambda: score_metric("idle_events", True),
        "not boolean",
    )


def test_score_totals() -> None:
    best_rates = {metric: 0 for metric in REQUIRED_METRICS}
    assert calculate_eco_score(best_rates) == {
        "metric_points": {
            "overrev_events_count": 15,
            "harsh_braking_events": 10,
            "harsh_acceleration_events": 10,
            "harsh_turning_events": 10,
            "idle_events": 10,
            "speeding_140_160_count": 15,
            "speeding_160_170_count": 15,
            "speeding_170_plus_count": 15,
        },
        "eco_driving_score_total": MAX_POSSIBLE_SCORE,
        "scoring_complete": True,
        "missing_metrics": [],
        "unknown_metrics": [],
        "max_possible_score": MAX_POSSIBLE_SCORE,
        "min_possible_score": MIN_POSSIBLE_SCORE,
    }

    worst_rates = {
        "overrev_events_count": Decimal("21"),
        "harsh_braking_events": Decimal("11"),
        "harsh_acceleration_events": Decimal("5"),
        "harsh_turning_events": Decimal("31"),
        "idle_events": Decimal("9"),
        "speeding_140_160_count": Decimal("11"),
        "speeding_160_170_count": Decimal("6"),
        "speeding_170_plus_count": Decimal("3"),
    }
    worst_result = calculate_eco_score(worst_rates)
    assert worst_result["eco_driving_score_total"] == MIN_POSSIBLE_SCORE
    assert worst_result["scoring_complete"] is True


def test_maxpoints_subtractions_and_top_validations() -> None:
    assert METRIC_MAX_POINTS["overrev_events_count"] == 15
    subtractions = calculate_maxpoints_subtractions(
        {
            "metric_points": {
                "overrev_events_count": 13,
                "harsh_braking_events": 3,
                "harsh_acceleration_events": 10,
                "harsh_turning_events": 10,
                "idle_events": 10,
                "speeding_140_160_count": 15,
                "speeding_160_170_count": 15,
                "speeding_170_plus_count": 15,
            }
        }
    )
    assert subtractions["overrev_events_count"] == -2
    assert subtractions["speeding_140_160_count"] == 0
    assert subtractions["harsh_braking_events"] == -7
    assert calculate_top_validations(subtractions) == (
        "Gwałtowne hamowania",
        "Nadmierne obroty",
    )

    one_negative = {metric: 0 for metric in REQUIRED_METRICS}
    one_negative["idle_events"] = -3
    assert calculate_top_validations(one_negative) == ("Nadmierny postój", None)

    all_zero = {metric: 0 for metric in REQUIRED_METRICS}
    assert calculate_top_validations(all_zero) == (None, None)

    tied = {metric: 0 for metric in REQUIRED_METRICS}
    tied["overrev_events_count"] = -5
    tied["harsh_braking_events"] = -5
    tied["speeding_170_plus_count"] = -5
    assert calculate_top_validations(tied) == (
        "Nadmierne obroty",
        "Gwałtowne hamowania",
    )


def test_ecodriving_rating_type_thresholds() -> None:
    assert classify_ecodriving_rating_type(85) == "bezpieczny"
    assert classify_ecodriving_rating_type(Decimal("84.999")) == "akceptowalny"
    assert classify_ecodriving_rating_type(84) == "akceptowalny"
    assert classify_ecodriving_rating_type(40) == "akceptowalny"
    assert classify_ecodriving_rating_type(Decimal("39.999")) == "niebezpieczny"
    assert classify_ecodriving_rating_type(MIN_POSSIBLE_SCORE) == "niebezpieczny"
    assert classify_ecodriving_rating_type(None) is None


def test_missing_none_and_unknown_metrics() -> None:
    missing_rates = {metric: 0 for metric in REQUIRED_METRICS[:-1]}
    missing_result = calculate_eco_score(missing_rates)
    assert missing_result["eco_driving_score_total"] is None
    assert missing_result["scoring_complete"] is False
    assert missing_result["missing_metrics"] == ["speeding_170_plus_count"]
    assert missing_result["metric_points"]["speeding_170_plus_count"] is None

    none_rates = {metric: 0 for metric in REQUIRED_METRICS}
    none_rates["idle_events"] = None
    none_result = calculate_eco_score(none_rates)
    assert none_result["eco_driving_score_total"] is None
    assert none_result["scoring_complete"] is False
    assert none_result["missing_metrics"] == ["idle_events"]
    assert none_result["metric_points"]["idle_events"] is None

    unknown_rates = {metric: 0 for metric in REQUIRED_METRICS}
    unknown_rates["extra_metric"] = 1
    unknown_result = calculate_eco_score(unknown_rates)
    assert unknown_result["eco_driving_score_total"] == MAX_POSSIBLE_SCORE
    assert unknown_result["scoring_complete"] is True
    assert unknown_result["unknown_metrics"] == ["extra_metric"]


def main() -> None:
    test_metric_boundaries()
    test_invalid_metric_values()
    test_score_totals()
    test_maxpoints_subtractions_and_top_validations()
    test_ecodriving_rating_type_thresholds()
    test_missing_none_and_unknown_metrics()
    print("Eco Driving scoring tests passed")


if __name__ == "__main__":
    main()

