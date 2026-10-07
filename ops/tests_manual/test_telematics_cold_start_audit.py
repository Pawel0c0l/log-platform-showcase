#!/usr/bin/env python3
"""Focused tests for the read-only zero-state cold-start audit.

Pure checks always run. The zero-state gates need two *disposable* PostgreSQL 16
databases — never logdb and never a real client business database — because the
audit reads the control plane and the client `public.client_trips` table:

  docker run -d --rm --name telematics-coldstart-pg -e POSTGRES_PASSWORD=... \\
      -e POSTGRES_USER=loguser -e POSTGRES_DB=coldstart_test \\
      -p 55731:5432 postgres:16
  docker exec telematics-coldstart-pg psql -U loguser -d coldstart_test \\
      -c 'CREATE DATABASE echo_business_test'
  TELEMATICS_COLD_START_TEST_DSN='postgresql://loguser:...@127.0.0.1:55731/coldstart_test' \\
  TELEMATICS_COLD_START_BUSINESS_DSN='postgresql://loguser:...@127.0.0.1:55731/echo_business_test' \\
      .venv/bin/python ops/tests_manual/test_telematics_cold_start_audit.py

The suite proves the two properties the tool's trustworthiness rests on: it
writes nothing, and it confirms the zero state only when the state is provably
zero.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ops import audit_telematics_cold_start as cold  # noqa: E402

PLATFORM_ENV = "TELEMATICS_COLD_START_TEST_DSN"
BUSINESS_ENV = "TELEMATICS_COLD_START_BUSINESS_DSN"

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

CLIENT_ID = "598bc0e3-99b6-4e38-88ea-c1c63e519b2f"
CLIENT_CODE = "ECHO00001"
SCHEDULE_TRIPS = "60c80b85-f294-4a00-8e09-b6a3688af443"
OTHER_SCHEDULE = "f25c8a6c-7ca5-4899-8a16-2490d9e5e241"
PLATFORM_UUID = "52517750-7438-4558-8490-2736ae4cc629"
ENVIRONMENT = "production"
SECRET_ENV_NAME = "TELEMATICS_COLD_START_TEST_DB_KEY"

MANAGED_START = "2026-07-01T00:00:00Z"

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Network guard — shared by every cold-start suite
# ---------------------------------------------------------------------------

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", ""})


def install_network_guard() -> None:
    """Make live non-loopback network access fatal for the rest of the process.

    These tools are read-only against a local database and must never contact a
    provider. A test that silently reached the internet would prove nothing, so
    any AF_INET/AF_INET6 connection outside loopback raises instead.
    """
    import socket

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_create = socket.create_connection

    def _check(sock_family, address) -> None:
        if sock_family not in (socket.AF_INET, socket.AF_INET6):
            return
        host = address[0] if isinstance(address, tuple) else address
        if str(host) not in LOOPBACK_HOSTS:
            raise AssertionError(
                f"live non-loopback network access attempted: {host!r}"
            )

    def guarded_connect(self, address):
        _check(self.family, address)
        return original_connect(self, address)

    def guarded_connect_ex(self, address):
        _check(self.family, address)
        return original_connect_ex(self, address)

    def guarded_create_connection(address, *args, **kwargs):
        _check(socket.AF_INET, address)
        return original_create(address, *args, **kwargs)

    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
    socket.create_connection = guarded_create_connection


def test_network_guard_is_effective() -> None:
    import socket

    install_network_guard()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.connect(("93.184.216.34", 80))
    except AssertionError as exc:
        assert "non-loopback" in str(exc)
    else:
        raise AssertionError("the network guard did not fire")
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# Pure
# ---------------------------------------------------------------------------

def test_bundle_hash_excludes_itself_and_detects_change() -> None:
    bundle = {"x": 1, "y": "z", "bundle_sha256": "0" * 64}
    digest = cold.bundle_sha256(bundle)
    assert cold.bundle_sha256({**bundle, "bundle_sha256": "f" * 64}) == digest
    assert cold.bundle_sha256({**bundle, "x": 2}) != digest


def test_versions_are_distinct_from_the_historical_audit() -> None:
    """A historical bundle must not be mistakable for a cold-start bundle."""
    from ops import audit_telematics_coverage_bootstrap as historical

    assert cold.COLD_START_BUNDLE_VERSION != historical.BUNDLE_VERSION
    assert (
        cold.COLD_START_SEMANTICS_VERSION
        != historical.BOOTSTRAP_SEMANTICS_VERSION
    )
    assert cold.CLASSIFICATION_ZERO_STATE_CONFIRMED not in \
        historical.AUDIT_CLASSIFICATIONS
    # The empty-client classification of the historical tool is never reused.
    assert cold.CLASSIFICATION_ZERO_STATE_CONFIRMED != \
        historical.CLASSIFICATION_UNRESOLVED_GAPS
    assert cold.CLASSIFICATION_ZERO_STATE_CONFIRMED != \
        historical.CLASSIFICATION_INSUFFICIENT_HISTORY


def test_cli_has_no_execution_switch() -> None:
    parser = cold.build_parser()
    options = {
        option for action in parser._actions for option in action.option_strings
    }
    for forbidden in ("--execute", "--apply", "--write", "--commit", "--force"):
        assert forbidden not in options, forbidden
    for required in (
        "--client-code", "--dataset", "--expected-schedule-id",
        "--desired-managed-start", "--expected-environment",
        "--expected-platform-uuid", "--output",
    ):
        assert required in options, required


def test_module_makes_no_provider_or_job_call() -> None:
    text = (REPO_ROOT / "ops" / "audit_telematics_cold_start.py").read_text(
        encoding="utf-8"
    )
    for forbidden in (
        "import requests", "provider_client", "runner.py", "Popen",
        "subprocess.check_call", "os.system", "INSERT ", "UPDATE ", "DELETE ",
    ):
        assert forbidden not in text, forbidden
    # The only subprocess is the repository HEAD read.
    assert text.count("subprocess.run") == 1
    assert '"git", "rev-parse", "HEAD"' in text


def test_managed_start_must_leave_a_recoverable_interval() -> None:
    now = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    boundary = cold.validate_managed_start(
        desired_managed_start_ts=datetime(2026, 7, 1, tzinfo=UTC),
        db_now=now,
        stabilization_delay_seconds=10800,
    )
    assert boundary == datetime(2026, 8, 4, 9, 0, tzinfo=UTC)

    for bad in (
        datetime(2026, 8, 4, 9, 0, tzinfo=UTC),      # exactly at the boundary
        datetime(2026, 8, 4, 11, 0, tzinfo=UTC),     # inside the delay
        datetime(2026, 8, 5, 0, 0, tzinfo=UTC),      # in the future
    ):
        try:
            cold.validate_managed_start(
                desired_managed_start_ts=bad,
                db_now=now,
                stabilization_delay_seconds=10800,
            )
        except cold.ColdStartAuditError as exc:
            assert exc.code == "COLD_START_REFUSED_MANAGED_START"
            continue
        raise AssertionError(f"managed start {bad} was accepted")


def test_output_inside_repository_is_refused() -> None:
    try:
        cold.write_bundle(REPO_ROOT / "cold.json", {"a": 1})
    except cold.ColdStartAuditError as exc:
        assert exc.code == "COLD_START_REFUSED_OUTPUT"
        return
    raise AssertionError("a bundle inside the repository tree was accepted")


def test_output_symlink_is_refused() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = root / "real.json"
        target.write_text("{}", encoding="utf-8")
        link = root / "link.json"
        link.symlink_to(target)
        try:
            cold.write_bundle(link, {"a": 1})
        except cold.ColdStartAuditError as exc:
            assert exc.code == "COLD_START_REFUSED_OUTPUT"
        else:
            raise AssertionError("a symlinked output path was accepted")

        linked_dir = root / "linked-dir"
        linked_dir.symlink_to(root / "sub", target_is_directory=True)
        (root / "sub").mkdir()
        try:
            cold.write_bundle(linked_dir / "b.json", {"a": 1})
        except cold.ColdStartAuditError as exc:
            assert exc.code == "COLD_START_REFUSED_OUTPUT"
        else:
            raise AssertionError("a symlinked output directory was accepted")


def test_bundle_file_permissions() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        output = Path(tmp) / "nested" / "cold.json"
        bundle = {"a": 1, "bundle_sha256": "0" * 64}
        resolved = cold.write_bundle(output, bundle)
        assert resolved.is_file() and not resolved.is_symlink()
        assert oct(resolved.stat().st_mode)[-3:] == "600"
        assert oct(resolved.parent.stat().st_mode)[-3:] == "700"
        assert json.loads(resolved.read_text(encoding="utf-8")) == bundle


# ---------------------------------------------------------------------------
# Disposable PostgreSQL fixture
# ---------------------------------------------------------------------------

def _sql(name: str) -> str:
    return (MIGRATIONS_DIR / name).read_text(encoding="utf-8")


def business_parts(business_dsn: str) -> dict:
    parts = urlsplit(business_dsn)
    return {
        "host": parts.hostname or "127.0.0.1",
        "port": int(parts.port or 5432),
        "name": (parts.path or "/").lstrip("/"),
        "user": parts.username or "loguser",
        "password": parts.password or "",
    }


def build_fixture(
    conn,
    business_conn,
    business_dsn: str,
    *,
    client_mode: str = "strict_meta",
    trips_enabled: bool = False,
    schedule_present: bool = True,
    competing_schedule: bool = False,
    history: tuple = (),
    platform_runs: tuple = (),
    with_coverage: bool = False,
    with_recovery: bool = False,
    client_trips_rows: int = 0,
) -> None:
    """Rebuild a disposable control plane plus a disposable business database."""
    parts = business_parts(business_dsn)
    os.environ[SECRET_ENV_NAME] = parts["password"]

    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS workflow_a_control CASCADE")
        cur.execute("DROP SCHEMA IF EXISTS ops_control CASCADE")
        cur.execute("DROP TABLE IF EXISTS public.schema_migrations")
        cur.execute("DROP TABLE IF EXISTS public.runs")
        cur.execute(
            "CREATE TABLE public.schema_migrations ("
            " filename TEXT PRIMARY KEY,"
            " applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        for name in PREREQUISITE_MIGRATIONS:
            cur.execute(_sql(name))
            cur.execute(
                "INSERT INTO public.schema_migrations (filename) VALUES (%s)"
                " ON CONFLICT DO NOTHING",
                (name,),
            )
        cur.execute(
            "CREATE TABLE public.runs ("
            " run_id UUID PRIMARY KEY, started_at TIMESTAMPTZ NOT NULL"
            "   DEFAULT now(), status TEXT NOT NULL,"
            " params JSONB NOT NULL DEFAULT '{}'::jsonb)"
        )
        cur.execute(
            """
            INSERT INTO ops_control.environment_identity
              (identity_key, environment, database_identity_id, database_role,
               database_name, provisioned_by)
            VALUES ('primary', %s, %s, 'platform', current_database(), 'test')
            """,
            (ENVIRONMENT, PLATFORM_UUID),
        )
        cur.execute(
            """
            INSERT INTO workflow_a_control.client_account
              (client_id, client_code, client_name, provider_type,
               provider_base_url, provider_basic_auth_username,
               provider_basic_auth_password_secret_ref, client_db_host,
               client_db_port, client_db_name, client_db_user,
               client_db_password_secret_ref, client_db_schema,
               speed_trigger_filter_text, enabled, trips_pagination_mode,
               trips_stabilization_delay_seconds, trips_overlap_seconds,
               trips_max_recovery_span_seconds)
            VALUES (%s,%s,'Echo Gallery','telematics','https://provider.invalid',
                    'user','TEST_PROVIDER_KEY',%s,%s,%s,%s,%s,'public',
                    'SPEEDING', true, %s, 10800, 3600, 2678400)
            """,
            (
                CLIENT_ID, CLIENT_CODE, parts["host"], parts["port"],
                parts["name"], parts["user"], SECRET_ENV_NAME, client_mode,
            ),
        )
        if schedule_present:
            cur.execute(
                """
                INSERT INTO workflow_a_control.client_dataset_schedule
                  (schedule_id, client_id, client_code, dataset_name, enabled,
                   frequency, run_time, timezone, lookback_days,
                   overwrite_existing)
                VALUES (%s,%s,%s,'trips_sync',%s,'daily','02:00:00','UTC',1,true)
                """,
                (SCHEDULE_TRIPS, CLIENT_ID, CLIENT_CODE, trips_enabled),
            )
        if competing_schedule:
            # A second `trips_sync` row is impossible under
            # `uq_client_dataset_schedule`, so ambiguity is modelled with a
            # second *client* owning a second row of the same dataset only when
            # a test needs it. Here the competing row is another dataset made to
            # look like the target by sharing the dataset name is not possible,
            # so the competing case is exercised by deleting the unique
            # constraint for the duration of the fixture.
            cur.execute(
                "ALTER TABLE workflow_a_control.client_dataset_schedule"
                " DROP CONSTRAINT IF EXISTS uq_client_dataset_schedule"
            )
            # M5 added a partial unique index enforcing one BASE schedule
            # per (client, dataset). Modelling an ambiguous schedule now
            # means defeating both structures, not just the constraint.
            cur.execute(
                "DROP INDEX IF EXISTS"
                " workflow_a_control.uq_client_dataset_schedule_base"
            )
            cur.execute(
                """
                INSERT INTO workflow_a_control.client_dataset_schedule
                  (schedule_id, client_id, client_code, dataset_name, enabled,
                   frequency, run_time, timezone, lookback_days,
                   overwrite_existing)
                VALUES (%s,%s,%s,'trips_sync',false,'daily','03:00:00','UTC',1,
                        true)
                """,
                (OTHER_SCHEDULE, CLIENT_ID, CLIENT_CODE),
            )
        for fire, status in history:
            cur.execute(
                """
                INSERT INTO workflow_a_control.client_schedule_run_history
                  (schedule_id, client_id, client_code, dataset_name,
                   window_start_ts, window_end_ts, scheduled_fire_ts, status)
                VALUES (%s,%s,%s,'trips_sync',
                        (%s::timestamptz - interval '1 day'), %s, %s, %s)
                """,
                (SCHEDULE_TRIPS, CLIENT_ID, CLIENT_CODE, fire, fire, fire,
                 status),
            )
        for run_id, status in platform_runs:
            cur.execute(
                "INSERT INTO public.runs (run_id, status, params)"
                " VALUES (%s, %s, %s::jsonb)",
                (run_id, status, json.dumps({"client_code": CLIENT_CODE})),
            )
        if with_coverage:
            cur.execute(
                """
                INSERT INTO workflow_a_control.client_dataset_coverage
                  (schedule_id, client_id, client_code, dataset_name,
                   bootstrap_status, covered_through_source)
                VALUES (%s,%s,%s,'trips_sync','UNINITIALIZED','bootstrap')
                """,
                (SCHEDULE_TRIPS, CLIENT_ID, CLIENT_CODE),
            )
        if with_recovery:
            cur.execute(
                """
                INSERT INTO workflow_a_control.client_dataset_recovery_run
                  (client_id, client_code, schedule_id, dataset_name,
                   window_start_ts, window_end_ts,
                   expected_old_covered_through_ts, status, reason,
                   approval_ref, repository_head, pagination_mode,
                   stabilization_delay_seconds, overlap_seconds,
                   max_recovery_span_seconds, initial_coverage_snapshot,
                   initial_coverage_fingerprint, started_at, finished_at,
                   error_classification, error_summary)
                VALUES (%s,%s,%s,'trips_sync',
                        '2026-07-01T00:00:00Z','2026-07-02T00:00:00Z',
                        '2026-07-01T00:00:00Z','FAILED','prior attempt','T-0',
                        %s,'data_invariants_v1',10800,3600,2678400,
                        '{}'::jsonb,%s,now(),now(),'RECOVERY_BUSINESS_FAILED',
                        'prior')
                """,
                (CLIENT_ID, CLIENT_CODE, SCHEDULE_TRIPS, "0" * 40, "a" * 64),
            )
    conn.commit()

    with business_conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS public.client_trips")
        cur.execute(
            "CREATE TABLE public.client_trips ("
            " record_id UUID PRIMARY KEY, client_id UUID, client_code TEXT)"
        )
        for index in range(client_trips_rows):
            cur.execute(
                "INSERT INTO public.client_trips (record_id, client_id,"
                " client_code) VALUES (gen_random_uuid(), %s, %s)",
                (CLIENT_ID, CLIENT_CODE),
            )
    business_conn.commit()


def snapshot(conn) -> dict:
    out = {}
    with conn.cursor() as cur:
        for table in (
            "workflow_a_control.client_account",
            "workflow_a_control.client_dataset_schedule",
            "workflow_a_control.client_schedule_run_history",
            "workflow_a_control.client_dataset_coverage",
            "workflow_a_control.client_dataset_recovery_run",
            "public.runs",
        ):
            cur.execute(f"SELECT to_jsonb(t) AS row FROM {table} AS t")
            out[table] = sorted(
                json.dumps(r["row"], sort_keys=True) for r in cur.fetchall()
            )
    conn.rollback()
    return out


def audit_args(dsn: str, output: Path, **overrides) -> Namespace:
    values = {
        "client_code": CLIENT_CODE,
        "dataset": "trips_sync",
        "expected_schedule_id": SCHEDULE_TRIPS,
        "desired_managed_start": MANAGED_START,
        "expected_environment": ENVIRONMENT,
        "expected_platform_uuid": PLATFORM_UUID,
        "output": str(output),
        "dsn": dsn,
    }
    values.update(overrides)
    return Namespace(**values)


# ---------------------------------------------------------------------------
# Disposable PostgreSQL cases
# ---------------------------------------------------------------------------

def test_zero_state_confirmed(conn, business_conn, dsn, business_dsn) -> None:
    build_fixture(conn, business_conn, business_dsn)
    before = snapshot(conn)
    with tempfile.TemporaryDirectory() as tmp:
        bundle, resolved = cold.run_audit(
            audit_args(dsn, Path(tmp) / "nested" / "cold.json")
        )
        assert oct(resolved.stat().st_mode)[-3:] == "600"
        assert oct(resolved.parent.stat().st_mode)[-3:] == "700"
        assert json.loads(resolved.read_text(encoding="utf-8")) == bundle
    conn.rollback()
    assert snapshot(conn) == before, "the cold-start audit must not mutate a row"

    assert bundle["audit_classification"] == \
        cold.CLASSIFICATION_ZERO_STATE_CONFIRMED
    assert bundle["bundle_version"] == cold.COLD_START_BUNDLE_VERSION
    assert bundle["client_id"] == CLIENT_ID
    assert bundle["schedule_id"] == SCHEDULE_TRIPS
    assert bundle["schedule_enabled"] is False
    assert bundle["client_mode"] == "strict_meta"
    assert bundle["desired_managed_start_ts"] == MANAGED_START
    assert bundle["bundle_sha256"] == cold.bundle_sha256(bundle)
    for key, value in bundle["zero_state_counts"].items():
        assert value == 0, key
    assert bundle["baseline_contract"]["covered_interval_seconds"] == 0
    assert bundle["baseline_contract"]["required_coverage_start_ts"] == \
        bundle["baseline_contract"]["required_covered_through_ts"]
    assert bundle["decision_contract"]["reporting_ready"] is False
    assert bundle["decision_contract"]["schedule_activation_authorized"] is False
    assert bundle["business_database"]["client_trips_row_count"] == 0
    assert bundle["business_database"]["business_database_name"] == \
        business_parts(business_dsn)["name"]

    text = cold.canonical_json(bundle).lower()
    for forbidden in (
        "password", "secret", "dbname=", "postgresql://", "basic ",
        "registration", "driver_name", "api_key",
    ):
        assert forbidden not in text, forbidden


def test_enabled_schedule_is_refused(conn, business_conn, dsn, business_dsn) -> None:
    build_fixture(conn, business_conn, business_dsn, trips_enabled=True)
    _expect(dsn, "COLD_START_REFUSED_SCHEDULE")


def test_absent_schedule_is_refused(conn, business_conn, dsn, business_dsn) -> None:
    build_fixture(conn, business_conn, business_dsn, schedule_present=False)
    _expect(dsn, "COLD_START_REFUSED_SCHEDULE")


def test_two_schedules_are_refused(conn, business_conn, dsn, business_dsn) -> None:
    build_fixture(conn, business_conn, business_dsn, competing_schedule=True)
    _expect(dsn, "COLD_START_REFUSED_SCHEDULE")


def test_any_history_row_is_refused(conn, business_conn, dsn, business_dsn) -> None:
    for status in ("SUCCESS", "FAILED", "RUNNING"):
        build_fixture(
            conn, business_conn, business_dsn,
            history=(("2026-07-02T02:00:00Z", status),),
        )
        _expect(dsn, "COLD_START_REFUSED_HISTORY_PRESENT")


def test_any_platform_run_is_refused(conn, business_conn, dsn, business_dsn) -> None:
    build_fixture(
        conn, business_conn, business_dsn,
        platform_runs=(("cf4c4732-fd3b-4f8a-85b6-0871950a2f22", "SUCCESS"),),
    )
    _expect(dsn, "COLD_START_REFUSED_RUNS_PRESENT")


def test_any_coverage_row_is_refused(conn, business_conn, dsn, business_dsn) -> None:
    build_fixture(conn, business_conn, business_dsn, with_coverage=True)
    _expect(dsn, "COLD_START_REFUSED_COVERAGE_PRESENT")


def test_any_recovery_row_is_refused(conn, business_conn, dsn, business_dsn) -> None:
    build_fixture(conn, business_conn, business_dsn, with_recovery=True)
    _expect(dsn, "COLD_START_REFUSED_RECOVERY_PRESENT")


def test_compatibility_mode_is_refused(conn, business_conn, dsn, business_dsn) -> None:
    build_fixture(
        conn, business_conn, business_dsn, client_mode="data_invariants_v1",
    )
    _expect(dsn, "COLD_START_REFUSED_MODE")


def test_client_trips_rows_are_refused(conn, business_conn, dsn, business_dsn) -> None:
    build_fixture(conn, business_conn, business_dsn, client_trips_rows=3)
    _expect(dsn, "COLD_START_REFUSED_BUSINESS_ROWS_PRESENT")


def test_identity_gates(conn, business_conn, dsn, business_dsn) -> None:
    build_fixture(conn, business_conn, business_dsn)
    for overrides, expected in (
        ({"expected_environment": "local_dev"}, "IDENTITY_ENVIRONMENT_MISMATCH"),
        ({"expected_platform_uuid": "db8055e0-e030-4d5a-816b-ec4dc338d698"},
         "IDENTITY_PLATFORM_UUID_MISMATCH"),
        ({"expected_schedule_id": "db8055e0-e030-4d5a-816b-ec4dc338d698"},
         "COLD_START_REFUSED_SCHEDULE"),
    ):
        _expect(dsn, expected, **overrides)


def test_active_process_is_refused(conn, business_conn, dsn, business_dsn) -> None:
    """A concurrent write-intent lock on a control relation blocks the audit."""
    import psycopg

    build_fixture(conn, business_conn, business_dsn)
    other = psycopg.connect(dsn, autocommit=False)
    try:
        with other.cursor() as cur:
            cur.execute(
                "SELECT client_id FROM workflow_a_control.client_account"
                " WHERE client_code = %s FOR UPDATE",
                (CLIENT_CODE,),
            )
        _expect(dsn, "COLD_START_REFUSED_ACTIVE_PROCESS")
    finally:
        other.rollback()
        other.close()
    # With the concurrent transaction gone the same state is confirmed again.
    with tempfile.TemporaryDirectory() as tmp:
        bundle, _ = cold.run_audit(audit_args(dsn, Path(tmp) / "cold.json"))
    assert bundle["audit_classification"] == \
        cold.CLASSIFICATION_ZERO_STATE_CONFIRMED
    assert bundle["active_process_result"][
        "blocking_control_plane_backends"
    ] == 0


def test_read_only_connection_rejects_writes(conn, business_conn, dsn, business_dsn) -> None:
    import psycopg

    build_fixture(conn, business_conn, business_dsn)
    ro = cold.open_read_only_connection(dsn)
    try:
        with ro.cursor() as cur:
            for statement in (
                "UPDATE workflow_a_control.client_dataset_schedule"
                " SET enabled = true",
                "INSERT INTO workflow_a_control.client_dataset_coverage"
                f" (schedule_id, client_id, dataset_name) VALUES"
                f" ('{SCHEDULE_TRIPS}', '{CLIENT_ID}', 'trips_sync')",
                "DELETE FROM workflow_a_control.client_account",
            ):
                try:
                    cur.execute(statement)
                except psycopg.Error:
                    ro.rollback()
                    continue
                raise AssertionError(f"read-only accepted: {statement}")
    finally:
        ro.rollback()
        ro.close()


def _expect(dsn: str, code: str, **overrides) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        output = Path(tmp) / "cold.json"
        try:
            cold.run_audit(audit_args(dsn, output, **overrides))
        except cold.ColdStartAuditError as exc:
            assert exc.code == code, f"expected {code}, got {exc.code}"
            assert not output.exists(), "a refused audit must produce no bundle"
            return
    raise AssertionError(f"expected refusal {code}")


# ---------------------------------------------------------------------------

def test_on_disposable_postgres(dsn: str, business_dsn: str) -> None:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row, autocommit=False) as conn, \
            psycopg.connect(
                business_dsn, row_factory=dict_row, autocommit=False
            ) as business_conn:
        test_zero_state_confirmed(conn, business_conn, dsn, business_dsn)
        test_enabled_schedule_is_refused(conn, business_conn, dsn, business_dsn)
        test_absent_schedule_is_refused(conn, business_conn, dsn, business_dsn)
        test_two_schedules_are_refused(conn, business_conn, dsn, business_dsn)
        test_any_history_row_is_refused(conn, business_conn, dsn, business_dsn)
        test_any_platform_run_is_refused(conn, business_conn, dsn, business_dsn)
        test_any_coverage_row_is_refused(conn, business_conn, dsn, business_dsn)
        test_any_recovery_row_is_refused(conn, business_conn, dsn, business_dsn)
        test_compatibility_mode_is_refused(
            conn, business_conn, dsn, business_dsn
        )
        test_client_trips_rows_are_refused(
            conn, business_conn, dsn, business_dsn
        )
        test_identity_gates(conn, business_conn, dsn, business_dsn)
        test_active_process_is_refused(conn, business_conn, dsn, business_dsn)
        test_read_only_connection_rejects_writes(
            conn, business_conn, dsn, business_dsn
        )
        conn.rollback()


def main() -> None:
    test_network_guard_is_effective()
    test_bundle_hash_excludes_itself_and_detects_change()
    test_versions_are_distinct_from_the_historical_audit()
    test_cli_has_no_execution_switch()
    test_module_makes_no_provider_or_job_call()
    test_managed_start_must_leave_a_recoverable_interval()
    test_output_inside_repository_is_refused()
    test_output_symlink_is_refused()
    test_bundle_file_permissions()
    dsn = os.getenv(PLATFORM_ENV)
    business_dsn = os.getenv(BUSINESS_ENV)
    if dsn and business_dsn:
        test_on_disposable_postgres(dsn, business_dsn)
        print("PASS: disposable PostgreSQL cold-start audit checks")
    else:
        print(f"SKIP: set {PLATFORM_ENV} and {BUSINESS_ENV} for PostgreSQL checks")
    print("OK - Telematics cold-start audit checks passed")


if __name__ == "__main__":
    main()
