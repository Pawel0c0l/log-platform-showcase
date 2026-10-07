#!/usr/bin/env python3
"""Focused tests for the inert Telematics trips pagination-mode contract.

Run pure checks from the repository root. Set
TELEMATICS_PAGINATION_MODE_TEST_DSN only to a disposable PostgreSQL database
to add real schema checks; this test never targets the configured platform DB.
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
from jobs import trips_pagination_mode as mode  # noqa: E402
from jobs.api.telematics import control_plane  # noqa: E402

MIGRATION_NAME = "055_workflow_a_trips_pagination_mode.sql"
MIGRATION_PATH = REPO_ROOT / "db" / "migrations" / MIGRATION_NAME


def _row(value: object = "strict_meta") -> dict[str, object]:
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
        "trips_pagination_mode": value,
        "trips_stabilization_delay_seconds": 10800,
        "trips_overlap_seconds": 3600,
        "trips_max_recovery_span_seconds": 2678400,
    }


class FakeCursor:
    def __init__(self, row: dict[str, object]) -> None:
        self.row = row
        self.sql = ""
        self.params: tuple[object, ...] = ()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql: str, params: tuple[object, ...]) -> None:
        self.sql = sql
        self.params = params

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
    assert conn.cursor_instance.params == (
        "bd7662a5-eeb4-4614-8720-d477abfcb227",
    )
    return config, conn.cursor_instance.sql


def test_helper_contract() -> None:
    assert mode.TRIPS_PAGINATION_MODE_VALUES == (
        "strict_meta",
        "data_invariants_v1",
    )
    assert mode.TRIPS_PAGINATION_MODE_DEFAULT == "strict_meta"
    assert mode.normalize_trips_pagination_mode(None) == "strict_meta"
    for accepted in mode.TRIPS_PAGINATION_MODE_VALUES:
        assert mode.normalize_trips_pagination_mode(accepted) == accepted
    for malformed in ("", "loose", " strict_meta", "STRICT_META", 1, object()):
        try:
            mode.normalize_trips_pagination_mode(malformed)
        except ValueError as exc:
            assert "trips_pagination_mode" in str(exc)
        else:
            raise AssertionError(f"malformed mode was accepted: {malformed!r}")


def test_control_plane_mapping_and_frozen_model() -> None:
    strict, sql = _load(_row("strict_meta"))
    assert strict.trips_pagination_mode == "strict_meta"
    assert "trip_metrics_population_source,\n                  trips_pagination_mode" in sql

    compatibility, _ = _load(_row("data_invariants_v1"))
    assert compatibility.trips_pagination_mode == "data_invariants_v1"

    pre_migration_row = _row()
    del pre_migration_row["trips_pagination_mode"]
    pre_migration, _ = _load(pre_migration_row)
    assert pre_migration.trips_pagination_mode == "strict_meta"
    null_row, _ = _load(_row(None))
    assert null_row.trips_pagination_mode == "strict_meta"

    try:
        _load(_row("loose"))
    except ValueError:
        pass
    else:
        raise AssertionError("unknown database mode must fail Python validation")

    try:
        strict.trips_pagination_mode = "data_invariants_v1"
    except FrozenInstanceError:
        pass
    else:
        raise AssertionError("ClientAccountConfig must remain frozen")


def test_migration_file_contract() -> None:
    migration_files = sorted(
        path.name for path in (REPO_ROOT / "db" / "migrations").glob("*.sql")
    )
    # Ordinal position relative to its neighbours, not to the repository
    # ceiling, so later additive migrations do not invalidate the contract.
    assert MIGRATION_NAME in migration_files
    assert "054_environment_identity_resume_contract.sql" in migration_files
    assert migration_files.index(MIGRATION_NAME) == (
        migration_files.index("054_environment_identity_resume_contract.sql") + 1
    )
    assert migration_files.index("056_workflow_a_trips_stabilization_config.sql") == (
        migration_files.index(MIGRATION_NAME) + 1
    )

    sql = MIGRATION_PATH.read_text(encoding="utf-8")
    for fragment in (
        "ADD COLUMN IF NOT EXISTS trips_pagination_mode TEXT",
        "SET trips_pagination_mode = 'strict_meta'",
        "ALTER COLUMN trips_pagination_mode SET DEFAULT 'strict_meta'",
        "ALTER COLUMN trips_pagination_mode SET NOT NULL",
        "ck_client_account_trips_pagination_mode",
        "trips_pagination_mode IN ('strict_meta', 'data_invariants_v1')",
    ):
        assert fragment in sql, fragment
    upper = sql.upper()
    for forbidden in (
        "DROP COLUMN",
        "DROP TABLE",
        "TRUNCATE",
        "CREATE TRIGGER",
        "CLIENT_DATASET_COVERAGE",
    ):
        assert forbidden not in upper
    assert "SET trips_pagination_mode = 'data_invariants_v1'" not in sql


def test_migration_on_disposable_postgres(dsn: str) -> None:
    import psycopg
    from psycopg.rows import dict_row

    sql = MIGRATION_PATH.read_text(encoding="utf-8")
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        conn.execute("DROP SCHEMA IF EXISTS workflow_a_control CASCADE")
        conn.execute("CREATE SCHEMA workflow_a_control")
        conn.execute(
            """
            CREATE TABLE workflow_a_control.client_account (
              client_id UUID PRIMARY KEY,
              client_name TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO workflow_a_control.client_account (client_id, client_name)
            VALUES ('bd7662a5-eeb4-4614-8720-d477abfcb227', 'Existing client')
            """
        )
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
            "SELECT trips_pagination_mode FROM workflow_a_control.client_account"
        ).fetchone()
        assert existing["trips_pagination_mode"] == "strict_meta"
        column = conn.execute(
            """
            SELECT is_nullable, column_default
              FROM information_schema.columns
             WHERE table_schema='workflow_a_control'
               AND table_name='client_account'
               AND column_name='trips_pagination_mode'
            """
        ).fetchone()
        assert column == {
            "is_nullable": "NO",
            "column_default": "'strict_meta'::text",
        }
        ownership_after = conn.execute(
            """
            SELECT tableowner, relacl::text
              FROM pg_tables
              JOIN pg_class ON oid='workflow_a_control.client_account'::regclass
             WHERE schemaname='workflow_a_control' AND tablename='client_account'
            """
        ).fetchone()
        assert ownership_after == ownership_before

        for accepted in mode.TRIPS_PAGINATION_MODE_VALUES:
            conn.execute(
                "UPDATE workflow_a_control.client_account SET trips_pagination_mode=%s",
                (accepted,),
            )
        for rejected in (None, "loose"):
            try:
                with conn.transaction():
                    conn.execute(
                        "UPDATE workflow_a_control.client_account SET trips_pagination_mode=%s",
                        (rejected,),
                    )
            except psycopg.errors.IntegrityError:
                pass
            else:
                raise AssertionError(f"database accepted invalid mode: {rejected!r}")

        conn.execute(
            "UPDATE workflow_a_control.client_account SET trips_pagination_mode='strict_meta'"
        )
        assert conn.execute(
            "SELECT count(*) AS n FROM workflow_a_control.client_account "
            "WHERE trips_pagination_mode='data_invariants_v1'"
        ).fetchone()["n"] == 0
        conn.rollback()


def main() -> None:
    test_helper_contract()
    test_control_plane_mapping_and_frozen_model()
    test_migration_file_contract()
    dsn = os.getenv("TELEMATICS_PAGINATION_MODE_TEST_DSN")
    if dsn:
        require_loopback_dsn_or_exit(
            dsn, label="TELEMATICS_PAGINATION_MODE_TEST_DSN",
        )
        test_migration_on_disposable_postgres(dsn)
        print("PASS: disposable PostgreSQL migration checks")
    else:
        print("SKIP: set TELEMATICS_PAGINATION_MODE_TEST_DSN for disposable PostgreSQL checks")
    print("OK - Telematics trips pagination-mode configuration checks passed")


if __name__ == "__main__":
    main()
