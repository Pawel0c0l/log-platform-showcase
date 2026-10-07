#!/usr/bin/env python3
"""Trace a Workflow B file from email attachment through Stage 3 lookup.

The script starts from a filename, not from an old raw_file_id, so operators can
diagnose the current fresh ingest row after cleanup and reruns.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.reports.stage3 import job_stage3  # noqa: E402


def _platform_pg_conn():
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


def _json_default(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _print_json(title: str, value: Any) -> None:
    print(f"\n## {title}")
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=_json_default))


def _fetch_raw_files(cur, *, filename: str) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT
            rf.id::text AS raw_file_id,
            rf.original_filename,
            rf.account,
            rf.sha256 AS attachment_sha256,
            rf.content_fingerprint,
            rf.dedup_basis,
            rf.report_key,
            rf.raw_path,
            rf.normalized_csv_path,
            rf.status AS stage1_status,
            rf.error AS stage1_error,
            rf.stage2_status,
            rf.stage2_report_type,
            rf.client_code,
            rf.stage2_scores,
            rf.stage2_schema_diff,
            rf.stage2_pending_reason,
            rf.stage2_updated_at,
            rf.stage3_status,
            rf.stage3_started_at,
            rf.stage3_finished_at,
            rf.stage3_error,
            NULL::timestamptz AS created_at,
            NULL::timestamptz AS updated_at,
            im.id AS imap_message_row_id,
            im.uid AS imap_uid,
            im.message_id,
            im.from_addr,
            im.subject,
            im.internal_date,
            im.fetched_at
        FROM ingest.raw_file rf
        JOIN ingest.imap_message im ON im.id = rf.imap_message_id
        WHERE rf.original_filename ILIKE %s
        ORDER BY im.fetched_at DESC NULLS LAST, rf.id DESC
        """,
        (f"%{filename}%",),
    )
    return [dict(row) for row in cur.fetchall()]


def _fetch_artifacts(cur, *, raw_file_id: str) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT
            artifact_id::text AS artifact_id,
            run_id::text AS run_id,
            workflow_name,
            stage_name,
            artifact_role,
            kind AS artifact_kind,
            report_type,
            client_code,
            raw_file_id::text AS raw_file_id,
            storage_key,
            filename,
            display_filename,
            original_filename,
            file_ext,
            size_bytes,
            sha256,
            created_at,
            metadata_json
        FROM artifacts
        WHERE raw_file_id = %s
        ORDER BY created_at DESC, artifact_id DESC
        """,
        (raw_file_id,),
    )
    return [dict(row) for row in cur.fetchall()]


def _normalized_csv_sheet_markers(path: str | None) -> dict[str, Any]:
    expected = {
        "GPS_baza_START_detection": "ID;Nr rejestracyjny;Data przydziału;RFID;PRYW;EDYS;OPTIMA;OTK",
        "LOG_output_start": "ID;Nr rejestracyjny;Data przydziału;Nazwa Pliku csv",
        "Status_Prywatnosci_stop": "ID;Data przydziału;PRYW stary;PRYW aktualny",
    }
    result = {"path": path, "exists": False, "markers": {name: False for name in expected}}
    if not path:
        return result
    csv_path = Path(path)
    result["exists"] = csv_path.exists()
    if not csv_path.exists():
        return result
    text = csv_path.read_text(encoding="utf-8-sig", errors="replace")
    result["size_bytes"] = csv_path.stat().st_size
    result["markers"] = {name: marker in text for name, marker in expected.items()}
    result["contains_all_expected_xlsm_sections"] = all(result["markers"].values())
    return result


def analyze_stage3_lookup(
    artifacts: list[dict[str, Any]],
    *,
    raw_file_id: str,
    report_type: str,
    client_code: str | None,
) -> dict[str, Any]:
    filters = job_stage3._stage2_cleaned_artifact_lookup_filters(
        raw_file_id=raw_file_id,
        report_type=report_type,
    )
    candidates = [
        artifact
        for artifact in artifacts
        if artifact.get("raw_file_id") == raw_file_id
        and artifact.get("workflow_name") == filters["workflow_name"]
        and artifact.get("stage_name") == filters["stage_name"]
        and artifact.get("artifact_role") == filters["artifact_role"]
        and artifact.get("report_type") in set(filters["report_type_any"])
    ]
    failures: list[str] = []
    if not artifacts:
        failures.append("no artifact rows linked to raw_file_id")
    if not any(a.get("raw_file_id") == raw_file_id for a in artifacts):
        failures.append("missing raw_file_id")
    if not any(a.get("stage_name") == filters["stage_name"] for a in artifacts):
        failures.append("wrong or missing stage_name")
    if not any(a.get("artifact_role") == filters["artifact_role"] for a in artifacts):
        failures.append("wrong or missing artifact_role")
    if not any(a.get("artifact_kind") == "REPORT" for a in artifacts):
        failures.append("wrong or missing artifact_kind")
    if not any(a.get("report_type") in set(filters["report_type_any"]) for a in artifacts):
        failures.append("wrong or missing report_type")
    if client_code and not any(a.get("client_code") == client_code for a in artifacts):
        failures.append("wrong or missing client_code")
    if candidates:
        missing_files = [
            a["artifact_id"]
            for a in candidates
            if not (a.get("storage_key") or a.get("filename") or a.get("display_filename"))
        ]
        if missing_files:
            failures.append("matching artifact row exists but has no object key/path/display filename")
    return {
        "filters": filters,
        "found": bool(candidates),
        "matching_artifact_ids": [a["artifact_id"] for a in candidates],
        "failure_reasons": [] if candidates else failures,
    }


def _fetch_load_policy(cur, *, client_code: str, report_type: str) -> dict[str, Any] | None:
    cur.execute(
        """
        SELECT client_code, report_type, data_overwrite, created_at, updated_at
        FROM workflow_b_control.report_type_client_load_policy
        WHERE client_code = %s
          AND report_type = %s
        """,
        (client_code, report_type),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def _target_table_status(cur, *, client_code: str, report_type: str) -> dict[str, Any]:
    destination_schema, destination_table = job_stage3._destination_for_report_type(report_type)
    status = {
        "client_code": client_code,
        "destination_schema": destination_schema,
        "destination_table": destination_table,
        "checked": False,
        "exists": None,
    }
    try:
        db_config = job_stage3._load_client_account_by_code(cur, client_code)
        status["client_db_name"] = db_config.client_db_name
        with job_stage3._client_business_pg_conn(db_config) as conn:
            with conn.cursor() as dest_cur:
                dest_cur.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM information_schema.tables
                        WHERE table_schema = %s
                          AND table_name = %s
                    ) AS exists
                    """,
                    (destination_schema, destination_table),
                )
                row = dest_cur.fetchone()
                status["checked"] = True
                status["exists"] = bool(row and row["exists"])
    except Exception as exc:
        status["error"] = f"{type(exc).__name__}: {exc}"
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--filename", required=True)
    parser.add_argument("--report-type", required=True)
    parser.add_argument("--client-code", required=True)
    args = parser.parse_args()

    with _platform_pg_conn() as conn:
        with conn.cursor() as cur:
            raw_files = _fetch_raw_files(cur, filename=args.filename)
            _print_json("A. raw_file rows matching filename", raw_files)
            if not raw_files:
                print("\nNo matching ingest.raw_file rows. Fetch did not create a current row for this filename.")
                return 1

            for row in raw_files:
                raw_file_id = row["raw_file_id"]
                artifacts = _fetch_artifacts(cur, raw_file_id=raw_file_id)
                _print_json(f"B. artifact rows linked to raw_file_id={raw_file_id}", artifacts)
                _print_json(
                    f"C. normalized CSV markers for raw_file_id={raw_file_id}",
                    _normalized_csv_sheet_markers(row.get("normalized_csv_path")),
                )
                _print_json(
                    f"D. Stage 3 expected lookup for raw_file_id={raw_file_id}",
                    analyze_stage3_lookup(
                        artifacts,
                        raw_file_id=raw_file_id,
                        report_type=args.report_type,
                        client_code=args.client_code,
                    ),
                )

            _print_json(
                "E. load policy",
                _fetch_load_policy(cur, client_code=args.client_code, report_type=args.report_type),
            )
            _print_json(
                "F. target table status",
                _target_table_status(cur, client_code=args.client_code, report_type=args.report_type),
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
