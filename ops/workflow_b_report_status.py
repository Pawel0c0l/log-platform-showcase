#!/usr/bin/env python3
"""Workflow B operator report: registry + Stage 2 processing status.

This is a read-only inspection utility. It does not participate in Stage 2
detection/cleaning and does not change any runtime behavior.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent

REGISTRY_TABLE = "workflow_b_control.report_type_registry"
RAW_FILE_TABLE = "ingest.raw_file"
REQUIRED_STAGE2_COLUMNS = {
    "stage2_status",
    "stage2_report_type",
    "stage2_scores",
    "stage2_schema_diff",
    "stage2_pending_reason",
    "stage2_updated_at",
}


class UserFacingError(RuntimeError):
    pass


def _load_dotenv_if_present() -> None:
    dotenv_path = REPO_ROOT / ".env"
    if not dotenv_path.exists():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(dotenv_path, override=False)
        return
    except ImportError:
        pass

    try:
        with dotenv_path.open("r", encoding="utf-8") as f:
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


def _require(module_name: str, pip_name: str | None = None):
    try:
        return __import__(module_name)
    except ImportError as exc:
        pkg = pip_name or module_name
        raise UserFacingError(f"Missing dependency '{module_name}'. Install: pip install {pkg}") from exc


def _platform_dsn() -> str:
    host = os.getenv("POSTGRES_HOST", "127.0.0.1")
    port = os.getenv("POSTGRES_PORT", "5432")
    dbname = os.getenv("POSTGRES_DB", "logdb")
    user = os.getenv("POSTGRES_USER", "loguser")
    password = os.getenv("POSTGRES_PASSWORD", "")
    return f"host={host} port={port} dbname={dbname} user={user} password={password}"


def _connect():
    psycopg = _require("psycopg")
    from psycopg.rows import dict_row

    return psycopg.connect(_platform_dsn(), row_factory=dict_row)


def _assert_relation(cur, fqn: str, hint: str) -> None:
    cur.execute("SELECT to_regclass(%s) AS rel", (fqn,))
    row = cur.fetchone()
    if not row or row["rel"] is None:
        raise UserFacingError(f"Missing required table {fqn}. {hint}")


def _assert_stage2_columns(cur) -> None:
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'ingest'
          AND table_name = 'raw_file'
          AND column_name = ANY(%s::text[])
        """,
        (sorted(REQUIRED_STAGE2_COLUMNS),),
    )
    present = {row["column_name"] for row in cur.fetchall()}
    missing = sorted(REQUIRED_STAGE2_COLUMNS - present)
    if missing:
        raise UserFacingError(
            "Missing Stage 2 column(s) on ingest.raw_file: "
            + ", ".join(missing)
            + ". Run platform migrations, especially 006_stage2_status.sql."
        )


def _preflight(cur) -> None:
    _assert_relation(
        cur,
        REGISTRY_TABLE,
        "Run platform migrations, especially 019_workflow_b_report_type_registry.sql.",
    )
    _assert_relation(
        cur,
        RAW_FILE_TABLE,
        "Run platform migrations, especially 001_ingest_imap_fetch_reports.sql.",
    )
    _assert_stage2_columns(cur)


def fetch_report_data(conn, *, recent_limit: int) -> dict[str, list[dict[str, Any]]]:
    with conn.cursor() as cur:
        _preflight(cur)

        cur.execute(
            """
            SELECT report_type, display_name, enabled, implementation_status, cleaner_module
            FROM workflow_b_control.report_type_registry
            ORDER BY priority, report_type
            """
        )
        registry_rows = list(cur.fetchall())

        cur.execute(
            """
            SELECT
              COALESCE(stage2_report_type, '(unknown)') AS report_type,
              COALESCE(stage2_status, '(not_processed)') AS stage2_status,
              COUNT(*)::bigint AS files_count,
              MAX(stage2_updated_at) AS latest_stage2_at
            FROM ingest.raw_file
            WHERE stage2_status IS NOT NULL
               OR stage2_report_type IS NOT NULL
            GROUP BY 1, 2
            ORDER BY 1, 2
            """
        )
        status_rows = list(cur.fetchall())

        cur.execute(
            """
            WITH successful AS (
              SELECT stage2_report_type AS report_type, COUNT(*)::bigint AS files_count
              FROM ingest.raw_file
              WHERE stage2_status = 'OK'
                AND stage2_report_type IS NOT NULL
              GROUP BY stage2_report_type
            )
            SELECT r.report_type, r.implementation_status, r.cleaner_module
            FROM workflow_b_control.report_type_registry r
            LEFT JOIN successful s ON s.report_type = r.report_type
            WHERE COALESCE(s.files_count, 0) = 0
            ORDER BY r.priority, r.report_type
            """
        )
        zero_success_rows = list(cur.fetchall())

        cur.execute(
            """
            SELECT
              rf.id::text AS raw_file_id,
              rf.stage2_report_type AS report_type,
              COALESCE(rf.stage2_status, rf.status) AS status,
              COALESCE(
                rf.stage2_pending_reason,
                rf.stage2_schema_diff->>'errors',
                rf.error
              ) AS reason,
              rf.original_filename,
              COALESCE(rf.normalized_csv_path, rf.raw_path) AS path,
              COALESCE(rf.stage2_updated_at, im.fetched_at, im.internal_date) AS latest_ts
            FROM ingest.raw_file rf
            LEFT JOIN ingest.imap_message im ON im.id = rf.imap_message_id
            WHERE (rf.stage2_status IS NOT NULL AND rf.stage2_status <> 'OK')
               OR rf.status = 'FAILED'
            ORDER BY COALESCE(rf.stage2_updated_at, im.fetched_at, im.internal_date) DESC NULLS LAST,
                     rf.id DESC
            LIMIT %s
            """,
            (recent_limit,),
        )
        recent_attention_rows = list(cur.fetchall())

    return {
        "registry": registry_rows,
        "status_by_type": status_rows,
        "zero_success": zero_success_rows,
        "recent_attention": recent_attention_rows,
    }


def _fmt(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return str(value)


def _print_table(headers: list[str], rows: list[list[Any]]) -> None:
    if not rows:
        print("(none)")
        return
    rendered = [[_fmt(cell) for cell in row] for row in rows]
    widths = [
        max(len(headers[i]), *(len(row[i]) for row in rendered))
        for i in range(len(headers))
    ]
    print(" | ".join(headers[i].ljust(widths[i]) for i in range(len(headers))))
    print("-+-".join("-" * width for width in widths))
    for row in rendered:
        print(" | ".join(row[i].ljust(widths[i]) for i in range(len(headers))))


def _section(title: str) -> None:
    print("")
    print(title)
    print("=" * len(title))


def render_report(data: dict[str, list[dict[str, Any]]]) -> None:
    print("Workflow B Report Status")

    _section("Registry Summary")
    _print_table(
        ["report_type", "display_name", "enabled", "implementation_status", "cleaner_module"],
        [
            [
                row["report_type"],
                row["display_name"],
                row["enabled"],
                row["implementation_status"],
                row["cleaner_module"],
            ]
            for row in data["registry"]
        ],
    )

    _section("Stage 2 Status By Report Type")
    _print_table(
        ["report_type", "stage2_status", "files_count", "latest_stage2_at"],
        [
            [
                row["report_type"],
                row["stage2_status"],
                row["files_count"],
                row["latest_stage2_at"],
            ]
            for row in data["status_by_type"]
        ],
    )

    _section("Registered Report Types With Zero Successful Stage 2 Files")
    _print_table(
        ["report_type", "implementation_status", "cleaner_module"],
        [
            [row["report_type"], row["implementation_status"], row["cleaner_module"]]
            for row in data["zero_success"]
        ],
    )

    _section("Recent Pending/Failed Stage 2 Files")
    _print_table(
        [
            "raw_file_id",
            "report_type",
            "status",
            "reason",
            "original_filename",
            "path",
            "latest_ts",
        ],
        [
            [
                row["raw_file_id"],
                row["report_type"],
                row["status"],
                row["reason"],
                row["original_filename"],
                row["path"],
                row["latest_ts"],
            ]
            for row in data["recent_attention"]
        ],
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Print Workflow B report registry and Stage 2 processing status."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum number of recent pending/failed files to show (default: 10).",
    )
    args = parser.parse_args(argv)

    if args.limit < 1:
        print("ERROR: --limit must be >= 1", file=sys.stderr)
        return 2

    _load_dotenv_if_present()

    try:
        conn = _connect()
    except UserFacingError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"ERROR: Could not connect to platform DB using POSTGRES_* env: {exc}", file=sys.stderr)
        return 2

    try:
        with conn:
            data = fetch_report_data(conn, recent_limit=args.limit)
    except UserFacingError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"ERROR: Workflow B report status query failed: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    render_report(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
