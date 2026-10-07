#!/usr/bin/env python3
"""Integration smoke test: future report_207 attachment through Stage 1 -> Stage 2 -> Stage 3.

Proves that a *future* report_207 email attachment (realistic multi-sheet / paginated
workbook with Excel-serial ``Data i czas`` values) is safe end-to-end:

* Stage 1 (real ``_convert_to_canonical_csv``) canonicalizes every recognized date cell to
  ``DD.MM.YYYY HH:MM`` across all sheets (including continuation sheets), leaving no serials.
* Stage 2 (real detection/clean/validate) detects report_207, drops title/repeated-header rows,
  and reports zero ``Data i czas`` parse failures.
* Stage 3 runs the *real* migration analysis SQL against an isolated, rolled-back schema and
  confirms ``INVALID_TIMESTAMP = 0`` plus correct >140 bucketing and trip matching.

The Stage 3 step needs a local Postgres (the dev stack). If none is reachable it is SKIPPED
(Stage 1/2 still assert). Nothing is ever committed: the schema/tables live inside a single
transaction that is always rolled back, so no live data is touched.

Run:
    cd /opt/log-platform
    PYTHONPATH="$PWD" .venv/bin/python ops/tests_manual/test_report_207_future_pipeline.py
"""
from __future__ import annotations

import csv
import io
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.mail.fetch_reports import _convert_to_canonical_csv  # noqa: E402
from jobs.reports.stage2 import io as s2io  # noqa: E402
from jobs.reports.stage2.types.report_207 import Report207  # noqa: E402
from jobs.reports.stage2.validation import validate  # noqa: E402
from jobs.reports.postprocess import job_report_207_speeding_migration as job  # noqa: E402

REPORT_207_HEADER = [
    "Data i czas",
    "Nr rejestracyjny",
    "Prędkość",
    "Ograniczenie prędkości drogowej",
    "Lokalizacja",
]
REPORT_207_TITLE = "207 Raport przekroczeń limitów prędkości drogowej"

CANON_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}$")
SERIAL_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")
EXCEL_BASE = datetime(1899, 12, 30)


def _serial(dt: datetime) -> float:
    return (dt - EXCEL_BASE).total_seconds() / 86400.0


# Future report event scenario (local Europe/Warsaw wall-clock).
EV_A = datetime(2026, 5, 4, 7, 24, 3)   # WD1234A, 155 -> 140_160, matches trip A
EV_B = datetime(2026, 5, 4, 8, 10, 30)  # WD1234A, 165 -> 160_170, matches trip A
EV_C = datetime(2026, 5, 4, 9, 0, 0)    # WD5678B, 175 -> 170_plus, matches trip C
EV_D = datetime(2026, 5, 4, 10, 0, 0)   # WD5678B, 90  -> sub-140, no bucket
EV_E = datetime(2026, 5, 4, 11, 0, 0)   # WD9999Z, 145 -> 140_160, NO matching trip


def _future_report_207_xlsx() -> bytes:
    """Multi-sheet workbook mimicking a paginated report_207 .xls export."""
    openpyxl = __import__("openpyxl")
    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    # Sheet 1: title + metadata block + header + data (own header near the top).
    p1 = wb.create_sheet("speeding p1")
    for row in [
        [REPORT_207_TITLE, "", "", "", ""],
        ["Data początek:", "", "", "", ""],
        ["Data koniec:", "", "", "", ""],
        REPORT_207_HEADER,
        [_serial(EV_A), "WD1234A", 155, 50, "ul. Testowa 1"],
        [_serial(EV_B), "WD1234A", 165, 50, "ul. Testowa 2"],
    ]:
        p1.append(row)

    # Sheet 2: pure continuation, NO header at all (inheritance must canonicalize).
    p2 = wb.create_sheet("speeding p2")
    for row in [
        [_serial(EV_C), "WD5678B", 175, 90, "ul. Druga 3"],
        [_serial(EV_D), "WD5678B", 90, 50, "ul. Druga 4"],
    ]:
        p2.append(row)

    # Sheet 3: leading continuation data BEFORE a repeated (deep) header.
    p3 = wb.create_sheet("speeding p3")
    for row in [
        [_serial(EV_E), "WD9999Z", 145, 50, "ul. Trzecia 5"],
        REPORT_207_HEADER,
    ]:
        p3.append(row)

    buffer = io.BytesIO()
    wb.save(buffer)
    wb.close()
    return buffer.getvalue()


def _read_rows(path: Path) -> list[list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.reader(f, delimiter=";"))


def _stage3_dry_run(cleaned_rows: list[tuple[str, str, str]]):
    """Execute the real migration analysis SQL against an isolated, rolled-back schema.

    Returns (counts_dict, None) on success or (None, skip_reason) if no DB is reachable.
    """
    try:
        from dotenv import load_dotenv

        env = REPO_ROOT / ".env"
        if env.exists():
            load_dotenv(env)
    except Exception:
        pass

    try:
        conn = job._platform_pg_conn()
    except Exception as exc:  # no DB available -> skip Stage 3, not a failure
        return None, f"no Postgres reachable ({type(exc).__name__})"

    schema = f"r207_smoke_{os.getpid()}"
    orig = (job.REPORT_SCHEMA, job.CLIENT_TRIPS_SCHEMA)
    try:
        with conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema}"')
            cur.execute(
                f'CREATE TABLE "{schema}"."report_207" ('
                '"Data i czas" text, "Nr rejestracyjny" text, "Prędkość" text, '
                '"Ograniczenie prędkości drogowej" text, "Lokalizacja" text, record_id text, '
                "migrated_to_client_db boolean DEFAULT FALSE, migrated_to_client_db_error text, "
                "migrated_to_client_db_at timestamptz, migrated_to_client_trip_id text)"
            )
            cur.execute(
                f'CREATE TABLE "{schema}"."client_trips" ('
                "client_id text, provider_trip_id text, registration text, "
                "start_timestamp timestamptz, end_timestamp timestamptz, record_id text)"
            )
            for ts, reg, speed in cleaned_rows:
                cur.execute(
                    f'INSERT INTO "{schema}"."report_207" '
                    '("Data i czas","Nr rejestracyjny","Prędkość") VALUES (%s,%s,%s)',
                    (ts, reg, speed),
                )
            trips = [
                ("c1", "trip-A", "WD1234A", "2026-05-04 07:00:00+02", "2026-05-04 08:30:00+02", "recA"),
                ("c1", "trip-C", "WD5678B", "2026-05-04 08:45:00+02", "2026-05-04 09:30:00+02", "recC"),
            ]
            for t in trips:
                cur.execute(
                    f'INSERT INTO "{schema}"."client_trips" '
                    "(client_id, provider_trip_id, registration, start_timestamp, end_timestamp, record_id) "
                    "VALUES (%s,%s,%s,%s,%s,%s)",
                    t,
                )
            job.REPORT_SCHEMA = schema
            job.CLIENT_TRIPS_SCHEMA = schema
            counts = job._analyze_report_rows(
                cur,
                limit=None,
                force_retry_errors=False,
                has_migrated_column=True,
                has_error_column=True,
            )
        return counts, None
    finally:
        job.REPORT_SCHEMA, job.CLIENT_TRIPS_SCHEMA = orig
        try:
            conn.rollback()
        finally:
            conn.close()


def test_future_report_207_pipeline() -> None:
    raw = _future_report_207_xlsx()

    with tempfile.TemporaryDirectory(prefix="r207-future-") as tmp:
        out = Path(tmp) / "normalized.csv"

        # ---- Stage 1 (real normalization path) ----
        meta = _convert_to_canonical_csv(raw, ".xlsx", out)
        rows = _read_rows(out)

        # Data rows = rows with a WD-style registration in col 1.
        data_rows = [r for r in rows if len(r) > 1 and r[1].startswith("WD")]
        assert len(data_rows) == 5, [r[:2] for r in rows]
        date_cells = [r[0] for r in data_rows]
        assert all(CANON_RE.match(v) for v in date_cells), date_cells
        assert not any(SERIAL_RE.match(v) for v in date_cells), date_cells
        # Title / repeated header preserved verbatim (not turned into a date).
        flat0 = {r[0] for r in rows if r}
        assert REPORT_207_TITLE in flat0 and "Data i czas" in flat0, flat0
        # Date-normalization metadata: every serial parsed, repeated header counted unparseable.
        col_meta = {c["column"]: c for c in meta["columns"]}
        assert "Data i czas" in col_meta, meta
        assert col_meta["Data i czas"]["parsed"] == 5, col_meta
        assert col_meta["Data i czas"]["source"] == "report_207", col_meta
        assert col_meta["Data i czas"]["unparseable"] >= 1, col_meta
        stage1_dates = sorted(date_cells)

        # ---- Stage 2 (real detection / clean / validate) ----
        df = s2io.read_csv_loose(str(out))
        tables = s2io.split_into_tables(df)
        score = Report207.detect(tables)
        assert score >= 1.0, score
        cleaned = Report207.clean(tables)
        assert len(cleaned.df) == 5, cleaned.df
        # Non-date numeric column survived intact.
        speeds = sorted(float(str(v).replace(",", ".")) for v in cleaned.df["Prędkość"].tolist())
        assert speeds == [90.0, 145.0, 155.0, 165.0, 175.0], speeds
        result = validate(cleaned, Report207)
        assert result.is_valid, result.schema_diff
        dt_stats = result.schema_diff["type_parse_stats"]["Data i czas"]
        assert dt_stats["fails"] == 0, dt_stats
        assert "Data i czas" not in result.schema_diff.get("type_parse_blocking_cols", []), result.schema_diff
        # Canonical format is accepted explicitly (not just by the pandas fallback).
        from jobs.reports.stage2 import validation as s2val

        assert s2val._parse_datetime_value("04.05.2026 07:24") == datetime(2026, 5, 4, 7, 24), "explicit DD.MM.YYYY HH:MM"

        cleaned_rows = [
            (str(r["Data i czas"]), str(r["Nr rejestracyjny"]), str(r["Prędkość"]))
            for _, r in cleaned.df.iterrows()
        ]

    print("PASS [Stage 1]: 5 data rows canonical DD.MM.YYYY HH:MM, 0 serials; numeric column intact")
    print(f"  stage1 Data i czas: {stage1_dates}")
    print("PASS [Stage 2]: report_207 detected (score=1.0), title/header rows dropped, Data i czas fails=0")

    # ---- Stage 3 (real migration analysis SQL, isolated + rolled back) ----
    counts, skip = _stage3_dry_run(cleaned_rows)
    if skip:
        print(f"SKIP [Stage 3]: {skip} -- Stage 1/2 verified; SQL parse path covered by test_report_207_speeding_migration.py")
        return

    assert counts["invalid_rows"] == 0, counts          # INVALID_TIMESTAMP = 0 (headline)
    assert counts["candidate_rows"] == 5, counts
    assert counts["valid_speed_rows"] == 4, counts       # A,B,C,E (>140); D excluded
    assert counts["speed_140_160_rows"] == 2, counts     # A=155, E=145
    assert counts["speed_160_170_rows"] == 1, counts     # B=165
    assert counts["speed_170_plus_rows"] == 1, counts    # C=175
    assert counts["matched_rows"] == 3, counts           # A,B,C matched a trip
    assert counts["unmatched_rows"] == 1, counts         # E has no trip
    assert counts["ambiguous_rows"] == 0, counts
    print(f"PASS [Stage 3]: canonical timestamps parsed, INVALID_TIMESTAMP=0, buckets 2/1/1, matched=3, unmatched=1")
    print(f"  stage3 counts: {counts}")


def main() -> None:
    test_future_report_207_pipeline()
    print("\nOK - future report_207 Stage 1 -> Stage 2 -> Stage 3 pipeline verified")


if __name__ == "__main__":
    main()
