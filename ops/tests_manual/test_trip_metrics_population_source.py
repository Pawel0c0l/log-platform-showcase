#!/usr/bin/env python3
"""Manual sanity tests for trip_metrics_population_source helper and migration.

Run from repo root:

    PYTHONPATH="$PWD" python3 ops/tests_manual/test_trip_metrics_population_source.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs import trip_metrics_population_source as source  # noqa: E402


def test_helper_values() -> None:
    assert source.TRIP_METRICS_POPULATION_SOURCE_VALUES == (
        "api_migration",
        "report_207_migration",
        "d105_2_ecodriving_migration",
        "disabled",
    )
    assert source.TRIP_METRICS_POPULATION_SOURCE_DEFAULT == "api_migration"
    assert source.TRIP_METRICS_SOURCE_MISMATCH_REASON == "trip_metrics_population_source_mismatch"
    assert source.normalize_trip_metrics_population_source(None) == "api_migration"
    assert source.normalize_trip_metrics_population_source(" REPORT_207_MIGRATION ") == "report_207_migration"
    assert source.is_required_trip_metrics_source("api_migration", "api_migration") is True
    assert source.is_required_trip_metrics_source("disabled", "api_migration") is False
    ctx = source.trip_metrics_source_skip_context("disabled", "api_migration")
    assert ctx == {
        "trip_metrics_population_source": "disabled",
        "required_trip_metrics_population_source": "api_migration",
        "skip_reason": "trip_metrics_population_source_mismatch",
    }
    try:
        source.normalize_trip_metrics_population_source("bad")
    except ValueError as exc:
        assert "trip_metrics_population_source" in str(exc)
    else:
        raise AssertionError("expected invalid source to raise ValueError")
    print("PASS: helper constants, normalization, mismatch context")


def test_migration_sql_contract() -> None:
    sql = (REPO_ROOT / "db/migrations/040_workflow_a_trip_metrics_population_source.sql").read_text()
    assert "ADD COLUMN IF NOT EXISTS trip_metrics_population_source TEXT" in sql
    assert "SET trip_metrics_population_source = 'api_migration'" in sql
    assert "SET DEFAULT 'api_migration'" in sql
    assert "SET NOT NULL" in sql
    assert "CHECK (" in sql
    for value in source.TRIP_METRICS_POPULATION_SOURCE_VALUES:
        assert f"'{value}'" in sql, value
    assert "CREATE TYPE" not in sql.upper()
    print("PASS: migration adds TEXT selector with api_migration default and allowed-value check")


def main() -> None:
    test_helper_values()
    test_migration_sql_contract()
    print("OK - trip_metrics_population_source helper/migration checks passed")


if __name__ == "__main__":
    main()
