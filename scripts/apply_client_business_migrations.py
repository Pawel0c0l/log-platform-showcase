#!/usr/bin/env python3
"""
Workflow A — per-client business migration runner.

Reads enabled clients from platform `workflow_a_control.client_account` and applies
any pending `db/client_business/*.sql` files to each client's business database.
Tracks applied filenames in a per-client `public.schema_migrations` table that
mirrors the platform-side pattern from `ops/db_migrate.sh`.

This is the canonical way to roll out new client-business DDL to existing
clients. New clients receive the current final base DDL during onboarding via
`scripts/onboard_workflow_a_client.py`; onboarding records superseded
client_trips rollout filenames in `public.schema_migrations` so this runner
does not later apply obsolete cleanup files to newly created databases.

Connection model:
  Connects to each client DB as the platform admin (`POSTGRES_USER`/`POSTGRES_PASSWORD`)
  because DDL (CREATE TABLE / ALTER TABLE / CREATE INDEX) requires ownership.
  This matches `apply_client_ddl(...)` in onboard_workflow_a_client.py.

Usage:
  # Dry-run across all enabled clients (default):
  python scripts/apply_client_business_migrations.py

  # Apply across all enabled clients:
  python scripts/apply_client_business_migrations.py --apply

  # Limit to one client by client_id or client_name:
  python scripts/apply_client_business_migrations.py --client-id <UUID> --apply
  python scripts/apply_client_business_migrations.py --client-name DELTA --apply

  # Show pending migrations per client without applying:
  python scripts/apply_client_business_migrations.py --list
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


REPO_ROOT = Path(__file__).resolve().parent.parent
CLIENT_BUSINESS_DIR = REPO_ROOT / "db" / "client_business"


# ---------------------------------------------------------------------------
# .env loading (optional, never overrides existing env)
# ---------------------------------------------------------------------------

def _load_dotenv_if_present() -> None:
    # DETERMINISTIC TESTS MUST NOT READ REAL OPERATOR CREDENTIALS.
    #     A schema/rollout test supplies its own synthetic loopback
    #     configuration and has no business opening the host's `.env` — not
    #     because a value would be printed, but because a test that can read a
    #     production secret file is a test that can act on production
    #     configuration by accident. `LOG_PLATFORM_NO_DOTENV=1` is the explicit
    #     opt-out those tests set; no operator workflow sets it, so ordinary
    #     manual and scheduled execution is unchanged.
    if os.environ.get("LOG_PLATFORM_NO_DOTENV") == "1":
        return

    dotenv_path = REPO_ROOT / ".env"
    if not dotenv_path.exists():
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(dotenv_path, override=False)
    except ImportError:
        try:
            with open(dotenv_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip().strip("'\"")
                    if key and key not in os.environ:
                        os.environ[key] = value
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _bold(text: str) -> str:
    return f"\033[1m{text}\033[0m"


def _green(text: str) -> str:
    return f"\033[32m{text}\033[0m"


def _yellow(text: str) -> str:
    return f"\033[33m{text}\033[0m"


def _red(text: str) -> str:
    return f"\033[31m{text}\033[0m"


def _info(msg: str) -> None:
    print(f"  {_green('OK')}    {msg}")


def _warn(msg: str) -> None:
    print(f"  {_yellow('WARN')}  {msg}")


def _err(msg: str) -> None:
    print(f"  {_red('FAIL')}  {msg}")


def _section(title: str) -> None:
    print(f"\n{'-' * 72}")
    print(f"  {_bold(title)}")
    print(f"{'-' * 72}")


def _dry(msg: str) -> None:
    print(f"  {_yellow('[DRY-RUN]')} {msg}")


def _require(module_name: str, pip_name: Optional[str] = None):
    try:
        return __import__(module_name)
    except ImportError:
        pkg = pip_name or module_name
        print(f"ERROR: Missing dependency '{module_name}'. Install: pip install {pkg}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Migration discovery
# ---------------------------------------------------------------------------

def _discover_client_business_files() -> List[Path]:
    """Return all *.sql files in db/client_business/ in lexical order."""
    if not CLIENT_BUSINESS_DIR.is_dir():
        print(f"ERROR: Missing directory: {CLIENT_BUSINESS_DIR}")
        sys.exit(1)
    files = sorted(p for p in CLIENT_BUSINESS_DIR.glob("*.sql") if p.is_file())
    if not files:
        print(f"WARN: No *.sql files found in {CLIENT_BUSINESS_DIR}")
    return files


# ---------------------------------------------------------------------------
# Platform DB — list of enabled clients
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ClientRow:
    client_id: str
    client_name: str
    client_db_host: str
    client_db_port: int
    client_db_name: str


def _platform_dsn() -> str:
    h = os.getenv("POSTGRES_HOST", "127.0.0.1")
    p = os.getenv("POSTGRES_PORT", "5432")
    d = os.getenv("POSTGRES_DB", "logdb")
    u = os.getenv("POSTGRES_USER", "loguser")
    pw = os.getenv("POSTGRES_PASSWORD", "")
    return f"host={h} port={p} dbname={d} user={u} password={pw}"


def _platform_conn():
    psycopg = _require("psycopg")
    from psycopg.rows import dict_row
    return psycopg.connect(_platform_dsn(), row_factory=dict_row, autocommit=True)


def list_enabled_clients(
    *,
    only_client_id: Optional[str],
    only_client_name: Optional[str],
) -> List[ClientRow]:
    conn = _platform_conn()
    try:
        with conn.cursor() as cur:
            sql = (
                "SELECT client_id::text AS client_id, client_name, "
                "       client_db_host, client_db_port, client_db_name "
                "FROM workflow_a_control.client_account "
                "WHERE enabled=true "
            )
            params: list = []
            if only_client_id:
                sql += "AND client_id::text=%s "
                params.append(only_client_id)
            if only_client_name:
                sql += "AND client_name=%s "
                params.append(only_client_name)
            sql += "ORDER BY client_name"
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
            return [
                ClientRow(
                    client_id=r["client_id"],
                    client_name=r["client_name"],
                    client_db_host=r["client_db_host"],
                    client_db_port=int(r["client_db_port"]),
                    client_db_name=r["client_db_name"],
                )
                for r in rows
            ]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Per-client DB connection (admin DSN)
# ---------------------------------------------------------------------------

def _admin_dsn(*, host: str, port: int, dbname: str) -> str:
    u = os.getenv("POSTGRES_USER", "loguser")
    pw = os.getenv("POSTGRES_PASSWORD", "")
    return f"host={host} port={port} dbname={dbname} user={u} password={pw}"


def _client_db_admin_conn(client: ClientRow):
    """Connect to client business DB as platform admin user (autocommit=False)."""
    psycopg = _require("psycopg")
    return psycopg.connect(
        _admin_dsn(
            host=client.client_db_host,
            port=client.client_db_port,
            dbname=client.client_db_name,
        ),
        autocommit=False,
    )


# ---------------------------------------------------------------------------
# Per-client schema_migrations
# ---------------------------------------------------------------------------

def _ensure_schema_migrations_table(conn) -> None:
    """Create public.schema_migrations if missing. Commits."""
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS public.schema_migrations (
              filename TEXT PRIMARY KEY,
              applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
    conn.commit()


def _get_applied_filenames(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT filename FROM public.schema_migrations")
        return {r[0] for r in cur.fetchall()}


def _record_applied(conn, filename: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO public.schema_migrations(filename) VALUES (%s) "
            "ON CONFLICT (filename) DO NOTHING",
            (filename,),
        )


# ---------------------------------------------------------------------------
# Per-client apply
# ---------------------------------------------------------------------------

@dataclass
class ClientResult:
    client_name: str
    client_id: str
    pending: List[str]
    applied: List[str]
    skipped: List[str]
    error: Optional[str] = None


def apply_for_client(
    client: ClientRow,
    files: List[Path],
    *,
    apply: bool,
) -> ClientResult:
    _section(f"Client: {client.client_name}  ({client.client_id})  db={client.client_db_name}@{client.client_db_host}")

    result = ClientResult(
        client_name=client.client_name,
        client_id=client.client_id,
        pending=[],
        applied=[],
        skipped=[],
    )

    try:
        conn = _client_db_admin_conn(client)
    except Exception as e:
        msg = f"Cannot connect to client DB: {e}"
        _err(msg)
        result.error = msg
        return result

    try:
        _ensure_schema_migrations_table(conn)
        applied_set = _get_applied_filenames(conn)

        for fpath in files:
            fname = fpath.name
            if fname in applied_set:
                result.skipped.append(fname)
                print(f"  SKIP   {fname}  (already applied)")
                continue

            result.pending.append(fname)

            if not apply:
                _dry(f"Would apply {fname}")
                continue

            sql_text = fpath.read_text(encoding="utf-8")
            try:
                with conn.cursor() as cur:
                    cur.execute(sql_text)
                    _record_applied(conn, fname)
                conn.commit()
                result.applied.append(fname)
                _info(f"APPLY  {fname}")
            except Exception as e:
                conn.rollback()
                msg = f"Failed to apply {fname}: {e}"
                _err(msg)
                result.error = msg
                return result
    finally:
        conn.close()

    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    # Loaded HERE rather than at import time: importing this module — which a
    # deterministic test does, for its DDL and grant declarations — must have
    # no environment side effect at all. Execution as a program still resolves
    # configuration exactly as before, because nothing above `main` reads it.
    _load_dotenv_if_present()

    parser = argparse.ArgumentParser(
        description="Apply pending db/client_business/*.sql files to each enabled client's business DB.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually apply pending migrations. Default: dry-run.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Only print pending migrations per client; no changes (implies dry-run).",
    )
    parser.add_argument("--client-id", default=None, help="Limit to a single client by UUID.")
    parser.add_argument("--client-name", default=None, help="Limit to a single client by client_name.")
    parser.add_argument(
        "--migration",
        action="append",
        default=[],
        help="Limit to an exact migration filename; may be repeated.",
    )
    args = parser.parse_args()

    if args.list and args.apply:
        print("ERROR: --list and --apply are mutually exclusive.")
        return 2

    apply_mode = args.apply and not args.list
    mode_label = _green("APPLY") if apply_mode else _yellow("DRY-RUN" if not args.list else "LIST")
    print(f"\n{'=' * 72}")
    print(f"  Workflow A — Per-client business migrations  [{mode_label}]")
    print(f"{'=' * 72}")

    files = _discover_client_business_files()
    if args.migration:
        requested = set(args.migration)
        available = {path.name for path in files}
        missing = sorted(requested - available)
        if missing:
            print("ERROR: Unknown client-business migration(s): " + ", ".join(missing))
            return 2
        files = [path for path in files if path.name in requested]
    if not files:
        return 0

    print(f"\nFound {len(files)} *.sql file(s) in {CLIENT_BUSINESS_DIR}:")
    for f in files:
        print(f"  - {f.name}")

    try:
        clients = list_enabled_clients(
            only_client_id=args.client_id,
            only_client_name=args.client_name,
        )
    except Exception as e:
        print(f"\nERROR: Cannot read client_account from platform DB: {e}")
        return 1

    if not clients:
        print("\nNo enabled clients matched filter — nothing to do.")
        return 0

    print(f"\nMatched {len(clients)} enabled client(s).")

    results: List[ClientResult] = []
    for client in clients:
        results.append(apply_for_client(client, files, apply=apply_mode))

    # ------------------- Summary -------------------
    _section("Summary")
    any_error = False
    for r in results:
        line = f"  {r.client_name:<24}  applied={len(r.applied):>2}  pending={len(r.pending):>2}  skipped={len(r.skipped):>2}"
        if r.error:
            any_error = True
            print(f"  {_red('FAIL')} {line}  error={r.error}")
        else:
            print(f"  {_green('OK')}   {line}")

    if not apply_mode:
        print()
        if any(r.pending for r in results):
            print(f"  Re-run with {_bold('--apply')} to apply pending migrations.")
        else:
            print("  All clients are up to date.")
    return 1 if any_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
