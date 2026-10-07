#!/usr/bin/env python3
"""Grant Workflow B Stage 3 loader permissions for client databases.

Default mode is dry-run: print SQL only. Use --apply to execute.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.reports.stage3 import permissions  # noqa: E402


def _load_dotenv_if_present() -> None:
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path, override=False)
        return
    except ImportError:
        pass
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip("'\"")


def _platform_conn():
    import psycopg
    from psycopg.rows import dict_row

    dsn = (
        f"host={os.getenv('POSTGRES_HOST', '127.0.0.1')} "
        f"port={os.getenv('POSTGRES_PORT', '5432')} "
        f"dbname={os.getenv('POSTGRES_DB', 'logdb')} "
        f"user={os.getenv('POSTGRES_USER', 'loguser')} "
        f"password={os.getenv('POSTGRES_PASSWORD', '')}"
    )
    return psycopg.connect(dsn, row_factory=dict_row)


def _fetch_client_rows(conn) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                client_code,
                client_db_host,
                client_db_port,
                client_db_name,
                client_db_user,
                enabled
            FROM workflow_a_control.client_account
            ORDER BY client_code NULLS LAST, client_db_name
            """
        )
        return [dict(row) for row in cur.fetchall()]


def _target_host(target: permissions.Stage3PermissionTarget, rows_by_code: dict[str, dict[str, Any]]) -> str:
    row = rows_by_code.get(target.client_code) or {}
    return str(row.get("client_db_host") or os.getenv("POSTGRES_HOST", "127.0.0.1"))


def _target_port(target: permissions.Stage3PermissionTarget, rows_by_code: dict[str, dict[str, Any]]) -> int:
    row = rows_by_code.get(target.client_code) or {}
    return int(row.get("client_db_port") or os.getenv("POSTGRES_PORT", "5432"))


def _print_sql_for_target(
    target: permissions.Stage3PermissionTarget,
    *,
    role_name: str,
    grant_existing_schema: bool,
) -> int:
    statements = [permissions.build_ensure_loader_role_sql(role_name)]
    statements.extend(
        permissions.build_stage3_permission_sql(
            client_db_name=target.client_db_name,
            client_db_user=target.client_db_user,
            role_name=role_name,
        )
    )
    if grant_existing_schema:
        statements.extend(
            permissions.build_existing_schema_permission_sql(role_name=role_name)
        )
    print(f"-- client_code={target.client_code} db={target.client_db_name} user={target.client_db_user}")
    for statement in statements:
        print(statement)
    print()
    return len(statements)


def main() -> int:
    _load_dotenv_if_present()

    parser = argparse.ArgumentParser(
        description="Grant Workflow B Stage 3 database permissions for existing clients."
    )
    parser.add_argument("--apply", action="store_true", help="Execute grants. Default prints SQL only.")
    parser.add_argument("--client-code", default=None, help="Restrict to one client_code.")
    parser.add_argument("--role", default=permissions.DEFAULT_STAGE3_LOADER_ROLE)
    parser.add_argument("--include-disabled", action="store_true")
    parser.add_argument("--grant-existing-schema", action="store_true")
    args = parser.parse_args()

    try:
        role_name = permissions.validate_role_name(args.role)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    try:
        with _platform_conn() as platform_conn:
            rows = _fetch_client_rows(platform_conn)
    except Exception as exc:
        print(f"ERROR: Could not read workflow_a_control.client_account: {exc}", file=sys.stderr)
        return 1

    targets, skipped = permissions.rows_to_permission_targets(
        rows,
        include_disabled=args.include_disabled,
        client_code=args.client_code,
    )
    rows_by_code = {str(row.get("client_code") or "").strip(): row for row in rows}

    print(f"Mode: {'APPLY' if args.apply else 'DRY-RUN'}")
    print(f"Role: {role_name}")
    print(f"Clients considered: {len(rows)}")
    print(f"Clients selected: {len(targets)}")
    print(f"Clients skipped: {len(skipped)}")

    failures: list[str] = []
    statements_total = 0

    for target in targets:
        if not args.apply:
            statements_total += _print_sql_for_target(
                target,
                role_name=role_name,
                grant_existing_schema=args.grant_existing_schema,
            )
            continue

        host = _target_host(target, rows_by_code)
        port = _target_port(target, rows_by_code)
        try:
            with permissions.admin_pg_conn(host=host, port=port, dbname="postgres") as admin_conn:
                statements_total += len(
                    permissions.ensure_stage3_permissions_for_client(
                        admin_conn,
                        target.client_code,
                        target.client_db_name,
                        target.client_db_user,
                        role_name=role_name,
                    )
                )
            if args.grant_existing_schema:
                with permissions.admin_pg_conn(
                    host=host,
                    port=port,
                    dbname=target.client_db_name,
                ) as client_admin_conn:
                    statements_total += len(
                        permissions.ensure_existing_schema_permissions(
                            client_admin_conn,
                            role_name=role_name,
                        )
                    )
            print(f"APPLIED: {target.client_code} ({target.client_db_name})")
        except Exception as exc:
            failures.append(f"{target.client_code or target.client_db_name}: {exc}")
            print(f"ERROR: {target.client_code or target.client_db_name}: {exc}", file=sys.stderr)

    if skipped:
        print("Skipped:")
        for row in skipped:
            print(f"  - {row.get('client_code') or '(no client_code)'}: {row['reason']}")

    print("Summary:")
    print(f"  grants generated/applied: {statements_total}")
    print(f"  failures: {len(failures)}")
    if failures:
        print(
            "This command must be run with PostgreSQL admin credentials allowed "
            "to CREATE ROLE and GRANT database privileges.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
