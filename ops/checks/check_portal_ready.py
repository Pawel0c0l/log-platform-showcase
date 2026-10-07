#!/usr/bin/env python3
"""Check whether the portal schema and first-admin path are deployment-ready.

This script is safe to run on the server. It checks only platform Postgres
metadata and prints counts, never passwords, tokens, DSNs, or row values.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row


REQUIRED_TABLES = (
    "artifact_users",
    "artifact_roles",
    "artifact_user_roles",
    "portal_clients",
    "portal_user_clients",
    "portal_report_folders",
    "portal_report_folder_users",
    "portal_database_datasets",
    "portal_database_dataset_columns",
    "portal_database_dataset_users",
    "portal_audit_events",
    "portal_groups",
    "portal_group_users",
    "portal_group_clients",
    "portal_report_folder_groups",
    "portal_database_dataset_groups",
)

COUNT_TABLES = (
    "artifact_users",
    "portal_clients",
    "portal_groups",
    "portal_report_folders",
    "portal_database_datasets",
    "portal_audit_events",
)


@dataclass
class CheckResult:
    ok: bool
    message: str


def _dsn() -> str:
    return " ".join(
        [
            f"host={os.getenv('POSTGRES_HOST', '127.0.0.1')}",
            f"port={os.getenv('POSTGRES_PORT', '5432')}",
            f"dbname={os.getenv('POSTGRES_DB', 'logdb')}",
            f"user={os.getenv('POSTGRES_USER', 'loguser')}",
            f"password={os.getenv('POSTGRES_PASSWORD', '')}",
        ]
    )


def _table_exists(cur: Any, table_name: str) -> bool:
    cur.execute("SELECT to_regclass(%s) AS table_regclass", (f"public.{table_name}",))
    row = cur.fetchone() or {}
    return bool(row.get("table_regclass"))


def _table_count(cur: Any, table_name: str) -> int | None:
    if not _table_exists(cur, table_name):
        return None
    cur.execute(f'SELECT count(*) AS count FROM "{table_name}"')
    row = cur.fetchone() or {}
    return int(row.get("count") or 0)


def run_checks() -> list[CheckResult]:
    results: list[CheckResult] = []
    try:
        conn = psycopg.connect(_dsn(), row_factory=dict_row)
    except Exception as exc:
        return [CheckResult(False, f"database connection failed: {type(exc).__name__}")]
    with conn:
        with conn.cursor() as cur:
            results.append(CheckResult(True, "database connection ok"))
            missing = [table for table in REQUIRED_TABLES if not _table_exists(cur, table)]
            if missing:
                results.append(CheckResult(False, "missing required portal tables: " + ", ".join(missing)))
            else:
                results.append(CheckResult(True, f"required portal tables present: {len(REQUIRED_TABLES)}"))
            if _table_exists(cur, "artifact_users"):
                cur.execute(
                    """
                    SELECT count(*) AS count
                    FROM artifact_users
                    WHERE is_active IS TRUE AND is_admin IS TRUE
                    """
                )
                active_admins = int((cur.fetchone() or {}).get("count") or 0)
                if active_admins < 1:
                    results.append(CheckResult(False, "no active admin user found"))
                else:
                    results.append(CheckResult(True, f"active admin users: {active_admins}"))
            for table in COUNT_TABLES:
                count = _table_count(cur, table)
                if count is not None:
                    results.append(CheckResult(True, f"{table} rows: {count}"))
    return results


def main() -> int:
    results = run_checks()
    failed = False
    for result in results:
        prefix = "OK" if result.ok else "FAIL"
        print(f"{prefix}: {result.message}")
        failed = failed or not result.ok
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
