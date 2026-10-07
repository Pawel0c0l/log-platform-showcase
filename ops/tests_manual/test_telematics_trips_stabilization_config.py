#!/usr/bin/env python3
"""Focused C2 tests for inert Telematics stabilization configuration.

Pure checks always run. Set TELEMATICS_STABILIZATION_CONFIG_TEST_DSN only to
a disposable PostgreSQL database for the schema and partial-state checks.
"""
from __future__ import annotations

import os
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ops.tests_manual.postgres_dsn_safety import (  # noqa: E402
    require_loopback_dsn_or_exit,
)
from jobs import trips_stabilization_config as stabilization  # noqa: E402
from jobs.api.telematics import control_plane  # noqa: E402

MIGRATION_NAME = "056_workflow_a_trips_stabilization_config.sql"
MIGRATION_PATH = REPO_ROOT / "db" / "migrations" / MIGRATION_NAME
MIGRATION_055_PATH = (
    REPO_ROOT / "db" / "migrations" / "055_workflow_a_trips_pagination_mode.sql"
)


def _row(
    *, delay: object = 10_800, overlap: object = 3_600, recovery: object = 2_678_400
) -> dict[str, object]:
    return {
        "client_id": "bd7662a5-eeb4-4614-8720-d477abfcb227",
        "client_code": "TST00001",
        "client_name": "Test Client",
        "provider_base_url": "https://provider.invalid",
        "provider_basic_auth_username": "user",
        "provider_basic_auth_password_secret_ref": "TEST_PROVIDER_KEY",
        "client_db_host": "127.0.0.1",
        "client_db_port": 5432,
        "client_db_name": "clientdb",
        "client_db_user": "clientuser",
        "client_db_password_secret_ref": "TEST_DB_KEY",
        "client_db_schema": "public",
        "speed_trigger_filter_text": "SPEEDING",
        "trip_metrics_population_source": "api_migration",
        "trips_pagination_mode": "strict_meta",
        "trips_stabilization_delay_seconds": delay,
        "trips_overlap_seconds": overlap,
        "trips_max_recovery_span_seconds": recovery,
    }


class FakeCursor:
    def __init__(self, row: dict[str, object]) -> None:
        self.row = row
        self.sql = ""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql: str, _params: tuple[object, ...]) -> None:
        self.sql = sql

    def fetchone(self) -> dict[str, object]:
        return self.row


class FakeConnection:
    def __init__(self, row: dict[str, object]) -> None:
        self.cursor_instance = FakeCursor(row)
        self.closed = False

    def cursor(self, **_kwargs):
        return self.cursor_instance

    def close(self) -> None:
        self.closed = True


def _load(row: dict[str, object]):
    conn = FakeConnection(row)
    with patch.object(control_plane, "_platform_pg_conn", return_value=conn):
        config = control_plane.load_client_account_config(
            client_id="bd7662a5-eeb4-4614-8720-d477abfcb227"
        )
    assert conn.closed
    return config, conn.cursor_instance.sql


def _expect_value_error(callable_) -> None:
    try:
        callable_()
    except ValueError:
        return
    raise AssertionError("invalid stabilization configuration was accepted")


def test_scalar_and_relationship_contract() -> None:
    assert stabilization.TRIPS_STABILIZATION_DELAY_SECONDS_DEFAULT == 10_800
    assert stabilization.TRIPS_OVERLAP_SECONDS_DEFAULT == 3_600
    assert stabilization.TRIPS_MAX_RECOVERY_SPAN_SECONDS_DEFAULT == 2_678_400
    assert stabilization.TRIPS_MAX_RECOVERY_SPAN_SECONDS_MAX == 2_678_400
    assert stabilization.CLOSED_INTERVAL_GRID_STEP_SECONDS == 1

    assert stabilization.validate_trips_stabilization_config(
        stabilization_delay_seconds=10_800,
        overlap_seconds=3_600,
        max_recovery_span_seconds=2_678_400,
    ) == (10_800, 3_600, 2_678_400)
    assert stabilization.validate_trips_stabilization_config(
        stabilization_delay_seconds=0,
        overlap_seconds=0,
        max_recovery_span_seconds=1,
    ) == (0, 0, 1)
    assert stabilization.validate_trips_stabilization_config(
        stabilization_delay_seconds=100_000_000,
        overlap_seconds=60,
        max_recovery_span_seconds=60,
    ) == (100_000_000, 60, 60)

    invalid = (
        ("10800", 3_600, 2_678_400),
        (True, 3_600, 2_678_400),
        (10_800.0, 3_600, 2_678_400),
        (-1, 3_600, 2_678_400),
        (10_800, "3600", 2_678_400),
        (10_800, False, 2_678_400),
        (10_800, 3_600.0, 2_678_400),
        (10_800, -1, 2_678_400),
        (10_800, 0, 0),
        (10_800, 3_600, True),
        (10_800, 3_600, 2_678_400.0),
        (10_800, 3_600, 2_678_401),
        (10_800, 3_601, 3_600),
    )
    for delay, overlap, recovery in invalid:
        _expect_value_error(
            lambda d=delay, o=overlap, r=recovery: (
                stabilization.validate_trips_stabilization_config(
                    stabilization_delay_seconds=d,
                    overlap_seconds=o,
                    max_recovery_span_seconds=r,
                )
            )
        )


def test_control_plane_mapping_and_missing_columns() -> None:
    config, sql = _load(_row())
    assert config.trips_stabilization_delay_seconds == 10_800
    assert config.trips_overlap_seconds == 3_600
    assert config.trips_max_recovery_span_seconds == 2_678_400
    for column in (
        "trips_stabilization_delay_seconds",
        "trips_overlap_seconds",
        "trips_max_recovery_span_seconds",
    ):
        assert column in sql

    non_default, _ = _load(_row(delay=7_200, overlap=1_800, recovery=86_400))
    assert (
        non_default.trips_stabilization_delay_seconds,
        non_default.trips_overlap_seconds,
        non_default.trips_max_recovery_span_seconds,
    ) == (7_200, 1_800, 86_400)

    for key in (
        "trips_stabilization_delay_seconds",
        "trips_overlap_seconds",
        "trips_max_recovery_span_seconds",
    ):
        missing = _row()
        del missing[key]
        try:
            _load(missing)
        except KeyError as exc:
            assert exc.args == (key,)
        else:
            raise AssertionError(f"missing authoritative column was hidden: {key}")

    for key in (
        "trips_stabilization_delay_seconds",
        "trips_overlap_seconds",
        "trips_max_recovery_span_seconds",
    ):
        malformed = _row()
        malformed[key] = None
        _expect_value_error(lambda row=malformed: _load(row))

    try:
        config.trips_overlap_seconds = 0
    except FrozenInstanceError:
        pass
    else:
        raise AssertionError("ClientAccountConfig must remain frozen")


def test_schedule_coupling_contract() -> None:
    cases = {
        "DELTA00001": (86_400, 3_600, 604_800, 90_000),
        "ALPHA00001": (86_400, 0, 86_400, 86_400),
        "FOXTROT00001": (86_400, 0, 86_400, 86_400),
        "BRAVO00016": (604_800, 3_600, 604_800, 608_400),
    }
    for client_code, (interval, dst, lookback, expected_delta) in cases.items():
        actual = stabilization.validate_trips_schedule_coupling(
            schedule_interval_seconds=interval,
            maximum_dst_extension_seconds=dst,
            lookback_duration_seconds=lookback,
            overlap_seconds=3_600,
            max_recovery_span_seconds=2_678_400,
        )
        assert actual == expected_delta, client_code

    # O=3599 passes exactly at the one-second closed-interval boundary;
    # O=3598 is the first value that leaves a one-second grid gap.
    assert stabilization.validate_trips_schedule_coupling(
        schedule_interval_seconds=604_800,
        maximum_dst_extension_seconds=3_600,
        lookback_duration_seconds=604_800,
        overlap_seconds=3_599,
        max_recovery_span_seconds=2_678_400,
    ) == 608_400
    _expect_value_error(
        lambda: stabilization.validate_trips_schedule_coupling(
            schedule_interval_seconds=604_800,
            maximum_dst_extension_seconds=3_600,
            lookback_duration_seconds=604_800,
            overlap_seconds=3_598,
            max_recovery_span_seconds=2_678_400,
        )
    )
    _expect_value_error(
        lambda: stabilization.validate_trips_schedule_coupling(
            schedule_interval_seconds=86_400,
            maximum_dst_extension_seconds=0,
            lookback_duration_seconds=86_400,
            overlap_seconds=3_601,
            max_recovery_span_seconds=3_600,
        )
    )


def test_migration_file_contract() -> None:
    migration_files = sorted(
        path.name for path in (REPO_ROOT / "db" / "migrations").glob("*.sql")
    )
    # Ordinal position relative to its predecessor, not to the repository
    # ceiling, so later additive migrations do not invalidate the contract.
    assert MIGRATION_NAME in migration_files
    assert migration_files.index(MIGRATION_NAME) == (
        migration_files.index("055_workflow_a_trips_pagination_mode.sql") + 1
    )
    sql = MIGRATION_PATH.read_text(encoding="utf-8")
    for fragment in (
        "ADD COLUMN IF NOT EXISTS trips_stabilization_delay_seconds INTEGER",
        "ADD COLUMN IF NOT EXISTS trips_overlap_seconds INTEGER",
        "ADD COLUMN IF NOT EXISTS trips_max_recovery_span_seconds INTEGER",
        "SET trips_stabilization_delay_seconds = 10800",
        "SET trips_overlap_seconds = 3600",
        "SET trips_max_recovery_span_seconds = 2678400",
        "ck_client_account_trips_overlap_within_recovery_span",
        "trips_overlap_seconds <= trips_max_recovery_span_seconds",
    ):
        assert fragment in sql, fragment
    upper = sql.upper()
    for forbidden in (
        "DROP COLUMN",
        "DROP TABLE",
        "TRUNCATE",
        "CREATE TRIGGER",
        "CLIENT_DATASET_COVERAGE",
        "INSERT INTO",
        "DATA_INVARIANTS_V1",
    ):
        assert forbidden not in upper


def _reset_client_account(conn, *, pagination_column: bool = False) -> None:
    conn.execute("DROP SCHEMA IF EXISTS workflow_a_control CASCADE")
    conn.execute("CREATE SCHEMA workflow_a_control")
    pagination = (
        "trips_pagination_mode TEXT NOT NULL DEFAULT 'strict_meta'"
        if pagination_column
        else "legacy_marker TEXT"
    )
    conn.execute(
        f"""
        CREATE TABLE workflow_a_control.client_account (
          client_id UUID PRIMARY KEY,
          client_name TEXT NOT NULL,
          enabled BOOLEAN NOT NULL DEFAULT true,
          {pagination}
        )
        """
    )
    conn.execute(
        """
        INSERT INTO workflow_a_control.client_account (client_id, client_name)
        VALUES ('bd7662a5-eeb4-4614-8720-d477abfcb227', 'Existing client')
        """
    )


def _expect_db_error(conn, sql: str) -> None:
    import psycopg

    try:
        with conn.transaction():
            conn.execute(sql)
    except (psycopg.Error, psycopg.DataError):
        return
    raise AssertionError("database accepted an invalid or incompatible state")


def test_migration_on_disposable_postgres(dsn: str) -> None:
    import psycopg
    from psycopg.rows import dict_row

    sql = MIGRATION_PATH.read_text(encoding="utf-8")
    sql_055 = MIGRATION_055_PATH.read_text(encoding="utf-8")
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        _reset_client_account(conn)
        _expect_db_error(
            conn,
            "SELECT trips_stabilization_delay_seconds "
            "FROM workflow_a_control.client_account",
        )
        conn.execute("GRANT SELECT ON workflow_a_control.client_account TO PUBLIC")
        ownership_before = conn.execute(
            """
            SELECT tableowner, relacl::text
              FROM pg_tables
              JOIN pg_class ON oid='workflow_a_control.client_account'::regclass
             WHERE schemaname='workflow_a_control' AND tablename='client_account'
            """
        ).fetchone()
        conn.execute(sql)
        conn.execute(sql)

        existing = conn.execute(
            """
            SELECT trips_stabilization_delay_seconds AS delay,
                   trips_overlap_seconds AS overlap,
                   trips_max_recovery_span_seconds AS recovery
              FROM workflow_a_control.client_account
            """
        ).fetchone()
        assert existing == {"delay": 10_800, "overlap": 3_600, "recovery": 2_678_400}
        columns = conn.execute(
            """
            SELECT column_name, is_nullable, column_default
              FROM information_schema.columns
             WHERE table_schema='workflow_a_control'
               AND table_name='client_account'
               AND column_name LIKE 'trips_%_seconds'
             ORDER BY column_name
            """
        ).fetchall()
        assert len(columns) == 3
        assert all(row["is_nullable"] == "NO" for row in columns)
        defaults = {row["column_name"]: row["column_default"] for row in columns}
        assert defaults == {
            "trips_max_recovery_span_seconds": "2678400",
            "trips_overlap_seconds": "3600",
            "trips_stabilization_delay_seconds": "10800",
        }

        conn.execute(
            """
            INSERT INTO workflow_a_control.client_account
              (client_id, client_name)
            VALUES ('b454f82c-5857-4bab-8342-b7258e5cf7de', 'New client')
            """
        )
        inserted = conn.execute(
            """
            SELECT trips_stabilization_delay_seconds AS delay,
                   trips_overlap_seconds AS overlap,
                   trips_max_recovery_span_seconds AS recovery
              FROM workflow_a_control.client_account
             WHERE client_id='b454f82c-5857-4bab-8342-b7258e5cf7de'
            """
        ).fetchone()
        assert inserted == existing

        ownership_after = conn.execute(
            """
            SELECT tableowner, relacl::text
              FROM pg_tables
              JOIN pg_class ON oid='workflow_a_control.client_account'::regclass
             WHERE schemaname='workflow_a_control' AND tablename='client_account'
            """
        ).fetchone()
        assert ownership_after == ownership_before
        assert conn.execute(
            "SELECT to_regclass('workflow_a_control.client_dataset_coverage') AS value"
        ).fetchone()["value"] is None

        invalid_updates = (
            "trips_stabilization_delay_seconds=-1",
            "trips_overlap_seconds=-1",
            "trips_max_recovery_span_seconds=0",
            "trips_max_recovery_span_seconds=2678401",
            "trips_overlap_seconds=3601, trips_max_recovery_span_seconds=3600",
        )
        for assignment in invalid_updates:
            _expect_db_error(
                conn,
                "UPDATE workflow_a_control.client_account SET " + assignment,
            )
        conn.execute(
            """
            UPDATE workflow_a_control.client_account
               SET trips_stabilization_delay_seconds=0,
                   trips_overlap_seconds=0,
                   trips_max_recovery_span_seconds=1
            """
        )

        # Partial compatible columns, NULL backfill, and an existing wrong named
        # constraint converge to the canonical contract.
        _reset_client_account(conn)
        conn.execute(
            "ALTER TABLE workflow_a_control.client_account "
            "ADD COLUMN trips_overlap_seconds INTEGER NULL"
        )
        conn.execute(
            "ALTER TABLE workflow_a_control.client_account "
            "ADD CONSTRAINT ck_client_account_trips_overlap_seconds CHECK (true)"
        )
        conn.execute(sql)
        assert conn.execute(
            "SELECT trips_overlap_seconds AS value "
            "FROM workflow_a_control.client_account"
        ).fetchone()["value"] == 3_600

        # Incompatible type and invalid non-NULL content fail visibly and are not
        # normalized to defaults.
        _reset_client_account(conn)
        conn.execute(
            "ALTER TABLE workflow_a_control.client_account "
            "ADD COLUMN trips_stabilization_delay_seconds BIGINT"
        )
        _expect_db_error(conn, sql)

        _reset_client_account(conn)
        conn.execute(
            """
            ALTER TABLE workflow_a_control.client_account
              ADD COLUMN trips_stabilization_delay_seconds INTEGER,
              ADD COLUMN trips_overlap_seconds INTEGER,
              ADD COLUMN trips_max_recovery_span_seconds INTEGER
            """
        )
        conn.execute(
            """
            UPDATE workflow_a_control.client_account
               SET trips_stabilization_delay_seconds=-1,
                   trips_overlap_seconds=3600,
                   trips_max_recovery_span_seconds=2678400
            """
        )
        _expect_db_error(conn, sql)
        assert conn.execute(
            "SELECT trips_stabilization_delay_seconds AS value "
            "FROM workflow_a_control.client_account"
        ).fetchone()["value"] == -1

        # C2 works whether C1 has already been applied and never changes its mode.
        _reset_client_account(conn)
        conn.execute(sql_055)
        conn.execute(sql)
        assert conn.execute(
            "SELECT count(*) AS value FROM workflow_a_control.client_account "
            "WHERE trips_pagination_mode <> 'strict_meta'"
        ).fetchone()["value"] == 0
        conn.rollback()


def main() -> None:
    test_scalar_and_relationship_contract()
    test_control_plane_mapping_and_missing_columns()
    test_schedule_coupling_contract()
    test_migration_file_contract()
    dsn = os.getenv("TELEMATICS_STABILIZATION_CONFIG_TEST_DSN")
    if dsn:
        require_loopback_dsn_or_exit(
            dsn, label="TELEMATICS_STABILIZATION_CONFIG_TEST_DSN",
        )
        test_migration_on_disposable_postgres(dsn)
        print("PASS: disposable PostgreSQL migration checks")
    else:
        print("SKIP: set TELEMATICS_STABILIZATION_CONFIG_TEST_DSN for PostgreSQL checks")
    print("OK - Telematics trips stabilization configuration checks passed")


if __name__ == "__main__":
    main()
