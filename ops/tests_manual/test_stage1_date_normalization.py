#!/usr/bin/env python3
"""Manual tests for Workflow B Stage 1 date/datetime normalization.

Covers the shared normalizer (jobs.reports.date_normalization), the column- and
report-aware Stage 1 integration in jobs.mail.fetch_reports, and Stage 2
report_207 acceptance of the canonical output.

Run:
    cd /opt/log-platform
    .venv/bin/python ops/tests_manual/test_stage1_date_normalization.py
"""
from __future__ import annotations

import csv
import io
import struct
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.mail.fetch_reports import _convert_to_canonical_csv  # noqa: E402
from jobs.reports import date_normalization as dn  # noqa: E402
from jobs.reports.stage2.types.report_207 import Report207  # noqa: E402
from jobs.reports.stage2.validation import validate  # noqa: E402

import pandas as pd  # noqa: E402


REPORT_207_HEADER = [
    "Data i czas",
    "Nr rejestracyjny",
    "Prędkość",
    "Ograniczenie prędkości drogowej",
    "Lokalizacja",
]


def _read_rows(path: Path) -> list[list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.reader(f, delimiter=";"))


# --------------------------------------------------------------------------- #
# Unit: header classification
# --------------------------------------------------------------------------- #
def test_classify_date_columns() -> None:
    data_i_czas = dn.classify_date_column("Data i czas")
    assert data_i_czas is not None and data_i_czas.kind == "datetime"
    assert data_i_czas.serial_ok and data_i_czas.source == "report_207"

    przydzialu = dn.classify_date_column("Data przydziału")
    assert przydzialu is not None and przydzialu.kind == "date" and przydzialu.serial_ok

    ts = dn.classify_date_column("event_ts")
    assert ts is not None and ts.kind == "datetime"
    assert dn.classify_date_column("Timestamp").kind == "datetime"

    # Non-date / numeric columns and risky standalone structural words: not matched.
    for header in ("Prędkość", "distance_km", "Ograniczenie prędkości drogowej", "start", "end", "Lokalizacja"):
        assert dn.classify_date_column(header) is None, header
    print("PASS: header classification recognizes date columns and ignores numeric/ambiguous ones")


# --------------------------------------------------------------------------- #
# Unit: cell normalization
# --------------------------------------------------------------------------- #
def test_excel_serial_datetime_and_date() -> None:
    dt_col = dn.DateColumn(kind="datetime", serial_ok=True, source="report_207")
    res = dn.normalize_date_cell(46164.509224537, dt_col)
    assert res.status == "normalized" and res.family == "excel_serial", res
    assert res.normalized_text == "22.05.2026 12:13", res.normalized_text

    date_col = dn.DateColumn(kind="date", serial_ok=True, source="generic")
    res_int = dn.normalize_date_cell(45000, date_col)
    assert res_int.status == "normalized" and res_int.emitted_kind == "date", res_int
    assert res_int.normalized_text == "15.03.2023", res_int.normalized_text
    print("PASS: Excel serial fractional datetimes and whole-day date-only serials normalize")


def test_real_date_objects() -> None:
    dt_col = dn.DateColumn(kind="datetime", serial_ok=True, source="report_207")
    res = dn.normalize_date_cell(datetime(2026, 5, 4, 7, 24, 47), dt_col)
    assert res.normalized_text == "04.05.2026 07:25", res.normalized_text  # seconds rounded
    res2 = dn.normalize_date_cell(date(2026, 5, 4), dn.DateColumn("date", True, "generic"))
    assert res2.normalized_text == "04.05.2026", res2.normalized_text
    print("PASS: real datetime/date objects normalize with second rounding")


def test_text_formats() -> None:
    dt_col = dn.DateColumn(kind="datetime", serial_ok=True, source="report_207")
    cases = {
        "2026-05-04 07:24:03": "04.05.2026 07:24",
        "2026-05-04 07:24": "04.05.2026 07:24",
        "2026-05-04": "04.05.2026 00:00",  # datetime column forces HH:MM
        "04.05.2026 07:24:03": "04.05.2026 07:24",
        "04.05.2026 07:24": "04.05.2026 07:24",
        "04/05/2026 07:24:03": "04.05.2026 07:24",
        "2026-05-04 07:24:03+02:00": "04.05.2026 07:24",  # tz suffix dropped, local kept
        "2026-05-04T07:24:03": "04.05.2026 07:24",
    }
    for raw, expected in cases.items():
        res = dn.normalize_date_cell(raw, dt_col)
        assert res.status == "normalized", (raw, res)
        assert res.normalized_text == expected, (raw, res.normalized_text)

    date_only_col = dn.DateColumn(kind="date", serial_ok=True, source="generic")
    res = dn.normalize_date_cell("04.05.2026", date_only_col)
    assert res.normalized_text == "04.05.2026", res.normalized_text
    print("PASS: accepted text date/datetime formats normalize to the canonical output")


def test_safety_rules() -> None:
    dt_col = dn.DateColumn(kind="datetime", serial_ok=True, source="report_207")
    assert dn.normalize_date_cell(None, dt_col).status == "empty"
    assert dn.normalize_date_cell("   ", dt_col).status == "empty"
    assert dn.normalize_date_cell(True, dt_col).status == "unparseable"  # bool not a serial
    assert dn.normalize_date_cell(float("inf"), dt_col).status == "unparseable"
    assert dn.normalize_date_cell("not-a-date", dt_col).status == "unparseable"
    # Out-of-window serial preserved + flagged, never silently converted.
    assert dn.normalize_date_cell(10.0, dt_col).status == "unparseable"

    # Numeric value in a non-serial date column is preserved (passthrough), not converted.
    weak_col = dn.DateColumn(kind="date", serial_ok=False, source="generic")
    res = dn.normalize_date_cell("123456", weak_col)
    assert res.status == "passthrough", res
    print("PASS: booleans, non-finite, out-of-range, and non-serial numerics are not corrupted")


# --------------------------------------------------------------------------- #
# Integration: CSV report_207 normalization
# --------------------------------------------------------------------------- #
def _report_207_csv_bytes(rows: list[list[str]]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";", lineterminator="\n")
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


def test_csv_report_207_canonicalizes_mixed_formats() -> None:
    rows = [
        ["207 Raport przekroczeń limitów prędkości drogowej", "", "", "", ""],
        ["", "", "", "", ""],
        REPORT_207_HEADER,
        ["2026-05-04 07:24:03", "WD2467V", "54", "50", "ulica Jesienna 122"],
        ["46164.509224537", "WD2467V", "53", "50", "Częstochowa"],
        ["04.05.2026 07:24", "WD2467V", "60", "50", "Łódź"],
        ["not-a-date", "WD2467V", "61", "50", "Kraków"],
    ]
    with tempfile.TemporaryDirectory(prefix="stage1-date-csv-") as tmp:
        out = Path(tmp) / "n.csv"
        meta = _convert_to_canonical_csv(_report_207_csv_bytes(rows), ".csv", out)
        out_rows = _read_rows(out)

    # Header preserved verbatim.
    assert out_rows[2] == REPORT_207_HEADER, out_rows[2]
    # Date column canonicalized; other columns untouched.
    assert out_rows[3][0] == "04.05.2026 07:24" and out_rows[3][2] == "54", out_rows[3]
    assert out_rows[4][0] == "22.05.2026 12:13", out_rows[4]
    assert out_rows[5][0] == "04.05.2026 07:24", out_rows[5]
    # Unparseable value preserved verbatim.
    assert out_rows[6][0] == "not-a-date", out_rows[6]

    col_meta = {c["column"]: c for c in meta["columns"]}
    assert "Data i czas" in col_meta, meta
    stats = col_meta["Data i czas"]
    assert stats["treated_as"] == "datetime" and stats["source"] == "report_207", stats
    assert stats["parsed"] == 3 and stats["unparseable"] == 1, stats
    assert stats["unparseable_sample"] == ["not-a-date"], stats
    assert meta["total_unparseable"] == 1, meta
    print("PASS: report_207 CSV emits stable DD.MM.YYYY HH:MM and flags unparseable values")


def test_csv_does_not_touch_numeric_columns() -> None:
    rows = [
        ["distance_km", "speed_kmh", "count"],
        ["120.5", "54", "3"],
        ["240", "61", "7"],
    ]
    with tempfile.TemporaryDirectory(prefix="stage1-date-num-") as tmp:
        out = Path(tmp) / "n.csv"
        meta = _convert_to_canonical_csv(_report_207_csv_bytes(rows), ".csv", out)
        out_rows = _read_rows(out)
    assert out_rows == rows, out_rows
    assert meta["columns"] == [], meta
    print("PASS: numeric-only tables are left untouched (no date columns detected)")


# --------------------------------------------------------------------------- #
# Integration: XLSX normalization (serial floats + real datetimes)
# --------------------------------------------------------------------------- #
def _xlsx_bytes(header: list, data_rows: list[list]) -> bytes:
    openpyxl = __import__("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(header)
    for row in data_rows:
        ws.append(row)
    buffer = io.BytesIO()
    wb.save(buffer)
    wb.close()
    return buffer.getvalue()


def test_xlsx_serial_and_datetime_objects() -> None:
    raw = _xlsx_bytes(
        REPORT_207_HEADER,
        [
            [datetime(2026, 5, 4, 7, 24, 3), "WD2467V", 54, 50, "ulica Jesienna 122"],
            [46164.509224537, "WD2467V", 53, 50, "Częstochowa"],
        ],
    )
    with tempfile.TemporaryDirectory(prefix="stage1-date-xlsx-") as tmp:
        out = Path(tmp) / "n.csv"
        meta = _convert_to_canonical_csv(raw, ".xlsx", out)
        out_rows = _read_rows(out)
    assert out_rows[0] == REPORT_207_HEADER, out_rows[0]
    assert out_rows[1][0] == "04.05.2026 07:24", out_rows[1]
    assert out_rows[2][0] == "22.05.2026 12:13", out_rows[2]
    # Non-date numeric columns keep their legacy normalized output.
    assert out_rows[1][2] == "54" and out_rows[1][3] == "50", out_rows[1]
    col_meta = {c["column"]: c for c in meta["columns"]}
    assert col_meta["Data i czas"]["parsed"] == 2, col_meta
    fams = col_meta["Data i czas"]["input_format_families"]
    assert fams.get("datetime_object") == 1 and fams.get("excel_serial") == 1, fams
    print("PASS: .xlsx real datetime objects and serial floats both normalize")


# --------------------------------------------------------------------------- #
# Integration: multi-sheet (paginated) workbooks
# --------------------------------------------------------------------------- #
def _xlsx_multisheet_bytes(sheets: list[tuple[str, list[list]]]) -> bytes:
    openpyxl = __import__("openpyxl")
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets:
        ws = wb.create_sheet(title=name)
        for row in rows:
            ws.append(row)
    buffer = io.BytesIO()
    wb.save(buffer)
    wb.close()
    return buffer.getvalue()


def _col0_format_counts(out_rows: list[list[str]]) -> dict[str, int]:
    import re

    canon = re.compile(r"^\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}$")
    serial = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")
    counts = {"canonical": 0, "serial": 0, "other": 0}
    for row in out_rows:
        if not row:
            continue
        v = (row[0] or "").strip()
        if not v:
            continue
        if canon.match(v):
            counts["canonical"] += 1
        elif serial.match(v):
            counts["serial"] += 1
        else:
            counts["other"] += 1
    return counts


def test_xlsx_multisheet_continuation_canonicalizes() -> None:
    """Paginated export: a continuation sheet may have no header, or a header that
    sits below leading continuation data. All serial date cells must canonicalize
    via workbook-level date-column inheritance (the real report_207 .xls splits
    across the 65,536-row sheet limit this way)."""
    serial = 46164.509224537  # -> 22.05.2026 12:13
    data = lambda: [serial, "WD2467V", 54, 50, "Łódź"]
    raw = _xlsx_multisheet_bytes(
        [
            # Sheet 1 establishes the Data i czas column via its own header.
            ("p1", [REPORT_207_HEADER, data(), data(), data()]),
            # Sheet 2: pure continuation, NO header at all.
            ("p2", [data(), data(), data()]),
            # Sheet 3: leading continuation rows BEFORE a deep repeated header.
            ("p3", [data(), data(), REPORT_207_HEADER, data()]),
        ]
    )
    with tempfile.TemporaryDirectory(prefix="stage1-date-multisheet-") as tmp:
        out = Path(tmp) / "n.csv"
        meta = _convert_to_canonical_csv(raw, ".xlsx", out)
        out_rows = _read_rows(out)

    counts = _col0_format_counts(out_rows)
    assert counts["serial"] == 0, counts  # no Data i czas serial leaks through
    assert counts["canonical"] == 9, counts  # 3 + 3 + 3 data rows across sheets
    assert meta["columns"][0]["parsed"] == 9, meta
    print("PASS: multi-sheet continuation serials canonicalize via workbook inheritance")


def test_xlsx_inheritance_guard_skips_non_date_sheet() -> None:
    """Inheritance must not reclassify a differently shaped sheet's numeric column:
    a continuation sheet whose first column is not date-like is left untouched."""
    serial = 46164.509224537
    raw = _xlsx_multisheet_bytes(
        [
            ("p1", [REPORT_207_HEADER, [serial, "WD2467V", 54, 50, "Łódź"], [serial, "WD9999X", 60, 50, "Kraków"]]),
            # Non-date numeric table: col0 values are clearly out of the serial window.
            ("odo", [["odometer", "trips", "note"], [120.5, 3, "x"], [240.0, 7, "y"]]),
        ]
    )
    with tempfile.TemporaryDirectory(prefix="stage1-date-guard-") as tmp:
        out = Path(tmp) / "n.csv"
        _convert_to_canonical_csv(raw, ".xlsx", out)
        out_rows = _read_rows(out)

    flat = {(r[0] or "").strip() for r in out_rows if r}
    assert "22.05.2026 12:13" in flat, out_rows  # report sheet still canonicalized
    assert "120.5" in flat and "240" in flat, out_rows  # odometer column preserved verbatim
    counts = _col0_format_counts(out_rows)
    assert counts["canonical"] == 2, counts
    print("PASS: inheritance guard leaves a non-date continuation column untouched")


# --------------------------------------------------------------------------- #
# Integration: XLS (BIFF) Excel serial in a date column
# --------------------------------------------------------------------------- #
def _biff_record(code: int, data: bytes = b"") -> bytes:
    return struct.pack("<HH", code, len(data)) + data


def _biff_bof(stream_type: int) -> bytes:
    return _biff_record(0x0809, struct.pack("<HHHHII", 0x0600, stream_type, 0x0DBB, 0x07CC, 0x41, 0x06))


def _biff_eof() -> bytes:
    return _biff_record(0x000A)


def _biff_dimensions(nrows: int, ncols: int) -> bytes:
    return _biff_record(0x0200, struct.pack("<IIHHH", 0, nrows, 0, ncols, 0))


def _biff_label(row_idx: int, col_idx: int, value: str) -> bytes:
    text = str(value).encode("latin1")
    return _biff_record(0x0204, struct.pack("<HHHHB", row_idx, col_idx, 0, len(text), 0) + text)


def _biff_number(row_idx: int, col_idx: int, value: float) -> bytes:
    return _biff_record(0x0203, struct.pack("<HHH", row_idx, col_idx, 0) + struct.pack("<d", value))


def _biff_boundsheet(offset: int, name: str) -> bytes:
    encoded = name.encode("latin1")
    return _biff_record(0x0085, struct.pack("<IBBB", offset, 0, 0, len(encoded)) + b"\x00" + encoded)


def _raw_xls_with_serial() -> bytes:
    header = ["Data i czas", "Nr rejestracyjny", "Speed"]  # ASCII-only (BIFF labels are latin1)
    rows_cells: list[list[tuple[str, object]]] = [
        [("label", h) for h in header],
        [("number", 46164.509224537), ("label", "WD2467V"), ("number", 54.0)],
        [("number", 45000.0), ("label", "WD2467V"), ("number", 61.0)],
    ]
    stream = _biff_bof(0x0010) + _biff_dimensions(len(rows_cells), len(header))
    for r, cells in enumerate(rows_cells):
        for c, (kind, value) in enumerate(cells):
            if kind == "label":
                stream += _biff_label(r, c, str(value))
            else:
                stream += _biff_number(r, c, float(value))
    stream += _biff_eof()

    offsets = [0]
    for _ in range(5):
        globals_stream = _biff_bof(0x0005) + _biff_boundsheet(offsets[0], "Sheet1") + _biff_eof()
        new_offset = len(globals_stream)
        if [new_offset] == offsets:
            break
        offsets = [new_offset]
    return globals_stream + stream


def test_xls_serial_in_date_column() -> None:
    with tempfile.TemporaryDirectory(prefix="stage1-date-xls-") as tmp:
        out = Path(tmp) / "n.csv"
        meta = _convert_to_canonical_csv(_raw_xls_with_serial(), ".xls", out)
        out_rows = _read_rows(out)
    assert out_rows[0] == ["Data i czas", "Nr rejestracyjny", "Speed"], out_rows[0]
    assert out_rows[1][0] == "22.05.2026 12:13", out_rows[1]
    assert out_rows[2][0] == "15.03.2023 00:00", out_rows[2]  # datetime column -> HH:MM
    # speed column untouched (legacy float formatting)
    assert out_rows[1][2] == "54", out_rows[1]
    col_meta = {c["column"]: c for c in meta["columns"]}
    assert col_meta["Data i czas"]["parsed"] == 2, col_meta
    print("PASS: .xls Excel serial numbers in a date column normalize via the shared helper")


# --------------------------------------------------------------------------- #
# Stage 2 acceptance of the canonical output
# --------------------------------------------------------------------------- #
def test_stage2_report_207_accepts_canonical_output() -> None:
    rows = [
        ["207 Raport przekroczeń limitów prędkości drogowej", "", "", "", ""],
        REPORT_207_HEADER,
    ]
    rows += [[f"04.05.2026 07:{i:02d}", f"WD{i:04d}", "54", "50", "ulica Jesienna 122"] for i in range(12)]
    df = pd.DataFrame(rows, dtype=str)
    cleaned = Report207.clean(df)
    result = validate(cleaned, Report207)
    assert result.is_valid, result.schema_diff
    stats = result.schema_diff["type_parse_stats"]["Data i czas"]
    assert stats["fails"] == 0, stats
    assert "Data i czas" not in result.schema_diff.get("type_parse_blocking_cols", []), result.schema_diff
    print("PASS: Stage 2 report_207 validation accepts canonical DD.MM.YYYY HH:MM datetimes")


def test_stage3_migration_regex_accepts_canonical_output() -> None:
    """The report_207 -> client_trips migration SQL must accept the new format."""
    import re

    # Mirror of the DD.MM.YYYY [HH:MM(:SS)] branch in the migration SQL.
    pattern = re.compile(r"^[0-9]{1,2}[.][0-9]{1,2}[.][0-9]{4}[ ]+[0-9]{1,2}:[0-9]{2}(:[0-9]{2})?$")
    assert pattern.match("04.05.2026 07:24"), "canonical datetime must match migration SQL"
    assert pattern.match("22.05.2026 12:13"), "serial-derived datetime must match migration SQL"
    print("PASS: Stage 3 report_207 migration SQL pattern accepts canonical DD.MM.YYYY HH:MM")


def main() -> None:
    test_classify_date_columns()
    test_excel_serial_datetime_and_date()
    test_real_date_objects()
    test_text_formats()
    test_safety_rules()
    test_csv_report_207_canonicalizes_mixed_formats()
    test_csv_does_not_touch_numeric_columns()
    test_xlsx_serial_and_datetime_objects()
    test_xlsx_multisheet_continuation_canonicalizes()
    test_xlsx_inheritance_guard_skips_non_date_sheet()
    test_xls_serial_in_date_column()
    test_stage2_report_207_accepts_canonical_output()
    test_stage3_migration_regex_accepts_canonical_output()
    print("\nOK - Stage 1 date normalization checks passed")


if __name__ == "__main__":
    main()
