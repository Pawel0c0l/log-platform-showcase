#!/usr/bin/env python3
"""Manual tests for jobs.alpha.import_gps_baza_log_xlsm.

These checks exercise workbook validation and the duplicate/force/dry-run
decision path without requiring a live Postgres database.

Run:
    cd /opt/log-platform
    PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_alpha_gps_baza_log_xlsm_import.py
"""
from __future__ import annotations

import tempfile
from datetime import date
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from openpyxl import Workbook

from jobs.alpha import import_gps_baza_log_xlsm as job


FAILURES: list[str] = []


def _check(label: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"\n        {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def _workbook_path(
    tmp: Path,
    *,
    sheet_name: str = "LOG",
    headers: tuple[str, ...] = job.REQUIRED_HEADERS,
    rows: list[tuple[object, object, object, object]] | None = None,
) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name
    ws.append(("noise", None, None, None))
    ws.append(headers)
    for row in rows or [("1", " wx 12345 ", date(2026, 5, 17), "file.csv")]:
        ws.append(row)
    path = tmp / "GPS_baza_START_skrypt.xlsm"
    wb.save(path)
    wb.close()
    return path


def test_missing_source_file() -> None:
    try:
        job._stable_source_file(Path("/tmp/does-not-exist-alpha-gps.xlsm"), sleep_s=0)
    except FileNotFoundError:
        _check("missing source file fails", True)
        return
    _check("missing source file fails", False)


def test_missing_log_worksheet(tmp: Path) -> None:
    path = _workbook_path(tmp, sheet_name="NOT_LOG")
    try:
        job._read_workbook(path, sheet_name="LOG")
    except ValueError as exc:
        _check("missing LOG worksheet fails", "Worksheet not found: LOG" in str(exc), str(exc))
        return
    _check("missing LOG worksheet fails", False)


def test_missing_required_header(tmp: Path) -> None:
    path = _workbook_path(tmp, headers=("ID", "Nr rejestracyjny", "Data przydziału", "bad"))
    try:
        job._read_workbook(path, sheet_name="LOG")
    except ValueError as exc:
        _check("missing required header fails", "Required header row not found" in str(exc), str(exc))
        return
    _check("missing required header fails", False)


def test_successful_parse(tmp: Path) -> None:
    path = _workbook_path(
        tmp,
        rows=[
            (" 42 ", " wx 12345 ", "2026-05-17", " gps.csv "),
            (None, None, None, None),
        ],
    )
    parsed = job._read_workbook(path, sheet_name="LOG")
    row = parsed.rows[0]
    _check("successful parse loads one data row", len(parsed.rows) == 1)
    _check("empty row skipped", parsed.empty_rows_skipped == 1)
    _check("source_id trimmed", row.source_id == "42")
    _check("registration normalized", row.registration == "WX 12345")
    _check("assignment date parsed", row.assignment_date == date(2026, 5, 17))
    _check("csv filename trimmed", row.csv_filename == "gps.csv")
    _check("source row number preserved", row.source_row_number == 3)


def test_invalid_date_fails(tmp: Path) -> None:
    path = _workbook_path(tmp, rows=[("1", "WX", "not-a-date", "gps.csv")])
    try:
        job._read_workbook(path, sheet_name="LOG")
    except ValueError as exc:
        _check("invalid date fails fast", "Invalid Data przydziału" in str(exc), str(exc))
        return
    _check("invalid date fails fast", False)


def test_duplicate_force_and_dry_run_static_shape() -> None:
    source = Path(job.__file__).read_text(encoding="utf-8")
    _check("duplicate sha success is skipped unless force",
           "if duplicate and not force:" in source and "SKIPPED_DUPLICATE_SHA256" in source)
    _check("force can reimport same sha by superseding old success",
           "SUPERSEDED" in source and "superseded_by_import_run_id" in source)
    _check("replace-all deletes before insert in transaction helper",
           "DELETE FROM {}" in source and "cur.executemany(" in source and "conn.commit()" in source)
    _check("dry-run avoids import history and target DML",
           "if dry_run:" in source and "DRY_RUN_SUCCESS" in source)


def test_migration_quotes_mixed_case_table() -> None:
    migration = (REPO_ROOT / "db" / "client_business" / "022_alpha_gps_baza_log.sql").read_text(
        encoding="utf-8"
    )
    _check("migration creates mixed-case table quoted",
           'telematics_reports."Alpha_GPS_Baza_LOG"' in migration)
    _check("migration has successful sha uniqueness",
           "uq_alpha_gps_baza_log_success_sha256" in migration and "WHERE status = 'SUCCESS'" in migration)


def main() -> int:
    test_missing_source_file()
    with tempfile.TemporaryDirectory(prefix="alpha-gps-test-") as tmpdir:
        tmp = Path(tmpdir)
        test_missing_log_worksheet(tmp)
        test_missing_required_header(tmp)
        test_successful_parse(tmp)
        test_invalid_date_fails(tmp)
    test_duplicate_force_and_dry_run_static_shape()
    test_migration_quotes_mixed_case_table()

    print("")
    if FAILURES:
        print(f"FAIL - {len(FAILURES)} check(s) failed:")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("OK - Alpha GPS XLSM import checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

