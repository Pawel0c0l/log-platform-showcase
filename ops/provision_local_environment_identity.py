#!/usr/bin/env python3
"""Provision fail-closed environment identity markers for local/dev only.

Exit codes: 0 success/inspect, 2 identity/config refusal, 3 database/runtime error.
The default is inspect-only. Writes require the explicit --apply flag.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from uuid import UUID

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api.timezone_utils import set_pg_session_timezone
from jobs.api.telematics.secret_resolver import resolve_secret
from jobs.common import environment_identity
from jobs.reports.stage3 import permissions

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _load_dotenv_if_present() -> None:
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv
    except Exception:
        return
    load_dotenv(env_path, override=False)


def _canonical_uuid(value: str, label: str) -> str:
    try:
        canonical = str(UUID(value))
    except (TypeError, ValueError) as exc:
        raise environment_identity.EnvironmentIdentityError(
            "PROVISION_IDENTITY_INVALID", f"{label} must be a canonical UUID"
        ) from exc
    if canonical != value:
        raise environment_identity.EnvironmentIdentityError(
            "PROVISION_IDENTITY_INVALID",
            f"{label} must use canonical lowercase UUID form",
        )
    return canonical


def _platform_conn():
    import psycopg
    from psycopg.rows import dict_row

    return set_pg_session_timezone(
        psycopg.connect(
            host=os.environ["POSTGRES_HOST"],
            port=int(os.environ["POSTGRES_PORT"]),
            dbname=os.environ["POSTGRES_DB"],
            user=os.environ["POSTGRES_USER"],
            password=os.getenv("POSTGRES_PASSWORD", ""),
            row_factory=dict_row,
        )
    )


def _client_admin_conn(*, host: str, port: int, database_name: str):
    import psycopg
    from psycopg.rows import dict_row

    return set_pg_session_timezone(
        psycopg.connect(
            host=host,
            port=port,
            dbname=database_name,
            user=os.environ["POSTGRES_USER"],
            password=os.getenv("POSTGRES_PASSWORD", ""),
            row_factory=dict_row,
        )
    )


def _client_runtime_conn(config: dict[str, object]):
    import psycopg
    from psycopg.rows import dict_row

    password = resolve_secret(str(config["client_db_password_secret_ref"]))
    return set_pg_session_timezone(
        psycopg.connect(
            host=str(config["client_db_host"]),
            port=int(config["client_db_port"]),
            dbname=str(config["client_db_name"]),
            user=str(config["client_db_user"]),
            password=password,
            sslmode="prefer",
            row_factory=dict_row,
        )
    )


def _require_local_host(host: str, label: str) -> None:
    if host not in LOCAL_HOSTS:
        raise environment_identity.EnvironmentIdentityError(
            "LOCAL_PROVISIONING_NONLOCAL_HOST",
            f"{label} must be a loopback host for local/dev provisioning",
        )


def _connection_identity(conn) -> dict[str, object]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT current_database() AS database_name,
                   current_user AS database_user,
                   inet_server_addr()::text AS server_addr,
                   inet_server_port() AS server_port
            """
        )
        return dict(cur.fetchone() or {})


def _load_client_config(platform_conn, client_code: str) -> dict[str, object]:
    with platform_conn.cursor() as cur:
        cur.execute(
            """
            SELECT client_code, client_db_host, client_db_port, client_db_name,
                   client_db_user, client_db_password_secret_ref,
                   client_db_environment, client_db_identity_id::text AS client_db_identity_id
            FROM workflow_a_control.client_account
            WHERE enabled IS TRUE AND client_code = %s
            """,
            (client_code,),
        )
        row = cur.fetchone()
    if not row:
        raise environment_identity.EnvironmentIdentityError(
            "CLIENT_EXPECTED_IDENTITY_MISSING",
            "enabled client account was not found",
        )
    return dict(row)


def _read_existing_marker(conn) -> dict[str, object] | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT to_regclass('ops_control.environment_identity')::text AS marker_table"
        )
        if not (cur.fetchone() or {}).get("marker_table"):
            raise environment_identity.EnvironmentIdentityError(
                "DB_MARKER_TABLE_MISSING",
                "required marker migration has not been applied",
            )
        cur.execute(
            """
            SELECT identity_key, environment,
                   database_identity_id::text AS database_identity_id,
                   database_role, database_name, client_code
            FROM ops_control.environment_identity
            ORDER BY identity_key
            """
        )
        rows = [dict(row) for row in cur.fetchall()]
    if not rows:
        return None
    if len(rows) != 1:
        raise environment_identity.EnvironmentIdentityError(
            "DB_MARKER_MULTIPLE_ROWS", "marker table must contain at most one row"
        )
    return rows[0]


def _require_empty_or_exact(
    existing: dict[str, object] | None,
    expected: dict[str, object],
    label: str,
) -> bool:
    if existing is None:
        return True
    comparable = {
        key: existing.get(key)
        for key in (
            "identity_key",
            "environment",
            "database_identity_id",
            "database_role",
            "database_name",
            "client_code",
        )
    }
    if comparable != expected:
        raise environment_identity.EnvironmentIdentityError(
            "PROVISION_MARKER_CONFLICT",
            f"existing {label} marker conflicts with requested local/dev identity",
        )
    return False


def _insert_marker(conn, marker: dict[str, object], provisioned_by: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops_control.environment_identity (
                identity_key, environment, database_identity_id, database_role,
                database_name, client_code, provisioned_by, notes
            ) VALUES (%s, %s, %s::uuid, %s, %s, %s, %s, %s)
            """,
            (
                marker["identity_key"],
                marker["environment"],
                marker["database_identity_id"],
                marker["database_role"],
                marker["database_name"],
                marker["client_code"],
                provisioned_by,
                "Explicit local/dev provisioning",
            ),
        )


def provision_local_identity(
    *,
    client_code: str,
    platform_identity_id: str,
    client_identity_id: str,
    apply: bool,
) -> dict[str, object]:
    runtime = environment_identity.load_runtime_identity()
    environment_identity.require_local_dev(
        runtime, operation_name="environment_identity_provisioning"
    )
    if runtime.platform_identity_id != platform_identity_id:
        raise environment_identity.EnvironmentIdentityError(
            "PROVISION_IDENTITY_MISMATCH",
            "platform identity argument does not match runtime expectation",
        )
    _require_local_host(runtime.postgres_host, "platform host")

    with _platform_conn() as platform_conn:
        platform_identity = _connection_identity(platform_conn)
        if (
            platform_identity.get("database_name") != runtime.postgres_db
            or platform_identity.get("database_user") != runtime.postgres_user
        ):
            raise environment_identity.EnvironmentIdentityError(
                "RUNTIME_POSTGRES_IDENTITY_MISMATCH",
                "connected platform database identity does not match runtime expectation",
            )
        config = _load_client_config(platform_conn, client_code)
        _require_local_host(str(config["client_db_host"]), "client host")

        with _client_runtime_conn(config) as runtime_client_conn:
            client_runtime_identity = _connection_identity(runtime_client_conn)
        if (
            client_runtime_identity.get("database_name") != config["client_db_name"]
            or client_runtime_identity.get("database_user") != config["client_db_user"]
        ):
            raise environment_identity.EnvironmentIdentityError(
                "RUNTIME_CLIENT_IDENTITY_MISMATCH",
                "connected client database identity does not match control-plane coordinates",
            )

        platform_marker = {
            "identity_key": "primary",
            "environment": "local_dev",
            "database_identity_id": platform_identity_id,
            "database_role": "platform",
            "database_name": runtime.postgres_db,
            "client_code": None,
        }
        client_marker = {
            "identity_key": "primary",
            "environment": "local_dev",
            "database_identity_id": client_identity_id,
            "database_role": "client_business",
            "database_name": str(config["client_db_name"]),
            "client_code": client_code,
        }

        platform_needs_insert = _require_empty_or_exact(
            _read_existing_marker(platform_conn), platform_marker, "platform"
        )
        expected_environment = config.get("client_db_environment")
        expected_identity = config.get("client_db_identity_id")
        if expected_environment not in (None, "local_dev") or expected_identity not in (
            None,
            client_identity_id,
        ):
            raise environment_identity.EnvironmentIdentityError(
                "PROVISION_MARKER_CONFLICT",
                "control-plane client identity conflicts with requested local/dev identity",
            )

        with _client_admin_conn(
            host=str(config["client_db_host"]),
            port=int(config["client_db_port"]),
            database_name=str(config["client_db_name"]),
        ) as client_admin_conn:
            client_admin_identity = _connection_identity(client_admin_conn)
            if client_admin_identity.get("database_name") != config["client_db_name"]:
                raise environment_identity.EnvironmentIdentityError(
                    "DB_MARKER_DATABASE_NAME_MISMATCH",
                    "connected client admin database name is unexpected",
                )
            client_needs_insert = _require_empty_or_exact(
                _read_existing_marker(client_admin_conn), client_marker, "client"
            )

            result = {
                "environment": "local_dev",
                "client_code": client_code,
                "platform_marker_action": "insert" if platform_needs_insert else "unchanged",
                "client_marker_action": "insert" if client_needs_insert else "unchanged",
                "client_expectation_action": (
                    "update"
                    if expected_environment is None or expected_identity is None
                    else "unchanged"
                ),
                "applied": False,
            }
            if not apply:
                platform_conn.rollback()
                client_admin_conn.rollback()
                return result

            if client_needs_insert:
                _insert_marker(
                    client_admin_conn,
                    client_marker,
                    str(client_admin_identity.get("database_user") or "local_admin"),
                )
            quoted_user = permissions.quote_ident(str(config["client_db_user"]))
            with client_admin_conn.cursor() as cur:
                cur.execute("REVOKE ALL ON TABLE ops_control.environment_identity FROM PUBLIC")
                cur.execute(f"GRANT USAGE ON SCHEMA ops_control TO {quoted_user}")
                cur.execute(
                    f"GRANT SELECT ON TABLE ops_control.environment_identity TO {quoted_user}"
                )
            client_admin_conn.commit()

        if platform_needs_insert:
            _insert_marker(platform_conn, platform_marker, runtime.postgres_user)
        with platform_conn.cursor() as cur:
            cur.execute(
                """
                UPDATE workflow_a_control.client_account
                SET client_db_environment = 'local_dev',
                    client_db_identity_id = %s::uuid
                WHERE enabled IS TRUE
                  AND client_code = %s
                  AND (client_db_environment IS NULL OR client_db_environment = 'local_dev')
                  AND (client_db_identity_id IS NULL OR client_db_identity_id = %s::uuid)
                """,
                (client_identity_id, client_code, client_identity_id),
            )
            if cur.rowcount != 1:
                raise environment_identity.EnvironmentIdentityError(
                    "PROVISION_MARKER_CONFLICT",
                    "control-plane client identity update was refused",
                )
        platform_conn.commit()
        result["applied"] = True
        return result


def main() -> int:
    _load_dotenv_if_present()
    parser = argparse.ArgumentParser(
        description="Inspect or explicitly provision local/dev environment identity markers."
    )
    parser.add_argument("--client-code", required=True)
    parser.add_argument("--platform-identity-id", required=True)
    parser.add_argument("--client-identity-id", required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Explicitly confirm local/dev marker writes; default is inspect-only.",
    )
    args = parser.parse_args()
    try:
        result = provision_local_identity(
            client_code=args.client_code.strip(),
            platform_identity_id=_canonical_uuid(
                args.platform_identity_id.strip(), "platform identity"
            ),
            client_identity_id=_canonical_uuid(
                args.client_identity_id.strip(), "client identity"
            ),
            apply=bool(args.apply),
        )
    except environment_identity.EnvironmentIdentityError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3

    mode = "APPLIED" if result["applied"] else "INSPECT_ONLY"
    print(
        f"{mode}: environment=local_dev client_code={result['client_code']} "
        f"platform_marker={result['platform_marker_action']} "
        f"client_marker={result['client_marker_action']} "
        f"client_expectation={result['client_expectation_action']}"
    )
    print("Identity UUID values intentionally omitted from output.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
