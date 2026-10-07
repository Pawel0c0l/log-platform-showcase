#!/usr/bin/env python3
"""Focused regressions for the fail-closed environment identity guard."""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.common import environment_identity as guard

PLATFORM_ID = "bd7662a5-eeb4-4614-8720-d477abfcb227"
CLIENT_ID = "b454f82c-5857-4bab-8342-b7258e5cf7de"


def runtime_env(environment: str = "local_dev") -> dict[str, str]:
    return {
        guard.TARGET_ENV_VAR: environment,
        guard.EXPECTED_PLATFORM_ID_VAR: PLATFORM_ID,
        guard.EXPECTED_POSTGRES_HOST_VAR: "127.0.0.1",
        guard.EXPECTED_POSTGRES_PORT_VAR: "5432",
        guard.EXPECTED_POSTGRES_DB_VAR: "logdb",
        guard.EXPECTED_POSTGRES_USER_VAR: "loguser",
        "POSTGRES_HOST": "127.0.0.1",
        "POSTGRES_PORT": "5432",
        "POSTGRES_DB": "logdb",
        "POSTGRES_USER": "loguser",
    }


def marker(*, environment: str, identity_id: str, role: str, database: str, client_code=None):
    return {
        "identity_key": "primary",
        "environment": environment,
        "database_identity_id": identity_id,
        "database_role": role,
        "database_name": database,
        "client_code": client_code,
    }


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, query, params=None):
        text = " ".join(str(query).split())
        self.conn.executed.append((text, params))
        if "current_database() AS database_name" in text:
            self.rows = [dict(self.conn.connection_identity)]
        elif "to_regclass('ops_control.environment_identity')" in text:
            self.rows = [{"marker_table": "ops_control.environment_identity" if self.conn.table_exists else None}]
        elif "FROM ops_control.environment_identity" in text:
            self.rows = [dict(row) for row in self.conn.marker_rows]
        else:
            self.rows = []

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class FakeConn:
    def __init__(self, *, database: str, user: str, rows, table_exists: bool = True):
        self.connection_identity = {
            "database_name": database,
            "database_user": user,
            "server_addr": "127.0.0.1",
            "server_port": 5432,
        }
        self.marker_rows = list(rows)
        self.table_exists = table_exists
        self.executed = []
        self.rollbacks = 0

    def cursor(self):
        return FakeCursor(self)

    def rollback(self):
        self.rollbacks += 1


def expect_code(code: str, fn) -> None:
    try:
        fn()
    except guard.EnvironmentIdentityError as exc:
        assert exc.code == code, (exc.code, str(exc))
    else:
        raise AssertionError(f"expected {code}")


def client_expectation(environment: str = "local_dev") -> guard.ClientIdentityExpectation:
    return guard.ClientIdentityExpectation(
        client_code="ALPHA00001",
        environment=environment,
        database_identity_id=CLIENT_ID,
        database_name="alpha_main",
        database_user="alpha_user",
    )


def test_valid_local_dev_platform_and_client_are_accepted() -> None:
    runtime = guard.load_runtime_identity(runtime_env())
    platform_conn = FakeConn(
        database="logdb",
        user="loguser",
        rows=[marker(environment="local_dev", identity_id=PLATFORM_ID, role="platform", database="logdb")],
    )
    client_conn = FakeConn(
        database="alpha_main",
        user="alpha_user",
        rows=[marker(environment="local_dev", identity_id=CLIENT_ID, role="client_business", database="alpha_main", client_code="ALPHA00001")],
    )
    assert guard.attest_platform_identity(platform_conn, runtime).environment == "local_dev"
    assert guard.attest_client_identity(client_conn, runtime, client_expectation()).client_code == "ALPHA00001"
    assert all(sql.startswith(("SET ", "SELECT ")) for sql, _ in platform_conn.executed + client_conn.executed)


def test_missing_and_invalid_runtime_target_fail() -> None:
    missing = runtime_env()
    missing.pop(guard.TARGET_ENV_VAR)
    expect_code("TARGET_ENVIRONMENT_MISSING", lambda: guard.load_runtime_identity(missing))
    invalid = runtime_env("prod")
    expect_code("TARGET_ENVIRONMENT_INVALID", lambda: guard.load_runtime_identity(invalid))


def test_missing_platform_marker_table_fails() -> None:
    runtime = guard.load_runtime_identity(runtime_env())
    conn = FakeConn(database="logdb", user="loguser", rows=[], table_exists=False)
    expect_code("DB_MARKER_TABLE_MISSING", lambda: guard.attest_platform_identity(conn, runtime))


def test_missing_client_marker_row_fails() -> None:
    runtime = guard.load_runtime_identity(runtime_env())
    conn = FakeConn(database="alpha_main", user="alpha_user", rows=[])
    expect_code("DB_MARKER_ROW_MISSING", lambda: guard.attest_client_identity(conn, runtime, client_expectation()))


def test_local_marker_falsely_declared_production_fails() -> None:
    runtime = guard.load_runtime_identity(runtime_env("production"))
    conn = FakeConn(
        database="logdb",
        user="loguser",
        rows=[marker(environment="local_dev", identity_id=PLATFORM_ID, role="platform", database="logdb")],
    )
    expect_code("DB_MARKER_ENVIRONMENT_MISMATCH", lambda: guard.attest_platform_identity(conn, runtime))


def test_platform_and_client_mismatches_fail() -> None:
    runtime = guard.load_runtime_identity(runtime_env())
    platform_conn = FakeConn(
        database="logdb",
        user="loguser",
        rows=[marker(environment="local_dev", identity_id=CLIENT_ID, role="platform", database="logdb")],
    )
    expect_code("DB_MARKER_IDENTITY_MISMATCH", lambda: guard.attest_platform_identity(platform_conn, runtime))
    client_conn = FakeConn(
        database="alpha_main",
        user="alpha_user",
        rows=[marker(environment="local_dev", identity_id=CLIENT_ID, role="client_business", database="alpha_main", client_code="ALPHA00001")],
    )
    expect_code(
        "RUNTIME_CLIENT_IDENTITY_MISMATCH",
        lambda: guard.attest_client_identity(client_conn, runtime, client_expectation("production")),
    )


def test_production_dry_run_requires_matching_attestations() -> None:
    runtime = guard.load_runtime_identity(runtime_env("production"))
    platform_conn = FakeConn(
        database="logdb",
        user="loguser",
        rows=[marker(environment="production", identity_id=PLATFORM_ID, role="platform", database="logdb")],
    )
    client_conn = FakeConn(
        database="alpha_main",
        user="alpha_user",
        rows=[marker(environment="production", identity_id=CLIENT_ID, role="client_business", database="alpha_main", client_code="ALPHA00001")],
    )
    guard.attest_platform_identity(platform_conn, runtime)
    guard.attest_client_identity(client_conn, runtime, client_expectation("production"))
    guard.require_production_write_confirmation(
        runtime,
        client_code="ALPHA00001",
        operation_name="d105_2_trip_metrics_migration",
        provided=None,
        dry_run=True,
    )


def test_production_write_requires_exact_confirmation() -> None:
    runtime = guard.load_runtime_identity(runtime_env("production"))
    kwargs = {
        "client_code": "ALPHA00001",
        "operation_name": "d105_2_trip_metrics_migration",
        "dry_run": False,
    }
    expect_code(
        "PRODUCTION_WRITE_CONFIRMATION_MISSING",
        lambda: guard.require_production_write_confirmation(runtime, provided=None, **kwargs),
    )
    expect_code(
        "PRODUCTION_WRITE_CONFIRMATION_MISMATCH",
        lambda: guard.require_production_write_confirmation(runtime, provided="wrong", **kwargs),
    )
    exact = guard.required_production_write_confirmation(runtime, **{k: kwargs[k] for k in ("client_code", "operation_name")})
    guard.require_production_write_confirmation(runtime, provided=exact, **kwargs)


def main() -> None:
    test_valid_local_dev_platform_and_client_are_accepted()
    test_missing_and_invalid_runtime_target_fail()
    test_missing_platform_marker_table_fails()
    test_missing_client_marker_row_fails()
    test_local_marker_falsely_declared_production_fails()
    test_platform_and_client_mismatches_fail()
    test_production_dry_run_requires_matching_attestations()
    test_production_write_requires_exact_confirmation()
    print("OK - environment identity guard regressions passed")


if __name__ == "__main__":
    main()
