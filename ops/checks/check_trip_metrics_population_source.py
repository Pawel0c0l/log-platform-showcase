#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.api.telematics.schedule_mutation_surfaces import SCHEDULE_RUN_TYPE_BASE
from jobs.api.telematics.secret_resolver import resolve_secret
from jobs.trip_metrics_population_source import (
    TRIP_METRICS_POPULATION_SOURCE_VALUES,
    TRIP_METRICS_SOURCE_API,
    TRIP_METRICS_SOURCE_DISABLED,
    TRIP_METRICS_SOURCE_REPORT_207,
    normalize_trip_metrics_population_source,
)


def _require_dependency(module: str, feature: str):
    try:
        return __import__(module)
    except ImportError as exc:
        raise RuntimeError(f"Missing dependency for {feature}: {module}") from exc


def _platform_dsn() -> str:
    return (
        f"host={os.getenv('POSTGRES_HOST', '127.0.0.1')} "
        f"port={os.getenv('POSTGRES_PORT', '5432')} "
        f"dbname={os.getenv('POSTGRES_DB', 'logdb')} "
        f"user={os.getenv('POSTGRES_USER', 'loguser')} "
        f"password={os.getenv('POSTGRES_PASSWORD', '')}"
    )


def _platform_conn():
    psycopg = _require_dependency("psycopg", "Postgres connection")
    from psycopg.rows import dict_row

    return psycopg.connect(_platform_dsn(), row_factory=dict_row, autocommit=True)


def _client_dsn(row: dict[str, Any]) -> str:
    password = resolve_secret(row["client_db_password_secret_ref"])
    return (
        f"host={row['client_db_host']} "
        f"port={int(row.get('client_db_port') or 5432)} "
        f"dbname={row['client_db_name']} "
        f"user={row['client_db_user']} "
        f"password={password}"
    )


def _load_clients(client_code: str | None) -> list[dict[str, Any]]:
    """One row per enabled client, decorated with its BASE `trips_sync` schedule.

    The caller maps each row to one record and probes that client's business
    database once, so this must emit each client exactly once — the `ORDER BY`
    over `client_code` is per-client for the same reason.

    **Scoped to `run_type = 'DAILY'` since M5.** The join predicate previously
    matched every cadence of `trips_sync`, which was unambiguous only because the
    schema permitted one. Once a reconciliation cadence exists it would return the
    client twice: two records, two client-database probes, and two potentially
    disagreeing answers for `trips_sync_enabled` / `event_enrichment_mode`. The
    base schedule is the production configuration this diagnostic reports on.

    The scope stays in the JOIN condition, never the WHERE clause: this is a LEFT
    JOIN precisely so an enabled client with no `trips_sync` schedule at all is
    still reported once, with NULL schedule columns. Moving it to WHERE would
    silently drop exactly the clients most worth noticing.
    """
    where = ["ca.enabled IS TRUE"]
    # The base-role parameter binds to the JOIN condition, which is evaluated
    # before the WHERE clause, so it leads the parameter list.
    params: list[Any] = [SCHEDULE_RUN_TYPE_BASE]
    if client_code:
        where.append("ca.client_code = %s")
        params.append(client_code)
    with _platform_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                  ca.client_id::text,
                  ca.client_code,
                  ca.client_name,
                  ca.enabled,
                  ca.client_db_host,
                  ca.client_db_port,
                  ca.client_db_name,
                  ca.client_db_user,
                  ca.client_db_password_secret_ref,
                  ca.client_db_schema,
                  ca.trip_metrics_population_source,
                  cds.enabled AS trips_sync_enabled,
                  cds.event_enrichment_mode
                FROM workflow_a_control.client_account ca
                LEFT JOIN workflow_a_control.client_dataset_schedule cds
                  ON cds.client_id = ca.client_id
                 AND cds.dataset_name = 'trips_sync'
                 AND cds.run_type = %s
                WHERE {' AND '.join(where)}
                ORDER BY ca.client_code NULLS LAST, ca.client_name
                """,
                params,
            )
            return [dict(row) for row in cur.fetchall()]


def _table_exists(cur, schema_name: str, table_name: str) -> bool:
    cur.execute(
        """
        SELECT EXISTS (
          SELECT 1 FROM information_schema.tables
          WHERE table_schema=%s AND table_name=%s
        ) AS exists
        """,
        (schema_name, table_name),
    )
    row = cur.fetchone()
    return bool(row and row.get("exists"))


def _columns(cur, schema_name: str, table_name: str) -> set[str]:
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema=%s AND table_name=%s
        """,
        (schema_name, table_name),
    )
    return {str(row["column_name"]) for row in cur.fetchall()}


def _probe_client_db(row: dict[str, Any]) -> dict[str, Any]:
    psycopg = _require_dependency("psycopg", "Postgres connection")
    from psycopg import sql
    from psycopg.rows import dict_row

    result: dict[str, Any] = {
        "client_db_checked": False,
        "report_207_table_exists": None,
        "report_207_total_rows": None,
        "report_207_unmigrated_rows": None,
        "client_trips_table_exists": None,
        "client_trips_metric_rows": None,
    }
    schema = row.get("client_db_schema") or "public"
    try:
        with psycopg.connect(_client_dsn(row), row_factory=dict_row, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("SET default_transaction_read_only = on")
                cur.execute("SET statement_timeout = '15s'")
                result["client_db_checked"] = True

                report_exists = _table_exists(cur, "telematics_reports", "report_207")
                result["report_207_table_exists"] = report_exists
                if report_exists:
                    report_cols = _columns(cur, "telematics_reports", "report_207")
                    migrated_expr = (
                        sql.SQL("COALESCE(migrated_to_client_db, false) IS TRUE")
                        if "migrated_to_client_db" in report_cols
                        else sql.SQL("false")
                    )
                    cur.execute(
                        sql.SQL(
                            "SELECT COUNT(*)::integer AS total_rows, "
                            "COUNT(*) FILTER (WHERE NOT ({migrated_expr}))::integer AS unmigrated_rows "
                            "FROM {}.{}"
                        ).format(
                            sql.Identifier("telematics_reports"),
                            sql.Identifier("report_207"),
                            migrated_expr=migrated_expr,
                        )
                    )
                    counts = cur.fetchone() or {}
                    result["report_207_total_rows"] = int(counts.get("total_rows") or 0)
                    result["report_207_unmigrated_rows"] = int(counts.get("unmigrated_rows") or 0)

                trips_exists = _table_exists(cur, schema, "client_trips")
                result["client_trips_table_exists"] = trips_exists
                if trips_exists:
                    trip_cols = _columns(cur, schema, "client_trips")
                    metric_cols = [
                        c
                        for c in (
                            "speeding_140_160_count",
                            "speeding_160_170_count",
                            "speeding_170_plus_count",
                            "high_rpm_events_count",
                            "overrev_events_count",
                        )
                        if c in trip_cols
                    ]
                    if metric_cols:
                        predicates = [
                            sql.SQL("COALESCE({}, 0) <> 0").format(sql.Identifier(c))
                            for c in metric_cols
                        ]
                        cur.execute(
                            sql.SQL("SELECT COUNT(*)::integer AS metric_rows FROM {}.{} WHERE ").format(
                                sql.Identifier(schema),
                                sql.Identifier("client_trips"),
                            )
                            + sql.SQL(" OR ").join(predicates)
                        )
                        result["client_trips_metric_rows"] = int((cur.fetchone() or {}).get("metric_rows") or 0)
                    else:
                        result["client_trips_metric_rows"] = 0
    except Exception as exc:
        result["client_db_error"] = f"{type(exc).__name__}: {exc}"
    return result


def _attention(row: dict[str, Any], probe: dict[str, Any], selected: str | None, valid: bool) -> list[str]:
    items: list[str] = []
    if not valid:
        items.append("invalid_trip_metrics_population_source")
    if selected == TRIP_METRICS_SOURCE_API and (probe.get("report_207_total_rows") or 0) > 0:
        items.append("review_report_207_data_with_api_default")
    if selected == TRIP_METRICS_SOURCE_REPORT_207 and not probe.get("report_207_table_exists"):
        items.append("report_207_selected_but_report_table_missing")
    if selected == TRIP_METRICS_SOURCE_DISABLED and row.get("trips_sync_enabled") is True:
        items.append("disabled_metrics_source_with_trips_sync_enabled")
    if probe.get("client_db_error"):
        items.append("client_db_probe_failed")
    return items


def _record(row: dict[str, Any], *, probe_client_dbs: bool) -> dict[str, Any]:
    raw_source = row.get("trip_metrics_population_source")
    valid = True
    try:
        selected = normalize_trip_metrics_population_source(raw_source)
    except ValueError:
        selected = None
        valid = False
    probe = _probe_client_db(row) if probe_client_dbs else {"client_db_checked": False}
    return {
        "client_code": row.get("client_code"),
        "client_id": row.get("client_id"),
        "client_name": row.get("client_name"),
        "trip_metrics_population_source": raw_source,
        "normalized_trip_metrics_population_source": selected,
        "valid": valid,
        "allowed_values": list(TRIP_METRICS_POPULATION_SOURCE_VALUES),
        "trips_sync_enabled": row.get("trips_sync_enabled"),
        "event_enrichment_mode": row.get("event_enrichment_mode"),
        **probe,
        "attention": _attention(row, probe, selected, valid),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only check for client trip_metrics_population_source settings."
    )
    parser.add_argument("--client-code", default=None, help="Limit to one client_code")
    parser.add_argument("--no-client-db-probe", action="store_true", help="Only read platform control-plane rows")
    parser.add_argument("--json", action="store_true", help="Print JSON records")
    parser.add_argument("--strict", action="store_true", help="Exit 1 when any invalid value or attention item is found")
    args = parser.parse_args()

    rows = _load_clients(args.client_code)
    records = [_record(row, probe_client_dbs=not args.no_client_db_probe) for row in rows]
    if args.json:
        print(json.dumps(records, indent=2, sort_keys=True))
    else:
        print("client_code	selected	valid	client_db_checked	attention")
        for record in records:
            print(
                f"{record.get('client_code') or ''}	"
                f"{record.get('normalized_trip_metrics_population_source') or record.get('trip_metrics_population_source') or ''}	"
                f"{str(record.get('valid')).lower()}	"
                f"{str(record.get('client_db_checked')).lower()}	"
                f"{','.join(record.get('attention') or [])}"
            )

    if args.strict and any((not r.get("valid")) or r.get("attention") for r in records):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
