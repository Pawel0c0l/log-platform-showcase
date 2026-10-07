#!/usr/bin/env python3
"""Static/manual checks for Eco Driving trend view migration.

Run:

    cd /opt/log-platform
    PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_eco_driving_trend_views.py
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import re


REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = REPO_ROOT / "db" / "client_business" / "031_eco_driving_validation_fields.sql"
RATING_SHARE_MIGRATION_PATH = REPO_ROOT / "db" / "client_business" / "034_eco_driving_rating_type_share_percent.sql"

WEEKLY_030_COLUMNS = [
    "client_id",
    "assigned_id",
    "driver_name",
    "email",
    "ranking_included",
    "ranking_group",
    "month_start_date",
    "period_start_date",
    "period_end_date",
    "period_sequence_in_month",
    "period_label",
    "is_partial_period",
    "trips_count",
    "total_distance_meters",
    "total_kilometers",
    "eco_driving_score_total",
    "previous_snapshot_score",
    "score_delta_abs",
    "score_delta_pct",
    "rolling_4_snapshot_avg_score",
    "rolling_8_snapshot_avg_score",
    "snapshots_observed",
    "best_score_to_date",
    "worst_score_to_date",
    "previous_snapshot_kilometers",
    "kilometers_delta_abs",
    "ranking_position",
    "previous_ranking_position",
    "ranking_position_delta",
    "ranking_total_participants",
    "qualification_status",
    "calculation_status",
]

MONTHLY_030_COLUMNS = [
    "client_id",
    "assigned_id",
    "driver_name",
    "email",
    "ranking_included",
    "ranking_group",
    "month_start_date",
    "month_end_date",
    "trips_count",
    "total_distance_meters",
    "total_kilometers",
    "eco_driving_score_total",
    "previous_month_score",
    "score_delta_abs",
    "score_delta_pct",
    "rolling_3_month_avg_score",
    "rolling_6_month_avg_score",
    "months_observed",
    "best_month_score_to_date",
    "worst_month_score_to_date",
    "previous_month_kilometers",
    "kilometers_delta_abs",
    "ranking_position",
    "previous_ranking_position",
    "ranking_position_delta",
    "ranking_total_participants",
    "qualification_status",
    "calculation_status",
]

VALIDATION_COLUMNS = [
    "overrev_maxpoints_subtract",
    "harsh_braking_maxpoints_subtract",
    "harsh_acceleration_maxpoints_subtract",
    "harsh_turning_maxpoints_subtract",
    "idle_maxpoints_subtract",
    "speeding_140_160_maxpoints_subtract",
    "speeding_160_170_maxpoints_subtract",
    "speeding_170_plus_maxpoints_subtract",
    "top_1_validation",
    "top_2_validation",
    "ecodriving_rating_type",
]


def _read_sql(path: Path = MIGRATION_PATH) -> str:
    return path.read_text(encoding="utf-8")


def _compact(sql: str) -> str:
    return re.sub(r"\s+", " ", sql)



def _view_output_columns(sql: str, view_name: str) -> list[str]:
    start = sql.index(f"CREATE OR REPLACE VIEW public.{view_name} AS")
    next_view = sql.find("CREATE OR REPLACE VIEW public.", start + 1)
    block = sql[start:] if next_view == -1 else sql[start:next_view]
    select_body = block.split("\nSELECT\n", 1)[1].split("\nFROM trend_base;", 1)[0]
    columns: list[str] = []
    pending_case = False
    for raw_line in select_body.splitlines():
        line = raw_line.strip().rstrip(",")
        if not line:
            continue
        if line == "CASE":
            pending_case = True
            continue
        if pending_case:
            match = re.search(r"END AS ([a-zA-Z_][a-zA-Z0-9_]*)$", line)
            if match:
                columns.append(match.group(1))
                pending_case = False
            continue
        columns.append(line)
    return columns

def _rolling_avg(values: list[Decimal | None]) -> Decimal | None:
    present = [value for value in values if value is not None]
    if not present:
        return None
    return sum(present) / Decimal(len(present))


def _trend_rows(rows: list[dict]) -> list[dict]:
    out: list[dict] = []
    for index, row in enumerate(rows):
        previous = rows[index - 1] if index else None
        score = row["score"]
        previous_score = previous["score"] if previous else None
        kilometers = row["kilometers"]
        previous_kilometers = previous["kilometers"] if previous else None
        ranking_position = row["ranking_position"]
        previous_ranking_position = previous["ranking_position"] if previous else None
        score_delta = (
            None
            if score is None or previous_score is None
            else score - previous_score
        )
        out.append(
            {
                **row,
                "previous_score": previous_score,
                "score_delta_abs": score_delta,
                "score_delta_pct": (
                    None
                    if score_delta is None or previous_score in (None, Decimal("0"))
                    else (score_delta / abs(previous_score)) * Decimal("100")
                ),
                "previous_kilometers": previous_kilometers,
                "kilometers_delta_abs": (
                    None
                    if previous_kilometers is None or kilometers is None
                    else kilometers - previous_kilometers
                ),
                "previous_ranking_position": previous_ranking_position,
                "ranking_position_delta": (
                    None
                    if previous_ranking_position is None or ranking_position is None
                    else previous_ranking_position - ranking_position
                ),
                "rolling_4_snapshot_avg_score": _rolling_avg(
                    [r["score"] for r in rows[max(0, index - 3) : index + 1]]
                ),
            }
        )
    return out


def test_views_declared_and_documented() -> None:
    sql = _read_sql()
    assert "CREATE OR REPLACE VIEW public.eco_driver_weekly_trends_view" in sql
    assert "CREATE OR REPLACE VIEW public.eco_driver_monthly_trends_view" in sql
    assert "COMMENT ON VIEW public.eco_driver_weekly_trends_view" in sql
    assert "COMMENT ON VIEW public.eco_driver_monthly_trends_view" in sql
    assert "cumulative month-to-date snapshots" in sql
    assert "Do not sum weekly trend rows" in sql
    assert "including validation/rating output fields" in sql
    print("PASS: trend views are declared and documented in migration comments")


def test_weekly_view_columns_and_windowing() -> None:
    sql = _read_sql()
    compact = _compact(sql)
    required = [
        "previous_snapshot_score",
        "overrev_maxpoints_subtract",
        "top_1_validation",
        "top_2_validation",
        "ecodriving_rating_type",
        "score_delta_abs",
        "score_delta_pct",
        "rolling_4_snapshot_avg_score",
        "rolling_8_snapshot_avg_score",
        "snapshots_observed",
        "best_score_to_date",
        "worst_score_to_date",
        "previous_snapshot_kilometers",
        "kilometers_delta_abs",
        "previous_ranking_position",
        "ranking_position_delta",
    ]
    for column in required:
        assert column in sql, column
    assert "PARTITION BY s.client_id, s.assigned_id ORDER BY s.month_start_date, s.period_end_date" in compact
    assert "PARTITION BY s.client_id, s.assigned_id, s.month_start_date" not in compact
    assert "ROWS BETWEEN 3 PRECEDING AND CURRENT ROW" in sql
    assert "ROWS BETWEEN 7 PRECEDING AND CURRENT ROW" in sql
    assert "COUNT(*) OVER weekly_to_date_window AS snapshots_observed" in sql
    assert "LEFT JOIN public.eco_drivers_id_chart c" in sql
    assert "c.driver_name" in sql and "c.email" in sql
    print("PASS: weekly trend view uses assigned-id chronology and rolling snapshot windows")


def test_weekly_delta_formulas() -> None:
    sql = _compact(_read_sql())
    assert "eco_driving_score_total - previous_snapshot_score" in sql
    assert "previous_snapshot_score = 0 THEN NULL" in sql
    assert "total_kilometers - previous_snapshot_kilometers" in sql
    assert "previous_ranking_position - ranking_position" in sql
    print("PASS: weekly deltas handle NULL/zero scores, kilometers, and rank direction")


def test_monthly_view_columns_and_windowing() -> None:
    sql = _read_sql()
    compact = _compact(sql)
    required = [
        "previous_month_score",
        "overrev_maxpoints_subtract",
        "top_1_validation",
        "top_2_validation",
        "ecodriving_rating_type",
        "rolling_3_month_avg_score",
        "rolling_6_month_avg_score",
        "months_observed",
        "best_month_score_to_date",
        "worst_month_score_to_date",
        "previous_month_kilometers",
        "kilometers_delta_abs",
        "previous_ranking_position",
        "ranking_position_delta",
    ]
    for column in required:
        assert column in sql, column
    assert "PARTITION BY s.client_id, s.assigned_id ORDER BY s.month_start_date" in compact
    assert "ROWS BETWEEN 2 PRECEDING AND CURRENT ROW" in sql
    assert "ROWS BETWEEN 5 PRECEDING AND CURRENT ROW" in sql
    assert "COUNT(*) OVER monthly_to_date_window AS months_observed" in sql
    assert "previous_ranking_position - ranking_position" in sql
    print("PASS: monthly trend view uses assigned-id chronology and rolling month windows")


def test_031_appends_validation_columns_without_reordering_030_views() -> None:
    sql = _read_sql()
    weekly_columns = _view_output_columns(sql, "eco_driver_weekly_trends_view")
    monthly_columns = _view_output_columns(sql, "eco_driver_monthly_trends_view")
    assert weekly_columns[: len(WEEKLY_030_COLUMNS)] == WEEKLY_030_COLUMNS
    assert weekly_columns[len(WEEKLY_030_COLUMNS) :] == VALIDATION_COLUMNS
    assert monthly_columns[: len(MONTHLY_030_COLUMNS)] == MONTHLY_030_COLUMNS
    assert monthly_columns[len(MONTHLY_030_COLUMNS) :] == VALIDATION_COLUMNS
    print("PASS: 031 view replacements preserve 030 column order and append validation fields")


def test_034_appends_rating_type_share_without_reordering_031_views() -> None:
    sql = _read_sql(RATING_SHARE_MIGRATION_PATH)
    weekly_columns = _view_output_columns(sql, "eco_driver_weekly_trends_view")
    monthly_columns = _view_output_columns(sql, "eco_driver_monthly_trends_view")
    assert weekly_columns[: len(WEEKLY_030_COLUMNS)] == WEEKLY_030_COLUMNS
    assert weekly_columns[len(WEEKLY_030_COLUMNS) : -1] == VALIDATION_COLUMNS
    assert weekly_columns[-1] == "ecodriving_rating_type_share_percent"
    assert monthly_columns[: len(MONTHLY_030_COLUMNS)] == MONTHLY_030_COLUMNS
    assert monthly_columns[len(MONTHLY_030_COLUMNS) : -1] == VALIDATION_COLUMNS
    assert monthly_columns[-1] == "ecodriving_rating_type_share_percent"
    print("PASS: 034 view replacements preserve prior column order and append rating-type share")


def test_expected_weekly_trend_semantics() -> None:
    rows = _trend_rows(
        [
            {
                "period_label": "2026-05-W1",
                "score": Decimal("0"),
                "kilometers": Decimal("10"),
                "ranking_position": 5,
                "ranking_group": "INCLUDED",
            },
            {
                "period_label": "2026-05-W2",
                "score": Decimal("20"),
                "kilometers": Decimal("30"),
                "ranking_position": 3,
                "ranking_group": "INCLUDED",
            },
            {
                "period_label": "2026-05-W3",
                "score": None,
                "kilometers": Decimal("60"),
                "ranking_position": 5,
                "ranking_group": "INCLUDED",
            },
            {
                "period_label": "2026-05-W5",
                "score": Decimal("40"),
                "kilometers": Decimal("100"),
                "ranking_position": 4,
                "ranking_group": "INCLUDED",
            },
            {
                "period_label": "2026-06-W1",
                "score": Decimal("5"),
                "kilometers": Decimal("5"),
                "ranking_position": None,
                "ranking_group": "UNKNOWN_DRIVER",
            },
        ]
    )

    assert rows[1]["previous_score"] == Decimal("0")
    assert rows[1]["score_delta_abs"] == Decimal("20")
    assert rows[1]["score_delta_pct"] is None
    assert rows[1]["kilometers_delta_abs"] == Decimal("20")
    assert rows[1]["ranking_position_delta"] == 2

    assert rows[2]["previous_score"] == Decimal("20")
    assert rows[2]["score_delta_abs"] is None
    assert rows[2]["kilometers_delta_abs"] == Decimal("30")
    assert rows[2]["ranking_position_delta"] == -2

    assert rows[3]["previous_score"] is None
    assert rows[3]["rolling_4_snapshot_avg_score"] == Decimal("20")
    assert rows[3]["kilometers_delta_abs"] == Decimal("40")
    assert rows[3]["ranking_position_delta"] == 1

    assert rows[4]["previous_score"] == Decimal("40")
    assert rows[4]["kilometers_delta_abs"] == Decimal("-95")
    assert rows[4]["ranking_group"] == "UNKNOWN_DRIVER"
    assert rows[4]["ranking_position_delta"] is None
    print("PASS: expected weekly trend semantics cover W1/W2/W3, reset, nulls, and ranks")


def test_expected_monthly_trend_semantics() -> None:
    rows = _trend_rows(
        [
            {
                "period_label": "2026-03",
                "score": Decimal("50"),
                "kilometers": Decimal("300"),
                "ranking_position": 3,
                "ranking_group": "INCLUDED",
            },
            {
                "period_label": "2026-04",
                "score": Decimal("60"),
                "kilometers": Decimal("450"),
                "ranking_position": 2,
                "ranking_group": "INCLUDED",
            },
            {
                "period_label": "2026-05",
                "score": Decimal("30"),
                "kilometers": Decimal("500"),
                "ranking_position": 4,
                "ranking_group": "INCLUDED",
            },
        ]
    )

    assert rows[1]["previous_score"] == Decimal("50")
    assert rows[1]["score_delta_abs"] == Decimal("10")
    assert rows[1]["kilometers_delta_abs"] == Decimal("150")
    assert rows[1]["ranking_position_delta"] == 1

    assert rows[2]["previous_score"] == Decimal("60")
    assert rows[2]["score_delta_abs"] == Decimal("-30")
    assert rows[2]["kilometers_delta_abs"] == Decimal("50")
    assert rows[2]["ranking_position_delta"] == -2
    assert _rolling_avg([row["score"] for row in rows[:3]]) == Decimal("46.66666666666666666666666667")
    print("PASS: expected monthly trend semantics cover previous month, rolling average, and ranks")


def main() -> None:
    test_views_declared_and_documented()
    test_weekly_view_columns_and_windowing()
    test_weekly_delta_formulas()
    test_monthly_view_columns_and_windowing()
    test_031_appends_validation_columns_without_reordering_030_views()
    test_034_appends_rating_type_share_without_reordering_031_views()
    test_expected_weekly_trend_semantics()
    test_expected_monthly_trend_semantics()
    print("OK - Eco Driving trend view checks passed")


if __name__ == "__main__":
    main()
