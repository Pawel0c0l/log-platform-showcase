#!/usr/bin/env python3
"""Focused tests for the read-only Telematics coverage bootstrap audit (C10).

Pure checks always run. Set TELEMATICS_BOOTSTRAP_AUDIT_TEST_DSN only to a
*disposable* PostgreSQL 16 database — never logdb — for resolution, read-only
enforcement and inventory checks:

  docker run -d --rm --name c10-bootstrap-pg -e POSTGRES_PASSWORD=... \\
      -e POSTGRES_USER=loguser -e POSTGRES_DB=c10_bootstrap_test \\
      -p 55707:5432 postgres:16
  TELEMATICS_BOOTSTRAP_AUDIT_TEST_DSN='postgresql://loguser:...@127.0.0.1:55707/c10_bootstrap_test' \\
      .venv/bin/python ops/tests_manual/test_telematics_coverage_bootstrap_audit.py

The suite proves the two properties the tool's trustworthiness rests on: it
writes nothing, and it recommends nothing.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from argparse import Namespace
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ops.tests_manual.postgres_dsn_safety import (  # noqa: E402
    require_loopback_dsn_or_exit,
)
from ops import audit_telematics_coverage_bootstrap as audit  # noqa: E402

MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"
PREREQUISITE_MIGRATIONS = (
    "008_workflow_a_control_plane.sql",
    "010_add_client_code.sql",
    "011_workflow_a_dataset_registry.sql",
    "012_workflow_a_client_dataset_schedule.sql",
    "013_workflow_a_client_table_retention.sql",
    "014_workflow_a_dispatcher_v1.sql",
    "017_workflow_a_add_client_code_to_control_tables.sql",
    "018_workflow_a_schedule_event_enrichment_mode.sql",
    "042_platform_environment_identity.sql",
    "055_workflow_a_trips_pagination_mode.sql",
    "056_workflow_a_trips_stabilization_config.sql",
    "057_workflow_a_trips_coverage_state.sql",
    "058_telematics_trips_manual_recovery.sql",
    "062_workflow_a_multi_cadence_schedule_identity.sql",
)

CLIENT_ID = "bd7662a5-eeb4-4614-8720-d477abfcb227"
CLIENT_CODE = "TST00001"
SCHEDULE_TRIPS = "7cac378a-5787-4d62-85d1-282bed208c8c"
SCHEDULE_FUEL = "9c9c9261-2cf7-4b6f-8955-b515814be2f7"
PLATFORM_UUID = "52517750-7438-4558-8490-2736ae4cc629"
ENVIRONMENT = "production"

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Pure — fire enumeration
# ---------------------------------------------------------------------------

def test_daily_enumeration_utc() -> None:
    fires = audit.enumerate_expected_fires(
        frequency="daily",
        day_of_week=None,
        day_of_month=None,
        day_of_month_last=False,
        run_time=time(2, 0, 0),
        timezone_name="UTC",
        range_start_utc=datetime(2026, 7, 28, 0, 0, tzinfo=UTC),
        range_end_utc=datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
    )
    assert fires == [
        datetime(2026, 7, 28, 2, 0, tzinfo=UTC),
        datetime(2026, 7, 29, 2, 0, tzinfo=UTC),
        datetime(2026, 7, 30, 2, 0, tzinfo=UTC),
        datetime(2026, 7, 31, 2, 0, tzinfo=UTC),
        datetime(2026, 8, 1, 2, 0, tzinfo=UTC),
    ]


def test_weekly_enumeration_warsaw() -> None:
    """BRAVO00016's real cadence shape: Monday 02:00 Europe/Warsaw."""
    fires = audit.enumerate_expected_fires(
        frequency="weekly",
        day_of_week=0,
        day_of_month=None,
        day_of_month_last=False,
        run_time=time(2, 0, 0),
        timezone_name="Europe/Warsaw",
        range_start_utc=datetime(2026, 7, 1, tzinfo=UTC),
        range_end_utc=datetime(2026, 8, 2, tzinfo=UTC),
    )
    # Summer time: 02:00 Warsaw is 00:00 UTC.
    assert fires == [
        datetime(2026, 7, 6, 0, 0, tzinfo=UTC),
        datetime(2026, 7, 13, 0, 0, tzinfo=UTC),
        datetime(2026, 7, 20, 0, 0, tzinfo=UTC),
        datetime(2026, 7, 27, 0, 0, tzinfo=UTC),
    ]
    assert all(fire.weekday() == 0 for fire in fires)


def test_enumeration_crosses_both_dst_transitions() -> None:
    """The absolute instant must move with the local wall clock, not drift."""
    spring = audit.enumerate_expected_fires(
        frequency="daily", day_of_week=None, day_of_month=None,
        day_of_month_last=False, run_time=time(2, 0, 0),
        timezone_name="Europe/Warsaw",
        range_start_utc=datetime(2026, 3, 28, tzinfo=UTC),
        range_end_utc=datetime(2026, 3, 31, tzinfo=UTC),
    )
    offsets = {fire.date().isoformat(): fire.hour for fire in spring}
    # Before the change 02:00 Warsaw == 01:00 UTC; after it == 00:00 UTC.
    assert offsets["2026-03-28"] == 1
    assert offsets["2026-03-30"] == 0

    autumn = audit.enumerate_expected_fires(
        frequency="daily", day_of_week=None, day_of_month=None,
        day_of_month_last=False, run_time=time(2, 0, 0),
        timezone_name="Europe/Warsaw",
        range_start_utc=datetime(2026, 10, 23, tzinfo=UTC),
        range_end_utc=datetime(2026, 10, 28, tzinfo=UTC),
    )
    autumn_offsets = {fire.date().isoformat(): fire.hour for fire in autumn}
    assert autumn_offsets["2026-10-23"] == 0
    assert autumn_offsets["2026-10-27"] == 1


def test_monthly_enumeration_including_last_day() -> None:
    fixed_day = audit.enumerate_expected_fires(
        frequency="monthly", day_of_week=None, day_of_month=15,
        day_of_month_last=False, run_time=time(3, 0, 0), timezone_name="UTC",
        range_start_utc=datetime(2026, 1, 1, tzinfo=UTC),
        range_end_utc=datetime(2026, 4, 1, tzinfo=UTC),
    )
    assert [fire.date().isoformat() for fire in fixed_day] == [
        "2026-01-15", "2026-02-15", "2026-03-15",
    ]

    last_day = audit.enumerate_expected_fires(
        frequency="monthly", day_of_week=None, day_of_month=None,
        day_of_month_last=True, run_time=time(3, 0, 0), timezone_name="UTC",
        range_start_utc=datetime(2026, 1, 1, tzinfo=UTC),
        range_end_utc=datetime(2026, 4, 1, tzinfo=UTC),
    )
    assert [fire.date().isoformat() for fire in last_day] == [
        "2026-01-31", "2026-02-28", "2026-03-31",
    ]


def test_unsupported_cadence_is_a_visible_stop() -> None:
    for kwargs in (
        {"frequency": "hourly"},
        {"frequency": "weekly", "day_of_week": None},
    ):
        base = {
            "frequency": "daily", "day_of_week": None, "day_of_month": None,
            "day_of_month_last": False, "run_time": time(2, 0),
            "timezone_name": "UTC",
            "range_start_utc": datetime(2026, 7, 1, tzinfo=UTC),
            "range_end_utc": datetime(2026, 7, 5, tzinfo=UTC),
        }
        base.update(kwargs)
        if base["frequency"] == "weekly":
            # A weekly schedule without a weekday matches nothing rather than
            # silently firing daily.
            assert audit.enumerate_expected_fires(**base) == []
        else:
            try:
                audit.enumerate_expected_fires(**base)
            except ValueError:
                continue
            raise AssertionError("unsupported cadence was accepted")


# ---------------------------------------------------------------------------
# Pure — canonical serialization
# ---------------------------------------------------------------------------

def test_canonical_json_is_order_independent() -> None:
    a = {"b": 1, "a": {"d": 2, "c": [3, 4]}}
    b = {"a": {"c": [3, 4], "d": 2}, "b": 1}
    assert audit.canonical_json(a) == audit.canonical_json(b)
    assert audit.canonical_json(a) == '{"a":{"c":[3,4],"d":2},"b":1}'


def test_bundle_hash_excludes_itself_and_detects_change() -> None:
    bundle = {"x": 1, "y": "z", "bundle_sha256": "0" * 64}
    digest = audit.bundle_sha256(bundle)
    assert audit.bundle_sha256({**bundle, "bundle_sha256": "f" * 64}) == digest
    assert audit.bundle_sha256({**bundle, "x": 2}) != digest


# ---------------------------------------------------------------------------
# Pure — bundle assembly
# ---------------------------------------------------------------------------

def _client(mode: str = "strict_meta") -> dict:
    return {
        "client_id": CLIENT_ID,
        "client_code": CLIENT_CODE,
        "trips_pagination_mode": mode,
        "trips_stabilization_delay_seconds": 10800,
        "trips_overlap_seconds": 3600,
        "trips_max_recovery_span_seconds": 2678400,
    }


def _schedule() -> dict:
    return {
        "schedule_id": SCHEDULE_TRIPS,
        "client_id": CLIENT_ID,
        "client_code": CLIENT_CODE,
        "dataset_name": "trips_sync",
        "enabled": True,
        "frequency": "daily",
        "day_of_week": None,
        "day_of_month": None,
        "day_of_month_last": False,
        "run_time": time(2, 0, 0),
        "timezone": "UTC",
        "lookback_days": 1,
        "overwrite_existing": True,
        "event_enrichment_mode": "enabled",
    }


def _history_row(day: int, status: str, **overrides) -> dict:
    fire = datetime(2026, 7, day, 2, 0, tzinfo=UTC)
    row = {
        "scheduled_fire_ts": fire,
        "status": status,
        "window_start_ts": fire - timedelta(days=1),
        "window_end_ts": fire,
        "nominal_window_start_ts": None,
        "nominal_window_end_ts": None,
        "stabilization_delay_seconds": None,
        "overlap_seconds": None,
        "trips_pagination_mode": None,
        "error_summary": None,
        "platform_run_id": None,
        "started_at": None,
        "finished_at": fire,
        "created_at": fire,
    }
    row.update(overrides)
    return row


def _build(history, existing_coverage=None, *, end_day: int = 31) -> dict:
    return audit.build_bundle(
        environment_name=ENVIRONMENT,
        platform_uuid=PLATFORM_UUID,
        repo_head="0" * 40,
        generated_at_utc=datetime(2026, 8, 2, 12, 0, tzinfo=UTC),
        client=_client(),
        schedule=_schedule(),
        history=history,
        existing_coverage=existing_coverage,
        range_start_utc=datetime(2026, 7, 27, 0, 0, tzinfo=UTC),
        range_end_utc=datetime(2026, 7, end_day, 12, 0, tzinfo=UTC),
        migration_ceiling=audit.MIGRATION_CEILING,
    )


def test_complete_inventory_classification() -> None:
    history = [_history_row(day, "SUCCESS") for day in (27, 28, 29, 30, 31)]
    bundle = _build(history)
    assert bundle["audit_classification"] == audit.CLASSIFICATION_COMPLETE
    assert len(bundle["successful_intervals"]) == 5
    assert bundle["missing_or_unproven_intervals"] == []
    assert bundle["failed_runs"] == []


def test_missing_fire_is_inventoried_as_unresolved() -> None:
    history = [_history_row(day, "SUCCESS") for day in (27, 28, 29)]
    bundle = _build(history)
    assert bundle["audit_classification"] == \
        audit.CLASSIFICATION_UNRESOLVED_GAPS
    kinds = {
        entry["scheduled_fire_ts"]: entry["kind"]
        for entry in bundle["missing_or_unproven_intervals"]
    }
    assert kinds["2026-07-30T02:00:00Z"] == "MISSING_FIRE"
    assert kinds["2026-07-31T02:00:00Z"] == "MISSING_FIRE"
    missing = next(
        entry for entry in bundle["missing_or_unproven_intervals"]
        if entry["scheduled_fire_ts"] == "2026-07-30T02:00:00Z"
    )
    # The interval a missing fire would have covered is its nominal window.
    assert missing["interval_start_ts"] == "2026-07-29T02:00:00Z"
    assert missing["interval_end_ts"] == "2026-07-30T02:00:00Z"


def test_failed_run_never_counts_as_coverage() -> None:
    history = [_history_row(day, "SUCCESS") for day in (27, 28, 29, 30)]
    history.append(_history_row(31, "FAILED", error_summary="PAGINATION_MISMATCH"))
    bundle = _build(history)
    assert bundle["audit_classification"] == \
        audit.CLASSIFICATION_UNRESOLVED_GAPS
    assert [entry["scheduled_fire_ts"] for entry in bundle["failed_runs"]] == [
        "2026-07-31T02:00:00Z"
    ]
    assert not any(
        entry["scheduled_fire_ts"] == "2026-07-31T02:00:00Z"
        for entry in bundle["successful_intervals"]
    )
    unresolved = next(
        entry for entry in bundle["missing_or_unproven_intervals"]
        if entry["scheduled_fire_ts"] == "2026-07-31T02:00:00Z"
    )
    assert unresolved["kind"] == "TERMINAL_FAILED_FIRE"


def test_non_terminal_fire_is_unresolved() -> None:
    history = [_history_row(day, "SUCCESS") for day in (27, 28, 29, 30)]
    history.append(_history_row(31, "RUNNING"))
    bundle = _build(history)
    unresolved = next(
        entry for entry in bundle["missing_or_unproven_intervals"]
        if entry["scheduled_fire_ts"] == "2026-07-31T02:00:00Z"
    )
    assert unresolved["kind"] == "NON_TERMINAL_FIRE"
    assert bundle["failed_runs"] == []


def test_existing_coverage_dominates_classification() -> None:
    history = [_history_row(day, "SUCCESS") for day in (27, 28, 29, 30, 31)]
    coverage = {
        "schedule_id": SCHEDULE_TRIPS,
        "client_id": CLIENT_ID,
        "client_code": CLIENT_CODE,
        "dataset_name": "trips_sync",
        "coverage_start_ts": datetime(2026, 7, 27, tzinfo=UTC),
        "covered_through_ts": datetime(2026, 7, 31, tzinfo=UTC),
        "bootstrap_status": "READY",
        "bootstrap_evidence_ref": "secret-looking-bundle-ref",
        "seeded_at": datetime(2026, 8, 1, tzinfo=UTC),
        "seeded_by": "operator@example.invalid",
        "covered_through_source": "bootstrap",
        "last_gap_detected_ts": None,
        "updated_at": datetime(2026, 8, 1, tzinfo=UTC),
    }
    bundle = _build(history, coverage)
    assert bundle["audit_classification"] == \
        audit.CLASSIFICATION_EXISTING_COVERAGE
    # The reference itself is reported only as a presence bit.
    assert bundle["existing_coverage"]["bootstrap_evidence_ref_present"] is True
    assert "secret-looking-bundle-ref" not in audit.canonical_json(bundle)


def test_empty_history_is_insufficient_evidence() -> None:
    bundle = _build([])
    assert bundle["audit_classification"] == \
        audit.CLASSIFICATION_INSUFFICIENT_HISTORY


def test_bundle_recommends_nothing() -> None:
    bundle = _build([_history_row(day, "SUCCESS") for day in (27, 28, 29)])
    contract = bundle["decision_contract"]
    assert contract["coverage_start_ts_recommended"] is False
    assert contract["covered_through_ts_recommended"] is False
    text = audit.canonical_json(bundle)
    # No top-level proposal of a bound, and no READY verdict anywhere.
    assert "recommended_coverage_start_ts" not in text
    assert "recommended_covered_through_ts" not in text
    assert "suggested" not in text.lower()
    assert "READY" not in text
    for forbidden in ("coverage_start_ts", "covered_through_ts"):
        assert forbidden not in bundle


def test_focus_dates_are_inventoried_without_a_decision() -> None:
    history = [_history_row(day, "SUCCESS") for day in (27, 28, 29)]
    history.append(_history_row(1, "FAILED", error_summary="PAGINATION_MISMATCH"))
    history[-1]["scheduled_fire_ts"] = datetime(2026, 8, 1, 2, 0, tzinfo=UTC)
    bundle = audit.build_bundle(
        environment_name=ENVIRONMENT, platform_uuid=PLATFORM_UUID,
        repo_head="0" * 40,
        generated_at_utc=datetime(2026, 8, 2, 12, 0, tzinfo=UTC),
        client=_client(), schedule=_schedule(), history=history,
        existing_coverage=None,
        range_start_utc=datetime(2026, 7, 27, tzinfo=UTC),
        range_end_utc=datetime(2026, 8, 2, tzinfo=UTC),
        migration_ceiling=audit.MIGRATION_CEILING,
    )
    focus = {entry["date"]: entry for entry in bundle["historical_focus_inventory"]}
    assert set(focus) == set(audit.HISTORICAL_FOCUS_DATES)
    assert focus["2026-07-30"]["history_rows"] == []
    assert focus["2026-07-30"]["expected_fires"] == ["2026-07-30T02:00:00Z"]
    assert focus["2026-08-01"]["history_rows"][0]["status"] == "FAILED"


def test_bundle_is_deterministic_and_self_hashing() -> None:
    history = [_history_row(day, "SUCCESS") for day in (27, 28, 29)]
    first = _build(history)
    second = _build(list(reversed(history)))
    assert audit.canonical_json(first) == audit.canonical_json(second)
    assert first["bundle_sha256"] == audit.bundle_sha256(first)


def test_bundle_excludes_secrets_and_business_content() -> None:
    bundle = _build([_history_row(day, "SUCCESS") for day in (27, 28, 29)])
    text = audit.canonical_json(bundle).lower()
    for forbidden in (
        "password", "secret", "dbname=", "postgresql://", "basic ",
        "registration", "driver_name", "provider_trip", "api_key",
    ):
        assert forbidden not in text, forbidden


# ---------------------------------------------------------------------------
# Pure — refusal branches with a fake cursor
# ---------------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def execute(self, sql, params=()) -> None:  # noqa: D401 - test double
        self.sql = sql

    def fetchall(self) -> list[dict]:
        return self._rows


def test_duplicate_client_code_is_refused() -> None:
    cur = _FakeCursor([
        {"client_id": CLIENT_ID, "client_code": CLIENT_CODE,
         "trips_pagination_mode": "strict_meta",
         "trips_stabilization_delay_seconds": 0, "trips_overlap_seconds": 0,
         "trips_max_recovery_span_seconds": 1},
        {"client_id": "b454f82c-5857-4bab-8342-b7258e5cf7de",
         "client_code": CLIENT_CODE, "trips_pagination_mode": "strict_meta",
         "trips_stabilization_delay_seconds": 0, "trips_overlap_seconds": 0,
         "trips_max_recovery_span_seconds": 1},
    ])
    try:
        audit.resolve_client(cur, CLIENT_CODE)
    except audit.AuditError as exc:
        assert exc.code == audit.CLASSIFICATION_AMBIGUOUS_SCHEDULE
        assert exc.exit_code == audit.EXIT_STATE_NOT_INVENTORIABLE
        return
    raise AssertionError("duplicate client_code was accepted")


def test_output_inside_repository_is_refused() -> None:
    try:
        audit.write_bundle(REPO_ROOT / "bundle.json", {"a": 1})
    except audit.AuditError as exc:
        assert exc.code == "OUTPUT_INSIDE_REPOSITORY"
        return
    raise AssertionError("a bundle inside the repository tree was accepted")


def test_cli_has_no_execution_switch() -> None:
    parser = audit.build_parser()
    options = {
        option for action in parser._actions for option in action.option_strings
    }
    for forbidden in ("--execute", "--apply", "--write", "--commit", "--force"):
        assert forbidden not in options, forbidden
    for required in (
        "--client-code", "--dataset", "--output",
        "--expected-environment", "--expected-platform-uuid",
    ):
        assert required in options, required


def test_audit_module_makes_no_provider_or_job_call() -> None:
    text = (REPO_ROOT / "ops" / "audit_telematics_coverage_bootstrap.py").read_text(
        encoding="utf-8"
    )
    for forbidden in (
        "import requests", "provider_client", "runner.py", "Popen",
        "subprocess.check_call", "os.system", "resolve_secret",
    ):
        assert forbidden not in text, forbidden
    # The only subprocess is the repository HEAD read.
    assert text.count("subprocess.run") == 1
    assert '"git", "rev-parse", "HEAD"' in text


# ---------------------------------------------------------------------------
# Disposable PostgreSQL
# ---------------------------------------------------------------------------

def _sql(name: str) -> str:
    return (MIGRATIONS_DIR / name).read_text(encoding="utf-8")


def build_fixture(
    conn,
    *,
    client_mode: str = "strict_meta",
    trips_enabled: bool = True,
    history: tuple[tuple[str, str], ...] = (),
    with_coverage: bool = False,
) -> None:
    """Rebuild a disposable control plane with one client and two schedules."""
    conn.execute("DROP SCHEMA IF EXISTS workflow_a_control CASCADE")
    conn.execute("DROP SCHEMA IF EXISTS ops_control CASCADE")
    conn.execute("DROP TABLE IF EXISTS public.schema_migrations")
    conn.execute(
        "CREATE TABLE public.schema_migrations ("
        " filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
    )
    for name in PREREQUISITE_MIGRATIONS:
        conn.execute(_sql(name))
        conn.execute(
            "INSERT INTO public.schema_migrations (filename) VALUES (%s)"
            " ON CONFLICT DO NOTHING",
            (name,),
        )
    conn.execute(
        """
        INSERT INTO ops_control.environment_identity
          (identity_key, environment, database_identity_id, database_role,
           database_name, provisioned_by)
        VALUES ('primary', %s, %s, 'platform', current_database(), 'test')
        """,
        (ENVIRONMENT, PLATFORM_UUID),
    )
    conn.execute(
        """
        INSERT INTO workflow_a_control.client_account
          (client_id, client_code, client_name, provider_type,
           provider_base_url, provider_basic_auth_username,
           provider_basic_auth_password_secret_ref, client_db_host,
           client_db_port, client_db_name, client_db_user,
           client_db_password_secret_ref, speed_trigger_filter_text,
           trips_pagination_mode)
        VALUES (%s, %s, 'Test Client', 'telematics', 'https://provider.invalid',
                'user', 'TEST_PROVIDER_KEY', '127.0.0.1', 5432, 'clientdb',
                'clientuser', 'TEST_DB_KEY', 'SPEEDING', %s)
        """,
        (CLIENT_ID, CLIENT_CODE, client_mode),
    )
    for schedule_id, dataset, enabled in (
        (SCHEDULE_TRIPS, "trips_sync", trips_enabled),
        (SCHEDULE_FUEL, "fuel_daily_aggregation", True),
    ):
        conn.execute(
            """
            INSERT INTO workflow_a_control.client_dataset_schedule
              (schedule_id, client_id, client_code, dataset_name, enabled,
               frequency, run_time, timezone, lookback_days)
            VALUES (%s, %s, %s, %s, %s, 'daily', '02:00:00', 'UTC', 1)
            """,
            (schedule_id, CLIENT_ID, CLIENT_CODE, dataset, enabled),
        )
    for fire, status in history:
        conn.execute(
            """
            INSERT INTO workflow_a_control.client_schedule_run_history
              (schedule_id, client_id, client_code, dataset_name,
               window_start_ts, window_end_ts, scheduled_fire_ts, status)
            VALUES (%s, %s, %s, 'trips_sync',
                    (%s::timestamptz - interval '1 day'), %s, %s, %s)
            """,
            (SCHEDULE_TRIPS, CLIENT_ID, CLIENT_CODE, fire, fire, fire, status),
        )
    if with_coverage:
        conn.execute(
            """
            INSERT INTO workflow_a_control.client_dataset_coverage
              (schedule_id, client_id, client_code, dataset_name,
               bootstrap_status, covered_through_source)
            VALUES (%s, %s, %s, 'trips_sync', 'UNINITIALIZED', 'bootstrap')
            """,
            (SCHEDULE_TRIPS, CLIENT_ID, CLIENT_CODE),
        )
    conn.commit()


def _snapshot(conn) -> dict:
    tables = (
        "workflow_a_control.client_account",
        "workflow_a_control.client_dataset_schedule",
        "workflow_a_control.client_schedule_run_history",
        "workflow_a_control.client_dataset_coverage",
        "ops_control.environment_identity",
    )
    return {
        table: conn.execute(f"SELECT * FROM {table}").fetchall()
        for table in tables
    }


def _args(dsn: str, output: Path, **overrides) -> Namespace:
    values = {
        "client_code": CLIENT_CODE,
        "dataset": "trips_sync",
        "output": str(output),
        "expected_environment": ENVIRONMENT,
        "expected_platform_uuid": PLATFORM_UUID,
        "range_start": "2026-07-27T00:00:00Z",
        "range_end": "2026-07-31T12:00:00Z",
        "dsn": dsn,
    }
    values.update(overrides)
    return Namespace(**values)


DEFAULT_HISTORY = (
    ("2026-07-27T02:00:00Z", "SUCCESS"),
    ("2026-07-28T02:00:00Z", "SUCCESS"),
    ("2026-07-29T02:00:00Z", "SUCCESS"),
)


def test_connection_is_read_only(conn, dsn: str) -> None:
    import psycopg

    build_fixture(conn, history=DEFAULT_HISTORY)
    ro = audit.open_read_only_connection(dsn)
    try:
        with ro.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM workflow_a_control.client_account")
            assert cur.fetchone()["n"] == 1
            for statement in (
                "INSERT INTO workflow_a_control.client_dataset_coverage "
                "(schedule_id, client_id, dataset_name) "
                f"VALUES ('{SCHEDULE_TRIPS}', '{CLIENT_ID}', 'trips_sync')",
                "UPDATE workflow_a_control.client_account "
                "SET trips_pagination_mode = 'data_invariants_v1'",
                "DELETE FROM workflow_a_control.client_schedule_run_history",
                "CREATE TABLE public.audit_should_not_exist (x int)",
            ):
                try:
                    cur.execute(statement)
                except psycopg.Error:
                    ro.rollback()
                    continue
                raise AssertionError(f"read-only connection accepted: {statement}")
    finally:
        ro.rollback()
        ro.close()


def test_audit_run_is_non_mutating(conn, dsn: str) -> None:
    build_fixture(conn, history=DEFAULT_HISTORY)
    before = _snapshot(conn)
    with tempfile.TemporaryDirectory() as tmp:
        output = Path(tmp) / "nested" / "bundle.json"
        bundle, resolved = audit.run_audit(_args(dsn, output))
        assert resolved.exists()
        assert oct(resolved.stat().st_mode)[-3:] == "600"
        assert oct(resolved.parent.stat().st_mode)[-3:] == "700"
        on_disk = json.loads(resolved.read_text(encoding="utf-8"))
        assert on_disk == bundle
    conn.rollback()
    assert _snapshot(conn) == before, "the audit must not mutate any row"

    assert bundle["client_id"] == CLIENT_ID
    assert bundle["schedule_id"] == SCHEDULE_TRIPS
    assert bundle["dataset_name"] == "trips_sync"
    assert bundle["client_mode"] == "strict_meta"
    assert bundle["existing_coverage"] is None
    assert bundle["audit_classification"] == \
        audit.CLASSIFICATION_UNRESOLVED_GAPS
    assert bundle["bundle_sha256"] == audit.bundle_sha256(bundle)


def test_audit_is_byte_deterministic(conn, dsn: str) -> None:
    """Identical state must produce identical inventory content.

    `generated_at_utc` is deliberately excluded from the comparison: each run
    is a distinct artifact stamped with the database clock, so two bundles are
    expected to hash differently. Everything the bootstrap decision rests on
    must be byte-identical.
    """
    build_fixture(conn, history=DEFAULT_HISTORY)
    rendered = set()
    with tempfile.TemporaryDirectory() as tmp:
        for index in range(2):
            bundle, _ = audit.run_audit(
                _args(dsn, Path(tmp) / f"bundle-{index}.json")
            )
            assert bundle["bundle_sha256"] == audit.bundle_sha256(bundle)
            stable = {
                key: value for key, value in bundle.items()
                if key not in {"generated_at_utc", "bundle_sha256"}
            }
            rendered.add(audit.canonical_json(stable))
    assert len(rendered) == 1, "two audits of identical state must agree"


def test_identity_and_schema_gates(conn, dsn: str) -> None:
    build_fixture(conn, history=DEFAULT_HISTORY)
    with tempfile.TemporaryDirectory() as tmp:
        output = Path(tmp) / "bundle.json"
        for overrides, expected in (
            ({"expected_environment": "local_dev"},
             "IDENTITY_ENVIRONMENT_MISMATCH"),
            ({"expected_platform_uuid": "db8055e0-e030-4d5a-816b-ec4dc338d698"},
             "IDENTITY_PLATFORM_UUID_MISMATCH"),
            ({"client_code": "NOPE00001"}, "CLIENT_NOT_FOUND"),
            ({"dataset": "nonexistent_dataset"},
             audit.CLASSIFICATION_AMBIGUOUS_SCHEDULE),
        ):
            try:
                audit.run_audit(_args(dsn, output, **overrides))
            except audit.AuditError as exc:
                assert exc.code == expected, (overrides, exc.code)
                assert not output.exists(), "no bundle on refusal"
                continue
            raise AssertionError(f"{overrides} was accepted")

    # A disabled trips schedule leaves no authoritative schedule.
    build_fixture(conn, trips_enabled=False, history=DEFAULT_HISTORY)
    with tempfile.TemporaryDirectory() as tmp:
        try:
            audit.run_audit(_args(dsn, Path(tmp) / "bundle.json"))
        except audit.AuditError as exc:
            assert exc.code == audit.CLASSIFICATION_AMBIGUOUS_SCHEDULE
        else:
            raise AssertionError("a disabled schedule was treated as authoritative")

    # Missing migration ceiling is a visible stop.
    build_fixture(conn, history=DEFAULT_HISTORY)
    conn.execute(
        "DELETE FROM public.schema_migrations WHERE filename = %s",
        (audit.MIGRATION_CEILING,),
    )
    conn.commit()
    with tempfile.TemporaryDirectory() as tmp:
        try:
            audit.run_audit(_args(dsn, Path(tmp) / "bundle.json"))
        except audit.AuditError as exc:
            assert exc.code == "MIGRATION_CEILING_MISSING"
        else:
            raise AssertionError("a missing migration ceiling was accepted")


def test_existing_coverage_is_read_not_touched(conn, dsn: str) -> None:
    build_fixture(conn, history=DEFAULT_HISTORY, with_coverage=True)
    before = conn.execute(
        "SELECT * FROM workflow_a_control.client_dataset_coverage"
    ).fetchall()
    with tempfile.TemporaryDirectory() as tmp:
        bundle, _ = audit.run_audit(_args(dsn, Path(tmp) / "bundle.json"))
    conn.rollback()
    after = conn.execute(
        "SELECT * FROM workflow_a_control.client_dataset_coverage"
    ).fetchall()
    assert before == after
    assert bundle["audit_classification"] == \
        audit.CLASSIFICATION_EXISTING_COVERAGE
    assert bundle["existing_coverage"]["bootstrap_status"] == "UNINITIALIZED"


def test_on_disposable_postgres(dsn: str) -> None:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row, autocommit=False) as conn:
        test_connection_is_read_only(conn, dsn)
        test_audit_run_is_non_mutating(conn, dsn)
        test_audit_is_byte_deterministic(conn, dsn)
        test_identity_and_schema_gates(conn, dsn)
        test_existing_coverage_is_read_not_touched(conn, dsn)
        conn.rollback()


def main() -> None:
    test_daily_enumeration_utc()
    test_weekly_enumeration_warsaw()
    test_enumeration_crosses_both_dst_transitions()
    test_monthly_enumeration_including_last_day()
    test_unsupported_cadence_is_a_visible_stop()
    test_canonical_json_is_order_independent()
    test_bundle_hash_excludes_itself_and_detects_change()
    test_complete_inventory_classification()
    test_missing_fire_is_inventoried_as_unresolved()
    test_failed_run_never_counts_as_coverage()
    test_non_terminal_fire_is_unresolved()
    test_existing_coverage_dominates_classification()
    test_empty_history_is_insufficient_evidence()
    test_bundle_recommends_nothing()
    test_focus_dates_are_inventoried_without_a_decision()
    test_bundle_is_deterministic_and_self_hashing()
    test_bundle_excludes_secrets_and_business_content()
    test_duplicate_client_code_is_refused()
    test_output_inside_repository_is_refused()
    test_cli_has_no_execution_switch()
    test_audit_module_makes_no_provider_or_job_call()
    dsn = os.getenv("TELEMATICS_BOOTSTRAP_AUDIT_TEST_DSN")
    if dsn:
        require_loopback_dsn_or_exit(
            dsn, label="TELEMATICS_BOOTSTRAP_AUDIT_TEST_DSN",
        )
        test_on_disposable_postgres(dsn)
        print("PASS: disposable PostgreSQL coverage-bootstrap audit checks")
    else:
        print("SKIP: set TELEMATICS_BOOTSTRAP_AUDIT_TEST_DSN for PostgreSQL checks")
    print("OK - Telematics coverage bootstrap audit checks passed")


if __name__ == "__main__":
    main()
