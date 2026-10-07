#!/usr/bin/env python3
"""Validation for migration 051 (Eco Driving portal permissions).

Two layers:

1. Static checks on the migration SQL text (always run): additive-only,
   idempotent ``ADD COLUMN IF NOT EXISTS``, NOT NULL DEFAULT FALSE on both
   ``portal_user_clients`` and ``portal_group_clients``, no backfill/UPDATE, no
   automatic GRANT, and documented comments.

2. Live application against an ISOLATED throwaway database in the local
   Dockerized Postgres (auto-skipped if Docker is unavailable). The migration is
   NEVER applied to logdb / local_dev portal tables: a fresh database is created,
   minimal pre-051 grant tables are seeded with an existing row, migration 051 is
   applied, columns/defaults are verified, idempotent re-run is checked, and the
   database is dropped.

Run:

    cd /opt/log-platform
    PYTHONPATH="$PWD" python3 ops/tests_manual/test_portal_eco_driving_permissions_migration.py
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

MIGRATION = REPO_ROOT / "db" / "migrations" / "051_portal_eco_driving_permissions.sql"
ECO_COLUMNS = ("can_view_eco_ranking", "can_view_eco_trip_details", "can_view_eco_trip_routes")
GRANT_TABLES = ("portal_user_clients", "portal_group_clients")


# --- static checks -----------------------------------------------------------

def test_migration_is_additive_and_idempotent() -> None:
    import re

    sql = MIGRATION.read_text(encoding="utf-8")
    upper = sql.upper()
    for table in GRANT_TABLES:
        assert f"ALTER TABLE {table}".upper() in upper, table
    for col in ECO_COLUMNS:
        # one ADD COLUMN IF NOT EXISTS per table (2 occurrences)
        needle = f"ADD COLUMN IF NOT EXISTS {col} BOOLEAN NOT NULL DEFAULT FALSE".upper()
        assert upper.count(needle) == 2, col

    # Executable SQL only: drop `--` comment lines and COMMENT ON string literals
    # (documentation text legitimately mentions words like "grant"/"update").
    code_lines = [ln for ln in sql.splitlines() if not ln.strip().startswith("--")]
    code = re.sub(r"COMMENT ON[\s\S]*?;", "", "\n".join(code_lines), flags=re.IGNORECASE)
    code_upper = code.upper()
    for banned in ("GRANT", "UPDATE", "INSERT", "DELETE", "DROP COLUMN", "DROP TABLE"):
        assert banned not in code_upper, f"migration must not contain {banned}"

    # each new column is documented
    for table in GRANT_TABLES:
        for col in ECO_COLUMNS:
            assert f"COMMENT ON COLUMN {table}.{col}" in sql, f"{table}.{col}"
    print("PASS: migration 051 is additive, idempotent, non-backfilling, and documented")


# --- live isolated-database application --------------------------------------

def _docker_container() -> str | None:
    try:
        out = subprocess.run(
            ["docker", "compose", "ps", "-q", "postgres"],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=30,
        )
    except Exception:
        return None
    cid = (out.stdout or "").strip()
    return cid or None


class _Psql:
    def __init__(self, cid: str, user: str) -> None:
        self.cid = cid
        self.user = user

    def run(self, dbname: str, sql: str, *, tuples_only: bool = False) -> str:
        args = ["docker", "exec", "-i", self.cid, "psql", "-v", "ON_ERROR_STOP=1",
                "-U", self.user, "-d", dbname]
        if tuples_only:
            args += ["-tA"]
        args += ["-c", sql] if not tuples_only else ["-c", sql]
        proc = subprocess.run(args, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            raise RuntimeError(f"psql failed ({dbname}): {proc.stderr.strip()}")
        return proc.stdout

    def run_stdin(self, dbname: str, sql: str) -> str:
        args = ["docker", "exec", "-i", self.cid, "psql", "-v", "ON_ERROR_STOP=1",
                "-U", self.user, "-d", dbname]
        proc = subprocess.run(args, input=sql, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            raise RuntimeError(f"psql failed ({dbname}): {proc.stderr.strip()}")
        return proc.stdout


_PREREQ_DDL = """
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE TABLE portal_user_clients (
  user_id UUID NOT NULL,
  client_code TEXT NOT NULL,
  can_view_database BOOLEAN NOT NULL DEFAULT true,
  can_view_reports BOOLEAN NOT NULL DEFAULT true,
  can_export_database BOOLEAN NOT NULL DEFAULT false,
  PRIMARY KEY (user_id, client_code)
);
CREATE TABLE portal_group_clients (
  group_id UUID NOT NULL,
  client_code TEXT NOT NULL,
  can_view_database BOOLEAN NOT NULL DEFAULT true,
  can_view_reports BOOLEAN NOT NULL DEFAULT true,
  can_export_database BOOLEAN NOT NULL DEFAULT false,
  PRIMARY KEY (group_id, client_code)
);
INSERT INTO portal_user_clients (user_id, client_code)
  VALUES (gen_random_uuid(), 'EXIST01');
INSERT INTO portal_group_clients (group_id, client_code)
  VALUES (gen_random_uuid(), 'EXIST01');
"""


def test_migration_applies_to_isolated_database() -> None:
    cid = _docker_container()
    if not cid:
        print("SKIP: Docker Postgres not available; live isolated migration check skipped")
        return
    user = os.getenv("POSTGRES_USER", "loguser")
    admin_db = os.getenv("POSTGRES_DB", "logdb")
    psql = _Psql(cid, user)
    test_db = f"eco_rbac_mig_test_{os.getpid()}"

    psql.run(admin_db, f"DROP DATABASE IF EXISTS {test_db};")
    psql.run(admin_db, f"CREATE DATABASE {test_db};")
    try:
        psql.run_stdin(test_db, _PREREQ_DDL)
        migration_sql = MIGRATION.read_text(encoding="utf-8")
        # First application.
        psql.run_stdin(test_db, migration_sql)

        col_query = (
            "SELECT table_name||':'||column_name||':'||data_type||':'||is_nullable||':'||"
            "coalesce(column_default,'') "
            "FROM information_schema.columns "
            "WHERE table_schema='public' AND column_name LIKE 'can_view_eco_%' "
            "ORDER BY table_name, column_name;"
        )
        rows = [r for r in psql.run(test_db, col_query, tuples_only=True).splitlines() if r.strip()]
        assert len(rows) == 6, rows
        for r in rows:
            _tbl, _col, dtype, nullable, default = r.split(":", 4)
            assert dtype == "boolean", r
            assert nullable == "NO", r
            assert "false" in default.lower(), r

        # Existing rows receive FALSE for the new columns; old flags unchanged.
        existing = psql.run(
            test_db,
            "SELECT can_view_database::text||','||can_view_eco_ranking::text||','||"
            "can_view_eco_trip_details::text||','||can_view_eco_trip_routes::text "
            "FROM portal_user_clients;",
            tuples_only=True,
        ).strip()
        assert existing == "true,false,false,false", existing

        # Idempotent re-run must not error and must not change the column set.
        psql.run_stdin(test_db, migration_sql)
        rows2 = [r for r in psql.run(test_db, col_query, tuples_only=True).splitlines() if r.strip()]
        assert len(rows2) == 6, rows2
        print("PASS: migration 051 applies cleanly and idempotently to an isolated database")
    finally:
        psql.run(admin_db, f"DROP DATABASE IF EXISTS {test_db};")


def main() -> None:
    test_migration_is_additive_and_idempotent()
    test_migration_applies_to_isolated_database()
    print("OK - migration 051 validation passed")


if __name__ == "__main__":
    main()
