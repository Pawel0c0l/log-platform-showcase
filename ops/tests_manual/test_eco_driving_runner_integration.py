#!/usr/bin/env python3
"""Manual checks for Eco Driving runner/dispatcher integration.

Run:

    cd /opt/log-platform
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" python3 ops/tests_manual/test_eco_driving_runner_integration.py
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys
from zoneinfo import ZoneInfo


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.api.telematics import registry  # noqa: E402
from jobs.api.telematics.dispatcher import _build_job_params  # noqa: E402
from jobs.ecodriving.job_eco_driving_aggregate import (  # noqa: E402
    DATASET_NAME,
    MODE_EXPLICIT_PERIOD,
    MODE_FINAL_MONTH_WEEKLY_SNAPSHOT,
    MODE_MONTHLY_FULL_AGGREGATION,
    MODE_SELECTED_MONTH_FULL_REBUILD,
    MODE_WEEKLY_CUMULATIVE_SNAPSHOT,
    _include_flags,
    _resolve_periods,
    resolve_final_month_weekly_snapshot,
    resolve_previous_completed_month,
    resolve_previous_completed_weekly_snapshot,
)


ECO_JOB = "jobs.ecodriving.job_eco_driving_aggregate"
WARSAW = ZoneInfo("Europe/Warsaw")


def test_registry_registration() -> None:
    expected = {
        "eco_driving_weekly_snapshot",
        "eco_driving_month_end_weekly_snapshot",
        "eco_driving_monthly_aggregation",
    }
    for dataset_name in expected:
        spec = registry.DATASETS[dataset_name]
        assert spec.job_module == ECO_JOB
    assert DATASET_NAME == "eco_driving_aggregate"
    print("PASS: Eco Driving datasets are discoverable in the Workflow A registry")


def test_manual_params_are_accepted() -> None:
    month_start, periods, explicit, mode = _resolve_periods({"month": "2026-05"})
    assert month_start.isoformat() == "2026-05-01"
    assert mode == MODE_SELECTED_MONTH_FULL_REBUILD
    assert explicit is False
    assert periods[0].period_start_date.isoformat() == "2026-05-01"
    assert periods[-1].period_end_date.isoformat() == "2026-06-01"
    assert _include_flags({}, selected_mode=mode, explicit_period_mode=explicit) == (True, True)

    month_start, periods, explicit, mode = _resolve_periods({
        "period_start_date": "2026-05-01",
        "period_end_date": "2026-05-11",
    })
    assert month_start.isoformat() == "2026-05-01"
    assert explicit is True
    assert mode == MODE_EXPLICIT_PERIOD
    assert periods[0].period_start_date.isoformat() == "2026-05-01"
    assert periods[0].period_end_date.isoformat() == "2026-05-11"
    assert _include_flags({}, selected_mode=mode, explicit_period_mode=explicit) == (True, False)
    print("PASS: selected-month and explicit cumulative-period params are accepted")


def test_weekly_resolver_normal_monday_boundary() -> None:
    period = resolve_previous_completed_weekly_snapshot(
        datetime(2026, 5, 18, 3, 0, tzinfo=WARSAW)
    )
    assert period.period_start_date.isoformat() == "2026-05-01"
    assert period.period_end_date.isoformat() == "2026-05-18"
    assert period.period_start_date == period.month_start_date
    print("PASS: weekly resolver returns month_start -> last completed Monday")


def test_weekly_resolver_month_start_partial_boundary() -> None:
    period = resolve_previous_completed_weekly_snapshot(
        datetime(2026, 4, 6, 3, 0, tzinfo=WARSAW)
    )
    assert period.period_start_date.isoformat() == "2026-04-01"
    assert period.period_end_date.isoformat() == "2026-04-06"
    assert period.period_sequence_in_month == 1
    assert period.is_partial_period is True
    print("PASS: weekly resolver handles first partial snapshot inside a month")


def test_final_month_weekly_snapshot_resolver() -> None:
    period = resolve_final_month_weekly_snapshot(
        datetime(2026, 7, 1, 3, 30, tzinfo=WARSAW)
    )
    assert period.period_start_date.isoformat() == "2026-06-01"
    assert period.period_end_date.isoformat() == "2026-07-01"
    assert period.period_end_date == period.month_start_date.replace(month=7)
    print("PASS: month-end weekly resolver returns previous_month_start -> current_month_start")


def test_monthly_resolver() -> None:
    month_start, month_end = resolve_previous_completed_month(
        datetime(2026, 6, 1, 4, 0, tzinfo=WARSAW)
    )
    assert month_start.isoformat() == "2026-05-01"
    assert month_end.isoformat() == "2026-06-01"
    print("PASS: monthly resolver returns previous full calendar month")


def test_dispatcher_params_for_eco_schedules() -> None:
    fire = datetime(2026, 5, 18, 1, 0, tzinfo=timezone.utc)
    common = {
        "client_id": "00000000-0000-0000-0000-000000000001",
        "client_code": "TEST",
        "event_enrichment_mode": "enabled",
        "window_start_ts": fire,
        "window_end_ts": fire,
    }

    weekly = _build_job_params(dataset_name="eco_driving_weekly_snapshot", **common)
    assert weekly["mode"] == MODE_WEEKLY_CUMULATIVE_SNAPSHOT
    assert weekly["include_weekly"] is True
    assert weekly["include_monthly"] is False
    assert weekly["trigger"] == "SCHEDULED"
    assert weekly["client_code"] == "TEST"

    final_weekly = _build_job_params(dataset_name="eco_driving_month_end_weekly_snapshot", **common)
    assert final_weekly["mode"] == MODE_FINAL_MONTH_WEEKLY_SNAPSHOT
    assert final_weekly["include_weekly"] is True
    assert final_weekly["include_monthly"] is False

    monthly = _build_job_params(dataset_name="eco_driving_monthly_aggregation", **common)
    assert monthly["mode"] == MODE_MONTHLY_FULL_AGGREGATION
    assert monthly["include_weekly"] is False
    assert monthly["include_monthly"] is True
    print("PASS: dispatcher passes Eco mode and include flags through existing runner params")


def test_static_logging_and_scheduler_contracts() -> None:
    job_src = (REPO_ROOT / "jobs" / "ecodriving" / "job_eco_driving_aggregate.py").read_text()
    dispatcher_src = (REPO_ROOT / "jobs" / "api" / "telematics" / "dispatcher.py").read_text()
    migration_src = (REPO_ROOT / "db" / "migrations" / "032_workflow_a_eco_driving_registry.sql").read_text()

    for key in (
        '"job_name"',
        '"selected_mode"',
        '"business_timezone"',
        '"source_trips_seen"',
        '"assignments_upserted"',
        '"private_excluded_count"',
        '"weekly_rows_upserted"',
        '"monthly_rows_upserted"',
        '"unknown_driver_rows"',
    ):
        assert key in job_src, key

    assert "ECO_DRIVING_SCHEDULE_MODES" in dispatcher_src
    assert "client_dataset_schedule" in migration_src
    assert "Europe/Warsaw" in migration_src
    assert "03:00" in migration_src
    assert "03:30" in migration_src
    assert "04:00" in migration_src
    print("PASS: logging payload and scheduler registration use existing dispatcher conventions")


def main() -> None:
    test_registry_registration()
    test_manual_params_are_accepted()
    test_weekly_resolver_normal_monday_boundary()
    test_weekly_resolver_month_start_partial_boundary()
    test_final_month_weekly_snapshot_resolver()
    test_monthly_resolver()
    test_dispatcher_params_for_eco_schedules()
    test_static_logging_and_scheduler_contracts()
    print("OK - Eco Driving runner integration checks passed")


if __name__ == "__main__":
    main()
