#!/usr/bin/env python3
"""Read-only duplicate diagnostic for Workflow A `client_trips`.

Reports actual constraints/indexes in a client business DB plus duplicate
groups and join-cardinality checks by:

  * record_id
  * (client_id, provider_trip_id)
  * (client_id, registration, start_timestamp, end_timestamp)
  * likely report joins to daily fuel, driver fuel, and notification tables

Run from repo root:

    PYTHONPATH="$PWD" .venv/bin/python ops/tests_manual/diagnose_client_trips_duplicates.py \
      --client-id 5f68d5db-6e2d-421d-8248-544640d3de9f
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


try:
    from dotenv import load_dotenv
except Exception:
    load_dotenv = None

if load_dotenv is not None:
    load_dotenv(REPO_ROOT / ".env")


from jobs.api.telematics.control_plane import load_client_account_config  # noqa: E402
from jobs.api.telematics.secret_resolver import resolve_secret  # noqa: E402


TRIP_KEY_SQL = (
    "COALESCE(t.record_id::text, t.client_id::text || ':' || t.provider_trip_id::text)"
)
TRIP_DAY_SQL = "(t.start_timestamp AT TIME ZONE 'Europe/Warsaw')::date"


def _safe_ident(name: str) -> str:
    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", name or ""):
        raise ValueError(f"Unsafe SQL identifier: {name!r}")
    return name


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _client_business_dsn(cfg) -> str:
    return (
        f"host={cfg.client_db_host} "
        f"port={cfg.client_db_port} "
        f"dbname={cfg.client_db_name} "
        f"user={cfg.client_db_user} "
        f"password={resolve_secret(cfg.client_db_password_secret_ref)}"
    )


def _fetch_all(cur, query, params: tuple = ()) -> List[Dict[str, Any]]:
    cur.execute(query, params)
    return [dict(row) for row in cur.fetchall()]


def _join_cardinality(cur, *, sql_text: str, params: tuple, limit: int) -> Dict[str, Any]:
    cur.execute(
        f"""
        WITH joined AS (
          {sql_text}
        ),
        per_trip AS (
          SELECT trip_key, COUNT(*) AS rows_per_trip
          FROM joined
          GROUP BY trip_key
        )
        SELECT
          COUNT(*) AS joined_rows,
          COUNT(DISTINCT trip_key) AS distinct_trip_rows,
          COALESCE(SUM(rows_per_trip - 1) FILTER (WHERE rows_per_trip > 1), 0) AS multiplied_extra_rows,
          COALESCE(MAX(rows_per_trip), 0) AS max_rows_per_trip,
          COUNT(*) FILTER (WHERE rows_per_trip > 1) AS trips_with_multiple_rows
        FROM per_trip
        """,
        params,
    )
    summary = dict(cur.fetchone())
    cur.execute(
        f"""
        WITH joined AS (
          {sql_text}
        )
        SELECT
          trip_key,
          provider_trip_id,
          registration,
          vehicle_id,
          driver_id,
          start_timestamp,
          end_timestamp,
          COUNT(*) AS rows_per_trip,
          ARRAY_AGG(joined_key ORDER BY joined_key NULLS LAST) FILTER (WHERE joined_key IS NOT NULL) AS joined_keys
        FROM joined
        GROUP BY trip_key, provider_trip_id, registration, vehicle_id, driver_id, start_timestamp, end_timestamp
        HAVING COUNT(*) > 1
        ORDER BY COUNT(*) DESC, start_timestamp NULLS LAST, provider_trip_id
        LIMIT %s
        """,
        (*params, limit),
    )
    summary["top_multiplying_trips"] = [dict(row) for row in cur.fetchall()]
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Diagnose duplicate rows and uniqueness on client_trips."
    )
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--schema", help="Override client DB schema from control plane.")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--output", help="Optional path to write the JSON report.")
    args = parser.parse_args()

    if args.limit <= 0:
        raise ValueError("--limit must be > 0")

    cfg = load_client_account_config(client_id=args.client_id)
    schema = _safe_ident(args.schema or cfg.client_db_schema)

    try:
        import psycopg
        from psycopg import sql as pgsql
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise RuntimeError("Missing dependency: psycopg") from exc

    table_ref = pgsql.SQL("{}.{}").format(
        pgsql.Identifier(schema),
        pgsql.Identifier("client_trips"),
    )
    vehicle_fuel_ref = pgsql.SQL("{}.{}").format(
        pgsql.Identifier(schema),
        pgsql.Identifier("client_vehicle_daily_fuel"),
    )
    driver_fuel_ref = pgsql.SQL("{}.{}").format(
        pgsql.Identifier(schema),
        pgsql.Identifier("client_vehicle_driver_daily_fuel"),
    )
    notifications_ref = pgsql.SQL("{}.{}").format(
        pgsql.Identifier(schema),
        pgsql.Identifier("client_speeding_notifications"),
    )

    conn = psycopg.connect(_client_business_dsn(cfg), row_factory=dict_row)
    try:
        with conn.cursor() as cur:
            constraints = _fetch_all(
                cur,
                """
                SELECT
                  c.conname AS constraint_name,
                  c.contype AS constraint_type,
                  pg_get_constraintdef(c.oid) AS definition
                FROM pg_constraint c
                JOIN pg_class t ON t.oid = c.conrelid
                JOIN pg_namespace n ON n.oid = t.relnamespace
                WHERE n.nspname = %s
                  AND t.relname = 'client_trips'
                ORDER BY c.contype, c.conname
                """,
                (schema,),
            )
            indexes = _fetch_all(
                cur,
                """
                SELECT
                  i.relname AS index_name,
                  ix.indisprimary AS is_primary,
                  ix.indisunique AS is_unique,
                  pg_get_indexdef(ix.indexrelid) AS definition
                FROM pg_index ix
                JOIN pg_class t ON t.oid = ix.indrelid
                JOIN pg_namespace n ON n.oid = t.relnamespace
                JOIN pg_class i ON i.oid = ix.indexrelid
                WHERE n.nspname = %s
                  AND t.relname = 'client_trips'
                ORDER BY ix.indisprimary DESC, ix.indisunique DESC, i.relname
                """,
                (schema,),
            )
            columns = _fetch_all(
                cur,
                """
                SELECT
                  column_name,
                  data_type,
                  is_nullable,
                  column_default
                FROM information_schema.columns
                WHERE table_schema = %s
                  AND table_name = 'client_trips'
                  AND column_name IN ('client_id', 'provider_trip_id', 'record_id',
                                      'registration', 'start_timestamp', 'end_timestamp')
                ORDER BY ordinal_position
                """,
                (schema,),
            )

            cur.execute(pgsql.SQL("SELECT COUNT(*) AS rows_count FROM {}").format(table_ref))
            row_count = int(cur.fetchone()["rows_count"])

            cur.execute(
                pgsql.SQL(
                    """
                    SELECT
                      COUNT(*) FILTER (WHERE record_id IS NULL) AS null_record_id_count,
                      COUNT(*) FILTER (WHERE provider_trip_id IS NULL) AS null_provider_trip_id_count,
                      COUNT(DISTINCT record_id) AS distinct_record_id_count,
                      COUNT(DISTINCT (client_id, provider_trip_id)) AS distinct_provider_trip_key_count
                    FROM {}
                    """
                ).format(table_ref)
            )
            null_and_distinct_counts = dict(cur.fetchone())

            duplicate_record_ids = _fetch_all(
                cur,
                pgsql.SQL(
                    """
                    SELECT
                      record_id::text AS record_id,
                      COUNT(*) AS rows_count,
                      COUNT(DISTINCT client_id) AS distinct_client_ids,
                      COUNT(DISTINCT provider_trip_id) AS distinct_provider_trip_ids,
                      MIN(synced_at) AS min_synced_at,
                      MAX(synced_at) AS max_synced_at,
                      ARRAY_AGG(provider_trip_id ORDER BY provider_trip_id) AS provider_trip_ids,
                      ARRAY_AGG(sync_run_id::text ORDER BY synced_at NULLS LAST) AS sync_run_ids
                    FROM {}
                    WHERE record_id IS NOT NULL
                    GROUP BY record_id
                    HAVING COUNT(*) > 1
                    ORDER BY COUNT(*) DESC, record_id
                    LIMIT %s
                    """
                ).format(table_ref),
                (args.limit,),
            )
            duplicate_provider_trip_ids = _fetch_all(
                cur,
                pgsql.SQL(
                    """
                    SELECT
                      client_id::text AS client_id,
                      provider_trip_id,
                      COUNT(*) AS rows_count,
                      COUNT(DISTINCT record_id) AS distinct_record_ids,
                      MIN(start_timestamp) AS min_start_timestamp,
                      MAX(start_timestamp) AS max_start_timestamp,
                      MIN(synced_at) AS min_synced_at,
                      MAX(synced_at) AS max_synced_at,
                      ARRAY_AGG(record_id::text ORDER BY synced_at NULLS LAST) AS record_ids,
                      ARRAY_AGG(sync_run_id::text ORDER BY synced_at NULLS LAST) AS sync_run_ids
                    FROM {}
                    GROUP BY client_id, provider_trip_id
                    HAVING COUNT(*) > 1
                    ORDER BY COUNT(*) DESC, client_id, provider_trip_id
                    LIMIT %s
                    """
                ).format(table_ref),
                (args.limit,),
            )
            duplicate_trip_windows = _fetch_all(
                cur,
                pgsql.SQL(
                    """
                    SELECT
                      client_id::text AS client_id,
                      registration,
                      start_timestamp,
                      end_timestamp,
                      COUNT(*) AS rows_count,
                      COUNT(DISTINCT provider_trip_id) AS distinct_provider_trip_ids,
                      COUNT(DISTINCT record_id) AS distinct_record_ids,
                      ARRAY_AGG(provider_trip_id ORDER BY provider_trip_id) AS provider_trip_ids,
                      ARRAY_AGG(record_id::text ORDER BY synced_at NULLS LAST) AS record_ids
                    FROM {}
                    GROUP BY client_id, registration, start_timestamp, end_timestamp
                    HAVING COUNT(*) > 1
                    ORDER BY COUNT(*) DESC, client_id, registration, start_timestamp
                    LIMIT %s
                    """
                ).format(table_ref),
                (args.limit,),
            )

            client_id_literal = "%s"
            base_projection = f"""
                SELECT
                  {TRIP_KEY_SQL} AS trip_key,
                  t.provider_trip_id,
                  t.registration,
                  t.vehicle_id::text AS vehicle_id,
                  t.identification_tag_id::text AS driver_id,
                  t.start_timestamp,
                  t.end_timestamp,
            """
            base_from = pgsql.SQL(
                """
                FROM {trips} t
                """
            ).format(trips=table_ref).as_string(cur)
            base_where = f"WHERE t.client_id = {client_id_literal}"
            join_sql = {
                "client_trips_only": {
                    "description": "Baseline raw client_trips rows.",
                    "sql": f"""
                      {base_projection}
                      NULL::text AS joined_key
                      {base_from}
                      {base_where}
                    """,
                },
                "join_vehicle_daily_by_vehicle_day": {
                    "description": "Safe expected join: one vehicle daily fuel row per trip by client_id + vehicle_id + local day.",
                    "sql": f"""
                      {base_projection}
                      vf.record_id::text AS joined_key
                      {base_from}
                      LEFT JOIN {vehicle_fuel_ref.as_string(cur)} vf
                        ON vf.client_id = t.client_id
                       AND vf.vehicle_id = t.vehicle_id::text
                       AND vf.day = {TRIP_DAY_SQL}
                      {base_where}
                    """,
                },
                "join_driver_daily_by_vehicle_driver_day": {
                    "description": "Safe expected driver-fuel join: includes identification_tag_id-derived driver_id, so it should be one row per trip.",
                    "sql": f"""
                      {base_projection}
                      df.record_id::text AS joined_key
                      {base_from}
                      LEFT JOIN {driver_fuel_ref.as_string(cur)} df
                        ON df.client_id = t.client_id
                       AND df.vehicle_id = t.vehicle_id::text
                       AND df.driver_id = t.identification_tag_id::text
                       AND df.day = {TRIP_DAY_SQL}
                      {base_where}
                    """,
                },
                "join_driver_daily_by_vehicle_day_only": {
                    "description": "Unsafe report join: omits driver_id. Trips multiply by number of drivers for the vehicle/day.",
                    "sql": f"""
                      {base_projection}
                      df.record_id::text AS joined_key
                      {base_from}
                      LEFT JOIN {driver_fuel_ref.as_string(cur)} df
                        ON df.client_id = t.client_id
                       AND df.vehicle_id = t.vehicle_id::text
                       AND df.day = {TRIP_DAY_SQL}
                      {base_where}
                    """,
                },
                "join_driver_daily_by_registration_day_only": {
                    "description": "Unsafe report join: joins driver fuel by registration/day only. Trips multiply by drivers/vehicles sharing the registration/day.",
                    "sql": f"""
                      {base_projection}
                      df.record_id::text AS joined_key
                      {base_from}
                      LEFT JOIN {driver_fuel_ref.as_string(cur)} df
                        ON df.client_id = t.client_id
                       AND df.registration = t.registration
                       AND df.day = {TRIP_DAY_SQL}
                      {base_where}
                    """,
                },
                "join_notifications_by_registration_time": {
                    "description": "Unsafe for trip-level reports unless pre-aggregated: one trip multiplies by every notification row inside its time window.",
                    "sql": f"""
                      {base_projection}
                      n.provider_notification_id::text AS joined_key
                      {base_from}
                      LEFT JOIN {notifications_ref.as_string(cur)} n
                        ON n.client_id = t.client_id
                       AND n.registration = t.registration
                       AND n.event_ts >= t.start_timestamp
                       AND n.event_ts <= t.end_timestamp
                      {base_where}
                    """,
                },
            }
            join_cardinality = {}
            for name, spec in join_sql.items():
                cur.execute("SET LOCAL statement_timeout = '60s'")
                summary = _join_cardinality(
                    cur,
                    sql_text=spec["sql"],
                    params=(args.client_id,),
                    limit=args.limit,
                )
                summary["description"] = spec["description"]
                join_cardinality[name] = summary

    finally:
        conn.close()

    report = {
        "client_id": args.client_id,
        "schema": schema,
        "table": "client_trips",
        "row_count": row_count,
        "columns": columns,
        "constraints": constraints,
        "indexes": indexes,
        "null_and_distinct_counts": null_and_distinct_counts,
        "duplicate_rows_by_record_id": duplicate_record_ids,
        "duplicate_rows_by_provider_trip_id": duplicate_provider_trip_ids,
        "duplicate_rows_by_registration_start_end": duplicate_trip_windows,
        "join_cardinality": join_cardinality,
        "note": "Read-only diagnostic. No data is modified or deleted.",
    }
    text = json.dumps(report, indent=2, default=_json_default, sort_keys=True)
    print(text)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
