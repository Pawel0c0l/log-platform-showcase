#!/usr/bin/env python3
"""Static checks for Eco Driving client-business schema migration.

Run:

    cd /opt/log-platform
    PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_eco_driving_schema.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

MIGRATION_PATH = REPO_ROOT / "db" / "client_business" / "027_eco_driving_schema.sql"
ADJUSTMENT_PATH = REPO_ROOT / "db" / "client_business" / "028_eco_driving_periods_and_driver_chart.sql"
NULLABLE_SCORES_PATH = REPO_ROOT / "db" / "client_business" / "029_eco_driving_nullable_scores.sql"
QUALIFIED_ONLY_RANKING_PATH = (
    REPO_ROOT / "db" / "client_business" / "046_eco_ranking_qualified_only.sql"
)
TREND_VIEWS_PATH = REPO_ROOT / "db" / "client_business" / "030_eco_driving_trend_views.sql"
VALIDATION_FIELDS_PATH = REPO_ROOT / "db" / "client_business" / "031_eco_driving_validation_fields.sql"
RATING_SHARE_PATH = REPO_ROOT / "db" / "client_business" / "034_eco_driving_rating_type_share_percent.sql"
ONBOARDING_PATH = REPO_ROOT / "scripts" / "onboard_workflow_a_client.py"

ASSIGNMENT_COLUMNS = [
    "client_id",
    "client_code",
    "provider_trip_id",
    "record_id",
    "assigned_id",
    "assignment_source",
    "driver_restrictions_raw",
    "dysponent_id_raw",
    "trip_start_ts",
    "trip_end_ts",
    "business_week_start_date",
    "business_week_end_date",
    "trip_distance_meters",
    "overrev_events_count",
    "harsh_braking_events",
    "harsh_acceleration_events",
    "harsh_turning_events",
    "idle_events",
    "speeding_140_160_count",
    "speeding_160_170_count",
    "speeding_170_plus_count",
    "created_at",
    "updated_at",
]

STATS_COLUMNS = [
    "client_id",
    "client_code",
    "assigned_id",
    "trips_count",
    "source_trips_count",
    "skipped_trips_count",
    "total_distance_meters",
    "total_kilometers",
    "overrev_events_count",
    "harsh_braking_events",
    "harsh_acceleration_events",
    "harsh_turning_events",
    "idle_events",
    "speeding_140_160_count",
    "speeding_160_170_count",
    "speeding_170_plus_count",
    "overrev_events_per_100km",
    "harsh_braking_events_per_100km",
    "harsh_acceleration_events_per_100km",
    "harsh_turning_events_per_100km",
    "idle_events_per_100km",
    "speeding_140_160_events_per_100km",
    "speeding_160_170_events_per_100km",
    "speeding_170_plus_events_per_100km",
    "overrev_points",
    "harsh_braking_points",
    "harsh_acceleration_points",
    "harsh_turning_points",
    "idle_points",
    "speeding_140_160_points",
    "speeding_160_170_points",
    "speeding_170_plus_points",
    "eco_driving_score_total",
    "qualification_status",
    "calculation_status",
    "created_at",
    "updated_at",
]

DRIVER_CHART_COLUMNS = [
    "client_id",
    "driver_id",
    "driver_name",
    "email",
    "ranking_included",
    "is_active",
    "metadata_json",
    "created_at",
    "updated_at",
]

PRIVATE_AUDIT_COLUMNS = [
    "driver_tag_description",
    "is_private_trip",
    "exclusion_reason",
    "aggregation_included",
]

WEEKLY_PERIOD_COLUMNS = [
    "period_start_date",
    "period_end_date",
    "month_start_date",
    "period_sequence_in_month",
    "period_label",
    "is_partial_period",
    "ranking_included",
    "ranking_group",
    "ranking_position",
    "ranking_total_participants",
]

MONTHLY_RANKING_COLUMNS = [
    "ranking_included",
    "ranking_group",
    "ranking_position",
    "ranking_total_participants",
]

DERIVED_VALIDATION_COLUMNS = [
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


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _table_body(sql: str, table_name: str) -> str:
    match = re.search(
        rf"CREATE TABLE IF NOT EXISTS public\.{re.escape(table_name)} \((.*?)\n\);",
        sql,
        flags=re.S,
    )
    if not match:
        raise AssertionError(f"table DDL not found: {table_name}")
    return match.group(1)


def _assert_columns(body: str, columns: list[str], *, table_name: str) -> None:
    for column in columns:
        assert re.search(rf"^\s*{re.escape(column)}\s+", body, flags=re.M), (
            f"{table_name} missing column {column}"
        )


def test_tables_and_columns() -> None:
    sql = _read(MIGRATION_PATH)
    assignment = _table_body(sql, "eco_trip_assignments")
    weekly = _table_body(sql, "eco_driver_weekly_stats")
    monthly = _table_body(sql, "eco_driver_monthly_stats")

    _assert_columns(assignment, ASSIGNMENT_COLUMNS, table_name="eco_trip_assignments")
    _assert_columns(weekly, ["week_start_date", "week_end_date", *STATS_COLUMNS], table_name="eco_driver_weekly_stats")
    _assert_columns(monthly, ["month_start_date", "month_end_date", *STATS_COLUMNS], table_name="eco_driver_monthly_stats")
    print("PASS: Eco Driving migration declares assignment, weekly, and monthly table columns")


def test_assignment_priority_values_and_audit_constraints() -> None:
    sql = _read(MIGRATION_PATH)
    assert "DRIVER_RESTRICTIONS" in sql
    assert "DYSPONENT_ID" in sql
    assert "SKIPPED_NO_ID" in sql
    assert "assignment_source = 'SKIPPED_NO_ID' AND assigned_id IS NULL" in sql
    assert "driver_restrictions_raw" in sql
    assert "dysponent_id_raw" in sql
    print("PASS: assignment source values and skipped-trip audit shape are constrained")


def test_unique_keys_and_indexes() -> None:
    sql = _read(MIGRATION_PATH)
    assert "PRIMARY KEY (client_id, provider_trip_id)" in sql
    assert "PRIMARY KEY (assigned_id, week_start_date)" in sql
    assert "PRIMARY KEY (assigned_id, month_start_date)" in sql

    expected_indexes = [
        "idx_eco_trip_assignments_assigned_id",
        "idx_eco_trip_assignments_trip_start_ts",
        "idx_eco_trip_assignments_assignment_source",
        "idx_eco_trip_assignments_business_week_start",
        "idx_eco_driver_weekly_stats_assigned_id",
        "idx_eco_driver_weekly_stats_week_start",
        "idx_eco_driver_monthly_stats_assigned_id",
        "idx_eco_driver_monthly_stats_month_start",
    ]
    for index_name in expected_indexes:
        assert index_name in sql, index_name
    print("PASS: Eco Driving migration has idempotent unique keys and practical indexes")


def test_business_week_and_status_constraints() -> None:
    sql = _read(MIGRATION_PATH)
    assert "business_week_end_date = business_week_start_date + 7" in sql
    assert "EXTRACT(ISODOW FROM business_week_start_date) = 1" in sql
    assert "month_end_date = (month_start_date + INTERVAL '1 month')::date" in sql
    assert "QUALIFIED" in sql and "LOW_DISTANCE" in sql and "NO_DISTANCE" in sql
    assert "calculation_status IN ('OK', 'NO_ASSIGNED_ID', 'NO_DISTANCE', 'ERROR')" in sql
    print("PASS: base business period and status constraints exist")


def test_driver_chart_table_and_compatibility_view() -> None:
    sql = _read(ADJUSTMENT_PATH)
    driver_chart = _table_body(sql, "eco_drivers_id_chart")
    _assert_columns(driver_chart, DRIVER_CHART_COLUMNS, table_name="eco_drivers_id_chart")
    assert "PRIMARY KEY (client_id, driver_id)" in driver_chart
    assert "CREATE OR REPLACE VIEW public.\"Eco_Drivers_ID_Chart\"" in sql
    assert "idx_eco_drivers_id_chart_ranking_included" in sql
    assert "idx_eco_drivers_id_chart_is_active" in sql
    assert "idx_eco_drivers_id_chart_email" in sql
    print("PASS: Eco_Drivers_ID_Chart physical table and compatibility view are defined")


def test_private_trip_audit_columns() -> None:
    sql = _read(ADJUSTMENT_PATH)
    for column in PRIVATE_AUDIT_COLUMNS:
        assert f"ADD COLUMN IF NOT EXISTS {column}" in sql, column
    assert "PRIVATE_DRIVER_TAG" in sql
    assert "is_private_trip IS NOT TRUE" in sql
    assert "aggregation_included IS FALSE" in sql
    print("PASS: private-trip exclusion audit columns and constraints are present")


def test_weekly_stats_support_month_bounded_partial_periods() -> None:
    sql = _read(ADJUSTMENT_PATH)
    for column in WEEKLY_PERIOD_COLUMNS:
        assert f"ADD COLUMN IF NOT EXISTS {column}" in sql, column
    assert "DROP CONSTRAINT IF EXISTS chk_eco_driver_weekly_stats_week" in sql
    assert "PRIMARY KEY (client_id, assigned_id, period_start_date, period_end_date)" in sql
    assert "UNIQUE (client_id, assigned_id, period_start_date, period_end_date)" in sql
    assert "period_end_date > period_start_date" in sql
    assert "period_end_date <= (month_start_date + INTERVAL '1 month')::date" in sql
    assert "ranking_group IN ('INCLUDED', 'EXCLUDED', 'UNKNOWN_DRIVER')" in sql
    print("PASS: weekly stats can represent month-bounded partial ranking periods")


def test_monthly_stats_support_full_month_rankings() -> None:
    sql = _read(ADJUSTMENT_PATH)
    for column in MONTHLY_RANKING_COLUMNS:
        assert f"ADD COLUMN IF NOT EXISTS {column}" in sql, column
    assert "PRIMARY KEY (client_id, assigned_id, month_start_date)" in sql
    assert "UNIQUE (client_id, assigned_id, month_start_date)" in sql
    assert "ranking_group IN ('INCLUDED', 'EXCLUDED', 'UNKNOWN_DRIVER')" in sql
    print("PASS: monthly stats include ranking metadata for full calendar months")


def test_score_columns_allow_null_for_no_distance() -> None:
    sql = _read(NULLABLE_SCORES_PATH)
    for table_name in ("eco_driver_weekly_stats", "eco_driver_monthly_stats"):
        assert f"ALTER TABLE IF EXISTS public.{table_name}" in sql
    for column in [
        "overrev_points",
        "harsh_braking_points",
        "harsh_acceleration_points",
        "harsh_turning_points",
        "idle_points",
        "speeding_140_160_points",
        "speeding_160_170_points",
        "speeding_170_plus_points",
        "eco_driving_score_total",
    ]:
        assert f"ALTER COLUMN {column} DROP NOT NULL" in sql, column
    print("PASS: score and point columns allow NULL for no-distance scoring")


def test_validation_fields_migration_adds_weekly_monthly_columns_and_rating_check() -> None:
    sql = _read(VALIDATION_FIELDS_PATH)
    for table_name in ("eco_driver_weekly_stats", "eco_driver_monthly_stats"):
        assert f"ALTER TABLE IF EXISTS public.{table_name}" in sql
        for column in DERIVED_VALIDATION_COLUMNS:
            assert f"ADD COLUMN IF NOT EXISTS {column}" in sql, (table_name, column)
    assert "EcoDriving_rating_type" not in sql
    assert "ecodriving_rating_type IN ('bezpieczny', 'akceptowalny', 'niebezpieczny')" in sql
    print("PASS: derived validation/rating migration adds nullable weekly and monthly columns")


def test_rating_type_share_migration_adds_weekly_monthly_columns() -> None:
    sql = _read(RATING_SHARE_PATH)
    assert "ALTER TABLE IF EXISTS public.eco_driver_weekly_stats" in sql
    assert "ALTER TABLE IF EXISTS public.eco_driver_monthly_stats" in sql
    assert "ADD COLUMN IF NOT EXISTS ecodriving_rating_type_share_percent NUMERIC(7, 2) NULL" in sql
    assert "COMMENT ON COLUMN public.eco_driver_weekly_stats.ecodriving_rating_type_share_percent" in sql
    assert "COMMENT ON COLUMN public.eco_driver_monthly_stats.ecodriving_rating_type_share_percent" in sql
    print("PASS: rating-type share migration adds weekly and monthly numeric reporting columns")


def test_trend_views_exist_for_weekly_and_monthly_stats() -> None:
    sql = _read(TREND_VIEWS_PATH)
    assert "CREATE OR REPLACE VIEW public.eco_driver_weekly_trends_view" in sql
    assert "CREATE OR REPLACE VIEW public.eco_driver_monthly_trends_view" in sql
    assert "FROM public.eco_driver_weekly_stats s" in sql
    assert "FROM public.eco_driver_monthly_stats s" in sql
    assert "previous_snapshot_score" in sql
    assert "previous_month_score" in sql
    assert "ranking_position_delta" in sql

    validation_sql = _read(VALIDATION_FIELDS_PATH)
    assert "CREATE OR REPLACE VIEW public.eco_driver_weekly_trends_view" in validation_sql
    assert "CREATE OR REPLACE VIEW public.eco_driver_monthly_trends_view" in validation_sql
    for column in DERIVED_VALIDATION_COLUMNS:
        assert column in validation_sql, column

    share_sql = _read(RATING_SHARE_PATH)
    assert "CREATE OR REPLACE VIEW public.eco_driver_weekly_trends_view" in share_sql
    assert "CREATE OR REPLACE VIEW public.eco_driver_monthly_trends_view" in share_sql
    assert "ecodriving_rating_type_share_percent" in share_sql
    print("PASS: trend views expose weekly cumulative, monthly progress, validation fields, and rating-type share")


def test_onboarding_applies_new_schema() -> None:
    onboarding = _read(ONBOARDING_PATH)
    assert "027_eco_driving_schema.sql" in onboarding
    assert "028_eco_driving_periods_and_driver_chart.sql" in onboarding
    assert "029_eco_driving_nullable_scores.sql" in onboarding
    assert "030_eco_driving_trend_views.sql" in onboarding
    assert "031_eco_driving_validation_fields.sql" in onboarding
    assert "034_eco_driving_rating_type_share_percent.sql" in onboarding
    print("PASS: new-client onboarding includes Eco Driving client-business migrations")


def test_ranking_group_is_nullable_for_non_qualified_rows() -> None:
    """046 lets `ranking_group` hold NULL: outside every ranking population."""

    sql = _read(QUALIFIED_ONLY_RANKING_PATH)
    for table_name in (
        "eco_driver_weekly_stats",
        "eco_driver_monthly_stats",
        "eco_person_weekly_stats",
        "eco_person_monthly_stats",
    ):
        assert f"'{table_name}'" in sql, table_name
    assert "ALTER COLUMN ranking_group DROP NOT NULL" in sql
    assert "ALTER COLUMN ranking_group DROP DEFAULT" in sql
    assert "ranking_group IS NULL " in sql
    assert "OR ranking_group IN (''INCLUDED'', ''EXCLUDED'', ''UNKNOWN_DRIVER'')" in sql
    # A row outside every ranking group can never carry ranking coordinates.
    # Satisfied by every pre-existing row, so it is added VALID.
    assert "ranking_group_position_coherence" in sql
    assert "ranking_position IS NULL AND ranking_total_participants IS NULL" in sql
    # The new business contract itself. Historical rows violate it, so it must be
    # NOT VALID and must never silently downgrade a completed validation.
    assert "ranking_requires_qualified" in sql
    # Biconditional: ranked if and only if QUALIFIED. Both directions matter.
    assert "(qualification_status = ''QUALIFIED'') = (ranking_group IS NOT NULL)" in sql
    assert "NOT VALID" in sql
    assert "convalidated" in sql
    # Historical rows are never rewritten by this migration.
    assert "UPDATE public." not in sql
    print("PASS: 046 makes ranking_group nullable and constrains ranking to QUALIFIED")


def main() -> None:
    test_tables_and_columns()
    test_assignment_priority_values_and_audit_constraints()
    test_unique_keys_and_indexes()
    test_business_week_and_status_constraints()
    test_driver_chart_table_and_compatibility_view()
    test_private_trip_audit_columns()
    test_weekly_stats_support_month_bounded_partial_periods()
    test_monthly_stats_support_full_month_rankings()
    test_score_columns_allow_null_for_no_distance()
    test_ranking_group_is_nullable_for_non_qualified_rows()
    test_validation_fields_migration_adds_weekly_monthly_columns_and_rating_check()
    test_rating_type_share_migration_adds_weekly_monthly_columns()
    test_trend_views_exist_for_weekly_and_monthly_stats()
    test_onboarding_applies_new_schema()
    print("OK - Eco Driving schema checks passed")


if __name__ == "__main__":
    main()
