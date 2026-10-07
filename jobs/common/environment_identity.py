from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from uuid import UUID


ENV_LOCAL_DEV = "local_dev"
ENV_STAGING = "staging"
ENV_PRODUCTION = "production"
ALLOWED_ENVIRONMENTS = (ENV_LOCAL_DEV, ENV_STAGING, ENV_PRODUCTION)

TARGET_ENV_VAR = "LOG_PLATFORM_TARGET_ENVIRONMENT"
EXPECTED_PLATFORM_ID_VAR = "LOG_PLATFORM_EXPECTED_PLATFORM_IDENTITY_ID"
EXPECTED_POSTGRES_HOST_VAR = "LOG_PLATFORM_EXPECTED_POSTGRES_HOST"
EXPECTED_POSTGRES_PORT_VAR = "LOG_PLATFORM_EXPECTED_POSTGRES_PORT"
EXPECTED_POSTGRES_DB_VAR = "LOG_PLATFORM_EXPECTED_POSTGRES_DB"
EXPECTED_POSTGRES_USER_VAR = "LOG_PLATFORM_EXPECTED_POSTGRES_USER"

MARKER_SCHEMA = "ops_control"
MARKER_TABLE = "environment_identity"
MARKER_KEY = "primary"
MARKER_ROLE_PLATFORM = "platform"
MARKER_ROLE_CLIENT = "client_business"


class EnvironmentIdentityError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class RuntimeIdentity:
    environment: str
    platform_identity_id: str
    postgres_host: str
    postgres_port: int
    postgres_db: str
    postgres_user: str

    def context(self) -> dict[str, object]:
        return {
            "target_environment": self.environment,
            "expected_postgres_host": self.postgres_host,
            "expected_postgres_port": self.postgres_port,
            "expected_postgres_db": self.postgres_db,
            "expected_postgres_user": self.postgres_user,
        }


@dataclass(frozen=True)
class ClientIdentityExpectation:
    client_code: str
    environment: str | None
    database_identity_id: str | None
    database_name: str
    database_user: str


@dataclass(frozen=True)
class AttestedDatabaseIdentity:
    environment: str
    database_identity_id: str
    database_role: str
    database_name: str
    database_user: str
    client_code: str | None

    def context(self) -> dict[str, object]:
        return {
            "environment": self.environment,
            "database_role": self.database_role,
            "database_name": self.database_name,
            "database_user": self.database_user,
            "client_code": self.client_code,
        }


def _required(env: Mapping[str, str], name: str, code: str) -> str:
    value = str(env.get(name) or "").strip()
    if not value:
        raise EnvironmentIdentityError(code, f"required non-secret variable {name} is missing")
    return value


def _canonical_uuid(value: str, *, code: str, label: str) -> str:
    try:
        parsed = UUID(value)
    except (TypeError, ValueError) as exc:
        raise EnvironmentIdentityError(code, f"{label} must be a canonical UUID") from exc
    canonical = str(parsed)
    if value != canonical:
        raise EnvironmentIdentityError(code, f"{label} must use canonical lowercase UUID form")
    return canonical


def load_runtime_identity(env: Mapping[str, str] | None = None) -> RuntimeIdentity:
    values = env if env is not None else os.environ
    environment = str(values.get(TARGET_ENV_VAR) or "").strip()
    if not environment:
        raise EnvironmentIdentityError(
            "TARGET_ENVIRONMENT_MISSING",
            f"required non-secret variable {TARGET_ENV_VAR} is missing",
        )
    if environment not in ALLOWED_ENVIRONMENTS:
        raise EnvironmentIdentityError(
            "TARGET_ENVIRONMENT_INVALID",
            f"{TARGET_ENV_VAR} must be one of: {', '.join(ALLOWED_ENVIRONMENTS)}",
        )

    platform_identity_id = _canonical_uuid(
        _required(values, EXPECTED_PLATFORM_ID_VAR, "EXPECTED_PLATFORM_IDENTITY_MISSING"),
        code="EXPECTED_PLATFORM_IDENTITY_MISSING",
        label=EXPECTED_PLATFORM_ID_VAR,
    )
    expected_host = _required(
        values, EXPECTED_POSTGRES_HOST_VAR, "RUNTIME_POSTGRES_IDENTITY_MISMATCH"
    )
    expected_port_raw = _required(
        values, EXPECTED_POSTGRES_PORT_VAR, "RUNTIME_POSTGRES_IDENTITY_MISMATCH"
    )
    expected_db = _required(
        values, EXPECTED_POSTGRES_DB_VAR, "RUNTIME_POSTGRES_IDENTITY_MISMATCH"
    )
    expected_user = _required(
        values, EXPECTED_POSTGRES_USER_VAR, "RUNTIME_POSTGRES_IDENTITY_MISMATCH"
    )
    try:
        expected_port = int(expected_port_raw)
    except ValueError as exc:
        raise EnvironmentIdentityError(
            "RUNTIME_POSTGRES_IDENTITY_MISMATCH",
            f"{EXPECTED_POSTGRES_PORT_VAR} must be an integer",
        ) from exc
    if expected_port < 1 or expected_port > 65535:
        raise EnvironmentIdentityError(
            "RUNTIME_POSTGRES_IDENTITY_MISMATCH",
            f"{EXPECTED_POSTGRES_PORT_VAR} must be between 1 and 65535",
        )

    effective = {
        "host": str(values.get("POSTGRES_HOST") or "").strip(),
        "port": str(values.get("POSTGRES_PORT") or "").strip(),
        "db": str(values.get("POSTGRES_DB") or "").strip(),
        "user": str(values.get("POSTGRES_USER") or "").strip(),
    }
    expected = {
        "host": expected_host,
        "port": str(expected_port),
        "db": expected_db,
        "user": expected_user,
    }
    mismatched = [name for name in expected if effective[name] != expected[name]]
    if mismatched:
        raise EnvironmentIdentityError(
            "RUNTIME_POSTGRES_IDENTITY_MISMATCH",
            "effective POSTGRES_* values do not match the declared expected "
            f"platform identity fields: {', '.join(sorted(mismatched))}",
        )

    return RuntimeIdentity(
        environment=environment,
        platform_identity_id=platform_identity_id,
        postgres_host=expected_host,
        postgres_port=expected_port,
        postgres_db=expected_db,
        postgres_user=expected_user,
    )


def _read_marker(conn) -> tuple[dict[str, object], list[dict[str, object]]]:
    try:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute("SET LOCAL statement_timeout = '10s'")
            cur.execute(
                """
                SELECT
                    current_database() AS database_name,
                    current_user AS database_user,
                    inet_server_addr()::text AS server_addr,
                    inet_server_port() AS server_port
                """
            )
            connection_row = dict(cur.fetchone() or {})
            cur.execute(
                "SELECT to_regclass('ops_control.environment_identity')::text AS marker_table"
            )
            table_row = dict(cur.fetchone() or {})
            if not table_row.get("marker_table"):
                raise EnvironmentIdentityError(
                    "DB_MARKER_TABLE_MISSING",
                    "ops_control.environment_identity does not exist",
                )
            cur.execute(
                """
                SELECT
                    identity_key,
                    environment,
                    database_identity_id::text AS database_identity_id,
                    database_role,
                    database_name,
                    client_code
                FROM ops_control.environment_identity
                ORDER BY identity_key
                """
            )
            rows = [dict(row) for row in cur.fetchall()]
            return connection_row, rows
    except EnvironmentIdentityError:
        raise
    except Exception as exc:
        raise EnvironmentIdentityError(
            "DB_MARKER_TABLE_MISSING",
            "environment identity marker could not be read",
        ) from exc
    finally:
        try:
            conn.rollback()
        except Exception:
            pass


def _single_marker(rows: list[dict[str, object]]) -> dict[str, object]:
    if not rows:
        raise EnvironmentIdentityError(
            "DB_MARKER_ROW_MISSING",
            "environment identity marker row is missing",
        )
    if len(rows) != 1 or rows[0].get("identity_key") != MARKER_KEY:
        raise EnvironmentIdentityError(
            "DB_MARKER_MULTIPLE_ROWS",
            "environment identity table must contain exactly the primary marker row",
        )
    return rows[0]


def _attest(
    conn,
    runtime: RuntimeIdentity,
    *,
    expected_role: str,
    expected_identity_id: str,
    expected_database_name: str,
    expected_database_user: str,
    expected_client_code: str | None,
) -> AttestedDatabaseIdentity:
    connection, rows = _read_marker(conn)
    marker = _single_marker(rows)

    marker_environment = str(marker.get("environment") or "")
    if marker_environment != runtime.environment:
        raise EnvironmentIdentityError(
            "DB_MARKER_ENVIRONMENT_MISMATCH",
            "runtime target environment does not match the database marker",
        )
    marker_identity_id = _canonical_uuid(
        str(marker.get("database_identity_id") or ""),
        code="DB_MARKER_IDENTITY_MISMATCH",
        label="database marker identity",
    )
    if marker_identity_id != expected_identity_id:
        raise EnvironmentIdentityError(
            "DB_MARKER_IDENTITY_MISMATCH",
            "database marker identity does not match the expected identity",
        )
    if str(marker.get("database_role") or "") != expected_role:
        raise EnvironmentIdentityError(
            "DB_MARKER_ROLE_MISMATCH",
            "database marker role does not match the guarded operation",
        )
    connected_database = str(connection.get("database_name") or "")
    marker_database = str(marker.get("database_name") or "")
    if connected_database != expected_database_name or marker_database != expected_database_name:
        raise EnvironmentIdentityError(
            "DB_MARKER_DATABASE_NAME_MISMATCH",
            "connected and marked database names do not match the expected database",
        )
    connected_user = str(connection.get("database_user") or "")
    if connected_user != expected_database_user:
        raise EnvironmentIdentityError(
            "RUNTIME_POSTGRES_IDENTITY_MISMATCH"
            if expected_role == MARKER_ROLE_PLATFORM
            else "RUNTIME_CLIENT_IDENTITY_MISMATCH",
            "connected database user does not match the expected database user",
        )
    marker_client_code = marker.get("client_code")
    normalized_client_code = (
        str(marker_client_code).strip() if marker_client_code is not None else None
    )
    if normalized_client_code != expected_client_code:
        raise EnvironmentIdentityError(
            "CLIENT_MARKER_CLIENT_CODE_MISMATCH",
            "client database marker does not match the selected client code",
        )

    return AttestedDatabaseIdentity(
        environment=marker_environment,
        database_identity_id=marker_identity_id,
        database_role=expected_role,
        database_name=expected_database_name,
        database_user=expected_database_user,
        client_code=expected_client_code,
    )


def attest_platform_identity(conn, runtime: RuntimeIdentity) -> AttestedDatabaseIdentity:
    return _attest(
        conn,
        runtime,
        expected_role=MARKER_ROLE_PLATFORM,
        expected_identity_id=runtime.platform_identity_id,
        expected_database_name=runtime.postgres_db,
        expected_database_user=runtime.postgres_user,
        expected_client_code=None,
    )


def attest_client_identity(
    conn,
    runtime: RuntimeIdentity,
    expected: ClientIdentityExpectation,
) -> AttestedDatabaseIdentity:
    if not expected.environment or not expected.database_identity_id:
        raise EnvironmentIdentityError(
            "CLIENT_EXPECTED_IDENTITY_MISSING",
            "control-plane client environment and database identity are required",
        )
    if expected.environment not in ALLOWED_ENVIRONMENTS:
        raise EnvironmentIdentityError(
            "CLIENT_EXPECTED_IDENTITY_MISSING",
            "control-plane client environment is invalid",
        )
    if expected.environment != runtime.environment:
        raise EnvironmentIdentityError(
            "RUNTIME_CLIENT_IDENTITY_MISMATCH",
            "runtime target environment does not match the expected client environment",
        )
    expected_identity_id = _canonical_uuid(
        expected.database_identity_id,
        code="CLIENT_EXPECTED_IDENTITY_MISSING",
        label="control-plane client database identity",
    )
    return _attest(
        conn,
        runtime,
        expected_role=MARKER_ROLE_CLIENT,
        expected_identity_id=expected_identity_id,
        expected_database_name=expected.database_name,
        expected_database_user=expected.database_user,
        expected_client_code=expected.client_code,
    )


def require_local_dev(runtime: RuntimeIdentity, *, operation_name: str) -> None:
    if runtime.environment != ENV_LOCAL_DEV:
        raise EnvironmentIdentityError(
            "RUNTIME_CLIENT_IDENTITY_MISMATCH",
            f"{operation_name} is local/dev-only",
        )


def required_production_write_confirmation(
    runtime: RuntimeIdentity,
    *,
    client_code: str,
    operation_name: str,
) -> str:
    return (
        f"{ENV_PRODUCTION}/{runtime.platform_identity_id}/"
        f"{client_code}/{operation_name}"
    )


def require_production_write_confirmation(
    runtime: RuntimeIdentity,
    *,
    client_code: str,
    operation_name: str,
    provided: object,
    dry_run: bool,
) -> None:
    if runtime.environment != ENV_PRODUCTION or dry_run:
        return
    text = str(provided or "")
    if not text:
        raise EnvironmentIdentityError(
            "PRODUCTION_WRITE_CONFIRMATION_MISSING",
            "production write-mode requires an exact explicit confirmation",
        )
    required = required_production_write_confirmation(
        runtime,
        client_code=client_code,
        operation_name=operation_name,
    )
    if text != required:
        raise EnvironmentIdentityError(
            "PRODUCTION_WRITE_CONFIRMATION_MISMATCH",
            "production write confirmation is invalid",
        )


def require_clean_production_worktree(
    runtime: RuntimeIdentity,
    *,
    repo_root: Path,
) -> None:
    if runtime.environment != ENV_PRODUCTION:
        return
    try:
        result = subprocess.run(
            ["git", "status", "--short", "--untracked-files=all"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:
        raise EnvironmentIdentityError(
            "PRODUCTION_WORKTREE_NOT_CLEAN",
            "production operation requires a verifiable clean Git worktree",
        ) from exc
    if result.stdout.strip():
        raise EnvironmentIdentityError(
            "PRODUCTION_WORKTREE_NOT_CLEAN",
            "production operation requires a clean Git worktree",
        )
