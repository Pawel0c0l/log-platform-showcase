#!/usr/bin/env python3
"""
Workflow A — multi-step `record_id` rollout for client business DBs.

This is the safe path to populate, validate, and lock down the `record_id`
column on existing client tables. It must be run **after** the additive DDL
in `db/client_business/014_add_record_id_and_synced_at.sql` has been applied
to each client (use `scripts/apply_client_business_migrations.py --apply`).

Steps (in order):

  1. backfill            — fill NULL `record_id` from the canonical Python
                           formula in `jobs.api.telematics.record_id`.
  2. validate            — assert no NULL `record_id` and no duplicate values.
  3. set-not-null        — `ALTER TABLE ... ALTER COLUMN record_id SET NOT NULL`.
                           Refuses unless validate passes.
  4. create-unique-index — `CREATE UNIQUE INDEX CONCURRENTLY` on `record_id`.
                           Must run outside a transaction (autocommit=True).

Use `all` to run 1→4 in order (fails fast on any per-table problem).

The script connects to each client business DB as the platform admin user
(`POSTGRES_USER` / `POSTGRES_PASSWORD`) — same model as
`apply_client_business_migrations.py` — because ALTER TABLE / CREATE INDEX
require ownership.

Examples:

  # Dry-run a backfill across all enabled clients, all four tables:
  python scripts/backfill_record_id.py backfill

  # Apply backfill for one client:
  python scripts/backfill_record_id.py backfill --client-name DELTA --apply

  # Run the full rollout for one client, one table:
  python scripts/backfill_record_id.py all --client-name DELTA \\
      --table client_trips --apply

  # Show NULL/duplicate counts only:
  python scripts/backfill_record_id.py validate --client-name DELTA
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# .env loading (optional, never overrides existing env)
# ---------------------------------------------------------------------------

def _load_dotenv_if_present() -> None:
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


_load_dotenv_if_present()


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _bold(t: str) -> str: return f"\033[1m{t}\033[0m"
def _green(t: str) -> str: return f"\033[32m{t}\033[0m"
def _yellow(t: str) -> str: return f"\033[33m{t}\033[0m"
def _red(t: str) -> str: return f"\033[31m{t}\033[0m"
def _info(m: str) -> None: print(f"  {_green('OK')}    {m}")
def _warn(m: str) -> None: print(f"  {_yellow('WARN')}  {m}")
def _err(m: str) -> None: print(f"  {_red('FAIL')}  {m}")
def _dry(m: str) -> None: print(f"  {_yellow('[DRY-RUN]')} {m}")


def _section(title: str) -> None:
    print(f"\n{'-' * 72}")
    print(f"  {_bold(title)}")
    print(f"{'-' * 72}")


def _require(module_name: str, pip_name: Optional[str] = None):
    try:
        return __import__(module_name)
    except ImportError:
        pkg = pip_name or module_name
        print(f"ERROR: Missing dependency '{module_name}'. Install: pip install {pkg}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Backfill specs (one per Workflow A table)
# ---------------------------------------------------------------------------

from jobs.api.telematics import record_id as record_id_mod
from jobs.api.telematics import registry


@dataclass(frozen=True)
class BackfillSpec:
    """How to backfill record_id on a single table.

    Attributes:
      table_name:  Logical table (validated against registry.TABLES).
      pk_columns:  Composite primary key. Doubles as the formula input names
                   and the WHERE clause for the UPDATE.
      compute:     Callable that returns a uuid.UUID for one row. Receives
                   kwargs whose names equal `pk_columns` (and possibly with
                   string casts already applied for non-string PG types).
      pk_select_expr: SQL fragment used in SELECT to read the PK columns. May
                   include casts (e.g. `vehicle_id::text AS vehicle_id`).
    """
    table_name: str
    pk_columns: Tuple[str, ...]
    compute: Callable[..., Any]
    pk_select_expr: Tuple[str, ...]


# Per-table specs. For non-text PG types we project to text in the SELECT so
# the Python formula always sees a stable string.
SPECS: Dict[str, BackfillSpec] = {
    "client_trips": BackfillSpec(
        table_name="client_trips",
        pk_columns=("client_id", "provider_trip_id"),
        compute=record_id_mod.for_client_trips,
        pk_select_expr=("client_id::text AS client_id", "provider_trip_id"),
    ),
    "client_speeding_notifications": BackfillSpec(
        table_name="client_speeding_notifications",
        pk_columns=("client_id", "provider_notification_id"),
        compute=record_id_mod.for_client_speeding_notifications,
        pk_select_expr=(
            "client_id::text AS client_id",
            "provider_notification_id::text AS provider_notification_id",
        ),
    ),
    "client_vehicle_daily_fuel": BackfillSpec(
        table_name="client_vehicle_daily_fuel",
        pk_columns=("client_id", "vehicle_id", "day"),
        compute=record_id_mod.for_client_vehicle_daily_fuel,
        pk_select_expr=(
            "client_id::text AS client_id",
            "vehicle_id",
            "day",
        ),
    ),
    "client_vehicle_driver_daily_fuel": BackfillSpec(
        table_name="client_vehicle_driver_daily_fuel",
        pk_columns=("client_id", "vehicle_id", "driver_id", "day"),
        compute=record_id_mod.for_client_vehicle_driver_daily_fuel,
        pk_select_expr=(
            "client_id::text AS client_id",
            "vehicle_id",
            "driver_id",
            "day",
        ),
    ),
}

# Sanity: every spec must be a known table in the registry.
for _name in SPECS:
    if _name not in registry.TABLES:
        raise RuntimeError(f"Backfill spec for unregistered table: {_name}")


# ---------------------------------------------------------------------------
# Platform DB / per-client DB connections
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ClientRow:
    client_id: str
    client_name: str
    client_db_host: str
    client_db_port: int
    client_db_name: str
    client_db_schema: str


def _platform_dsn() -> str:
    h = os.getenv("POSTGRES_HOST", "127.0.0.1")
    p = os.getenv("POSTGRES_PORT", "5432")
    d = os.getenv("POSTGRES_DB", "logdb")
    u = os.getenv("POSTGRES_USER", "loguser")
    pw = os.getenv("POSTGRES_PASSWORD", "")
    return f"host={h} port={p} dbname={d} user={u} password={pw}"


def _admin_dsn(*, host: str, port: int, dbname: str) -> str:
    u = os.getenv("POSTGRES_USER", "loguser")
    pw = os.getenv("POSTGRES_PASSWORD", "")
    return f"host={host} port={port} dbname={dbname} user={u} password={pw}"


def list_enabled_clients(
    *, only_client_id: Optional[str], only_client_name: Optional[str],
) -> List[ClientRow]:
    psycopg = _require("psycopg")
    from psycopg.rows import dict_row
    conn = psycopg.connect(_platform_dsn(), row_factory=dict_row, autocommit=True)
    try:
        sql_text = (
            "SELECT client_id::text AS client_id, client_name, "
            "       client_db_host, client_db_port, client_db_name, "
            "       client_db_schema "
            "FROM workflow_a_control.client_account "
            "WHERE enabled=true "
        )
        params: list = []
        if only_client_id:
            sql_text += "AND client_id::text=%s "
            params.append(only_client_id)
        if only_client_name:
            sql_text += "AND client_name=%s "
            params.append(only_client_name)
        sql_text += "ORDER BY client_name"
        with conn.cursor() as cur:
            cur.execute(sql_text, tuple(params))
            return [
                ClientRow(
                    client_id=r["client_id"],
                    client_name=r["client_name"],
                    client_db_host=r["client_db_host"],
                    client_db_port=int(r["client_db_port"]),
                    client_db_name=r["client_db_name"],
                    client_db_schema=r["client_db_schema"] or "public",
                )
                for r in cur.fetchall()
            ]
    finally:
        conn.close()


def _client_admin_conn(client: ClientRow, *, autocommit: bool):
    psycopg = _require("psycopg")
    return psycopg.connect(
        _admin_dsn(
            host=client.client_db_host,
            port=client.client_db_port,
            dbname=client.client_db_name,
        ),
        autocommit=autocommit,
    )


def _table_exists(conn, schema: str, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema=%s AND table_name=%s",
            (schema, table),
        )
        return cur.fetchone() is not None


def _column_exists(conn, schema: str, table: str, column: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name=%s AND column_name=%s",
            (schema, table, column),
        )
        return cur.fetchone() is not None


# ---------------------------------------------------------------------------
# Step 1: backfill
# ---------------------------------------------------------------------------

def step_backfill(
    client: ClientRow, spec: BackfillSpec, *, batch_size: int, apply: bool,
) -> Dict[str, int]:
    """Backfill `record_id` for one (client, table). Returns counters."""
    psycopg = _require("psycopg")
    from psycopg import sql as pgsql

    schema = client.client_db_schema
    table = spec.table_name
    counters = {"scanned": 0, "filled": 0, "skipped": 0, "remaining_null": 0}

    print(f"  ↳ backfill {schema}.{table}")

    # autocommit=False so each batch can be wrapped in a small transaction.
    conn = _client_admin_conn(client, autocommit=False)
    try:
        if not _table_exists(conn, schema, table):
            _warn(f"table {schema}.{table} does not exist — skipping")
            return counters
        if not _column_exists(conn, schema, table, "record_id"):
            _err(f"column record_id missing on {schema}.{table} — apply DDL 014 first")
            counters["error"] = 1  # type: ignore[assignment]
            return counters

        select_sql = pgsql.SQL("SELECT {cols} FROM {schema}.{table} "
                               "WHERE record_id IS NULL "
                               "ORDER BY {pk_order} "
                               "LIMIT %s").format(
            cols=pgsql.SQL(", ").join(pgsql.SQL(c) for c in spec.pk_select_expr),
            schema=pgsql.Identifier(schema),
            table=pgsql.Identifier(table),
            pk_order=pgsql.SQL(", ").join(pgsql.Identifier(c) for c in spec.pk_columns),
        )

        update_where = pgsql.SQL(" AND ").join(
            pgsql.SQL("{col} = %s").format(col=pgsql.Identifier(c))
            for c in spec.pk_columns
        )
        update_sql = pgsql.SQL(
            "UPDATE {schema}.{table} SET record_id = %s "
            "WHERE record_id IS NULL AND " + "{where}"
        ).format(
            schema=pgsql.Identifier(schema),
            table=pgsql.Identifier(table),
            where=update_where,
        )

        while True:
            with conn.cursor() as cur:
                cur.execute(select_sql, (batch_size,))
                rows = cur.fetchall()
            if not rows:
                break

            counters["scanned"] += len(rows)

            update_rows: List[Tuple[Any, ...]] = []
            for r in rows:
                kwargs = {col: r[i] for i, col in enumerate(spec.pk_columns)}
                try:
                    rid = spec.compute(**kwargs)
                except ValueError as e:
                    _err(f"cannot compute record_id for row {kwargs}: {e}")
                    counters["skipped"] += 1
                    continue
                update_rows.append((str(rid), *(kwargs[c] for c in spec.pk_columns)))

            if not update_rows:
                # All rows in batch had bad business keys; advance is impossible
                # without DELETE — surface and stop to avoid an infinite loop.
                _err(
                    f"no rows in batch could be backfilled (bad business keys?); "
                    f"resolve manually before re-running"
                )
                break

            if not apply:
                _dry(f"would UPDATE {len(update_rows)} rows in {schema}.{table}")
                # break out: dry-run does not progress, so stop after one batch
                break

            with conn.cursor() as cur:
                cur.executemany(update_sql, update_rows)
            conn.commit()
            counters["filled"] += len(update_rows)
            print(f"    filled batch of {len(update_rows)}; total filled={counters['filled']}")

            # If the batch was smaller than batch_size, we drained the table.
            if len(rows) < batch_size:
                break

        with conn.cursor() as cur:
            cur.execute(
                pgsql.SQL("SELECT COUNT(*) FROM {schema}.{table} "
                          "WHERE record_id IS NULL").format(
                    schema=pgsql.Identifier(schema),
                    table=pgsql.Identifier(table),
                )
            )
            counters["remaining_null"] = int(cur.fetchone()[0])

    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()

    return counters


# ---------------------------------------------------------------------------
# Step 2: validate
# ---------------------------------------------------------------------------

def step_validate(client: ClientRow, spec: BackfillSpec) -> Dict[str, int]:
    """Return {'null_count': N, 'duplicate_count': M, 'duplicate_examples': [...]}."""
    psycopg = _require("psycopg")
    from psycopg import sql as pgsql

    schema = client.client_db_schema
    table = spec.table_name
    out = {"null_count": 0, "duplicate_count": 0}

    print(f"  ↳ validate {schema}.{table}")
    conn = _client_admin_conn(client, autocommit=True)
    try:
        if not _table_exists(conn, schema, table):
            _warn(f"table {schema}.{table} does not exist — skipping")
            return out
        if not _column_exists(conn, schema, table, "record_id"):
            _err(f"column record_id missing on {schema}.{table}")
            out["error"] = 1  # type: ignore[assignment]
            return out

        with conn.cursor() as cur:
            cur.execute(
                pgsql.SQL("SELECT COUNT(*) FROM {schema}.{table} "
                          "WHERE record_id IS NULL").format(
                    schema=pgsql.Identifier(schema),
                    table=pgsql.Identifier(table),
                )
            )
            out["null_count"] = int(cur.fetchone()[0])

            cur.execute(
                pgsql.SQL(
                    "SELECT record_id, COUNT(*) AS n FROM {schema}.{table} "
                    "WHERE record_id IS NOT NULL "
                    "GROUP BY record_id HAVING COUNT(*) > 1 LIMIT 5"
                ).format(
                    schema=pgsql.Identifier(schema),
                    table=pgsql.Identifier(table),
                )
            )
            dups = cur.fetchall()
            out["duplicate_count"] = len(dups)
            if dups:
                out["duplicate_examples"] = [  # type: ignore[assignment]
                    f"{r[0]} x{r[1]}" for r in dups
                ]
    finally:
        conn.close()
    return out


# ---------------------------------------------------------------------------
# Step 3: SET NOT NULL
# ---------------------------------------------------------------------------

def step_set_not_null(client: ClientRow, spec: BackfillSpec, *, apply: bool) -> bool:
    psycopg = _require("psycopg")
    from psycopg import sql as pgsql

    schema = client.client_db_schema
    table = spec.table_name
    print(f"  ↳ SET NOT NULL on {schema}.{table}.record_id")

    if not apply:
        _dry(f"would ALTER TABLE {schema}.{table} ALTER COLUMN record_id SET NOT NULL")
        return True

    conn = _client_admin_conn(client, autocommit=False)
    try:
        with conn.cursor() as cur:
            cur.execute(
                pgsql.SQL("ALTER TABLE {schema}.{table} "
                          "ALTER COLUMN record_id SET NOT NULL").format(
                    schema=pgsql.Identifier(schema),
                    table=pgsql.Identifier(table),
                )
            )
        conn.commit()
        _info("done")
        return True
    except Exception as e:
        conn.rollback()
        _err(f"SET NOT NULL failed: {e}")
        return False
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Step 4: CREATE UNIQUE INDEX CONCURRENTLY
# ---------------------------------------------------------------------------

def step_create_unique_index(
    client: ClientRow, spec: BackfillSpec, *, apply: bool,
) -> bool:
    psycopg = _require("psycopg")
    from psycopg import sql as pgsql

    schema = client.client_db_schema
    table = spec.table_name
    index_name = f"uq_{table}_record_id"

    print(f"  ↳ CREATE UNIQUE INDEX CONCURRENTLY {index_name} ON {schema}.{table}(record_id)")

    if not apply:
        _dry(f"would create unique index {index_name}")
        return True

    # CONCURRENTLY cannot run inside a transaction.
    conn = _client_admin_conn(client, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                pgsql.SQL(
                    "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {idx} "
                    "ON {schema}.{table} (record_id)"
                ).format(
                    idx=pgsql.Identifier(index_name),
                    schema=pgsql.Identifier(schema),
                    table=pgsql.Identifier(table),
                )
            )
        _info("done")
        return True
    except Exception as e:
        _err(f"CREATE UNIQUE INDEX CONCURRENTLY failed: {e}")
        # If creation fails partway, leave operator to inspect / drop the
        # invalid index manually with: REINDEX or DROP INDEX CONCURRENTLY.
        return False
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def _resolve_specs(table_filter: Optional[str]) -> List[BackfillSpec]:
    if table_filter:
        if table_filter not in SPECS:
            print(f"ERROR: unknown --table {table_filter!r}. "
                  f"Known: {sorted(SPECS)}")
            sys.exit(2)
        return [SPECS[table_filter]]
    return list(SPECS.values())


def cmd_backfill(args: argparse.Namespace) -> int:
    clients = list_enabled_clients(
        only_client_id=args.client_id, only_client_name=args.client_name,
    )
    if not clients:
        print("No enabled clients matched filter.")
        return 0
    specs = _resolve_specs(args.table)

    any_error = False
    for client in clients:
        _section(f"backfill — {client.client_name} ({client.client_db_name}@{client.client_db_host})")
        for spec in specs:
            counters = step_backfill(client, spec, batch_size=args.batch_size, apply=args.apply)
            if counters.get("error"):
                any_error = True
            print(f"    counters: {counters}")
    return 1 if any_error else 0


def cmd_validate(args: argparse.Namespace) -> int:
    clients = list_enabled_clients(
        only_client_id=args.client_id, only_client_name=args.client_name,
    )
    if not clients:
        print("No enabled clients matched filter.")
        return 0
    specs = _resolve_specs(args.table)

    any_problem = False
    for client in clients:
        _section(f"validate — {client.client_name}")
        for spec in specs:
            out = step_validate(client, spec)
            null_count = out.get("null_count", 0)
            dup_count = out.get("duplicate_count", 0)
            if out.get("error") or null_count or dup_count:
                any_problem = True
                _err(f"{spec.table_name}: nulls={null_count} duplicates={dup_count}")
                if "duplicate_examples" in out:
                    for ex in out["duplicate_examples"]:  # type: ignore[index]
                        print(f"      dup: {ex}")
            else:
                _info(f"{spec.table_name}: nulls=0 duplicates=0")
    return 1 if any_problem else 0


def cmd_set_not_null(args: argparse.Namespace) -> int:
    clients = list_enabled_clients(
        only_client_id=args.client_id, only_client_name=args.client_name,
    )
    if not clients:
        print("No enabled clients matched filter.")
        return 0
    specs = _resolve_specs(args.table)

    any_error = False
    for client in clients:
        _section(f"set-not-null — {client.client_name}")

        # Refuse if validate would fail.
        for spec in specs:
            v = step_validate(client, spec)
            if v.get("error") or v.get("null_count", 0) or v.get("duplicate_count", 0):
                _err(f"refusing SET NOT NULL on {spec.table_name}: validate not clean")
                any_error = True
                continue
            ok = step_set_not_null(client, spec, apply=args.apply)
            if not ok:
                any_error = True
    return 1 if any_error else 0


def cmd_create_unique_index(args: argparse.Namespace) -> int:
    clients = list_enabled_clients(
        only_client_id=args.client_id, only_client_name=args.client_name,
    )
    if not clients:
        print("No enabled clients matched filter.")
        return 0
    specs = _resolve_specs(args.table)

    any_error = False
    for client in clients:
        _section(f"create-unique-index — {client.client_name}")
        for spec in specs:
            ok = step_create_unique_index(client, spec, apply=args.apply)
            if not ok:
                any_error = True
    return 1 if any_error else 0


def cmd_all(args: argparse.Namespace) -> int:
    rc = cmd_backfill(args)
    if rc != 0:
        _err("backfill step had errors — stopping")
        return rc
    rc = cmd_validate(args)
    if rc != 0:
        _err("validate step had problems — stopping (no SET NOT NULL / index)")
        return rc
    rc = cmd_set_not_null(args)
    if rc != 0:
        _err("set-not-null step failed — stopping")
        return rc
    rc = cmd_create_unique_index(args)
    return rc


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Multi-step record_id rollout for Workflow A client business DBs.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    def _common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--client-id", default=None, help="Limit to one client by UUID.")
        p.add_argument("--client-name", default=None, help="Limit to one client by name.")
        p.add_argument("--table", default=None,
                       choices=sorted(SPECS.keys()),
                       help="Limit to one table.")
        p.add_argument("--apply", action="store_true",
                       help="Actually write changes (default: dry-run).")

    p_b = sub.add_parser("backfill", help="Fill NULL record_id from the Python formula.")
    _common(p_b)
    p_b.add_argument("--batch-size", type=int, default=1000)
    p_b.set_defaults(func=cmd_backfill)

    p_v = sub.add_parser("validate", help="Check NULL count and uniqueness.")
    _common(p_v)
    p_v.set_defaults(func=cmd_validate)

    p_n = sub.add_parser("set-not-null", help="ALTER COLUMN record_id SET NOT NULL.")
    _common(p_n)
    p_n.set_defaults(func=cmd_set_not_null)

    p_i = sub.add_parser("create-unique-index",
                         help="CREATE UNIQUE INDEX CONCURRENTLY on record_id.")
    _common(p_i)
    p_i.set_defaults(func=cmd_create_unique_index)

    p_a = sub.add_parser("all", help="Run backfill -> validate -> set-not-null -> index.")
    _common(p_a)
    p_a.add_argument("--batch-size", type=int, default=1000)
    p_a.set_defaults(func=cmd_all)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
