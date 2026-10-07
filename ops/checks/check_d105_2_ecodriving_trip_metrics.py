#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.reports.postprocess import job_d105_2_ecodriving_trip_metrics_migration as job
from jobs.trip_metrics_population_source import (
    TRIP_METRICS_SOURCE_D105_2_ECODRIVING,
    TRIP_METRICS_SOURCE_MISMATCH_REASON,
)


def _client_record(config: job.ClientDbConfig) -> dict[str, Any]:
    return {
        "client_code": config.client_code,
        "client_id": config.client_id,
        "client_db_name": config.client_db_name,
        "trip_metrics_population_source": config.trip_metrics_population_source,
        "required_trip_metrics_population_source": TRIP_METRICS_SOURCE_D105_2_ECODRIVING,
    }


def _check_client(config: job.ClientDbConfig, *, limit: int | None, force_retry_errors: bool) -> dict[str, Any]:
    record = _client_record(config)
    if config.trip_metrics_population_source != TRIP_METRICS_SOURCE_D105_2_ECODRIVING:
        return {
            **record,
            "status": "SKIPPED",
            "skip_reason": TRIP_METRICS_SOURCE_MISMATCH_REASON,
            "client_db_checked": False,
            "safe_for_write": False,
        }

    result: dict[str, Any] = {
        **record,
        "status": "OK",
        "client_db_checked": False,
        "report_table_exists": None,
        "required_report_columns_present": None,
        "missing_report_columns": [],
        "client_trips_table_exists": None,
        "required_client_trips_columns_present": None,
        "missing_client_trips_columns": [],
        "missing_metric_columns": [],
        "candidate_counts": {},
        "safe_for_write": False,
        "attention": [],
    }

    conn = None
    try:
        conn = job._client_business_pg_conn(config)
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute("SET LOCAL statement_timeout = '30s'")
            result["client_db_checked"] = True

            report_exists = job._table_exists(cur, job.REPORT_SCHEMA, job.REPORT_TABLE)
            result["report_table_exists"] = report_exists
            if not report_exists:
                result["status"] = "WARNING"
                result["attention"].append("report_table_missing")
                return result

            report_columns = job._existing_columns(cur, job.REPORT_SCHEMA, job.REPORT_TABLE)
            missing_report = [column for column in job.REPORT_REQUIRED_COLUMNS if column not in report_columns]
            result["missing_report_columns"] = missing_report
            result["required_report_columns_present"] = not missing_report
            if missing_report:
                result["status"] = "WARNING"
                result["attention"].append("required_report_columns_missing")
                return result

            trips_exists = job._table_exists(cur, job.CLIENT_TRIPS_SCHEMA, job.CLIENT_TRIPS_TABLE)
            result["client_trips_table_exists"] = trips_exists
            if not trips_exists:
                result["status"] = "WARNING"
                result["attention"].append("client_trips_missing")
                return result

            trip_columns = job._existing_columns(cur, job.CLIENT_TRIPS_SCHEMA, job.CLIENT_TRIPS_TABLE)
            required_trip_columns = {"client_id", "provider_trip_id", "registration", "start_timestamp", "end_timestamp", "record_id"}
            missing_trip = sorted(required_trip_columns - trip_columns)
            missing_metric = [column for column in job.CLIENT_TRIPS_COUNTER_COLUMNS if column not in trip_columns]
            result["missing_client_trips_columns"] = missing_trip
            result["required_client_trips_columns_present"] = not missing_trip
            result["missing_metric_columns"] = missing_metric
            if missing_trip:
                result["status"] = "WARNING"
                result["attention"].append("required_client_trips_columns_missing")
                return result

            counts = job._analyze_report_rows(
                cur,
                limit=limit,
                force_retry_errors=force_retry_errors,
                has_migrated_column="migrated_to_client_db" in report_columns,
                has_error_column="migrated_to_client_db_error" in report_columns,
            )
            result["candidate_counts"] = counts
            non_zero = counts.get("non_zero_metric_rows", 0)
            result["match_rate_non_zero"] = (
                round(counts.get("matched_rows", 0) / non_zero, 6) if non_zero else None
            )
            if missing_metric:
                result["attention"].append("metric_columns_would_need_write_mode_ddl")
            for key, label in [
                ("invalid_registration_rows", "invalid_registration_rows_present"),
                ("invalid_timestamp_rows", "invalid_timestamp_rows_present"),
                ("invalid_metric_rows", "invalid_metric_rows_present"),
                ("ambiguous_rows", "ambiguous_trip_matches_present"),
                ("unmatched_rows", "unmatched_rows_present"),
            ]:
                if counts.get(key, 0):
                    result["attention"].append(label)
            result["safe_for_write"] = not any(
                counts.get(key, 0)
                for key in [
                    "invalid_registration_rows",
                    "invalid_timestamp_rows",
                    "invalid_metric_rows",
                    "ambiguous_rows",
                    "unmatched_rows",
                ]
            )
            if result["attention"]:
                result["status"] = "WARNING"
            return result
    except Exception as exc:
        result["status"] = "ERROR"
        result["client_db_error"] = f"{type(exc).__name__}: {exc}"
        result["attention"].append("client_db_probe_failed")
        return result
    finally:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only D105.2 EcoDriving trip metrics migration validator."
    )
    parser.add_argument("--client-code", default=None, help="Limit to one client_code")
    parser.add_argument("--limit", type=int, default=None, help="Limit candidate report rows per client")
    parser.add_argument("--force-retry-errors", action="store_true", help="Include all non-migrated error rows in analysis")
    parser.add_argument("--json", action="store_true", help="Print full JSON records")
    parser.add_argument("--strict", action="store_true", help="Exit 1 when any record is not OK or not safe for write")
    args = parser.parse_args()

    with job._platform_pg_conn() as platform_conn:
        clients = job._load_enabled_clients(platform_conn, client_code=job._optional_client_code(args.client_code))
    records = [
        _check_client(config, limit=args.limit, force_retry_errors=args.force_retry_errors)
        for config in clients
    ]

    if args.json:
        print(json.dumps(records, indent=2, sort_keys=True))
    else:
        print("client_code\tselected\tstatus\tcandidates\tnon_zero\tmatched\tsafe_for_write\tattention")
        for record in records:
            counts = record.get("candidate_counts") or {}
            print(
                f"{record.get('client_code') or ''}\t"
                f"{record.get('trip_metrics_population_source') or ''}\t"
                f"{record.get('status') or ''}\t"
                f"{counts.get('candidate_rows', '')}\t"
                f"{counts.get('non_zero_metric_rows', '')}\t"
                f"{counts.get('matched_rows', '')}\t"
                f"{record.get('safe_for_write')}\t"
                f"{','.join(record.get('attention') or [])}"
            )

    if args.strict and any(record.get("status") != "OK" or not record.get("safe_for_write") for record in records):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
