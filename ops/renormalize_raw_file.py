#!/usr/bin/env python3
"""Re-run Stage 1 normalization for a single ``ingest.raw_file`` row.

Useful when a Stage 1 normalization bug stored the wrong canonical CSV for an
existing raw file (for example, the legacy ``_xlsm_to_canonical_csv`` only
emitted the ``LOG`` worksheet, hiding the Stage 2 detection signature for
ALPHA00001 Alpha GPS workbooks). After re-normalizing, the operator can
re-run Stage 2 with the same ``raw_file_id`` or by ``normalized_csv_path``.

This script does not touch Stage 1 IMAP fetching or de-duplication state; it
only rewrites the canonical CSV file at ``ingest.raw_file.normalized_csv_path``
from the on-disk ``raw_path`` bytes, and clears the cached Stage 2 status so
the next Stage 2 run re-evaluates the file.

Usage::

    PYTHONPATH="$PWD" python3 ops/renormalize_raw_file.py <raw_file_id>
    PYTHONPATH="$PWD" python3 ops/renormalize_raw_file.py --filename GPS_baza_START_skrypt.xlsm
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.mail import fetch_reports  # noqa: E402


def _pg_conn():
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


def _select_rows(cur, *, raw_file_id: str | None, filename_like: str | None) -> list[dict]:
    if raw_file_id:
        cur.execute(
            """
            SELECT id, original_filename, raw_path, normalized_csv_path, status,
                   stage2_status, stage2_report_type
            FROM ingest.raw_file
            WHERE id = %s
            """,
            (raw_file_id,),
        )
    else:
        cur.execute(
            """
            SELECT id, original_filename, raw_path, normalized_csv_path, status,
                   stage2_status, stage2_report_type
            FROM ingest.raw_file
            WHERE original_filename ILIKE %s
              AND status = 'NORMALIZED'
              AND raw_path IS NOT NULL
              AND normalized_csv_path IS NOT NULL
            ORDER BY id
            """,
            (f"%{filename_like}%",),
        )
    return [dict(r) for r in cur.fetchall()]


def _extension_for(row: dict) -> str:
    name = row["original_filename"] or ""
    ext = Path(name).suffix.lower()
    if ext not in {".csv", ".xls", ".xlsx", ".xlsm"}:
        raise ValueError(f"unsupported extension for {row['id']}: {ext!r}")
    return ext


def _renormalize(row: dict, *, dry_run: bool) -> dict:
    raw_path = Path(row["raw_path"])
    out_path = Path(row["normalized_csv_path"])
    if not raw_path.exists():
        return {"id": str(row["id"]), "status": "skip", "reason": "raw_path_missing", "raw_path": str(raw_path)}
    payload = raw_path.read_bytes()
    ext = _extension_for(row)
    if dry_run:
        return {
            "id": str(row["id"]),
            "status": "would_renormalize",
            "raw_path": str(raw_path),
            "normalized_csv_path": str(out_path),
            "ext": ext,
        }
    fetch_reports._convert_to_canonical_csv(payload, ext, out_path)
    return {
        "id": str(row["id"]),
        "status": "renormalized",
        "raw_path": str(raw_path),
        "normalized_csv_path": str(out_path),
        "size_bytes": out_path.stat().st_size,
        "ext": ext,
    }


def _reset_stage2(cur, raw_file_id: str) -> None:
    cur.execute(
        """
        UPDATE ingest.raw_file
        SET stage2_status = NULL,
            stage2_report_type = NULL,
            stage2_scores = NULL,
            stage2_schema_diff = NULL,
            stage2_pending_reason = NULL,
            stage2_updated_at = NULL
        WHERE id = %s
        """,
        (raw_file_id,),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("raw_file_id", nargs="?", help="ingest.raw_file.id")
    parser.add_argument("--filename", help="re-normalize all NORMALIZED raw_files whose original_filename ILIKE this")
    parser.add_argument("--apply", action="store_true", help="actually write changes (default is dry-run)")
    parser.add_argument(
        "--reset-stage2",
        action="store_true",
        help="also clear stage2_* columns so Stage 2 re-runs cleanly (only with --apply)",
    )
    args = parser.parse_args()

    if not args.raw_file_id and not args.filename:
        parser.error("one of raw_file_id or --filename is required")

    dry_run = not args.apply
    conn = _pg_conn()
    try:
        with conn.cursor() as cur:
            rows = _select_rows(cur, raw_file_id=args.raw_file_id, filename_like=args.filename)
            if not rows:
                print("no matching ingest.raw_file rows")
                return 1
            for row in rows:
                outcome = _renormalize(row, dry_run=dry_run)
                print(outcome)
                if args.apply and outcome.get("status") == "renormalized" and args.reset_stage2:
                    _reset_stage2(cur, str(row["id"]))
                    print({"id": str(row["id"]), "status": "stage2_reset"})
        if args.apply:
            conn.commit()
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
