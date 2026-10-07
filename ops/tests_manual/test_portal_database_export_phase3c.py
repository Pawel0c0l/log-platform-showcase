#!/usr/bin/env python3
"""Manual regression tests for User Portal Phase 3C database export and audit events.

Run:

    cd /opt/log-platform
    env PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_portal_database_export_phase3c.py
"""
from __future__ import annotations

import csv
import io
import sys
import types
import uuid
import zipfile
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from xml.etree import ElementTree as ET


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class _HTTPException(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class _App:
    def __init__(self, *args, **kwargs):
        pass

    def get(self, *args, **kwargs):
        return lambda fn: fn

    def post(self, *args, **kwargs):
        return lambda fn: fn

    def patch(self, *args, **kwargs):
        return lambda fn: fn

    def delete(self, *args, **kwargs):
        return lambda fn: fn

    def on_event(self, *args, **kwargs):
        return lambda fn: fn


class _StreamingResponse:
    def __init__(self, body, media_type=None, headers=None):
        if isinstance(body, (bytes, bytearray)):
            self.body = bytes(body)
        else:
            self.body = b"".join(body)
        self.media_type = media_type
        self.headers = headers or {}
        self.status_code = 200


class _HTMLResponse:
    def __init__(self, content, status_code=200, headers=None, media_type=None):
        self.body = str(content).encode("utf-8")
        self.status_code = status_code
        self.headers = headers or {}
        self.media_type = media_type or "text/html"


def _identity_default(default=None, *args, **kwargs):
    return default


def _install_import_stubs() -> None:
    fastapi = types.ModuleType("fastapi")
    fastapi.FastAPI = _App
    fastapi.Header = _identity_default
    fastapi.HTTPException = _HTTPException
    fastapi.Request = object
    fastapi.UploadFile = object
    fastapi.File = _identity_default
    fastapi.Form = _identity_default
    fastapi.Query = _identity_default
    fastapi.Body = _identity_default
    responses = types.ModuleType("fastapi.responses")
    responses.HTMLResponse = _HTMLResponse
    responses.StreamingResponse = _StreamingResponse

    boto3 = types.ModuleType("boto3")
    boto3.client = lambda *args, **kwargs: None

    psycopg = types.ModuleType("psycopg")
    rows = types.ModuleType("psycopg.rows")
    rows.dict_row = object()

    sys.modules.setdefault("fastapi", fastapi)
    sys.modules.setdefault("fastapi.responses", responses)
    sys.modules.setdefault("boto3", boto3)
    sys.modules.setdefault("psycopg", psycopg)
    sys.modules.setdefault("psycopg.rows", rows)


_install_import_stubs()

import api.main as api_main  # noqa: E402


ADMIN_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
USER_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
DATASET_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"


class _FakeUrl:
    def __init__(self, path=f"/user/database/datasets/{DATASET_ID}/export", query=""):
        self.path = path
        self.query = query


class _FakeClient:
    host = "127.0.0.1"


class _FakeRequest:
    def __init__(self, *, path=f"/user/database/datasets/{DATASET_ID}/export", query="", cookies=None):
        self.url = _FakeUrl(path, query)
        self.cookies = cookies or {}
        self.headers = {"user-agent": "manual-test"}
        self.client = _FakeClient()


def _html(response) -> str:
    return response.body.decode("utf-8")


def _user(*, admin=False, user_id=USER_ID, username="alice"):
    return {
        "user_id": user_id,
        "username": username,
        "display_name": username.title(),
        "is_active": True,
        "is_admin": admin,
        "permissions": [],
    }


def _dataset(**overrides):
    data = {
        "dataset_id": DATASET_ID,
        "client_code": "ACME_01",
        "client_display_name": "Acme Logistics",
        "dataset_name": "Approved trips",
        "slug": "approved-trips",
        "description": "Approved portal dataset",
        "schema_name": "public",
        "table_name": "trips",
        "default_date_column": "trip_date",
        "is_active": True,
        "can_view_rows": True,
        "can_filter_rows": True,
        "can_export_rows": True,
    }
    data.update(overrides)
    return data


def _visible_columns():
    return [
        {
            "column_name": "trip_date",
            "display_name": "Trip date",
            "data_type": "date",
            "is_visible": True,
            "is_filterable": True,
            "is_sortable": True,
            "display_order": 10,
        },
        {
            "column_name": "driver_name",
            "display_name": "Driver",
            "data_type": "text",
            "is_visible": True,
            "is_filterable": True,
            "is_sortable": True,
            "display_order": 20,
        },
    ]


def _rows():
    return [
        {"trip_date": "2026-05-28", "driver_name": "Alice", "internal_secret": "hidden"},
        {"trip_date": "2026-05-29", "driver_name": "Bob", "internal_secret": "hidden2"},
    ]


def _patch(name, value):
    old = getattr(api_main, name)
    setattr(api_main, name, value)
    return old


def _restore(patches):
    for name, old in reversed(patches):
        setattr(api_main, name, old)


def _xlsx_sheet_values(body: bytes) -> list[list[str]]:
    with zipfile.ZipFile(io.BytesIO(body)) as workbook:
        names = set(workbook.namelist())
        assert "xl/workbook.xml" in names and "xl/worksheets/sheet1.xml" in names, names
        sheet_xml = workbook.read("xl/worksheets/sheet1.xml")
    root = ET.fromstring(sheet_xml)
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    rows = []
    for row in root.findall(".//m:row", ns):
        values = []
        for cell in row.findall("m:c", ns):
            inline = cell.find("m:is/m:t", ns)
            raw = cell.find("m:v", ns)
            values.append((inline.text if inline is not None else raw.text if raw is not None else "") or "")
        rows.append(values)
    return rows


def _xlsx_sheet_cells(body: bytes) -> dict[str, dict[str, str | bool]]:
    with zipfile.ZipFile(io.BytesIO(body)) as workbook:
        sheet_xml = workbook.read("xl/worksheets/sheet1.xml")
    root = ET.fromstring(sheet_xml)
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    cells: dict[str, dict[str, str | bool]] = {}
    for cell in root.findall(".//m:c", ns):
        inline = cell.find("m:is/m:t", ns)
        raw = cell.find("m:v", ns)
        ref = str(cell.get("r") or "")
        cells[ref] = {
            "type": str(cell.get("t") or ""),
            "value": (inline.text if inline is not None else raw.text if raw is not None else "") or "",
            "inline": inline is not None,
        }
    return cells


_DEFAULT_DATASET = object()


def _base_export_patches(dataset=_DEFAULT_DATASET, *, columns=None, rows=None, total=2, audit_events=None, captured_calls=None):
    dataset = _dataset() if dataset is _DEFAULT_DATASET else dataset
    columns = _visible_columns() if columns is None else columns
    rows = _rows() if rows is None else rows
    audit_events = [] if audit_events is None else audit_events
    captured_calls = [] if captured_calls is None else captured_calls

    def fake_count(dataset_arg, columns_arg, params_arg):
        return total, {"sort": "trip_date", "direction": "desc", "active_filters": ["driver_name"]}, None

    def fake_list(dataset_arg, columns_arg, params_arg, limit, offset, display_columns=None):
        captured_calls.append({"params": params_arg, "limit": limit, "offset": offset, "display_columns": display_columns})
        return rows, {"sort": "trip_date", "direction": "desc", "active_filters": ["driver_name"]}, None

    def fake_audit(**kwargs):
        audit_events.append(kwargs)

    return [
        ("get_current_artifact_user", _patch("get_current_artifact_user", lambda request: _user(user_id=USER_ID))),
        ("_get_portal_database_dataset_for_user", _patch("_get_portal_database_dataset_for_user", lambda dataset_id, user_id: dataset if dataset_id == DATASET_ID and user_id == USER_ID else None)),
        ("_get_portal_database_visible_columns", _patch("_get_portal_database_visible_columns", lambda dataset_id: columns if dataset_id == DATASET_ID else [])),
        ("_count_portal_database_rows", _patch("_count_portal_database_rows", fake_count)),
        ("_list_portal_database_rows", _patch("_list_portal_database_rows", fake_list)),
        ("_portal_audit_event_safe", _patch("_portal_audit_event_safe", fake_audit)),
    ]


def _test_unauthenticated_export_redirects_and_audits() -> None:
    audit_events = []
    patches = [
        ("get_current_artifact_user", _patch("get_current_artifact_user", lambda request: None)),
        ("_portal_audit_event_safe", _patch("_portal_audit_event_safe", lambda **kwargs: audit_events.append(kwargs))),
    ]
    try:
        response = api_main.user_portal_database_dataset_export(DATASET_ID, _FakeRequest(query="format=csv"))
    finally:
        _restore(patches)
    assert response.status_code == 303, response.status_code
    assert response.headers["Location"].startswith("/artifact-explorer/login"), response.headers
    assert audit_events[-1]["event_type"] == "database_export_denied", audit_events
    assert audit_events[-1]["metadata_json"]["reason"] == "unauthenticated", audit_events
    print("PASS: unauthenticated exports redirect to login and write denied audit event")


def _test_export_access_boundaries_audit_denied() -> None:
    audit_events = []
    patches = _base_export_patches(dataset=None, audit_events=audit_events)
    try:
        missing = api_main.user_portal_database_dataset_export(DATASET_ID, _FakeRequest(query="format=csv"))
    finally:
        _restore(patches)
    assert missing.status_code == 404, _html(missing)
    assert audit_events[-1]["event_type"] == "database_export_denied", audit_events
    assert audit_events[-1]["metadata_json"]["reason"] == "dataset_unavailable", audit_events

    audit_events = []
    patches = _base_export_patches(dataset=_dataset(can_export_rows=False), audit_events=audit_events)
    try:
        denied = api_main.user_portal_database_dataset_export(DATASET_ID, _FakeRequest(query="format=csv"))
    finally:
        _restore(patches)
    assert denied.status_code == 403, _html(denied)
    assert "Export is not enabled" in _html(denied), _html(denied)
    assert audit_events[-1]["event_type"] == "database_export_denied", audit_events
    assert audit_events[-1]["metadata_json"]["reason"] == "export_not_enabled", audit_events
    print("PASS: missing assignment and disabled export are blocked and audited")


def _test_authorized_csv_export_visible_columns_only() -> None:
    audit_events = []
    captured_calls = []
    patches = _base_export_patches(audit_events=audit_events, captured_calls=captured_calls)
    try:
        response = api_main.user_portal_database_dataset_export(
            DATASET_ID,
            _FakeRequest(query="format=csv&sort=trip_date&direction=desc&date_from=2026-05-01&filter__driver_name=Ali&op__driver_name=contains&limit=20000"),
        )
    finally:
        _restore(patches)
    assert response.status_code == 200, response.status_code
    assert response.media_type == "text/csv; charset=utf-8", response.media_type
    disposition = response.headers.get("Content-Disposition", "")
    assert "acme_01__approved-trips__" in disposition and disposition.endswith('__export.csv"'), disposition
    filename_part = disposition.split("filename=", 1)[1]
    assert " " not in filename_part and ";" not in filename_part, disposition
    text = response.body.decode("utf-8-sig")
    parsed = list(csv.reader(io.StringIO(text)))
    assert parsed[0] == ["Trip date", "Driver"], parsed
    assert parsed[1] == ["2026-05-28", "Alice"], parsed
    assert "internal_secret" not in text and "hidden" not in text, text
    assert captured_calls[-1]["limit"] == api_main.PORTAL_DATABASE_DIRECT_EXPORT_ROW_CAP == 20_000, captured_calls
    assert captured_calls[-1]["offset"] == 0, captured_calls
    assert captured_calls[-1]["params"]["filter__driver_name"] == ["Ali"], captured_calls
    success = audit_events[-1]
    assert success["event_type"] == "database_export_success", audit_events
    assert success["metadata_json"]["row_limit"] == api_main.PORTAL_DATABASE_DIRECT_EXPORT_ROW_CAP, success
    assert success["metadata_json"]["filter_keys"] == ["driver_name"], success
    assert "sql" not in str(success["metadata_json"]).lower(), success
    print("PASS: authorized CSV export uses visible columns, safe filename, limits, filters, and success audit")


def _test_validation_failures_are_clean_and_audited() -> None:
    audit_events = []
    patches = _base_export_patches(audit_events=audit_events)
    try:
        unsupported = api_main.user_portal_database_dataset_export(DATASET_ID, _FakeRequest(query="format=pdf"))
    finally:
        _restore(patches)
    assert unsupported.status_code == 400, unsupported.status_code
    assert "Export format must be csv or xlsx" in _html(unsupported), _html(unsupported)
    assert audit_events[-1]["event_type"] == "database_export_validation_failed", audit_events

    audit_events = []
    patches = _base_export_patches(audit_events=audit_events)
    old_count = _patch("_count_portal_database_rows", lambda dataset, columns, params: (0, {}, "Sort column is not available for this dataset."))
    patches.append(("_count_portal_database_rows", old_count))
    try:
        invalid = api_main.user_portal_database_dataset_export(DATASET_ID, _FakeRequest(query="format=csv&sort=internal_secret"))
    finally:
        _restore(patches)
    assert "Sort column is not available" in _html(invalid), _html(invalid)
    assert audit_events[-1]["event_type"] == "database_export_validation_failed", audit_events
    assert "internal_secret" not in str(audit_events[-1]["metadata_json"].get("filter_keys")), audit_events
    print("PASS: unsupported format and invalid query params fail cleanly and are audited")


def _representative_columns():
    return [
        {"column_name": "trip_date", "display_name": "Trip date", "data_type": "date", "is_visible": True, "is_filterable": True, "is_sortable": True, "display_order": 10},
        {"column_name": "driver_name", "display_name": "Driver", "data_type": "text", "is_visible": True, "is_filterable": True, "is_sortable": True, "display_order": 20},
        {"column_name": "amount", "display_name": "Amount", "data_type": "numeric", "is_visible": True, "is_filterable": True, "is_sortable": True, "display_order": 30},
        {"column_name": "active", "display_name": "Active", "data_type": "boolean", "is_visible": True, "is_filterable": True, "is_sortable": True, "display_order": 40},
        {"column_name": "seen_at", "display_name": "Seen at", "data_type": "timestamp with time zone", "is_visible": True, "is_filterable": True, "is_sortable": True, "display_order": 50},
        {"column_name": "uid", "display_name": "UID", "data_type": "uuid", "is_visible": True, "is_filterable": True, "is_sortable": True, "display_order": 60},
        {"column_name": "payload", "display_name": "Payload", "data_type": "jsonb", "is_visible": True, "is_filterable": True, "is_sortable": False, "display_order": 70},
        {"column_name": "blob", "display_name": "Blob", "data_type": "bytea", "is_visible": True, "is_filterable": False, "is_sortable": False, "display_order": 80},
        {"column_name": "notes", "display_name": "Notes", "data_type": "text", "is_visible": True, "is_filterable": True, "is_sortable": False, "display_order": 90},
        {"column_name": "unicode_text", "display_name": "Unicode", "data_type": "text", "is_visible": True, "is_filterable": True, "is_sortable": False, "display_order": 100},
        {"column_name": "dirty_text", "display_name": "Dirty XML", "data_type": "text", "is_visible": True, "is_filterable": True, "is_sortable": False, "display_order": 110},
        {"column_name": "dirty_formula", "display_name": "Dirty formula", "data_type": "text", "is_visible": True, "is_filterable": True, "is_sortable": False, "display_order": 120},
    ]


def _representative_rows():
    dirty_surrogate = "ok\x01bad\x00" + chr(0xD800) + "end"
    return [
        {
            "trip_date": date(2026, 5, 28),
            "driver_name": "Alice",
            "amount": Decimal("12.34"),
            "active": True,
            "seen_at": datetime(2026, 5, 28, 10, 30, tzinfo=timezone.utc),
            "uid": uuid.UUID("749408f8-a9da-4854-82c7-9bcdefaacb92"),
            "payload": {"score": Decimal("9.5"), "flags": ["ok"]},
            "blob": b"\x00\xff",
            "notes": "=SUM(A1:A2)",
            "unicode_text": "Zażółć & < > \"quotes\" 😀",
            "dirty_text": dirty_surrogate,
            "dirty_formula": "\x01=1+1",
        },
        {
            "trip_date": None,
            "driver_name": "Bob",
            "amount": 7,
            "active": False,
            "seen_at": None,
            "uid": "uuid-like",
            "payload": ["x", 2],
            "blob": None,
            "notes": "plain",
            "unicode_text": "regular",
            "dirty_text": "clean",
            "dirty_formula": "@cmd",
        },
    ]


def _test_xlsx_export_supported_safely() -> None:
    audit_events = []
    patches = _base_export_patches(columns=_representative_columns(), rows=_representative_rows(), total=2, audit_events=audit_events)
    try:
        response = api_main.user_portal_database_dataset_export(DATASET_ID, _FakeRequest(query="format=xlsx&limit=10"))
    finally:
        _restore(patches)
    assert response.status_code == 200, response.status_code
    assert response.body.startswith(b"PK"), response.body[:8]
    assert response.media_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", response.media_type
    assert response.headers["Content-Disposition"].endswith('__export.xlsx"'), response.headers
    rows = _xlsx_sheet_values(response.body)
    assert rows[0] == ["Trip date", "Driver", "Amount", "Active", "Seen at", "UID", "Payload", "Blob", "Notes", "Unicode", "Dirty XML", "Dirty formula"], rows
    flat = "\n".join("|".join(row) for row in rows)
    assert "2026-05-28" in flat and "2026-05-28T10:30:00+00:00" in flat, flat
    assert "12.34" in flat and "749408f8-a9da-4854-82c7-9bcdefaacb92" in flat, flat
    assert "{&quot;" not in flat and '"flags"' in flat and "00ff" in flat, flat
    assert "'=SUM(A1:A2)" in flat and "'@cmd" in flat and "'�=1+1" in flat, flat
    assert "Zażółć & < > \"quotes\" 😀" in flat, flat
    assert "ok�bad��end" in flat, flat
    assert "\x01" not in flat and chr(0xD800) not in flat, repr(flat)
    csv_text = api_main._portal_database_rows_to_csv(_representative_columns(), _representative_rows()).decode("utf-8-sig")
    assert "'=SUM(A1:A2)" in csv_text and "'@cmd" in csv_text and "'�=1+1" in csv_text, csv_text
    assert audit_events[-1]["event_type"] == "database_export_success", audit_events
    print("PASS: XLSX export creates a valid workbook, sanitizes XML text, and safely serializes common database values")


def _test_decimal_numeric_exports_and_text_formula_protection() -> None:
    precise = "1234567890.12345678901234567890"
    columns = [
        {"column_name": "neg_decimal", "display_name": "Negative Decimal"},
        {"column_name": "text_negative", "display_name": "Text Negative"},
        {"column_name": "pos_decimal", "display_name": "Positive Decimal"},
        {"column_name": "zero_decimal", "display_name": "Zero Decimal"},
        {"column_name": "precise_decimal", "display_name": "Precise Decimal"},
        {"column_name": "decimal_nan", "display_name": "Decimal NaN"},
        {"column_name": "decimal_inf", "display_name": "Decimal Infinity"},
        {"column_name": "decimal_neg_inf", "display_name": "Decimal -Infinity"},
        {"column_name": "float_nan", "display_name": "Float NaN"},
        {"column_name": "float_inf", "display_name": "Float Infinity"},
    ]
    rows = [{
        "neg_decimal": Decimal("-12.34"),
        "text_negative": "-12.34",
        "pos_decimal": Decimal("12.34"),
        "zero_decimal": Decimal("0"),
        "precise_decimal": Decimal(precise),
        "decimal_nan": Decimal("NaN"),
        "decimal_inf": Decimal("Infinity"),
        "decimal_neg_inf": Decimal("-Infinity"),
        "float_nan": float("nan"),
        "float_inf": float("inf"),
    }]

    csv_rows = list(csv.reader(io.StringIO(api_main._portal_database_rows_to_csv(columns, rows).decode("utf-8-sig"))))
    assert csv_rows[1] == ["-12.34", "'-12.34", "12.34", "0", precise, "", "", "", "", ""], csv_rows

    cells = _xlsx_sheet_cells(api_main._portal_database_rows_to_xlsx(columns, rows))
    for ref, expected in {"A2": "-12.34", "C2": "12.34", "D2": "0", "E2": precise}.items():
        assert cells[ref]["type"] == "" and cells[ref]["inline"] is False and cells[ref]["value"] == expected, (ref, cells[ref])
    assert cells["B2"] == {"type": "inlineStr", "value": "'-12.34", "inline": True}, cells["B2"]
    for ref in ("F2", "G2", "H2", "I2", "J2"):
        assert cells[ref] == {"type": "inlineStr", "value": "", "inline": True}, (ref, cells[ref])
    print("PASS: Decimal exports keep finite numerics numeric, protect text negatives, preserve precision, and blank non-finite values")


def _test_xlsx_export_zero_rows_keeps_headers() -> None:
    audit_events = []
    patches = _base_export_patches(rows=[], total=0, audit_events=audit_events)
    try:
        response = api_main.user_portal_database_dataset_export(DATASET_ID, _FakeRequest(query="format=xlsx&limit=10"))
    finally:
        _restore(patches)
    assert response.status_code == 200, response.status_code
    assert response.media_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", response.media_type
    assert response.headers["Content-Disposition"].endswith('__export.xlsx"'), response.headers
    assert _xlsx_sheet_values(response.body) == [["Trip date", "Driver"]]
    assert audit_events[-1]["event_type"] == "database_export_success", audit_events
    print("PASS: XLSX export with zero matching rows still returns a valid workbook with headers")


def _test_serializer_failure_is_not_audited_as_success() -> None:
    audit_events = []
    patches = _base_export_patches(audit_events=audit_events)
    old_writer = _patch("_portal_database_rows_to_xlsx", lambda columns, rows: (_ for _ in ()).throw(RuntimeError("serializer exploded")))
    patches.append(("_portal_database_rows_to_xlsx", old_writer))
    try:
        try:
            api_main.user_portal_database_dataset_export(DATASET_ID, _FakeRequest(query="format=xlsx&limit=10"))
        except RuntimeError as exc:
            assert "serializer exploded" in str(exc), exc
        else:
            raise AssertionError("serializer failure did not propagate")
    finally:
        _restore(patches)
    event_types = [event["event_type"] for event in audit_events]
    assert "database_export_success" not in event_types, audit_events
    assert audit_events[-1]["event_type"] == "database_export_failed", audit_events
    assert audit_events[-1]["metadata_json"]["reason"] == "RuntimeError", audit_events
    print("PASS: serializer failures are audited as failed and never as successful exports")


def _test_csv_and_xlsx_share_filtered_rows_and_export_all_matches() -> None:
    rows = [
        {"trip_date": "2026-05-28", "driver_name": "Alice", "internal_secret": "hidden"},
        {"trip_date": "2026-05-29", "driver_name": "Bob", "internal_secret": "hidden2"},
    ]
    captured_csv = []
    patches = _base_export_patches(rows=rows, total=2, captured_calls=captured_csv)
    try:
        csv_response = api_main.user_portal_database_dataset_export(DATASET_ID, _FakeRequest(query="format=csv&page=3&limit=10&filter__driver_name=A&op__driver_name=contains"))
    finally:
        _restore(patches)
    captured_xlsx = []
    patches = _base_export_patches(rows=rows, total=2, captured_calls=captured_xlsx)
    try:
        xlsx_response = api_main.user_portal_database_dataset_export(DATASET_ID, _FakeRequest(query="format=xlsx&page=3&limit=10&filter__driver_name=A&op__driver_name=contains"))
    finally:
        _restore(patches)
    csv_rows = list(csv.reader(io.StringIO(csv_response.body.decode("utf-8-sig"))))
    xlsx_rows = _xlsx_sheet_values(xlsx_response.body)
    assert csv_rows == xlsx_rows == [["Trip date", "Driver"], ["2026-05-28", "Alice"], ["2026-05-29", "Bob"]], (csv_rows, xlsx_rows)
    assert captured_csv[-1]["offset"] == 0 and captured_xlsx[-1]["offset"] == 0, (captured_csv, captured_xlsx)
    assert captured_csv[-1]["params"]["filter__driver_name"] == ["A"], captured_csv
    assert captured_xlsx[-1]["params"]["filter__driver_name"] == ["A"], captured_xlsx
    print("PASS: CSV and XLSX use the same filtered rows and ignore browser page for export")


def _many_rows(count: int) -> list[dict]:
    return [{"trip_date": f"2026-05-{(idx % 28) + 1:02d}", "driver_name": f"Driver {idx}"} for idx in range(count)]


def _test_direct_export_boundaries_and_no_truncation() -> None:
    for fmt in ("csv", "xlsx"):
        rows = _many_rows(api_main.PORTAL_DATABASE_DIRECT_EXPORT_ROW_CAP)
        audit_events = []
        captured_calls = []
        patches = _base_export_patches(total=api_main.PORTAL_DATABASE_DIRECT_EXPORT_ROW_CAP, rows=rows, audit_events=audit_events, captured_calls=captured_calls)
        try:
            response = api_main.user_portal_database_dataset_export(DATASET_ID, _FakeRequest(query=f"format={fmt}"))
        finally:
            _restore(patches)
        assert response.status_code == 200, (fmt, response.status_code)
        assert captured_calls[-1]["limit"] == api_main.PORTAL_DATABASE_DIRECT_EXPORT_ROW_CAP, captured_calls
        assert audit_events[-1]["event_type"] == "database_export_success", audit_events
        assert audit_events[-1]["metadata_json"]["row_limit"] == api_main.PORTAL_DATABASE_DIRECT_EXPORT_ROW_CAP, audit_events
        assert audit_events[-1]["metadata_json"]["row_count"] == api_main.PORTAL_DATABASE_DIRECT_EXPORT_ROW_CAP, audit_events
        if fmt == "csv":
            parsed = list(csv.reader(io.StringIO(response.body.decode("utf-8-sig"))))
            assert len(parsed) == api_main.PORTAL_DATABASE_DIRECT_EXPORT_ROW_CAP + 1, len(parsed)
            assert parsed[-1][1] == f"Driver {api_main.PORTAL_DATABASE_DIRECT_EXPORT_ROW_CAP - 1}", parsed[-1]
        else:
            assert response.body.startswith(b"PK"), response.body[:8]
    print("PASS: direct CSV/XLSX exports allow exactly 20,000 rows without silent truncation")


def _test_large_direct_export_requires_explicit_background_action() -> None:
    for fmt in ("csv", "xlsx"):
        audit_events = []
        captured_calls = []
        queued = []
        patches = _base_export_patches(total=api_main.PORTAL_DATABASE_DIRECT_EXPORT_ROW_CAP + 1, rows=[], audit_events=audit_events, captured_calls=captured_calls)
        patches.append(("_enqueue_database_export_job", _patch("_enqueue_database_export_job", lambda *args, **kwargs: queued.append(args) or "unexpected")))
        try:
            response = api_main.user_portal_database_dataset_export(DATASET_ID, _FakeRequest(query=f"format={fmt}"))
        finally:
            _restore(patches)
        html = _html(response)
        assert response.status_code == 400, (fmt, response.status_code, html)
        assert "Direct export is available for up to 20,000 rows" in html, html
        assert "Use Export report from the dataset page" in html, html
        assert "Prepare background export" not in html, html
        assert not captured_calls and not queued, (captured_calls, queued)
        assert audit_events[-1]["event_type"] == "database_export_validation_failed", audit_events
        assert audit_events[-1]["metadata_json"]["reason"] == "direct_export_limit_exceeded", audit_events
        assert audit_events[-1]["metadata_json"]["row_count"] == api_main.PORTAL_DATABASE_DIRECT_EXPORT_ROW_CAP + 1, audit_events
    print("PASS: 20,001-row direct CSV/XLSX download is rejected without direct streaming or implicit job queueing")


def _test_pre043_direct_boundary_and_background_unavailable() -> None:
    audit_events = []
    captured_calls = []
    patches = _base_export_patches(
        total=api_main.PORTAL_DATABASE_DIRECT_EXPORT_ROW_CAP,
        rows=_many_rows(api_main.PORTAL_DATABASE_DIRECT_EXPORT_ROW_CAP),
        audit_events=audit_events,
        captured_calls=captured_calls,
    )
    patches.append(("_database_export_schema_available", _patch("_database_export_schema_available", lambda: False)))
    try:
        direct = api_main.user_portal_database_dataset_export(DATASET_ID, _FakeRequest(query="format=csv"))
    finally:
        _restore(patches)
    assert direct.status_code == 200, direct.status_code
    assert captured_calls[-1]["limit"] == api_main.PORTAL_DATABASE_DIRECT_EXPORT_ROW_CAP, captured_calls
    assert audit_events[-1]["event_type"] == "database_export_success", audit_events

    audit_events = []
    captured_calls = []
    patches = _base_export_patches(total=api_main.PORTAL_DATABASE_DIRECT_EXPORT_ROW_CAP + 1, rows=[], audit_events=audit_events, captured_calls=captured_calls)
    patches.extend([
        ("_database_export_schema_available", _patch("_database_export_schema_available", lambda: False)),
        ("_enqueue_database_export_job", _patch("_enqueue_database_export_job", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("pre-043 must not queue")))),
    ])
    try:
        unavailable = api_main._portal_database_enqueue_export_response(_user(), DATASET_ID, _FakeRequest(query=""), format_name="csv")
    finally:
        _restore(patches)
    html = _html(unavailable)
    assert unavailable.status_code == 200, unavailable.status_code
    assert api_main.ASYNC_DATABASE_EXPORT_SCHEMA_UNAVAILABLE_MESSAGE in html, html
    # `Export report` was the pre-S8 legacy submit label. The approved `DB-009`
    # panel replaced it with a scope/column/format form whose submit label is
    # path-dependent; what this assertion is actually about — the dataset page
    # rendering with its export surface intact — is now `db-export-form`.
    assert "Approved trips" in html and "db-export-form" in html, html
    assert "Background exports unavailable" not in html and "My Background Exports" not in html and "My background exports" not in html, html
    assert not any(event.get("event_type") == "database_export_job_queued" for event in audit_events), audit_events
    assert any(event.get("metadata_json", {}).get("reason") == "async_export_schema_unavailable" for event in audit_events), audit_events
    print("PASS: pre-043 allows 20,000-row direct export and keeps 20,001-row export on dataset page with controlled notice")


def _test_export_global_limit_exceeded_is_clear() -> None:
    audit_events = []
    captured_calls = []
    queued = []
    patches = _base_export_patches(total=api_main.PORTAL_DATABASE_ASYNC_EXPORT_ROW_CAP + 1, rows=_rows(), audit_events=audit_events, captured_calls=captured_calls)
    patches.append(("_enqueue_database_export_job", _patch("_enqueue_database_export_job", lambda *args, **kwargs: queued.append(args) or "unexpected")))
    try:
        response = api_main.user_portal_database_dataset_export(DATASET_ID, _FakeRequest(query="format=csv"))
    finally:
        _restore(patches)
    assert response.status_code == 400, response.status_code
    assert "Export cannot be created" in _html(response), _html(response)
    assert "exceeds the maximum export limit of 1,000,000" in _html(response), _html(response)
    assert not captured_calls and not queued, (captured_calls, queued)
    assert audit_events[-1]["event_type"] == "database_export_validation_failed", audit_events
    assert audit_events[-1]["metadata_json"]["reason"] == "export_global_limit_exceeded", audit_events
    print("PASS: exports above 1,000,000 rows are rejected without queueing or truncation")


def _test_admin_audit_page_is_admin_only_and_lists_events() -> None:
    old_user = _patch("get_current_artifact_user", lambda request: _user(admin=False))
    try:
        denied = api_main.admin_portal_audit(_FakeRequest(path="/admin/audit"))
    finally:
        api_main.get_current_artifact_user = old_user
    assert denied.status_code == 403, denied.status_code

    event = {
        "created_at": "2026-05-28T10:00:00+00:00",
        "event_type": "database_export_success",
        "actor_username": "alice",
        "client_display_name": "Acme Logistics",
        "dataset_name": "Approved trips",
        "metadata_json": {"format": "csv", "row_count": 2, "row_limit": 20000, "filter_keys": ["driver_name"]},
    }
    patches = [
        ("get_current_artifact_user", _patch("get_current_artifact_user", lambda request: _user(admin=True, user_id=ADMIN_ID, username="admin"))),
        ("_search_portal_audit_events", _patch("_search_portal_audit_events", lambda filters=None, page=1, limit=100: ([event], 1))),
    ]
    try:
        page = api_main.admin_portal_audit(_FakeRequest(path="/admin/audit"))
    finally:
        _restore(patches)
    html = _html(page)
    assert page.status_code == 200, page.status_code
    assert "Audit events" in html and "database_export_success" in html, html
    assert "Acme Logistics" in html and "Approved trips" in html, html
    assert "row_count: 2" in html and "filters: driver_name" in html, html
    print("PASS: /admin/audit is admin-only and renders recent audit events")


def _test_existing_token_behavior_still_works() -> None:
    api_main.READ_TOKEN = "read-token"
    api_main.WRITE_TOKEN = "write-token"
    api_main.require_token("Bearer read-token", "read")
    api_main.require_token("Bearer write-token", "write")
    try:
        api_main.require_token("Bearer bad", "read")
    except _HTTPException as exc:
        assert exc.status_code == 403, exc.status_code
    else:
        raise AssertionError("bad read token unexpectedly accepted")
    print("PASS: Artifact Browser bearer token checks remain unchanged")


def main() -> None:
    _test_unauthenticated_export_redirects_and_audits()
    _test_export_access_boundaries_audit_denied()
    _test_authorized_csv_export_visible_columns_only()
    _test_validation_failures_are_clean_and_audited()
    _test_xlsx_export_supported_safely()
    _test_decimal_numeric_exports_and_text_formula_protection()
    _test_xlsx_export_zero_rows_keeps_headers()
    _test_serializer_failure_is_not_audited_as_success()
    _test_csv_and_xlsx_share_filtered_rows_and_export_all_matches()
    _test_direct_export_boundaries_and_no_truncation()
    _test_large_direct_export_requires_explicit_background_action()
    _test_pre043_direct_boundary_and_background_unavailable()
    _test_export_global_limit_exceeded_is_clear()
    _test_admin_audit_page_is_admin_only_and_lists_events()
    _test_existing_token_behavior_still_works()
    print("PASS: Phase 3C database export and audit manual regression checks completed")


if __name__ == "__main__":
    main()
