from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

from api.timezone_utils import set_pg_session_timezone


DEFAULT_STAGE3_LOADER_ROLE = "workflow_b_stage3_loader"
DESTINATION_SCHEMA = "telematics_reports"
SAFE_ROLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class Stage3PermissionTarget:
    client_code: str
    client_db_name: str
    client_db_user: str
    enabled: bool = True


def quote_ident(value: str) -> str:
    text = str(value or "")
    if not text:
        raise ValueError("SQL identifier cannot be empty")
    if "\x00" in text:
        raise ValueError("SQL identifier cannot contain NUL bytes")
    return '"' + text.replace('"', '""') + '"'


def quote_literal(value: str) -> str:
    text = str(value)
    if "\x00" in text:
        raise ValueError("SQL literal cannot contain NUL bytes")
    return "'" + text.replace("'", "''") + "'"


def validate_role_name(role_name: str) -> str:
    role = str(role_name or "").strip()
    if not SAFE_ROLE_RE.match(role):
        raise ValueError(
            f"Unsafe Stage 3 loader role name: {role_name!r}. "
            "Use letters, digits, and underscores; first character must be a letter or underscore."
        )
    return role


def build_ensure_loader_role_sql(role_name: str = DEFAULT_STAGE3_LOADER_ROLE) -> str:
    role = validate_role_name(role_name)
    return (
        "DO $$\n"
        "BEGIN\n"
        f"  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {quote_literal(role)}) THEN\n"
        f"    CREATE ROLE {quote_ident(role)};\n"
        "  END IF;\n"
        "END\n"
        "$$;"
    )


def build_stage3_permission_sql(
    *,
    client_db_name: str,
    client_db_user: str,
    role_name: str = DEFAULT_STAGE3_LOADER_ROLE,
) -> list[str]:
    role = validate_role_name(role_name)
    db_name = str(client_db_name or "").strip()
    db_user = str(client_db_user or "").strip()
    if not db_name:
        raise ValueError("client_db_name is required")
    if not db_user:
        raise ValueError("client_db_user is required")
    return [
        f"GRANT {quote_ident(role)} TO {quote_ident(db_user)};",
        f"GRANT CONNECT ON DATABASE {quote_ident(db_name)} TO {quote_ident(role)};",
    ]


def build_existing_schema_permission_sql(
    *,
    schema_name: str = DESTINATION_SCHEMA,
    role_name: str = DEFAULT_STAGE3_LOADER_ROLE,
) -> list[str]:
    role = validate_role_name(role_name)
    schema = str(schema_name or "").strip()
    if not schema:
        raise ValueError("schema_name is required")
    return [
        f"GRANT USAGE ON SCHEMA {quote_ident(schema)} TO {quote_ident(role)};",
        (
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "
            f"{quote_ident(schema)} TO {quote_ident(role)};"
        ),
    ]


def ensure_workflow_b_stage3_loader_role(
    admin_conn,
    role_name: str = DEFAULT_STAGE3_LOADER_ROLE,
) -> None:
    with admin_conn.cursor() as cur:
        cur.execute(build_ensure_loader_role_sql(role_name))


def ensure_stage3_permissions_for_client(
    admin_conn,
    client_code: str,
    client_db_name: str,
    client_db_user: str,
    role_name: str = DEFAULT_STAGE3_LOADER_ROLE,
) -> list[str]:
    statements = [build_ensure_loader_role_sql(role_name)] + build_stage3_permission_sql(
        client_db_name=client_db_name,
        client_db_user=client_db_user,
        role_name=role_name,
    )
    with admin_conn.cursor() as cur:
        for statement in statements:
            cur.execute(statement)
    return statements


def ensure_existing_schema_permissions(
    admin_conn,
    *,
    schema_name: str = DESTINATION_SCHEMA,
    role_name: str = DEFAULT_STAGE3_LOADER_ROLE,
) -> list[str]:
    statements = build_existing_schema_permission_sql(
        schema_name=schema_name,
        role_name=role_name,
    )
    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s",
            (schema_name,),
        )
        if not cur.fetchone():
            return []
        for statement in statements:
            cur.execute(statement)
    return statements


def admin_dsn(*, host: str, port: int | str, dbname: str = "postgres") -> str:
    return (
        f"host={host} "
        f"port={port} "
        f"dbname={dbname} "
        f"user={os.getenv('POSTGRES_USER', 'loguser')} "
        f"password={os.getenv('POSTGRES_PASSWORD', '')}"
    )


def admin_pg_conn(*, host: str, port: int | str, dbname: str = "postgres"):
    import psycopg
    from psycopg.rows import dict_row

    return set_pg_session_timezone(
        psycopg.connect(admin_dsn(host=host, port=port, dbname=dbname), row_factory=dict_row)
    )


def rows_to_permission_targets(
    rows: list[dict[str, Any]],
    *,
    include_disabled: bool = False,
    client_code: str | None = None,
) -> tuple[list[Stage3PermissionTarget], list[dict[str, Any]]]:
    selected: list[Stage3PermissionTarget] = []
    skipped: list[dict[str, Any]] = []
    wanted_code = str(client_code or "").strip()
    for row in rows:
        code = str(row.get("client_code") or "").strip()
        db_name = str(row.get("client_db_name") or "").strip()
        db_user = str(row.get("client_db_user") or "").strip()
        enabled = bool(row.get("enabled"))
        if wanted_code and code != wanted_code:
            skipped.append({"client_code": code, "reason": "client_code_filter"})
            continue
        if not include_disabled and not enabled:
            skipped.append({"client_code": code, "reason": "disabled"})
            continue
        if not db_name or not db_user:
            skipped.append({"client_code": code, "reason": "missing_client_db_name_or_user"})
            continue
        selected.append(
            Stage3PermissionTarget(
                client_code=code,
                client_db_name=db_name,
                client_db_user=db_user,
                enabled=enabled,
            )
        )
    return selected, skipped
