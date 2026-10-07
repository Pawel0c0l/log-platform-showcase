#!/usr/bin/env python3
import csv
import io
import re
import struct
import zipfile
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.mail.fetch_reports import _convert_to_canonical_csv


def _read_rows(path: Path) -> list[list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.reader(f, delimiter=";"))


def _write_openpyxl_workbook(ext: str, *, empty: bool = False) -> bytes:
    openpyxl = __import__("openpyxl")
    wb = openpyxl.Workbook()
    ws1 = wb.active
    ws1.title = "FleetA"
    if empty:
        ws1.append([None, None])
        ws2 = wb.create_sheet("FleetB")
        ws2.append([None, None])
    else:
        ws1.append([None, None])
        ws1.append([f"{ext}_HEADER_A", "distance_km"])
        ws1.append([f"{ext}_A_MARKER", 120])
        ws1.append([None, None])
        ws1.append([f"{ext}_A_AFTER_EMPTY", 121])

        ws2 = wb.create_sheet("FleetB")
        ws2.append([None, None])
        ws2.append([f"{ext}_HEADER_B", "distance_km"])
        ws2.append([f"{ext}_B_MARKER", 240])

    buffer = io.BytesIO()
    wb.save(buffer)
    wb.close()
    return buffer.getvalue()


def _biff_record(code: int, data: bytes = b"") -> bytes:
    return struct.pack("<HH", code, len(data)) + data


def _biff_bof(stream_type: int) -> bytes:
    return _biff_record(
        0x0809,
        struct.pack("<HHHHII", 0x0600, stream_type, 0x0DBB, 0x07CC, 0x00000041, 0x00000006),
    )


def _biff_eof() -> bytes:
    return _biff_record(0x000A)


def _biff_dimensions(nrows: int, ncols: int) -> bytes:
    return _biff_record(0x0200, struct.pack("<IIHHH", 0, nrows, 0, ncols, 0))


def _biff_label(row_idx: int, col_idx: int, value: object) -> bytes:
    text = str(value).encode("latin1")
    return _biff_record(0x0204, struct.pack("<HHHHB", row_idx, col_idx, 0, len(text), 0) + text)


def _biff_boundsheet(offset: int, name: str) -> bytes:
    encoded = name.encode("latin1")
    return _biff_record(0x0085, struct.pack("<IBBB", offset, 0, 0, len(encoded)) + b"\x00" + encoded)


def _raw_xls_bytes() -> bytes:
    sheet_defs = [
        ("FleetA", [["XLS_HEADER_A", "distance_km"], ["XLS_A_MARKER", 120], [None, None], ["XLS_A_AFTER_EMPTY", 121]]),
        ("FleetB", [["XLS_HEADER_B", "distance_km"], ["XLS_B_MARKER", 240]]),
    ]
    sheet_streams: list[tuple[str, bytes]] = []
    for sheet_name, rows in sheet_defs:
        ncols = max(len(row) for row in rows)
        stream = _biff_bof(0x0010) + _biff_dimensions(len(rows), ncols)
        for row_idx, row in enumerate(rows):
            for col_idx, value in enumerate(row):
                if value is None or value == "":
                    continue
                stream += _biff_label(row_idx, col_idx, value)
        stream += _biff_eof()
        sheet_streams.append((sheet_name, stream))

    offsets = [0] * len(sheet_streams)
    for _ in range(5):
        globals_stream = _biff_bof(0x0005)
        globals_stream += b"".join(_biff_boundsheet(offset, sheet_name) for (sheet_name, _), offset in zip(sheet_streams, offsets))
        globals_stream += _biff_eof()
        next_offsets: list[int] = []
        pos = len(globals_stream)
        for _sheet_name, stream in sheet_streams:
            next_offsets.append(pos)
            pos += len(stream)
        if next_offsets == offsets:
            break
        offsets = next_offsets

    return globals_stream + b"".join(stream for _sheet_name, stream in sheet_streams)


def _assert_exact_one_sheet_separator(rows: list[list[str]]) -> None:
    separator_indices = [idx for idx, row in enumerate(rows) if row == []]
    assert separator_indices == [4], rows
    assert rows[3] != [] and rows[5] != [], rows


def _assert_excel_output(rows: list[list[str]], *, prefix: str) -> None:
    flattened = "\n".join(";".join(row) for row in rows)
    assert f"{prefix}_HEADER_A;distance_km" in flattened, flattened
    assert f"{prefix}_A_MARKER;120" in flattened, flattened
    assert f"{prefix}_A_AFTER_EMPTY;121" in flattened, flattened
    assert f"{prefix}_HEADER_B;distance_km" in flattened, flattened
    assert f"{prefix}_B_MARKER;240" in flattened, flattened
    assert "__sheet_name" not in flattened, flattened
    assert rows[0] == [f"{prefix}_HEADER_A", "distance_km"], rows
    assert rows[2] == ["", ""], rows
    _assert_exact_one_sheet_separator(rows)



def _workbook_with_stale_a1_dimension() -> bytes:
    openpyxl = __import__("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "FleetWeb"
    ws.append(["Nr Rejestracyjny", "Data rozpoczęcia", " Czas rozpoczęcia", " przekroczenia obr/min"])
    ws.append(["WD12345", "2026-05-12", "08:30", 2])
    buffer = io.BytesIO()
    wb.save(buffer)
    wb.close()

    source = io.BytesIO(buffer.getvalue())
    patched = io.BytesIO()
    with zipfile.ZipFile(source, "r") as zin, zipfile.ZipFile(patched, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            payload = zin.read(item.filename)
            if item.filename == "xl/worksheets/sheet1.xml":
                xml = payload.decode("utf-8")
                xml = re.sub(r'<dimension ref="[^"]+"/>', '<dimension ref="A1"/>', xml, count=1)
                payload = xml.encode("utf-8")
            zout.writestr(item, payload)
    return patched.getvalue()


def _test_xlsx_stale_a1_dimension_reads_real_rows() -> None:
    with tempfile.TemporaryDirectory(prefix="stage1-xlsx-stale-dim-") as tmp:
        out = Path(tmp) / "normalized.csv"
        _convert_to_canonical_csv(_workbook_with_stale_a1_dimension(), ".xlsx", out)
        rows = _read_rows(out)
    flattened = "\n".join(";".join(row) for row in rows)
    assert "Nr Rejestracyjny;Data rozpoczęcia;Czas rozpoczęcia;przekroczenia obr/min" in flattened, flattened
    assert "WD12345;12.05.2026;08:30;2" in flattened, flattened
    assert len(rows) == 2, rows
    print("PASS: .xlsx normalization ignores stale A1 worksheet dimension metadata")

def _test_xls_includes_all_sheets() -> None:
    with tempfile.TemporaryDirectory(prefix="stage1-xls-normalize-") as tmp:
        out = Path(tmp) / "normalized.csv"
        _convert_to_canonical_csv(_raw_xls_bytes(), ".xls", out)
        rows = _read_rows(out)
    _assert_excel_output(rows, prefix="XLS")
    print("PASS: .xls normalization emits every non-empty worksheet")


def _test_xlsx_preserves_first_rows_without_sheet_name_column() -> None:
    with tempfile.TemporaryDirectory(prefix="stage1-xlsx-normalize-") as tmp:
        out = Path(tmp) / "normalized.csv"
        _convert_to_canonical_csv(_write_openpyxl_workbook("XLSX"), ".xlsx", out)
        rows = _read_rows(out)
    _assert_excel_output(rows, prefix="XLSX")
    print("PASS: .xlsx normalization preserves first rows and omits __sheet_name")


def _test_xlsm_all_sheets_regression() -> None:
    with tempfile.TemporaryDirectory(prefix="stage1-xlsm-normalize-") as tmp:
        out = Path(tmp) / "normalized.csv"
        _convert_to_canonical_csv(_write_openpyxl_workbook("XLSM"), ".xlsm", out)
        rows = _read_rows(out)
    _assert_excel_output(rows, prefix="XLSM")
    print("PASS: .xlsm normalization still emits every non-empty worksheet")


def _test_csv_still_normalizes_to_canonical_semicolon_utf8_sig() -> None:
    with tempfile.TemporaryDirectory(prefix="stage1-csv-normalize-") as tmp:
        out = Path(tmp) / "normalized.csv"
        _convert_to_canonical_csv("name,value\nLodz,1\n".encode("utf-8"), ".csv", out)
        rows = _read_rows(out)
        raw = out.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), raw[:8]
    assert rows == [["name", "value"], ["Lodz", "1"]], rows
    assert b";" in raw and b"," not in raw.replace(b"\xef\xbb\xbf", b""), raw
    print("PASS: .csv normalization still writes canonical semicolon UTF-8-SIG CSV")


def _test_empty_workbook_raises_clear_runtime_error() -> None:
    with tempfile.TemporaryDirectory(prefix="stage1-empty-xlsx-") as tmp:
        out = Path(tmp) / "normalized.csv"
        try:
            _convert_to_canonical_csv(_write_openpyxl_workbook("XLSX", empty=True), ".xlsx", out)
        except RuntimeError as exc:
            assert "No non-empty worksheets in .XLSX file" in str(exc), str(exc)
            print("PASS: empty workbook raises clear RuntimeError")
            return
    raise AssertionError("Expected RuntimeError for workbook with only empty sheets")


def main() -> None:
    _test_xls_includes_all_sheets()
    _test_xlsx_preserves_first_rows_without_sheet_name_column()
    _test_xlsx_stale_a1_dimension_reads_real_rows()
    _test_xlsm_all_sheets_regression()
    _test_csv_still_normalizes_to_canonical_semicolon_utf8_sig()
    _test_empty_workbook_raises_clear_runtime_error()


if __name__ == "__main__":
    main()
