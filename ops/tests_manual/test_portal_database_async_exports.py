#!/usr/bin/env python3
"""Manual regression tests for async Database Explorer exports.

Run:

    cd /opt/log-platform
    env PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_portal_database_async_exports.py
"""
from __future__ import annotations

import csv
import io
import json
import math
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import types
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs
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
        self.routes = []

    def _route(self, path, methods):
        def decorator(fn):
            self.routes.append(types.SimpleNamespace(path=path, methods=set(methods), endpoint=fn))
            return fn
        return decorator

    def get(self, *args, **kwargs):
        return self._route(args[0], {"GET"})

    def post(self, *args, **kwargs):
        return self._route(args[0], {"POST"})

    def patch(self, *args, **kwargs):
        return self._route(args[0], {"PATCH"})

    def delete(self, *args, **kwargs):
        return self._route(args[0], {"DELETE"})

    def on_event(self, *args, **kwargs):
        return lambda fn: fn


class _HTMLResponse:
    def __init__(self, content, status_code=200, headers=None, media_type=None):
        self.body = str(content).encode("utf-8")
        self.status_code = status_code
        self.headers = headers or {}
        self.media_type = media_type or "text/html"


class _StreamingResponse:
    def __init__(self, body, media_type=None, headers=None):
        if isinstance(body, (bytes, bytearray)):
            self.body = bytes(body)
        else:
            self.body = b"".join(body)
        self.media_type = media_type
        self.headers = headers or {}
        self.status_code = 200


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
import ops.database_export_worker as worker  # noqa: E402

USER_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OTHER_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
DATASET_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"


class _FakeUrl:
    def __init__(self, path: str, query: str = ""):
        self.path = path
        self.query = query


class _FakeRequest:
    def __init__(self, path: str, query: str = "", cookies: dict | None = None):
        self.url = _FakeUrl(path, query)
        self.cookies = cookies or {}


def _dataset(**overrides):
    data = {
        "dataset_id": DATASET_ID,
        "client_code": "ACME_01",
        "dataset_name": "Trips",
        "slug": "trips",
        "schema_name": "public",
        "table_name": "trips",
        "default_date_column": "trip_date",
        "can_filter_rows": True,
        "can_export_rows": True,
    }
    data.update(overrides)
    return data


def _columns():
    return [
        {"column_name": "trip_date", "display_name": "Trip date", "data_type": "date", "is_visible": True, "is_filterable": True, "is_sortable": True, "display_order": 10},
        {"column_name": "driver_name", "display_name": "Driver", "data_type": "text", "is_visible": True, "is_filterable": True, "is_sortable": True, "display_order": 20},
        {"column_name": "amount", "display_name": "Amount", "data_type": "numeric", "is_visible": True, "is_filterable": True, "is_sortable": True, "display_order": 30},
    ]


def _xlsx_cells(path: str) -> dict[str, dict[str, str]]:
    with zipfile.ZipFile(path) as workbook:
        root = ET.fromstring(workbook.read("xl/worksheets/sheet1.xml"))
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    cells = {}
    for cell in root.findall(".//m:c", ns):
        ref = str(cell.get("r") or "")
        inline = cell.find("m:is/m:t", ns)
        raw = cell.find("m:v", ns)
        cells[ref] = {"type": str(cell.get("t") or ""), "value": (inline.text if inline is not None else raw.text if raw is not None else "") or ""}
    return cells


def _test_canonical_snapshot() -> None:
    params = {
        "filter__driver_name": [" Alice "],
        "op__driver_name": ["contains"],
        "dateop__trip_date": ["older"],
        "date__trip_date": ["2026-06-01"],
        "sort": ["trip_date"],
        "direction": ["desc"],
        "search": ["Warszawa"],
        "raw_sql": ["select * from secret"],
    }
    snapshot, state, error = api_main._portal_database_canonical_export_snapshot(_dataset(), _columns(), params, format_name="csv")
    assert not error, error
    assert snapshot["columns"] == ["trip_date", "driver_name", "amount"], snapshot
    assert snapshot["sort"] == "trip_date" and snapshot["direction"] == "desc", snapshot
    as_text = json.dumps(snapshot, ensure_ascii=False).lower()
    assert "sql" not in as_text and "select" not in as_text, snapshot
    replay, replay_error = api_main._portal_database_export_snapshot_params(_dataset(), _columns(), snapshot)
    assert replay_error is None and replay is not None, replay_error
    assert replay["filter__driver_name"] == ["Alice"], replay
    assert replay["op__trip_date"] == ["lt"], replay
    assert replay["search"] == ["Warszawa"], replay
    assert state["active_filters"], state
    print("PASS: queued snapshot stores normalized allowlisted state only")


def _test_report_207_quoted_polish_columns_async_snapshot_and_serializers() -> None:
    dataset = _dataset(
        client_code="BRAVO00016",
        dataset_name="Report 207",
        slug="areport207bravo",
        schema_name="telematics_reports",
        table_name="report_207",
        default_date_column=None,
    )
    columns = [
        {"column_name": "Data i czas", "display_name": "Data i czas", "data_type": "timestamp", "is_visible": True, "is_filterable": True, "is_sortable": True, "display_order": 10},
        {"column_name": "Nr rejestracyjny", "display_name": "Nr rejestracyjny", "data_type": "text", "is_visible": True, "is_filterable": True, "is_sortable": True, "display_order": 20},
        {"column_name": "Prędkość", "display_name": "Prędkość", "data_type": "numeric", "is_visible": True, "is_filterable": True, "is_sortable": True, "display_order": 30},
    ]
    params = {
        "sort": ["Data i czas"],
        "direction": ["asc"],
        "filter__Nr rejestracyjny": ["ABC"],
        "op__Nr rejestracyjny": ["contains"],
    }
    snapshot, state, error = api_main._portal_database_canonical_export_snapshot(dataset, columns, params, format_name="xlsx")
    assert error is None, error
    assert snapshot["columns"] == ["Data i czas", "Nr rejestracyjny", "Prędkość"], snapshot
    assert snapshot["sort"] == "Data i czas", snapshot
    replay, replay_error = api_main._portal_database_export_snapshot_params(dataset, columns, snapshot)
    assert replay_error is None and replay is not None, replay_error
    query, values, qstate, qerror = api_main._build_portal_database_rows_query(dataset, columns, replay, unbounded=True)
    assert qerror is None, qerror
    assert 'FROM "telematics_reports"."report_207"' in query, query
    assert '"Data i czas"' in query and '"Nr rejestracyjny"' in query and '"Prędkość"' in query, query
    assert 'CAST("Nr rejestracyjny" AS TEXT) ILIKE %s' in query, query
    assert values == ["%ABC%"], values
    assert qstate["sort"] == state["sort"] == "Data i czas", (qstate, state)

    rows = [
        {"Data i czas": datetime(2026, 7, 9, 12, 30, tzinfo=timezone.utc), "Nr rejestracyjny": "ABC123", "Prędkość": Decimal("151.5")},
        {"Data i czas": "2026-07-09 12:31:00", "Nr rejestracyjny": "=FORMULA", "Prędkość": None},
    ]
    with tempfile.TemporaryDirectory() as tmpdir:
        csv_path = str(Path(tmpdir) / "report_207.csv")
        xlsx_path = str(Path(tmpdir) / "report_207.xlsx")
        assert api_main._portal_database_write_csv_file(columns, iter(rows), csv_path, max_rows=10) == 2
        with open(csv_path, "r", encoding="utf-8-sig", newline="") as handle:
            parsed = list(csv.reader(handle))
        assert parsed[0] == ["Data i czas", "Nr rejestracyjny", "Prędkość"], parsed
        assert parsed[2][1].startswith("'="), parsed
        assert api_main._portal_database_write_xlsx_file(columns, iter(rows), xlsx_path, max_rows=10) == 2
        cells = _xlsx_cells(xlsx_path)
        assert cells["A1"]["value"] == "Data i czas", cells
        assert cells["B1"]["value"] == "Nr rejestracyjny", cells
        assert cells["C1"]["value"] == "Prędkość", cells
        assert cells["B3"]["value"].startswith("'="), cells["B3"]
    print("PASS: Report 207 quoted Polish columns survive async snapshot, query, CSV, and XLSX export paths")


def _test_streaming_serializers_and_cap() -> None:
    rows = [
        {"trip_date": datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc), "driver_name": "=SUM(A1:A2)", "amount": Decimal("1234567890.12345678901234567890")},
        {"trip_date": "2026-06-02", "driver_name": "Polski tekst 🚗 \ud800 \x01", "amount": Decimal("-42")},
        {"trip_date": None, "driver_name": "-not formula", "amount": Decimal("NaN")},
        {"trip_date": "2026-06-04", "driver_name": "infinite", "amount": float("inf")},
    ]
    with tempfile.TemporaryDirectory() as tmpdir:
        csv_path = str(Path(tmpdir) / "export.csv")
        count = api_main._portal_database_write_csv_file(_columns(), iter(rows), csv_path, max_rows=10)
        assert count == 4, count
        with open(csv_path, "r", encoding="utf-8-sig", newline="") as handle:
            parsed = list(csv.reader(handle))
        assert parsed[0] == ["Trip date", "Driver", "Amount"], parsed
        assert parsed[1][1].startswith("'="), parsed[1]
        assert parsed[3][1].startswith("'-"), parsed[3]
        assert parsed[3][2] == "", parsed[3]
        assert "�" in parsed[2][1], parsed[2]

        xlsx_path = str(Path(tmpdir) / "export.xlsx")
        count = api_main._portal_database_write_xlsx_file(_columns(), iter(rows), xlsx_path, max_rows=10)
        assert count == 4, count
        cells = _xlsx_cells(xlsx_path)
        assert cells["B2"]["value"].startswith("'="), cells["B2"]
        assert cells["C2"]["type"] == "" and cells["C2"]["value"] == "1234567890.12345678901234567890", cells["C2"]
        assert cells["C4"]["value"] == "", cells["C4"]

        empty_path = str(Path(tmpdir) / "empty.csv")
        assert api_main._portal_database_write_csv_file(_columns(), iter([]), empty_path, max_rows=10) == 0
        with open(empty_path, "r", encoding="utf-8-sig", newline="") as handle:
            assert list(csv.reader(handle)) == [["Trip date", "Driver", "Amount"]]

        try:
            api_main._portal_database_write_csv_file(_columns(), iter(rows), str(Path(tmpdir) / "cap.csv"), max_rows=1)
        except ValueError as exc:
            assert str(exc) == "ASYNC_EXPORT_ROW_CAP_EXCEEDED"
        else:
            raise AssertionError("row cap did not fail")
    print("PASS: CSV/XLSX serializers stream rows, preserve safe values, and enforce cap")


def _test_pre043_schema_capability_and_route_guards() -> None:
    old_schema = api_main._database_export_schema_available
    old_enqueue = api_main._enqueue_database_export_job
    old_audit = api_main._portal_audit_event_safe
    old_dataset = api_main._get_portal_database_dataset_for_user
    old_columns = api_main._get_portal_database_visible_columns
    old_count = api_main._count_portal_database_rows
    old_rows = api_main._list_portal_database_rows
    audits = []
    api_main._database_export_schema_available = lambda: False
    api_main._enqueue_database_export_job = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("enqueue should not run pre-043"))
    api_main._portal_audit_event_safe = lambda **kwargs: audits.append(kwargs)
    api_main._get_portal_database_dataset_for_user = lambda dataset_id, user_id: _dataset()
    api_main._get_portal_database_visible_columns = lambda dataset_id: _columns()
    api_main._count_portal_database_rows = lambda dataset, columns, params: (api_main.PORTAL_DATABASE_DIRECT_EXPORT_ROW_CAP + 1, {"sort": "trip_date", "direction": "desc", "active_filters": []}, None)
    api_main._list_portal_database_rows = lambda dataset, columns, params, limit, offset, display_columns=None: ([{"trip_date": "2026-06-01", "driver_name": "Alice", "amount": 1}], {"sort": "trip_date", "direction": "desc", "active_filters": []}, None)
    try:
        select_sql = api_main._artifact_browser_select_sql()
        assert "NULL::UUID AS owner_user_id" in select_sql, select_sql
        assert "artifacts.owner_user_id" not in select_sql, select_sql
        assert "artifacts.expires_at" not in select_sql, select_sql
        assert "artifacts.expired_at" not in select_sql, select_sql

        clause, params = api_main.build_artifact_permission_where_clause({"user_id": OTHER_ID, "is_admin": False}, "view")
        assert "artifact_user_roles" in clause, clause
        assert "owner_user_id" not in clause and "expires_at" not in clause and "expired_at" not in clause, clause
        assert "NOT (artifacts.workflow_name = 'database_explorer'" not in clause, clause
        assert params == [OTHER_ID], params

        exports_response = api_main._user_database_exports_response({"user_id": USER_ID, "username": "alice", "is_admin": False})
        assert exports_response.status_code == 303, exports_response.status_code
        assert exports_response.headers.get("Location") == "/user/database?background_exports_unavailable=1", exports_response.headers

        enqueue_response = api_main._portal_database_enqueue_export_response(
            {"user_id": USER_ID, "username": "alice", "is_admin": False},
            DATASET_ID,
            None,
            format_name="csv",
        )
        enqueue_html = enqueue_response.body.decode("utf-8")
        assert enqueue_response.status_code == 200, enqueue_response.status_code
        assert api_main.ASYNC_DATABASE_EXPORT_SCHEMA_UNAVAILABLE_MESSAGE in enqueue_html, enqueue_html
        assert "Background exports unavailable" not in enqueue_html and "My Background Exports" not in enqueue_html, enqueue_html
        assert any(a.get("metadata_json", {}).get("reason") == "async_export_schema_unavailable" for a in audits), audits
    finally:
        api_main._database_export_schema_available = old_schema
        api_main._enqueue_database_export_job = old_enqueue
        api_main._portal_audit_event_safe = old_audit
        api_main._get_portal_database_dataset_for_user = old_dataset
        api_main._get_portal_database_visible_columns = old_columns
        api_main._count_portal_database_rows = old_count
        api_main._list_portal_database_rows = old_rows
    print("PASS: pre-043 schema guard keeps generic artifacts safe and async export routes controlled")


def _test_schema_capability_metadata_probe() -> None:
    class Cursor:
        def __init__(self, rows):
            self.rows = rows
            self.query = ""
            self.params = None
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc, tb):
            return False
        def execute(self, query, params=()):
            self.query = str(query)
            self.params = params
        def fetchall(self):
            return list(self.rows)
    class Conn:
        def __init__(self, cursor):
            self.cursor_obj = cursor
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc, tb):
            return False
        def cursor(self):
            return self.cursor_obj

    artifact_rows = [{"table_name": "artifacts", "column_name": column} for column in api_main.ASYNC_DATABASE_EXPORT_REQUIRED_ARTIFACT_COLUMNS]
    job_rows = [{"table_name": "database_export_jobs", "column_name": column} for column in api_main.ASYNC_DATABASE_EXPORT_REQUIRED_JOB_COLUMNS]
    attempt_rows = [{"table_name": "database_export_attempt_objects", "column_name": column} for column in api_main.ASYNC_DATABASE_EXPORT_REQUIRED_ATTEMPT_OBJECT_COLUMNS]
    cursor = Cursor(artifact_rows + job_rows + attempt_rows)
    old_db_conn = api_main.db_conn
    api_main.db_conn = lambda: Conn(cursor)
    try:
        assert api_main._database_export_schema_available()
        assert "information_schema.columns" in cursor.query, cursor.query
        assert "artifacts.owner_user_id" not in cursor.query, cursor.query
        assert "database_export_jobs." not in cursor.query, cursor.query
        assert set(cursor.params[0]) == api_main.ASYNC_DATABASE_EXPORT_REQUIRED_ARTIFACT_COLUMNS, cursor.params
        assert set(cursor.params[1]) == api_main.ASYNC_DATABASE_EXPORT_REQUIRED_JOB_COLUMNS, cursor.params
        assert set(cursor.params[2]) == api_main.ASYNC_DATABASE_EXPORT_REQUIRED_ATTEMPT_OBJECT_COLUMNS, cursor.params
    finally:
        api_main.db_conn = old_db_conn

    cursor = Cursor(artifact_rows)
    api_main.db_conn = lambda: Conn(cursor)
    try:
        assert not api_main._database_export_schema_available()
    finally:
        api_main.db_conn = old_db_conn
    print("PASS: schema capability probe uses metadata only and requires the full 043 schema")


def _test_post043_enqueue_and_exports_routes() -> None:
    old_schema = api_main._database_export_schema_available
    old_dataset = api_main._get_portal_database_dataset_for_user
    old_columns = api_main._get_portal_database_visible_columns
    old_enqueue = api_main._enqueue_database_export_job
    old_audit = api_main._portal_audit_event_safe
    old_count = api_main._count_portal_database_rows
    old_list = api_main._list_database_export_jobs_for_user
    queued = []
    job_id = "dddddddd-dddd-dddd-dddd-dddddddddddd"
    api_main._database_export_schema_available = lambda: True
    api_main._get_portal_database_dataset_for_user = lambda dataset_id, user_id: _dataset()
    api_main._get_portal_database_visible_columns = lambda dataset_id: _columns()
    api_main._count_portal_database_rows = lambda dataset, columns, params: (api_main.PORTAL_DATABASE_DIRECT_EXPORT_ROW_CAP + 1, {"sort": "trip_date", "direction": "desc", "active_filters": []}, None)
    api_main._enqueue_database_export_job = lambda user, dataset, snapshot, format_name: queued.append((snapshot, format_name)) or job_id
    api_main._portal_audit_event_safe = lambda **kwargs: None
    # Expiry relative to now: a fixed date silently reclassifies this ready job
    # as expired once wall-clock time passes it.
    _ready_expiry = (api_main.utcnow() + timedelta(days=2)).isoformat()
    api_main._list_database_export_jobs_for_user = lambda user_id: [{
        "job_id": job_id,
        "dataset_name": "Trips",
        "client_code": "ACME_01",
        "requested_format": "csv",
        "status": "completed",
        "row_count": 10,
        "completed_at": api_main.utcnow().isoformat(),
        "expires_at": _ready_expiry,
        "artifact_expires_at": _ready_expiry,
        "artifact_id": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
    }]
    try:
        response = api_main._portal_database_enqueue_export_response(
            {"user_id": USER_ID, "username": "alice", "is_admin": False},
            DATASET_ID,
            None,
            format_name="csv",
        )
        assert response.status_code == 303, response.status_code
        assert "export_job_queued=" + job_id in response.headers.get("Location", ""), response.headers
        assert queued and queued[0][1] == "csv", queued
        assert queued[0][0]["format"] == "csv", queued
        response = api_main._user_database_exports_response({"user_id": USER_ID, "username": "alice", "is_admin": False})
        html = response.body.decode("utf-8")
        assert response.status_code == 200, response.status_code
        # Approved `DB-007` vocabulary replaced the English `Ready`/`Download`.
        assert "Gotowy" in html and "Pobierz" in html and "Trips" in html, html
    finally:
        api_main._database_export_schema_available = old_schema
        api_main._get_portal_database_dataset_for_user = old_dataset
        api_main._get_portal_database_visible_columns = old_columns
        api_main._enqueue_database_export_job = old_enqueue
        api_main._portal_audit_event_safe = old_audit
        api_main._count_portal_database_rows = old_count
        api_main._list_database_export_jobs_for_user = old_list
    print("PASS: post-043 async export queue and My exports routes remain active")



def _test_route_method_contract_and_prg() -> None:
    routes_by_path = {}
    for route in getattr(api_main.app, "routes", []):
        routes_by_path.setdefault(route.path, set()).update(route.methods)
    assert routes_by_path.get("/user/database/exports") == {"GET"}, routes_by_path.get("/user/database/exports")
    assert routes_by_path.get("/user/database/datasets/{dataset_id}/exports") == {"POST"}, routes_by_path.get("/user/database/datasets/{dataset_id}/exports")
    assert "/user/database/exports/{job_id}" not in routes_by_path, routes_by_path
    assert "POST" not in routes_by_path.get("/user/database/exports", set()), routes_by_path.get("/user/database/exports")

    get_endpoint = next(route.endpoint for route in api_main.app.routes if route.path == "/user/database/exports" and "GET" in route.methods)
    post_endpoint = next(route.endpoint for route in api_main.app.routes if route.path == "/user/database/datasets/{dataset_id}/exports" and "POST" in route.methods)

    old_schema = api_main._database_export_schema_available
    old_require = api_main._require_portal_user
    old_list = api_main._list_database_export_jobs_for_user
    old_get_job = api_main._get_database_export_job_for_user
    old_dataset = api_main._get_portal_database_dataset_for_user
    old_columns = api_main._get_portal_database_visible_columns
    old_count = api_main._count_portal_database_rows
    old_enqueue = api_main._enqueue_database_export_job
    old_audit = api_main._portal_audit_event_safe
    seen_user_ids = []
    job_id = "dddddddd-dddd-dddd-dddd-dddddddddddd"

    def list_jobs(user_id):
        seen_user_ids.append(user_id)
        assert user_id == USER_ID, user_id
        # Expiry relative to now, not a fixed date: a hard-coded one silently
        # turns this into an *expired* job once wall-clock time passes it, and
        # the assertion below is about the ready state.
        ready_expiry = (api_main.utcnow() + timedelta(days=2)).isoformat()
        return [{
            "job_id": job_id,
            "dataset_name": "Trips",
            "client_code": "ACME_01",
            "requested_format": "csv",
            "status": "completed",
            "row_count": 10,
            "completed_at": api_main.utcnow().isoformat(),
            "expires_at": ready_expiry,
            "artifact_expires_at": ready_expiry,
            "artifact_id": "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
        }]

    try:
        user = {"user_id": USER_ID, "username": "alice", "is_admin": False}
        api_main._database_export_schema_available = lambda: True
        api_main._require_portal_user = lambda request: user
        api_main._list_database_export_jobs_for_user = list_jobs
        api_main._get_database_export_job_for_user = lambda requested_job_id, requested_user_id: {"job_id": requested_job_id} if requested_job_id == job_id and requested_user_id == USER_ID else None

        response = get_endpoint(_FakeRequest("/user/database/exports"))
        html = response.body.decode("utf-8")
        assert response.status_code == 200, response.status_code
        # Approved S8 vocabulary: the page is `Eksporty danych` and a ready
        # export offers `Pobierz`. `My Background Exports` and `Download` were
        # the pre-S8 English labels the approved `DB-007` list replaced.
        assert "Eksporty danych" in html and "Trips" in html, html
        assert "Gotowy" in html and "Pobierz" in html, html
        assert "other-user" not in html, html
        assert seen_user_ids == [USER_ID], seen_user_ids

        unauth = _HTMLResponse("", status_code=303, headers={"Location": "/artifact-explorer/login?next=/user/database/exports"})
        api_main._require_portal_user = lambda request: unauth
        response = get_endpoint(_FakeRequest("/user/database/exports"))
        assert response.status_code == 303, response.status_code
        assert response.headers.get("Location", "").startswith("/artifact-explorer/login"), response.headers
        response = post_endpoint(DATASET_ID, _FakeRequest(f"/user/database/datasets/{DATASET_ID}/exports", "sort=trip_date"), format_name="csv")
        assert response.status_code == 303, response.status_code
        unauth_location = response.headers.get("Location", "")
        assert unauth_location.startswith("/artifact-explorer/login"), unauth_location
        assert f"%2Fuser%2Fdatabase%2Fdatasets%2F{DATASET_ID}" in unauth_location, unauth_location
        assert f"%2Fuser%2Fdatabase%2Fdatasets%2F{DATASET_ID}%2Fexports" not in unauth_location, unauth_location

        api_main._require_portal_user = lambda request: user
        api_main._get_portal_database_dataset_for_user = lambda dataset_id, user_id: _dataset()
        api_main._get_portal_database_visible_columns = lambda dataset_id: _columns()
        api_main._count_portal_database_rows = lambda dataset, columns, params: (api_main.PORTAL_DATABASE_DIRECT_EXPORT_ROW_CAP + 1, {"sort": "trip_date", "direction": "desc", "active_filters": []}, None)
        api_main._enqueue_database_export_job = lambda user, dataset, snapshot, format_name: job_id
        api_main._portal_audit_event_safe = lambda **kwargs: None
        response = post_endpoint(DATASET_ID, _FakeRequest(f"/user/database/datasets/{DATASET_ID}/exports", "sort=trip_date"), format_name="csv")
        assert response.status_code == 303, response.status_code
        assert response.headers.get("Location") == f"/user/database/exports?export_job_queued={job_id}", response.headers

        api_main._database_export_schema_available = lambda: False
        response = get_endpoint(_FakeRequest("/user/database/exports"))
        assert response.status_code == 303, response.status_code
        assert response.headers.get("Location") == "/user/database?background_exports_unavailable=1", response.headers
    finally:
        api_main._database_export_schema_available = old_schema
        api_main._require_portal_user = old_require
        api_main._list_database_export_jobs_for_user = old_list
        api_main._get_database_export_job_for_user = old_get_job
        api_main._get_portal_database_dataset_for_user = old_dataset
        api_main._get_portal_database_visible_columns = old_columns
        api_main._count_portal_database_rows = old_count
        api_main._enqueue_database_export_job = old_enqueue
        api_main._portal_audit_event_safe = old_audit
    print("PASS: async export route table exposes GET list, POST queue, PRG redirect, owner-scoped listing, and pre-043 fallback")

def _test_background_export_row_count_thresholds() -> None:
    old_schema = api_main._database_export_schema_available
    old_dataset = api_main._get_portal_database_dataset_for_user
    old_columns = api_main._get_portal_database_visible_columns
    old_count = api_main._count_portal_database_rows
    old_rows = api_main._list_portal_database_rows
    old_enqueue = api_main._enqueue_database_export_job
    old_audit = api_main._portal_audit_event_safe
    audits = []
    queued = []
    api_main._database_export_schema_available = lambda: True
    api_main._get_portal_database_dataset_for_user = lambda dataset_id, user_id: _dataset()
    api_main._get_portal_database_visible_columns = lambda dataset_id: _columns()
    api_main._portal_audit_event_safe = lambda **kwargs: audits.append(kwargs)
    api_main._enqueue_database_export_job = lambda user, dataset, snapshot, format_name: queued.append((snapshot, format_name)) or f"job-{len(queued)}"
    api_main._list_portal_database_rows = lambda dataset, columns, params, limit, offset, display_columns=None: ([{"trip_date": "2026-06-01", "driver_name": "Alice", "amount": 1}], {"sort": "trip_date", "direction": "desc", "active_filters": []}, None)
    try:
        for fmt, media_type in (("csv", "text/csv; charset=utf-8"), ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")):
            api_main._count_portal_database_rows = lambda dataset, columns, params: (api_main.PORTAL_DATABASE_DIRECT_EXPORT_ROW_CAP, {"sort": "trip_date", "direction": "desc", "active_filters": []}, None)
            before = len(queued)
            response = api_main._portal_database_enqueue_export_response(
                {"user_id": USER_ID, "username": "alice", "is_admin": False},
                DATASET_ID,
                None,
                format_name=fmt,
            )
            assert response.status_code == 200, (fmt, response.status_code)
            assert response.media_type == media_type, (fmt, response.media_type)
            assert len(queued) == before, queued
        for total in (api_main.PORTAL_DATABASE_DIRECT_EXPORT_ROW_CAP + 1, api_main.PORTAL_DATABASE_ASYNC_EXPORT_ROW_CAP):
            api_main._count_portal_database_rows = lambda dataset, columns, params, total=total: (total, {"sort": "trip_date", "direction": "desc", "active_filters": []}, None)
            response = api_main._portal_database_enqueue_export_response(
                {"user_id": USER_ID, "username": "alice", "is_admin": False},
                DATASET_ID,
                None,
                format_name="csv",
            )
            assert response.status_code == 303, (total, response.status_code, response.headers)
            assert audits[-1]["event_type"] == "database_export_job_queued", audits[-1]
            assert audits[-1]["metadata_json"]["row_count"] == total, audits[-1]
        api_main._count_portal_database_rows = lambda dataset, columns, params: (api_main.PORTAL_DATABASE_ASYNC_EXPORT_ROW_CAP + 1, {"sort": "trip_date", "direction": "desc", "active_filters": []}, None)
        before = len(queued)
        response = api_main._portal_database_enqueue_export_response(
            {"user_id": USER_ID, "username": "alice", "is_admin": False},
            DATASET_ID,
            None,
            format_name="csv",
        )
        html = response.body.decode("utf-8")
        assert response.status_code == 400, response.status_code
        assert "exceeds the maximum export limit of 1,000,000" in html, html
        assert len(queued) == before, queued
        assert audits[-1]["metadata_json"]["reason"] == "export_global_limit_exceeded", audits[-1]
    finally:
        api_main._database_export_schema_available = old_schema
        api_main._get_portal_database_dataset_for_user = old_dataset
        api_main._get_portal_database_visible_columns = old_columns
        api_main._count_portal_database_rows = old_count
        api_main._list_portal_database_rows = old_rows
        api_main._enqueue_database_export_job = old_enqueue
        api_main._portal_audit_event_safe = old_audit
    print("PASS: unified export route downloads 20,000 rows, queues 20,001..1,000,000 rows, and rejects above cap")


def _artifact_route_row(owner_id=USER_ID, *, expires_at=None, artifact_id="eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"):
    return {
        "artifact_id": artifact_id,
        "run_id": None,
        "created_at": api_main.utcnow(),
        "kind": "REPORT",
        "filename": "database-export.csv",
        "content_type": "text/csv; charset=utf-8",
        "size_bytes": 12,
        "sha256": "abc",
        "storage_backend": "minio",
        "storage_key": "database_explorer/exports/private.csv",
        "raw_file_id": None,
        "workflow_name": "database_explorer",
        "stage_name": "async_export",
        "artifact_role": "database_export",
        "report_type": "database_export",
        "client_code": "ACME_01",
        "display_filename": "database-export.csv",
        "original_filename": "database-export.csv",
        "file_ext": "csv",
        "layout_version": api_main.LAYOUT_VERSION,
        "metadata_json": {"export_job_id": "job-1"},
        "owner_user_id": owner_id,
        "expires_at": expires_at or (api_main.utcnow() + timedelta(days=1)),
        "expired_at": None,
        "description": None,
        "manual_metadata_json": {},
        "tags": [],
    }


def _test_direct_guessed_async_artifact_routes_denied() -> None:
    row = _artifact_route_row()
    owner = {"user_id": USER_ID, "username": "alice", "is_admin": False, "is_active": True}
    other = {"user_id": OTHER_ID, "username": "bob", "is_admin": False, "is_active": True}
    old_row = api_main._get_artifact_browser_row
    old_run = api_main._get_run_summary
    old_raw = api_main._get_raw_file_summary
    old_folders = api_main._get_artifact_virtual_folders
    old_preview = api_main._artifact_explorer_preview_for_row
    old_stream = api_main._stream_artifact_download
    old_route_user = api_main._artifact_explorer_user_for_route
    api_main._get_artifact_browser_row = lambda artifact_id: dict(row)
    api_main._get_run_summary = lambda run_id: None
    api_main._get_raw_file_summary = lambda raw_file_id: None
    api_main._get_artifact_virtual_folders = lambda artifact_id: []
    api_main._artifact_explorer_preview_for_row = lambda source_row, artifact: None
    api_main._stream_artifact_download = lambda source_row, disposition="attachment": _StreamingResponse([b"ok"], media_type="text/csv")
    try:
        assert api_main._artifact_explorer_detail_response(row["artifact_id"], user=owner).status_code == 200
        api_main._artifact_explorer_user_for_route = lambda request: owner
        assert api_main.artifact_explorer_download_artifact(row["artifact_id"]).media_type == "text/csv"
        try:
            api_main._artifact_explorer_detail_response(row["artifact_id"], user=other)
        except _HTTPException as exc:
            assert exc.status_code == 403, exc.status_code
        else:
            raise AssertionError("non-owner detail access was not denied")
        api_main._artifact_explorer_user_for_route = lambda request: other
        try:
            api_main.artifact_explorer_download_artifact(row["artifact_id"])
        except _HTTPException as exc:
            assert exc.status_code == 403, exc.status_code
        else:
            raise AssertionError("non-owner download access was not denied")
    finally:
        api_main._get_artifact_browser_row = old_row
        api_main._get_run_summary = old_run
        api_main._get_raw_file_summary = old_raw
        api_main._get_artifact_virtual_folders = old_folders
        api_main._artifact_explorer_preview_for_row = old_preview
        api_main._stream_artifact_download = old_stream
        api_main._artifact_explorer_user_for_route = old_route_user
    print("PASS: direct guessed async artifact detail/download routes deny non-owners")


def _test_owner_and_expiry_access() -> None:
    future = (api_main.utcnow() + timedelta(days=1)).isoformat()
    past = (api_main.utcnow() - timedelta(seconds=1)).isoformat()
    artifact = {
        "workflow_name": "database_explorer",
        "stage_name": "async_export",
        "artifact_role": "database_export",
        "report_type": "database_export",
        "client_code": "ACME_01",
        "file_ext": "csv",
        "layout_version": api_main.LAYOUT_VERSION,
        "owner_user_id": USER_ID,
        "expires_at": future,
        "expired_at": None,
        "tags": [],
    }
    owner = {"user_id": USER_ID, "is_active": True, "is_admin": False}
    other = {"user_id": OTHER_ID, "is_active": True, "is_admin": False}
    admin = {"user_id": OTHER_ID, "is_active": True, "is_admin": True}
    original_permissions = api_main.list_user_permissions
    original_schema = api_main._database_export_schema_available
    api_main._database_export_schema_available = lambda: True
    api_main.list_user_permissions = lambda user: [{
        "can_view": True,
        "can_preview": True,
        "can_download": True,
        "workflow_name": "database_explorer",
        "stage_name": "async_export",
        "artifact_role": "database_export",
        "report_type": None,
        "client_code": "ACME_01",
        "file_ext": None,
        "layout_version": None,
        "tag": None,
    }]
    try:
        assert api_main.user_can_access_artifact(owner, artifact, "view")
        assert api_main.user_can_access_artifact(owner, artifact, "download")
        assert not api_main.user_can_access_artifact(other, artifact, "view")
        assert not api_main.user_can_access_artifact(other, artifact, "download")
        assert api_main.user_can_access_artifact(admin, artifact, "view")
        assert api_main.user_can_access_artifact(admin, artifact, "download")
    finally:
        api_main.list_user_permissions = original_permissions
        api_main._database_export_schema_available = original_schema

    original_schema = api_main._database_export_schema_available
    api_main._database_export_schema_available = lambda: True
    clause, params = api_main.build_artifact_permission_where_clause(other, "view")
    assert "NOT (artifacts.workflow_name = 'database_explorer'" in clause, clause
    assert "artifacts.owner_user_id = %s" in clause, clause
    assert params == [OTHER_ID, OTHER_ID], params
    api_main._database_export_schema_available = original_schema

    artifact["expires_at"] = past
    assert not api_main.user_can_access_artifact(owner, artifact, "download")
    assert not api_main.user_can_access_artifact(admin, artifact, "download")

    source = Path("api/main.py").read_text()
    assert "WHERE dej.requested_by_user_id = %s" in source
    print("PASS: async export artifacts are owner-only for ordinary users and expiry blocks download")


def _test_worker_claim_and_source_invariants() -> None:
    source = Path("ops/database_export_worker.py").read_text()
    assert "FOR UPDATE SKIP LOCKED" in source
    assert "pg_try_advisory_xact_lock" in source
    assert "claim_token" in source
    assert "AND claim_token = %s" in source
    assert ".fetchall(" not in source
    assert worker.MAX_ATTEMPTS == 3
    assert api_main.PORTAL_DATABASE_ASYNC_EXPORT_ROW_CAP == 1_000_000
    assert api_main.PORTAL_DATABASE_ASYNC_EXPORT_RETENTION_DAYS == 3
    assert "AND last_cleanup_success_at IS NULL" in source
    print("PASS: worker source uses atomic claim guard, claim fencing, no fetchall, and fixed global caps")


def _test_schema_bootstrap_and_migration_contract() -> None:
    bootstrap = api_main.SCHEMA_SQL
    migration = Path("db/migrations/043_database_explorer_async_exports.sql").read_text()
    folder_migration = Path("db/migrations/044_database_export_system_folders.sql").read_text()
    assert "CREATE INDEX IF NOT EXISTS idx_artifacts_owner_created" not in bootstrap
    assert "CREATE INDEX IF NOT EXISTS idx_artifacts_expires_at" not in bootstrap
    assert "owner_user_id UUID" not in bootstrap
    assert "expires_at TIMESTAMPTZ" not in bootstrap
    assert "expired_at TIMESTAMPTZ" not in bootstrap
    assert "database_export_jobs" not in bootstrap
    assert "database_export_system_folders" not in bootstrap
    assert "ADD COLUMN IF NOT EXISTS owner_user_id UUID REFERENCES artifact_users" in migration
    assert "ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ" in migration
    assert "claim_token UUID" in migration
    assert "attempt_object_key TEXT" in migration
    assert "CREATE TABLE IF NOT EXISTS database_export_attempt_objects" in migration
    assert "state TEXT NOT NULL" in migration
    assert "CHECK (state IN ('active', 'published', 'cleanup_pending'))" in migration
    assert "UNIQUE (job_id, claim_token)" in migration
    assert "UNIQUE (object_key)" in migration
    assert "idx_database_export_jobs_running_claim" in migration
    assert "idx_artifacts_owner_created" in migration
    assert "idx_database_export_jobs_attempt_object" in migration
    assert "idx_database_export_attempt_objects_cleanup" in migration
    assert "ADD CONSTRAINT artifacts_owner_user_id_fkey" in migration
    assert "CREATE TABLE IF NOT EXISTS database_export_system_folders" in folder_migration
    assert "CHECK (system_key = 'database_exports')" in folder_migration
    assert "UNIQUE (owner_user_id, system_key)" in folder_migration
    assert "UNIQUE (folder_id, owner_user_id)" in folder_migration
    assert "ADD COLUMN IF NOT EXISTS system_folder_id" in folder_migration
    assert "database_export_jobs_system_folder_id_fkey" in folder_migration
    normalized_folder_migration = " ".join(folder_migration.split())
    assert "FOREIGN KEY (system_folder_id, requested_by_user_id) REFERENCES database_export_system_folders(folder_id, owner_user_id) ON DELETE RESTRICT" in normalized_folder_migration
    assert "FOREIGN KEY (system_folder_id) REFERENCES database_export_system_folders(folder_id)" not in normalized_folder_migration
    assert "idx_database_export_jobs_system_folder" in folder_migration
    print("PASS: bootstrap avoids async-export schema; migrations own owner/expiry/job/attempt ledger and system-folder schema")


def _attempt_row(job: dict, state: str = "active", *, key: str | None = None, token: str | None = None) -> dict:
    return {
        "attempt_object_id": str(uuid.uuid4()),
        "job_id": str(job.get("job_id")),
        "claim_token": str(token or job.get("claim_token")),
        "object_key": str(key or job.get("attempt_object_key")),
        "state": state,
        "cleanup_requested_at": api_main.utcnow() if state == "cleanup_pending" else None,
        "last_cleanup_attempt_at": None,
        "last_cleanup_success_at": None,
        "created_at": api_main.utcnow(),
        "updated_at": api_main.utcnow(),
    }


def _state_attempts(state: dict) -> list[dict]:
    return state.setdefault("attempts", [])


def _find_attempt(state: dict, *, job_id, token) -> dict | None:
    for attempt in _state_attempts(state):
        if str(attempt.get("job_id")) == str(job_id) and str(attempt.get("claim_token")) == str(token):
            return attempt
    return None



class _FakeCursor:
    def __init__(self, state):
        self.state = state
        self.rowcount = 0
        self._fetchone = None
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def __iter__(self):
        return iter(self._rows)

    def fetchone(self):
        return self._fetchone

    def execute(self, query, params=()):
        normalized = " ".join(str(query).split())
        self.rowcount = 0
        self._fetchone = None
        self._rows = []
        job = self.state["job"]
        attempts = _state_attempts(self.state)
        if normalized.startswith("SELECT job_id, requested_by_user_id") and "WHERE status = 'running'" in normalized:
            if job.get("status") == "running" and not job.get("lease_valid", True) and job.get("artifact_id") is None:
                self._rows = [dict(job)]
        elif normalized.startswith("SELECT job_id FROM database_export_jobs"):
            job_id, token, key = map(str, params[:3])
            if (
                str(job.get("job_id")) == job_id
                and job.get("status") == "running"
                and str(job.get("claim_token")) == token
                and job.get("lease_valid", True)
                and str(job.get("attempt_object_key")) == key
            ):
                self._fetchone = {"job_id": job_id}
        elif normalized.startswith("INSERT INTO database_export_attempt_objects"):
            job_id, token, key, ledger_state, _state_again = params
            attempt = _find_attempt(self.state, job_id=job_id, token=token)
            if attempt is None:
                attempt = _attempt_row({"job_id": job_id, "claim_token": token, "attempt_object_key": key}, str(ledger_state))
                attempts.append(attempt)
            else:
                attempt.update({"object_key": str(key), "state": str(ledger_state), "updated_at": api_main.utcnow()})
                if ledger_state == "cleanup_pending" and attempt.get("cleanup_requested_at") is None:
                    attempt["cleanup_requested_at"] = api_main.utcnow()
            self._fetchone = dict(attempt)
            self.rowcount = 1
        elif normalized.startswith("UPDATE database_export_attempt_objects SET state = 'published'"):
            job_id, token, key = params
            attempt = _find_attempt(self.state, job_id=job_id, token=token)
            if attempt and str(attempt.get("object_key")) == str(key) and attempt.get("state") in {"active", "cleanup_pending"}:
                attempt["state"] = "published"
                attempt["updated_at"] = api_main.utcnow()
                self.rowcount = 1
        elif normalized.startswith("SELECT attempt_object_id, job_id, claim_token, object_key"):
            self._rows = [
                dict(a)
                for a in attempts
                if a.get("state") == "cleanup_pending" and a.get("last_cleanup_success_at") is None
            ]
        elif normalized.startswith("UPDATE database_export_attempt_objects SET last_cleanup_attempt_at"):
            # A delete that removed a real object finalizes immediately; a delete
            # that found nothing finalizes only once the settle window measured
            # from `cleanup_requested_at` has closed, so an in-flight upload can
            # still be discovered on a later sweep.
            removed, absent, settle_seconds, attempt_object_id = params
            for attempt in attempts:
                if str(attempt.get("attempt_object_id")) == str(attempt_object_id) and attempt.get("state") == "cleanup_pending":
                    attempt["last_cleanup_attempt_at"] = api_main.utcnow()
                    if removed:
                        attempt["last_cleanup_success_at"] = api_main.utcnow()
                    elif absent:
                        anchor = attempt.get("cleanup_requested_at") or attempt.get("created_at")
                        if anchor and anchor + timedelta(seconds=int(settle_seconds)) <= api_main.utcnow():
                            attempt["last_cleanup_success_at"] = api_main.utcnow()
                    attempt["updated_at"] = api_main.utcnow()
                    self.rowcount = 1
                    self._fetchone = {"finalized": attempt.get("last_cleanup_success_at") is not None}
                    break
        elif normalized.startswith("INSERT INTO artifacts"):
            if self.state.get("insert_raises"):
                raise RuntimeError(self.state.get("insert_error") or "insert failed")
            artifact = {
                "artifact_id": str(params[0]),
                "created_at": params[1],
                "kind": params[2],
                "filename": params[3],
                "content_type": params[4],
                "size_bytes": params[5],
                "sha256": params[6],
                "storage_key": params[7],
                "client_code": params[8],
                "display_filename": params[9],
                "original_filename": params[10],
                "file_ext": params[11],
                "layout_version": params[12],
                "metadata_json": params[13],
                "owner_user_id": params[14],
                "expires_at": params[15],
                "workflow_name": "database_explorer",
                "stage_name": "async_export",
                "artifact_role": "database_export",
                "expired_at": None,
                "tags": [],
            }
            self.state.setdefault("pending_artifacts", []).append(artifact)
            self.rowcount = 1
        elif normalized.startswith("UPDATE database_export_jobs SET status = 'completed'"):
            completed_at, expires_at, row_count, artifact_id, object_key, job_id, token, attempt_key = params
            if (
                not self.state.get("complete_update_fails")
                and str(job.get("job_id")) == str(job_id)
                and job.get("status") == "running"
                and str(job.get("claim_token")) == str(token)
                and job.get("lease_valid", True)
                and str(job.get("attempt_object_key")) == str(attempt_key)
            ):
                job.update({
                    "status": "completed",
                    "completed_at": completed_at,
                    "expires_at": expires_at,
                    "claim_token": None,
                    "row_count": row_count,
                    "artifact_id": artifact_id,
                    "object_key": object_key,
                    "attempt_object_key": None,
                })
                self.rowcount = 1
        elif normalized.startswith("UPDATE database_export_jobs SET lease_expires_at"):
            token = str(params[-1])
            job_id = str(params[-2])
            if str(job.get("job_id")) == job_id and job.get("status") == "running" and str(job.get("claim_token")) == token:
                self.rowcount = 1
                if "row_count = %s" in normalized:
                    job["row_count"] = params[1]
        elif normalized.startswith("UPDATE database_export_jobs SET status = 'failed', completed_at"):
            clear_attempt_key, code, message, job_id, token = params
            if str(job.get("job_id")) == str(job_id) and job.get("status") == "running" and str(job.get("claim_token")) == str(token):
                job.update({"status": "failed", "claim_token": None, "safe_error_code": code, "safe_error_message": message})
                if clear_attempt_key:
                    job["db_attempt_object_key_cleared"] = True
                self.rowcount = 1
        elif normalized.startswith("UPDATE database_export_jobs SET status = 'queued'"):
            job_id, token = params
            if str(job.get("job_id")) == str(job_id) and job.get("status") == "running" and str(job.get("claim_token")) == str(token) and not job.get("lease_valid", True):
                job.update({"status": "queued", "claim_token": None, "attempt_object_key": None, "lease_valid": True})
                self.rowcount = 1
        elif normalized.startswith("UPDATE database_export_jobs SET status = 'failed', lease_expires_at"):
            job_id, token = params
            if str(job.get("job_id")) == str(job_id) and job.get("status") == "running" and str(job.get("claim_token")) == str(token) and not job.get("lease_valid", True):
                job.update({"status": "failed", "claim_token": None, "attempt_object_key": None, "safe_error_code": "WORKER_INTERRUPTED"})
                self.rowcount = 1


class _FakeConn:
    def __init__(self, state):
        self.state = state

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return _FakeCursor(self.state)

    def commit(self):
        self.state.setdefault("artifacts", []).extend(self.state.pop("pending_artifacts", []))
        self.state["commits"] = self.state.get("commits", 0) + 1

    def rollback(self):
        self.state.pop("pending_artifacts", None)
        self.state["rollbacks"] = self.state.get("rollbacks", 0) + 1


class _NotFound(Exception):
    """The object-store 404 shape the delete path distinguishes from a failure."""

    response = {"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}}


class _FakeS3:
    def __init__(self):
        self.objects = set()
        self.deleted = []
        self.upload_error = None
        self.delete_error = None
        self.head_error = None
        self.upload_hook = None

    def upload_file(self, filename, bucket, key):
        if self.upload_error:
            raise self.upload_error
        self.objects.add(key)
        if self.upload_hook:
            self.upload_hook()

    def head_object(self, Bucket, Key):
        if self.head_error:
            raise self.head_error
        if Key not in self.objects:
            raise _NotFound()
        return {"ContentLength": 1}

    def delete_object(self, Bucket, Key):
        self.deleted.append(Key)
        if self.delete_error:
            raise self.delete_error
        self.objects.discard(Key)


def _artifact_payload(job, key, *, artifact_id="11111111-1111-1111-1111-111111111111"):
    completed_at = api_main.utcnow()
    expires_at = completed_at + timedelta(days=3)
    return {
        "artifact_id": artifact_id,
        "created_at": completed_at,
        "kind": "REPORT",
        "filename": "database-export.csv",
        "content_type": "text/csv; charset=utf-8",
        "size_bytes": 12,
        "sha256": "abc",
        "storage_key": key,
        "client_code": "ACME_01",
        "display_filename": "database-export.csv",
        "original_filename": "database-export.csv",
        "file_ext": "csv",
        "layout_version": api_main.LAYOUT_VERSION,
        "metadata_json": {"export_job_id": str(job["job_id"]), "claim_token": str(job["claim_token"])},
        "owner_user_id": USER_ID,
        "expires_at": expires_at,
    }, completed_at, expires_at


def _with_fake_worker_state(state, fn):
    original_db_conn = worker.api_main.db_conn
    original_s3 = worker.api_main.s3
    original_audit = worker.api_main._portal_audit_event_safe
    fake_s3 = _FakeS3()
    audits = []
    worker.api_main.db_conn = lambda: _FakeConn(state)
    worker.api_main.s3 = fake_s3
    worker.api_main._portal_audit_event_safe = lambda **kwargs: audits.append(kwargs)
    try:
        return fn(fake_s3, audits)
    finally:
        worker.api_main.db_conn = original_db_conn
        worker.api_main.s3 = original_s3
        worker.api_main._portal_audit_event_safe = original_audit


def _capture_worker_logs(fn):
    stream = io.StringIO()
    handler = worker.logging.StreamHandler(stream)
    handler.setFormatter(worker.logging.Formatter("%(message)s"))
    old_handlers = list(worker.LOGGER.handlers)
    old_level = worker.LOGGER.level
    worker.LOGGER.handlers = [handler]
    worker.LOGGER.setLevel(worker.logging.INFO)
    try:
        result = fn()
        handler.flush()
        return stream.getvalue(), result
    finally:
        worker.LOGGER.handlers = old_handlers
        worker.LOGGER.setLevel(old_level)


def _with_worker_process_helpers(fn):
    saved = {
        "dataset": api_main._get_portal_database_dataset_for_user,
        "columns": api_main._get_portal_database_visible_columns,
        "from_snapshot": api_main._portal_database_columns_from_snapshot,
        "canonical": api_main._portal_database_canonical_export_snapshot,
        "snapshot_params": api_main._portal_database_export_snapshot_params,
        "iter_rows": api_main._portal_database_iter_export_rows,
        "write_xlsx": api_main._portal_database_write_xlsx_file,
        "write_csv": api_main._portal_database_write_csv_file,
        "artifact": api_main._database_export_artifact_record,
    }

    def write_file(columns, rows, path, *, max_rows, heartbeat=None):
        count = 0
        for _row in rows:
            count += 1
        Path(path).write_bytes(b"export bytes")
        if heartbeat:
            assert heartbeat(count)
        return count

    def artifact_record(**kwargs):
        artifact, _completed_at, _expires_at = _artifact_payload(kwargs["job"], kwargs["storage_key"])
        artifact.update({
            "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "file_ext": str(kwargs.get("format_name") or "xlsx"),
            "filename": "database-export.xlsx",
            "display_filename": "database-export.xlsx",
            "original_filename": "database-export.xlsx",
        })
        return artifact

    api_main._get_portal_database_dataset_for_user = lambda dataset_id, user_id: _dataset()
    api_main._get_portal_database_visible_columns = lambda dataset_id: _columns()
    api_main._portal_database_columns_from_snapshot = lambda columns, snapshot: (columns, None)
    api_main._portal_database_canonical_export_snapshot = lambda dataset, columns, params, format_name: ({}, {}, None)
    api_main._portal_database_export_snapshot_params = lambda dataset, columns, snapshot: ({}, None)
    api_main._portal_database_iter_export_rows = lambda dataset, columns, snapshot: iter([{"trip_date": "2026-07-01", "driver_name": "Alice", "amount": Decimal("1.25")}])
    api_main._portal_database_write_xlsx_file = write_file
    api_main._portal_database_write_csv_file = write_file
    api_main._database_export_artifact_record = artifact_record
    try:
        return fn()
    finally:
        api_main._get_portal_database_dataset_for_user = saved["dataset"]
        api_main._get_portal_database_visible_columns = saved["columns"]
        api_main._portal_database_columns_from_snapshot = saved["from_snapshot"]
        api_main._portal_database_canonical_export_snapshot = saved["canonical"]
        api_main._portal_database_export_snapshot_params = saved["snapshot_params"]
        api_main._portal_database_iter_export_rows = saved["iter_rows"]
        api_main._portal_database_write_xlsx_file = saved["write_xlsx"]
        api_main._portal_database_write_csv_file = saved["write_csv"]
        api_main._database_export_artifact_record = saved["artifact"]


def _service_value(path: Path, key: str) -> str:
    for line in path.read_text().splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1]
    raise AssertionError(f"{key} not found in {path}")


def _service_values(path: Path, key: str) -> list[str]:
    return [
        line.split("=", 1)[1]
        for line in path.read_text().splitlines()
        if line.startswith(key + "=")
    ]


# The committed units no longer name a deployment CHECKOUT at all. They name the
# release ROOT, and the module they execute is resolved from `<root>/current` by
# /usr/local/bin/log-ops-runner.sh at process start. That is the difference this
# assertion now guards: the earlier shape — one checkout used for
# `WorkingDirectory`, `PYTHONPATH` and the interpreter alike — was internally
# consistent and still wrong, because the release pointer did not control it.
#
# The executable check below is unchanged in spirit: the same module and flags,
# rebased onto the current checkout, so the invocation is proven to import `api`
# rather than merely to look right.
DEPLOYMENT_UNITS = (
    "ops/systemd/proposed/database-export-worker.service",
    "ops/systemd/proposed/database-export-cleanup.service",
)
RELEASE_LAUNCHER = "/usr/local/bin/log-ops-runner.sh"
WORKER_MODULE = "ops.database_export_worker"
# What the active release must contain before either unit is allowed to start.
# `api/main.py` is here because the worker imports it — a release carrying one
# without the other would run a mixed pair.
REQUIRED_RELEASE_FILES = {"ops/database_export_worker.py", "api/main.py"}


def _declared_deployment_root(unit: Path) -> Path:
    root = Path(_service_value(unit, "WorkingDirectory"))
    assert root.is_absolute(), (unit.name, str(root))
    # The mutable development checkout must never be the root again. Prefix
    # matching alone would be wrong: the release root is `<checkout>-release`.
    checkout = str(REPO_ROOT)
    assert str(root) != checkout and not str(root).startswith(checkout + "/"), (
        unit.name, str(root))
    return root


def _test_systemd_proposed_invocations_import_api() -> None:
    roots = set()
    for rel in DEPLOYMENT_UNITS:
        unit = REPO_ROOT / rel
        deployment_root = _declared_deployment_root(unit)
        roots.add(deployment_root)
        env_lines = _service_values(unit, "Environment")
        env_files = _service_values(unit, "EnvironmentFile")
        command = shlex.split(_service_value(unit, "ExecStart"))

        # No static PYTHONPATH. It CANNOT be correct here even if it named the
        # release: the active release is a symlink target that changes without
        # this file changing, so the launcher must resolve it per process start.
        assert not [v for v in env_lines if v.startswith("PYTHONPATH=")], (rel, env_lines)
        declared = [v.partition("=")[2] for v in env_lines
                    if v.startswith("OPS_RUNNER_REQUIRE_RELEASE_FILE=")]
        assert len(declared) == 1, (rel, env_lines)
        assert {part for part in declared[0].split(":") if part} == REQUIRED_RELEASE_FILES, (
            rel, declared[0])
        # `environment-identity.env` joined these units in `1bec254` (the
        # identity guard every guarded surface reads). Configuration and secrets
        # stay host-managed and are deliberately NOT release content: they must
        # survive a code rollback unchanged.
        assert env_files == [
            "/etc/log-platform/runtime.env",
            "/etc/log-platform/database-export-minio-host.env",
            "/etc/log-platform/environment-identity.env",
        ], (rel, env_files)
        # The interpreter is no longer named by the unit. The launcher resolves
        # `<release root>/current`, refuses anything that is not a real release,
        # and execs that release's own linked virtualenv.
        assert command[:2] == [RELEASE_LAUNCHER, WORKER_MODULE], (rel, command)

        # Rebase the declared command onto THIS checkout and run it for real.
        # Same module, same flags; only the code root differs, which is exactly
        # the part the release pointer now owns.
        local_command = [sys.executable, "-m", *command[1:]]
        env = {"PYTHONDONTWRITEBYTECODE": "1", "PATH": os.environ.get("PATH", "")}
        env["PYTHONPATH"] = str(REPO_ROOT)
        result = subprocess.run(
            [*local_command, "--help"],
            cwd=str(REPO_ROOT),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, (rel, result.returncode, result.stdout, result.stderr)
        assert "No module named 'api'" not in result.stderr, result.stderr
        assert "Database Explorer async export worker" in result.stdout, result.stdout

    # Both units resolve the same release root; two roots would mean the worker
    # and its cleanup pass could run different builds of the same module.
    assert len(roots) == 1, sorted(str(r) for r in roots)
    api_unit = REPO_ROOT / "ops/systemd/log-platform-api.service.example"
    api_installer = REPO_ROOT / "ops/systemd/install_log_platform_api_service.sh"
    host_env = "database-export-minio-host.env"
    assert host_env not in api_unit.read_text(), host_env
    assert host_env not in api_installer.read_text(), host_env
    ops_doc = (REPO_ROOT / "docs/07_operations.md").read_text()
    assert "MINIO_ENDPOINT=127.0.0.1:9000" in ops_doc
    assert "MINIO_ENDPOINT=http://127.0.0.1:9000" not in ops_doc
    print("PASS: proposed systemd worker and cleanup invocations import api from the active release")


def _test_worker_atomic_publication_and_owner_visibility() -> None:
    job = {
        "job_id": str(uuid.uuid4()),
        "requested_by_user_id": USER_ID,
        "dataset_id": DATASET_ID,
        "status": "running",
        "claim_token": str(uuid.uuid4()),
        "attempt_object_key": "attempt/key-owner.csv",
        "artifact_id": None,
        "lease_valid": True,
    }
    state = {"job": job, "artifacts": [], "attempts": [_attempt_row(job, "active")]}

    def scenario(fake_s3, audits):
        fake_s3.upload_file("local.csv", api_main.MINIO_BUCKET, job["attempt_object_key"])
        assert state["artifacts"] == [], state
        artifact, completed_at, expires_at = _artifact_payload(job, job["attempt_object_key"])
        published = worker.publish_completed_job(job, artifact, 7, completed_at, expires_at)
        assert published and job["status"] == "completed", job
        assert job["object_key"] == "attempt/key-owner.csv", job
        assert job["attempt_object_key"] is None, job
        assert state["attempts"][0]["state"] == "published", state["attempts"]
        assert len(state["artifacts"]) == 1, state
        owner = {"user_id": USER_ID, "is_active": True, "is_admin": False}
        other = {"user_id": OTHER_ID, "is_active": True, "is_admin": False}
        stored = state["artifacts"][0]
        assert api_main.user_can_access_artifact(owner, stored, "download")
        assert not api_main.user_can_access_artifact(other, stored, "view")
        assert any(event.get("event_type") == "database_export_job_completed" for event in audits), audits

    _with_fake_worker_state(state, scenario)
    print("PASS: artifact row is committed atomically with fenced completed job and published attempt ledger")


def _running_worker_job(*, requested_format: str = "xlsx") -> dict:
    return {
        "job_id": str(uuid.uuid4()),
        "requested_by_user_id": USER_ID,
        "dataset_id": DATASET_ID,
        "requested_format": requested_format,
        "request_snapshot_json": {"columns": ["trip_date", "driver_name", "amount"]},
        "status": "running",
        "claim_token": str(uuid.uuid4()),
        "attempt_object_key": f"attempt/{uuid.uuid4()}.xlsx",
        "artifact_id": None,
        "attempt_count": 1,
        "lease_valid": True,
    }


SENSITIVE_EXCEPTION_PAYLOADS = [
    "SELECT * FROM trips WHERE driver='Jan Kowalski'",
    '{"search":"secret driver","filters":{"vehicle":"ABC123"}}',
    "driver=Jan Kowalski; plate=ABC123; email=test@example.com",
    "Cookie: session=supersecret",
    "Authorization: Bearer secret-token",
    "postgresql://user:password@host/db",
    "DB_PASSWORD=secret-value",
    "MINIO_SECRET_KEY=secret-value",
]

WORKER_EVENT_KEY_ALLOWLIST = {"event", "job_id", "stage", "attempt", "error_category", "message"}
WORKER_STATIC_FAILURE_VALUES = {
    "authorization_failed",
    "query_failed",
    "csv_generation_failed",
    "xlsx_generation_failed",
    "upload_failed",
    "publication_failed",
    "cleanup_failed",
    "unexpected_operation_failed",
}
PROHIBITED_WORKER_EVENT_KEYS = {
    "row_count",
    "cleaned_attempts",
    "requeued",
    "failed",
    "expired",
    "artifact_id",
    "object_key",
    "attempt_object_key",
    "storage_key",
    "path",
    "file_path",
    "temp_path",
    "exc_type",
}


def _sensitive_exception_payload() -> str:
    return " | ".join(SENSITIVE_EXCEPTION_PAYLOADS)


def _worker_log_events(logs: str) -> list[dict[str, str]]:
    events = []
    for line in logs.splitlines():
        if not line.strip():
            continue
        parsed = {}
        for token in shlex.split(line):
            key, sep, value = token.partition("=")
            assert sep, line
            parsed[key] = value
        events.append(parsed)
    return events


def _assert_worker_log_events_safe(logs: str) -> list[dict[str, str]]:
    events = _worker_log_events(logs)
    assert events, logs
    for event in events:
        assert set(event).issubset(WORKER_EVENT_KEY_ALLOWLIST), event
        assert not (set(event) & PROHIBITED_WORKER_EVENT_KEYS), event
        if "message" in event:
            assert event["message"] in WORKER_STATIC_FAILURE_VALUES, event
        if "error_category" in event:
            assert event["error_category"] in WORKER_STATIC_FAILURE_VALUES, event
    _assert_no_sensitive_log_content(logs)
    return events


def _assert_no_sensitive_log_content(logs: str) -> None:
    forbidden = SENSITIVE_EXCEPTION_PAYLOADS + [
        "SELECT * FROM trips",
        "Jan Kowalski",
        "secret driver",
        "ABC123",
        "test@example.com",
        "session=supersecret",
        "secret-token",
        "user:password",
        "DB_PASSWORD",
        "MINIO_SECRET_KEY",
        "secret-value",
        "Traceback",
        "exc_type=",
        "runtime_error",
        "ValueError",
        "RuntimeError",
        "attempt/key",
        "attempt/orphan.csv",
        "database_explorer/exports",
        "/tmp/",
        "local.csv",
    ]
    for value in forbidden:
        assert value not in logs, (value, logs)


def _test_worker_event_logger_allows_only_static_safe_fields() -> None:
    rich_job = {
        "job_id": str(uuid.uuid4()),
        "attempt_count": 4,
        "row_count": 999,
        "artifact_id": "11111111-1111-1111-1111-111111111111",
        "object_key": "database_explorer/exports/private.csv",
        "attempt_object_key": "attempt/key-secret.csv",
        "path": "/tmp/database_export_secret.csv",
        "safe_error_message": _sensitive_exception_payload(),
    }
    logs, _result = _capture_worker_logs(
        lambda: worker._log_worker_event("database_export_job_failed", job=rich_job, stage="upload", failed=True)
    )
    events = _assert_worker_log_events_safe(logs)
    assert events == [{
        "event": "database_export_job_failed",
        "job_id": rich_job["job_id"],
        "attempt": "4",
        "stage": "upload",
        "error_category": "upload_failed",
        "message": "upload_failed",
    }], events

    logs, _result = _capture_worker_logs(
        lambda: worker._log_worker_event("database_export_cleanup_completed", job={"object_key": "attempt/key.csv"}, stage="cleanup")
    )
    events = _assert_worker_log_events_safe(logs)
    assert events == [{"event": "database_export_cleanup_completed", "stage": "cleanup"}], events
    print("PASS: worker logger emits only the six allowlisted fields and omits unavailable values")


def _test_worker_snapshot_validation_exception_is_query_failure() -> None:
    job = _running_worker_job()
    state = {"job": job, "artifacts": [], "attempts": [_attempt_row(job, "active")]}

    def scenario(_fake_s3, _audits):
        def invalid_snapshot():
            api_main._portal_database_canonical_export_snapshot = lambda dataset, columns, params, format_name: (_ for _ in ()).throw(RuntimeError(_sensitive_exception_payload()))
            return worker.process_job(job)

        logs, result = _capture_worker_logs(lambda: _with_worker_process_helpers(invalid_snapshot))
        assert result is False
        assert logs.count("event=database_export_job_failed") == 1, logs
        assert "stage=query" in logs, logs
        assert "error_category=query_failed" in logs, logs
        assert "message=query_failed" in logs, logs
        events = _assert_worker_log_events_safe(logs)
        assert sum(1 for event in events if event.get("event") == "database_export_job_failed") == 1, events
        assert job["safe_error_code"] == "QUERY_FAILED", job
        assert job["safe_error_message"] == "This export could not be generated. Please contact an administrator.", job

    _with_fake_worker_state(state, scenario)
    print("PASS: worker snapshot validation exceptions are classified as query failures with generic user-facing text")


def _test_worker_upload_failure_structured_logging_and_redaction() -> None:
    job = _running_worker_job()
    state = {"job": job, "artifacts": [], "attempts": [_attempt_row(job, "active")]}

    def scenario(fake_s3, _audits):
        fake_s3.upload_error = RuntimeError(_sensitive_exception_payload())
        logs, result = _capture_worker_logs(lambda: _with_worker_process_helpers(lambda: worker.process_job(job)))
        assert result is False
        assert logs.count("event=database_export_job_failed") == 1, logs
        assert "stage=upload" in logs, logs
        assert "attempt=1" in logs, logs
        assert "error_category=upload_failed" in logs, logs
        assert "message=upload_failed" in logs, logs
        events = _assert_worker_log_events_safe(logs)
        assert sum(1 for event in events if event.get("event") == "database_export_job_failed") == 1, events
        assert job["status"] == "failed", job
        assert job["safe_error_code"] == "UPLOAD_FAILED", job
        assert job["safe_error_message"] == "This export could not be generated. Please contact an administrator.", job
        assert state["attempts"][0]["state"] == "cleanup_pending", state["attempts"]

    _with_fake_worker_state(state, scenario)

    success_job = _running_worker_job()
    success_state = {"job": success_job, "artifacts": [], "attempts": [_attempt_row(success_job, "active")]}

    def success_scenario(_fake_s3, _audits):
        logs, result = _capture_worker_logs(lambda: _with_worker_process_helpers(lambda: worker.process_job(success_job)))
        assert result is True
        assert "event=database_export_job_completed" in logs, logs
        _assert_worker_log_events_safe(logs)
        assert success_job["status"] == "completed", success_job

    _with_fake_worker_state(success_state, success_scenario)
    print("PASS: upload failure logs only safe allowlisted fields, keeps generic portal failure, and worker continues")


def _test_worker_portal_safe_failure_remains_generic_and_continues() -> None:
    job = _running_worker_job()
    state = {"job": job, "artifacts": [], "attempts": [_attempt_row(job, "active")]}

    def scenario(_fake_s3, _audits):
        def denied():
            api_main._get_portal_database_dataset_for_user = lambda dataset_id, user_id: None
            return worker.process_job(job)

        logs, result = _capture_worker_logs(lambda: _with_worker_process_helpers(denied))
        assert result is False
        assert logs.count("event=database_export_job_failed") == 1, logs
        assert "stage=authorization" in logs, logs
        assert "message=authorization_failed" in logs, logs
        assert "AUTHORIZATION_REVOKED" not in logs, logs
        events = _assert_worker_log_events_safe(logs)
        assert sum(1 for event in events if event.get("event") == "database_export_job_failed") == 1, events
        assert job["safe_error_code"] == "AUTHORIZATION_REVOKED", job
        assert job["safe_error_message"] == "Your access to export this dataset changed before the export ran. No file was generated.", job

    _with_fake_worker_state(state, scenario)

    success_job = _running_worker_job()
    success_state = {"job": success_job, "artifacts": [], "attempts": [_attempt_row(success_job, "active")]}

    def success_scenario(_fake_s3, _audits):
        logs, result = _capture_worker_logs(lambda: _with_worker_process_helpers(lambda: worker.process_job(success_job)))
        assert result is True
        assert success_job["status"] == "completed", success_job
        _assert_worker_log_events_safe(logs)

    _with_fake_worker_state(success_state, success_scenario)
    print("PASS: portal-safe worker failure stays generic and a later job still completes")


def _test_worker_publication_exception_structured_logging() -> None:
    job = _running_worker_job()
    state = {
        "job": job,
        "artifacts": [],
        "attempts": [_attempt_row(job, "active")],
        "insert_raises": True,
        "insert_error": _sensitive_exception_payload(),
    }

    def scenario(_fake_s3, _audits):
        logs, result = _capture_worker_logs(lambda: _with_worker_process_helpers(lambda: worker.process_job(job)))
        assert result is False
        assert logs.count("event=database_export_job_failed") == 1, logs
        assert "stage=publication" in logs, logs
        assert "error_category=publication_failed" in logs, logs
        assert "message=publication_failed" in logs, logs
        events = _assert_worker_log_events_safe(logs)
        assert sum(1 for event in events if event.get("event") == "database_export_job_failed") == 1, events
        assert job["status"] == "failed", job
        assert job["safe_error_code"] == "PUBLICATION_FAILED", job
        assert job["safe_error_message"] == "This export could not be generated. Please contact an administrator.", job
        assert state["artifacts"] == [], state
        assert state["attempts"][0]["state"] == "cleanup_pending", state["attempts"]

    _with_fake_worker_state(state, scenario)
    print("PASS: publication exception logs only safe allowlisted fields and preserves generic portal failure")


def _test_worker_publication_failure_rolls_back_artifact_and_keeps_cleanup_ledger() -> None:
    job = {
        "job_id": str(uuid.uuid4()),
        "requested_by_user_id": USER_ID,
        "dataset_id": DATASET_ID,
        "status": "running",
        "claim_token": str(uuid.uuid4()),
        "attempt_object_key": "attempt/key-fail.csv",
        "artifact_id": None,
        "lease_valid": True,
    }
    state = {"job": job, "artifacts": [], "attempts": [_attempt_row(job, "active")], "complete_update_fails": True}

    def scenario(fake_s3, _audits):
        fake_s3.upload_file("local.csv", api_main.MINIO_BUCKET, job["attempt_object_key"])
        artifact, completed_at, expires_at = _artifact_payload(job, job["attempt_object_key"])
        logs, published = _capture_worker_logs(lambda: worker.publish_completed_job(job, artifact, 3, completed_at, expires_at))
        assert published is None
        assert "event=database_export_job_fence_lost" in logs, logs
        assert "stage=publication" in logs, logs
        assert "message=publication_failed" in logs, logs
        events = _assert_worker_log_events_safe(logs)
        assert all(event.get("message") in WORKER_STATIC_FAILURE_VALUES for event in events if "message" in event), events
        assert "event=database_export_job_failed" not in logs, logs
        cleanup_row = worker._mark_attempt_cleanup_pending(job)
        assert cleanup_row and cleanup_row["state"] == "cleanup_pending", cleanup_row
        assert worker._cleanup_attempt_object_row(cleanup_row)
        assert state["artifacts"] == [], state
        assert job["status"] == "running", job
        assert state.get("rollbacks", 0) >= 1, state
        assert state["attempts"][0]["state"] == "cleanup_pending", state["attempts"]
        assert state["attempts"][0]["last_cleanup_success_at"] is not None, state["attempts"]
        assert fake_s3.objects == set(), fake_s3.objects
        assert worker.cleanup_unpublished_attempt_objects() == 0
        assert state["attempts"][0]["state"] == "cleanup_pending", state["attempts"]

    _with_fake_worker_state(state, scenario)
    print("PASS: publication failure leaves no visible artifact and retains cleanup_pending attempt ledger")


def _test_worker_stale_recovery_successful_cleanup_not_reselected() -> None:
    job_id = str(uuid.uuid4())
    token_a = str(uuid.uuid4())
    job = {
        "job_id": job_id,
        "requested_by_user_id": USER_ID,
        "dataset_id": DATASET_ID,
        "status": "running",
        "claim_token": token_a,
        "attempt_object_key": "attempt/key-a.csv",
        "artifact_id": None,
        "attempt_count": 1,
        "lease_valid": False,
    }
    state = {"job": job, "artifacts": [], "attempts": [_attempt_row(job, "active")]}

    def scenario(fake_s3, _audits):
        # The interrupted attempt had already uploaded its object, so the
        # recovery sweep has something real to remove — which is what lets its
        # cleanup become terminal.
        fake_s3.objects.add("attempt/key-a.csv")
        assert worker.recover_stale_running_jobs() == {"requeued": 1, "failed": 0}
        assert job["status"] == "queued", job
        assert job["attempt_object_key"] is None, job
        attempt_a = state["attempts"][0]
        assert attempt_a["state"] == "cleanup_pending", state["attempts"]
        # The delete removed a real object, which is what makes cleanup terminal:
        # nothing can recreate it, so the row must never be selected again.
        assert attempt_a["last_cleanup_success_at"] is not None, state["attempts"]
        assert "attempt/key-a.csv" in fake_s3.deleted, fake_s3.deleted
        assert "attempt/key-a.csv" not in fake_s3.objects, fake_s3.objects

        token_b = str(uuid.uuid4())
        job.update({
            "status": "running",
            "claim_token": token_b,
            "attempt_object_key": "attempt/key-b.csv",
            "lease_valid": True,
            "attempt_count": 2,
        })
        state["attempts"].append(_attempt_row(job, "active"))

        assert worker.cleanup_unpublished_attempt_objects() == 0
        assert attempt_a["state"] == "cleanup_pending", state["attempts"]
        assert attempt_a["object_key"] == "attempt/key-a.csv", state["attempts"]
        assert attempt_a["last_cleanup_success_at"] is not None, state["attempts"]
        assert state["artifacts"] == [], state

    _with_fake_worker_state(state, scenario)
    print("PASS: stale recovery cleanup keeps durable successful ledger row without reselecting it")


def _test_worker_stale_attempt_cannot_publish_over_reclaimed_claim() -> None:
    job_id = str(uuid.uuid4())
    token_a = str(uuid.uuid4())
    token_b = str(uuid.uuid4())
    job = {
        "job_id": job_id,
        "requested_by_user_id": USER_ID,
        "dataset_id": DATASET_ID,
        "status": "running",
        "claim_token": token_b,
        "attempt_object_key": "attempt/key-b.csv",
        "artifact_id": None,
        "lease_valid": True,
    }
    state = {
        "job": job,
        "artifacts": [],
        "attempts": [
            _attempt_row({"job_id": job_id, "claim_token": token_a, "attempt_object_key": "attempt/key-a.csv"}, "cleanup_pending"),
            _attempt_row(job, "active"),
        ],
    }

    def scenario(fake_s3, _audits):
        job_a = {"job_id": job_id, "claim_token": token_a, "attempt_object_key": "attempt/key-a.csv", "requested_by_user_id": USER_ID, "dataset_id": DATASET_ID}
        job_b = {"job_id": job_id, "claim_token": token_b, "attempt_object_key": "attempt/key-b.csv", "requested_by_user_id": USER_ID, "dataset_id": DATASET_ID}
        artifact_a, completed_at_a, expires_at_a = _artifact_payload(job_a, "attempt/key-a.csv", artifact_id="aaaaaaaa-1111-1111-1111-111111111111")
        artifact_b, completed_at_b, expires_at_b = _artifact_payload(job_b, "attempt/key-b.csv", artifact_id="bbbbbbbb-2222-2222-2222-222222222222")
        fake_s3.upload_file("a.csv", api_main.MINIO_BUCKET, "attempt/key-a.csv")
        fake_s3.upload_file("b.csv", api_main.MINIO_BUCKET, "attempt/key-b.csv")
        assert not worker.refresh_lease(job_a, 10)
        assert not worker.mark_failed(job_a, "STALE", "stale")
        assert worker.publish_completed_job(job_a, artifact_a, 10, completed_at_a, expires_at_a) is None
        assert worker.cleanup_unpublished_attempt_objects() >= 1
        assert "attempt/key-a.csv" not in fake_s3.objects, fake_s3.objects
        assert "attempt/key-b.csv" in fake_s3.objects, fake_s3.objects
        assert state["artifacts"] == [], state
        assert worker.refresh_lease(job_b, 11)
        assert worker.publish_completed_job(job_b, artifact_b, 11, completed_at_b, expires_at_b)
        assert job["status"] == "completed", job
        assert job["artifact_id"] == artifact_b["artifact_id"], job
        assert state["attempts"][1]["state"] == "published", state["attempts"]
        assert len(state["artifacts"]) == 1 and state["artifacts"][0]["artifact_id"] == artifact_b["artifact_id"], state

    _with_fake_worker_state(state, scenario)
    print("PASS: stale worker cannot refresh, fail, publish, or delete the reclaimed worker object")



def _test_worker_idle_loop_stop_event_exits_promptly() -> None:
    worker._reset_stop_request_for_tests()
    original_run_once = worker.run_once
    calls = []
    result = []

    def fake_run_once(*, cleanup=True):
        calls.append(cleanup)
        return 0

    worker.run_once = fake_run_once
    try:
        thread = threading.Thread(
            target=lambda: result.append(worker.run_loop(poll_seconds=30, cleanup_interval_seconds=3600)),
            daemon=True,
        )
        started = time.monotonic()
        thread.start()
        deadline = time.monotonic() + 1.0
        while not calls and time.monotonic() < deadline:
            time.sleep(0.01)
        assert calls, calls
        worker._handle_stop(None, None)
        thread.join(timeout=1.0)
        elapsed = time.monotonic() - started
        assert not thread.is_alive(), elapsed
        assert elapsed < 1.0, elapsed
        assert result == [0], result
    finally:
        worker.run_once = original_run_once
        worker._reset_stop_request_for_tests()
    print("PASS: idle worker loop exits promptly after simulated SIGTERM without waiting for poll interval")


def _test_worker_stop_request_prevents_next_claim() -> None:
    worker._reset_stop_request_for_tests()
    saved = {
        "cleanup_attempts": worker.cleanup_unpublished_attempt_objects,
        "recover": worker.recover_stale_running_jobs,
        "cleanup_expired": worker.cleanup_expired_exports,
        "claim": worker.claim_one_job,
    }
    claimed = []

    worker.cleanup_unpublished_attempt_objects = lambda: 0
    worker.recover_stale_running_jobs = lambda: {"requeued": 0, "failed": 0}
    worker.cleanup_expired_exports = lambda: 0
    worker.claim_one_job = lambda: claimed.append(True) or None
    try:
        worker._handle_stop(None, None)
        assert worker.run_once(cleanup=True) == 0
        assert claimed == [], claimed
    finally:
        worker.cleanup_unpublished_attempt_objects = saved["cleanup_attempts"]
        worker.recover_stale_running_jobs = saved["recover"]
        worker.cleanup_expired_exports = saved["cleanup_expired"]
        worker.claim_one_job = saved["claim"]
        worker._reset_stop_request_for_tests()
    print("PASS: stop request prevents the next async export claim")


def _test_worker_stop_during_generation_recovers_without_publication() -> None:
    worker._reset_stop_request_for_tests()
    job = _running_worker_job()
    state = {"job": job, "artifacts": [], "attempts": [_attempt_row(job, "active")]}

    def scenario(fake_s3, _audits):
        def stop_after_generation(columns, rows, path, *, max_rows, heartbeat=None):
            count = 0
            for _row in rows:
                count += 1
            Path(path).write_bytes(b"export bytes")
            worker._handle_stop(None, None)
            return count

        def run_stopped_generation():
            api_main._portal_database_write_xlsx_file = stop_after_generation
            return worker.process_job(job)

        logs, result = _capture_worker_logs(lambda: _with_worker_process_helpers(run_stopped_generation))
        assert result is False
        assert "event=database_export_job_completed" not in logs, logs
        assert "event=database_export_job_failed" not in logs, logs
        assert job["status"] == "running", job
        assert state["artifacts"] == [], state
        assert fake_s3.objects == set(), fake_s3.objects
        assert state["attempts"][0]["state"] == "active", state["attempts"]

        worker._reset_stop_request_for_tests()
        job["lease_valid"] = False
        assert worker.recover_stale_running_jobs() == {"requeued": 1, "failed": 0}
        assert job["status"] == "queued", job
        assert state["attempts"][0]["state"] == "cleanup_pending", state["attempts"]
        # Nothing was ever uploaded, so the delete found nothing. That proves the
        # object is absent *now*, not that the interrupted attempt can no longer
        # write one, so the ledger stays selectable rather than going terminal.
        assert state["attempts"][0]["last_cleanup_attempt_at"] is not None, state["attempts"]
        assert state["attempts"][0]["last_cleanup_success_at"] is None, state["attempts"]
        assert job["attempt_object_key"] is None, job

        # Once the settle window has closed the same sweep finalizes it, so the
        # retry is bounded rather than permanent.
        state["attempts"][0]["cleanup_requested_at"] = api_main.utcnow() - timedelta(
            seconds=worker.ATTEMPT_CLEANUP_SETTLE_SECONDS + 60
        )
        assert worker.cleanup_unpublished_attempt_objects() == 1
        assert state["attempts"][0]["last_cleanup_success_at"] is not None, state["attempts"]

    try:
        _with_fake_worker_state(state, scenario)
    finally:
        worker._reset_stop_request_for_tests()
    print("PASS: stop during active generation cannot publish or complete and remains recoverable by stale-lease cleanup")


def _test_worker_stop_after_upload_cleanup_before_publication() -> None:
    worker._reset_stop_request_for_tests()
    job = _running_worker_job()
    state = {"job": job, "artifacts": [], "attempts": [_attempt_row(job, "active")]}

    def scenario(fake_s3, _audits):
        fake_s3.upload_hook = lambda: worker._handle_stop(None, None)
        logs, result = _capture_worker_logs(lambda: _with_worker_process_helpers(lambda: worker.process_job(job)))
        assert result is False
        assert "event=database_export_job_completed" not in logs, logs
        assert "event=database_export_job_failed" not in logs, logs
        assert job["status"] == "running", job
        assert state["artifacts"] == [], state
        assert state["attempts"][0]["state"] == "cleanup_pending", state["attempts"]
        assert state["attempts"][0]["last_cleanup_success_at"] is not None, state["attempts"]
        assert job["attempt_object_key"] == state["attempts"][0]["object_key"], job
        assert job["attempt_object_key"] in fake_s3.deleted, fake_s3.deleted
        assert job["attempt_object_key"] not in fake_s3.objects, fake_s3.objects

    try:
        _with_fake_worker_state(state, scenario)
    finally:
        worker._reset_stop_request_for_tests()
    print("PASS: stop after upload marks attempt cleanup_pending and prevents artifact publication")

def _test_worker_cleanup_failure_structured_logging_and_redaction() -> None:
    job = {
        "job_id": str(uuid.uuid4()),
        "requested_by_user_id": USER_ID,
        "dataset_id": DATASET_ID,
        "status": "failed",
        "claim_token": None,
        "attempt_object_key": None,
        "artifact_id": None,
        "updated_at": api_main.utcnow(),
    }
    attempt = _attempt_row({"job_id": job["job_id"], "claim_token": str(uuid.uuid4()), "attempt_object_key": "attempt/orphan.csv"}, "cleanup_pending")
    state = {"job": job, "artifacts": [], "attempts": [attempt]}

    def scenario(fake_s3, _audits):
        fake_s3.objects.add("attempt/orphan.csv")
        fake_s3.delete_error = RuntimeError(_sensitive_exception_payload())
        logs, result = _capture_worker_logs(lambda: worker._cleanup_attempt_object_row(attempt))
        assert result is False
        assert "event=database_export_cleanup_failed" in logs, logs
        assert "stage=cleanup" in logs, logs
        assert "error_category=cleanup_failed" in logs, logs
        assert "message=cleanup_failed" in logs, logs
        _assert_worker_log_events_safe(logs)
        assert state["attempts"][0]["state"] == "cleanup_pending", state["attempts"]
        assert state["attempts"][0]["last_cleanup_attempt_at"] is not None, state["attempts"]
        assert state["attempts"][0]["last_cleanup_success_at"] is None, state["attempts"]

    _with_fake_worker_state(state, scenario)
    print("PASS: cleanup deletion failure logs a safe structured cleanup event and keeps ledger state")


def _test_failed_cleanup_record_remains_retryable() -> None:
    job = {
        "job_id": str(uuid.uuid4()),
        "requested_by_user_id": USER_ID,
        "dataset_id": DATASET_ID,
        "status": "failed",
        "claim_token": None,
        "attempt_object_key": None,
        "artifact_id": None,
        "updated_at": api_main.utcnow(),
    }
    attempt = _attempt_row({"job_id": job["job_id"], "claim_token": str(uuid.uuid4()), "attempt_object_key": "attempt/retry.csv"}, "cleanup_pending")
    state = {"job": job, "artifacts": [], "attempts": [attempt]}

    def scenario(fake_s3, _audits):
        fake_s3.objects.add("attempt/retry.csv")
        fake_s3.delete_error = RuntimeError("endpoint unreachable")
        logs, cleaned = _capture_worker_logs(lambda: worker.cleanup_unpublished_attempt_objects())
        assert cleaned == 0, cleaned
        assert "event=database_export_cleanup_failed" in logs, logs
        _assert_worker_log_events_safe(logs)
        assert "attempt/retry.csv" in fake_s3.objects, fake_s3.objects
        assert attempt["last_cleanup_attempt_at"] is not None, attempt
        assert attempt["last_cleanup_success_at"] is None, attempt

        fake_s3.delete_error = None
        assert worker.cleanup_unpublished_attempt_objects() == 1
        assert "attempt/retry.csv" not in fake_s3.objects, fake_s3.objects
        assert attempt["last_cleanup_success_at"] is not None, attempt
        assert worker.cleanup_unpublished_attempt_objects() == 0

    _with_fake_worker_state(state, scenario)
    print("PASS: failed unpublished-attempt cleanup remains retryable until a later successful delete")


def _test_missing_unpublished_attempt_object_finalizes_when_storage_reachable() -> None:
    job = {
        "job_id": str(uuid.uuid4()),
        "requested_by_user_id": USER_ID,
        "dataset_id": DATASET_ID,
        "status": "failed",
        "claim_token": None,
        "attempt_object_key": None,
        "artifact_id": None,
        "updated_at": api_main.utcnow(),
    }
    attempt = _attempt_row({"job_id": job["job_id"], "claim_token": str(uuid.uuid4()), "attempt_object_key": "attempt/missing.csv"}, "cleanup_pending")
    # The producer is definitively finished: cleanup was requested longer ago
    # than the export lease, so no attempt that was still running then could
    # still be writing now.
    attempt["cleanup_requested_at"] = api_main.utcnow() - timedelta(
        seconds=worker.ATTEMPT_CLEANUP_SETTLE_SECONDS + 60
    )
    state = {"job": job, "artifacts": [], "attempts": [attempt]}

    def scenario(fake_s3, _audits):
        assert "attempt/missing.csv" not in fake_s3.objects, fake_s3.objects
        assert worker.cleanup_unpublished_attempt_objects() == 1
        assert "attempt/missing.csv" in fake_s3.deleted, fake_s3.deleted
        assert attempt["state"] == "cleanup_pending", attempt
        assert attempt["object_key"] == "attempt/missing.csv", attempt
        assert attempt["last_cleanup_success_at"] is not None, attempt
        assert worker.cleanup_unpublished_attempt_objects() == 0

    _with_fake_worker_state(state, scenario)
    print("PASS: missing unpublished-attempt object finalizes once the producing attempt can no longer write")


def _test_missing_object_does_not_finalize_while_the_producer_may_still_upload() -> None:
    """The other half of the same rule, and the reason the settle window exists.

    A delete that found nothing proves only that nothing was there at that
    instant. Finalizing on it while the producing attempt may still be uploading
    is exactly what let a late object survive with the ledger claiming success.
    """
    job = {
        "job_id": str(uuid.uuid4()),
        "requested_by_user_id": USER_ID,
        "dataset_id": DATASET_ID,
        "status": "cancelled",
        "claim_token": None,
        "attempt_object_key": None,
        "artifact_id": None,
        "updated_at": api_main.utcnow(),
    }
    attempt = _attempt_row(
        {"job_id": job["job_id"], "claim_token": str(uuid.uuid4()), "attempt_object_key": "attempt/inflight.csv"},
        "cleanup_pending",
    )
    state = {"job": job, "artifacts": [], "attempts": [attempt]}

    def scenario(fake_s3, _audits):
        # Cleanup runs while the upload is still in flight: nothing to delete.
        assert worker.cleanup_unpublished_attempt_objects() == 0, "an unsettled absence was finalized"
        assert attempt["last_cleanup_success_at"] is None, attempt
        assert attempt["last_cleanup_attempt_at"] is not None, "the attempt was not recorded"
        # The row is still selectable, which is the durable retry path.
        assert worker.cleanup_unpublished_attempt_objects() == 0
        assert attempt["last_cleanup_success_at"] is None, attempt

    _with_fake_worker_state(state, scenario)
    print("PASS: a delete that found nothing stays retryable while the producing attempt could still upload")


def _test_cancelled_inflight_upload_cannot_orphan_an_object() -> None:
    """The exact temporal race an independent review identified.

    Ordering, in real time:

      1. a running attempt is uploading its object;
      2. the user cancels; the API marks the attempt's ledger row cleanup_pending;
      3. the cleanup sweep runs and deletes — finding nothing, because the
         upload has not landed yet;
      4. the upload completes, so the object now exists;
      5. the worker process exits before executing any cooperative cleanup.

    Before the correction step 3 recorded terminal success, so the object created
    in step 4 was never selected again and survived indefinitely with the ledger
    claiming it had been cleaned. The durable guarantee is that step 3 cannot be
    terminal while the producing attempt could still write, so a later sweep
    finds the real object and removes it.
    """
    job_id = str(uuid.uuid4())
    token = str(uuid.uuid4())
    key = "attempt/inflight-cancelled.csv"
    job = {
        "job_id": job_id,
        "requested_by_user_id": USER_ID,
        "dataset_id": DATASET_ID,
        # The API's cancel has already run: terminal status, fence cleared.
        "status": "cancelled",
        "claim_token": None,
        "attempt_object_key": None,
        "artifact_id": None,
        "attempt_count": 1,
        "lease_valid": False,
    }
    attempt = _attempt_row({"job_id": job_id, "claim_token": token, "attempt_object_key": key}, "cleanup_pending")
    state = {"job": job, "artifacts": [], "attempts": [attempt]}

    def scenario(fake_s3, _audits):
        # 3. sweep runs while the upload is still in flight.
        assert key not in fake_s3.objects, fake_s3.objects
        assert worker.cleanup_unpublished_attempt_objects() == 0, "an unsettled absence was finalized"
        assert key in fake_s3.deleted, fake_s3.deleted
        assert attempt["last_cleanup_success_at"] is None, attempt

        # 4. the in-flight upload lands.
        fake_s3.objects.add(key)
        # 5. the worker exits here — no cooperative cleanup runs at all. Nothing
        #    below depends on the producing process doing anything further.

        # A later sweep is still entitled to the row and removes the real object.
        assert worker.cleanup_unpublished_attempt_objects() == 1
        assert key not in fake_s3.objects, "the late object survived cancellation"
        assert attempt["last_cleanup_success_at"] is not None, attempt

        # And once genuinely clean the row is terminal, so this is not an
        # unbounded retry loop.
        assert worker.cleanup_unpublished_attempt_objects() == 0
        # Cancellation never yields something downloadable.
        assert state["artifacts"] == [], state
        assert job["status"] == "cancelled", job
        assert job["artifact_id"] is None, job

    _with_fake_worker_state(state, scenario)
    print("PASS: a cancelled attempt's late upload is still collected after the worker exits")


def _test_cleanup_unpublished_attempt_object() -> None:
    job = {
        "job_id": str(uuid.uuid4()),
        "requested_by_user_id": USER_ID,
        "dataset_id": DATASET_ID,
        "status": "failed",
        "claim_token": None,
        "attempt_object_key": None,
        "artifact_id": None,
        "updated_at": api_main.utcnow(),
    }
    attempt = _attempt_row({"job_id": job["job_id"], "claim_token": str(uuid.uuid4()), "attempt_object_key": "attempt/orphan.csv"}, "cleanup_pending")
    state = {"job": job, "artifacts": [], "attempts": [attempt]}

    def scenario(fake_s3, _audits):
        fake_s3.objects.add("attempt/orphan.csv")
        assert worker.cleanup_unpublished_attempt_objects() == 1
        assert "attempt/orphan.csv" not in fake_s3.objects, fake_s3.objects
        assert state["attempts"][0]["state"] == "cleanup_pending", state["attempts"]
        assert state["attempts"][0]["last_cleanup_success_at"] is not None, state["attempts"]
        assert state["attempts"][0]["object_key"] == "attempt/orphan.csv", state["attempts"]
        assert worker.cleanup_unpublished_attempt_objects() == 0
        assert state["attempts"][0]["state"] == "cleanup_pending", state["attempts"]

    _with_fake_worker_state(state, scenario)
    print("PASS: successful unpublished attempt cleanup is durable and not selected again")

def _test_database_export_system_folder_provisioning() -> None:
    folder_id = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    state = {"folders": {}, "jobs": []}

    class Cursor:
        def __init__(self):
            self._row = None
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc, tb):
            return False
        def execute(self, query, params=()):
            normalized = " ".join(str(query).split())
            self._row = None
            if normalized.startswith("INSERT INTO database_export_system_folders"):
                owner = str(params[0])
                state["folders"].setdefault(owner, folder_id)
                self._row = {"folder_id": state["folders"][owner]}
            elif normalized.startswith("INSERT INTO database_export_jobs"):
                state["jobs"].append({"params": params, "query": normalized})
                self._row = {"job_id": params[0]}
        def fetchone(self):
            return self._row

    class Conn:
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc, tb):
            return False
        def cursor(self):
            return Cursor()
        def commit(self):
            pass

    old_db = api_main.db_conn
    old_schema = api_main._database_export_system_folder_schema_available
    api_main.db_conn = lambda: Conn()
    api_main._database_export_system_folder_schema_available = lambda: True
    try:
        job_a = api_main._enqueue_database_export_job({"user_id": USER_ID}, _dataset(), {"format": "csv"}, format_name="csv")
        job_b = api_main._enqueue_database_export_job({"user_id": USER_ID}, _dataset(), {"format": "xlsx"}, format_name="xlsx")
        assert job_a and job_b and job_a != job_b
        assert state["folders"] == {USER_ID: folder_id}, state
        assert len(state["jobs"]) == 2, state
        assert all("system_folder_id" in job["query"] for job in state["jobs"]), state
        assert all(str(job["params"][-1]) == folder_id for job in state["jobs"]), state
    finally:
        api_main.db_conn = old_db
        api_main._database_export_system_folder_schema_available = old_schema
    source = Path("db/migrations/044_database_export_system_folders.sql").read_text()
    assert "UNIQUE (owner_user_id, system_key)" in source
    assert "ON DELETE RESTRICT" in source
    assert "ADD COLUMN IF NOT EXISTS system_folder_id" in source
    print("PASS: first and repeated async queueing provisions one owner system folder with durable job relation")


def _test_database_export_system_folder_lifecycle_ui_and_auth() -> None:
    folder_id = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    ready_artifact_id = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
    expired_artifact_id = "99999999-9999-9999-9999-999999999999"
    folder = {
        "folder_id": folder_id,
        "owner_user_id": USER_ID,
        "folder_name": api_main.DATABASE_EXPORT_SYSTEM_FOLDER_NAME,
        "system_key": api_main.DATABASE_EXPORT_SYSTEM_FOLDER_KEY,
        "slug": api_main.DATABASE_EXPORT_SYSTEM_FOLDER_SLUG,
        "description": api_main.DATABASE_EXPORT_SYSTEM_FOLDER_DESCRIPTION,
    }
    future = (api_main.utcnow() + timedelta(days=1)).isoformat()
    past = (api_main.utcnow() - timedelta(days=1)).isoformat()
    jobs = [
        {"job_id": "j1", "system_folder_id": folder_id, "dataset_name": "Trips", "client_code": "ACME_01", "requested_format": "csv", "status": "queued", "queued_at": "2026-07-01T10:00:00+00:00", "artifact_id": None},
        {"job_id": "j2", "system_folder_id": folder_id, "dataset_name": "Trips", "client_code": "ACME_01", "requested_format": "xlsx", "status": "running", "started_at": "2026-07-01T10:01:00+00:00", "artifact_id": None},
        {"job_id": "j3", "system_folder_id": folder_id, "dataset_name": "Trips", "client_code": "ACME_01", "requested_format": "csv", "status": "completed", "row_count": 7, "completed_at": "2026-07-01T10:02:00+00:00", "expires_at": future, "artifact_expires_at": future, "artifact_expired_at": None, "artifact_id": ready_artifact_id},
        {"job_id": "j4", "system_folder_id": folder_id, "dataset_name": "Trips", "client_code": "ACME_01", "requested_format": "csv", "status": "failed", "completed_at": "2026-07-01T10:03:00+00:00", "safe_error_message": "SELECT secret FROM internal_table", "artifact_id": None},
        {"job_id": "j5", "system_folder_id": folder_id, "dataset_name": "Trips", "client_code": "ACME_01", "requested_format": "csv", "status": "expired", "completed_at": "2026-07-01T10:04:00+00:00", "expires_at": past, "artifact_expires_at": past, "artifact_expired_at": "2026-07-04T10:04:00+00:00", "artifact_id": expired_artifact_id},
    ]
    old_schema = api_main._database_export_schema_available
    old_folder_schema = api_main._database_export_system_folder_schema_available
    old_get = api_main._get_database_export_system_folder_for_user
    old_get_id = api_main._get_database_export_system_folder_for_user_by_id
    old_list = api_main._list_database_export_jobs_for_user
    old_row = api_main._get_artifact_browser_row
    old_stream = api_main._stream_artifact_download
    old_access = api_main.user_can_access_artifact
    api_main._database_export_schema_available = lambda: True
    api_main._database_export_system_folder_schema_available = lambda: True
    api_main._get_database_export_system_folder_for_user = lambda user_id: dict(folder) if user_id == USER_ID else None
    api_main._get_database_export_system_folder_for_user_by_id = lambda user_id, requested_folder_id: dict(folder) if user_id == USER_ID and requested_folder_id == folder_id else None
    api_main._list_database_export_jobs_for_user = lambda user_id, limit=100: list(jobs) if user_id == USER_ID else []
    api_main._get_artifact_browser_row = lambda artifact_id: _artifact_route_row(artifact_id=artifact_id) if artifact_id == ready_artifact_id else None
    api_main._stream_artifact_download = lambda row, disposition="attachment": _StreamingResponse(b"ok", media_type="text/csv")
    api_main.user_can_access_artifact = lambda user, artifact, action: str(user.get("user_id")) == USER_ID and action == "download"
    try:
        user = {"user_id": USER_ID, "username": "alice", "is_admin": False, "is_active": True}
        response = api_main._user_database_export_system_folder_response(user, _FakeRequest("/user/reports/database-exports"), folder_id=folder_id)
        html = response.body.decode("utf-8")
        # Approved S8 presentation: the page is `Eksporty danych` and the list
        # has the four `DB-007` states. `queued`/`running` are one presented
        # state (`W toku`) because the difference is worker scheduling, not
        # something the user can act on differently; the five English labels
        # this used to assert were the internal statuses, not a user contract.
        assert api_main._tr("db.exports.title") in html, html
        assert all(label in html for label in ("W toku", "Gotowy", "Błąd", "Pliki wygasły")), html
        # Each state offers only the actions it can perform (`DB-50`): the ready
        # job downloads, the expired and failed ones requeue, the running ones
        # cancel — and no state renders a disabled control.
        assert "/cancel" in html and "/requeue" in html, html
        assert "disabled" not in html, html
        assert "SELECT secret" not in html, html
        assert api_main.DATABASE_EXPORT_GENERIC_FAILURE_MESSAGE in html, html
        assert html.count("/download") == 1, html
        assert ready_artifact_id in html and expired_artifact_id not in html.split("/download")[-1], html

        other = {"user_id": OTHER_ID, "username": "bob", "is_admin": False, "is_active": True}
        denied = api_main._user_database_export_system_folder_response(other, _FakeRequest("/user/reports/database-exports/" + folder_id), folder_id=folder_id)
        assert denied.status_code == 403, denied.status_code
        assert api_main._database_export_artifact_access(other, folder_id, ready_artifact_id) is None
        assert api_main._database_export_artifact_access(user, folder_id, ready_artifact_id) is not None
    finally:
        api_main._database_export_schema_available = old_schema
        api_main._database_export_system_folder_schema_available = old_folder_schema
        api_main._get_database_export_system_folder_for_user = old_get
        api_main._get_database_export_system_folder_for_user_by_id = old_get_id
        api_main._list_database_export_jobs_for_user = old_list
        api_main._get_artifact_browser_row = old_row
        api_main._stream_artifact_download = old_stream
        api_main.user_can_access_artifact = old_access
    print("PASS: system folder lifecycle rows are safe, owner-only, and expose only valid downloads")


def _test_database_exports_compatibility_route_and_reports_entry() -> None:
    folder = {"folder_id": "ffffffff-ffff-ffff-ffff-ffffffffffff", "owner_user_id": USER_ID}
    old_schema = api_main._database_export_schema_available
    old_folder_schema = api_main._database_export_system_folder_schema_available
    old_get = api_main._get_database_export_system_folder_for_user
    old_list_folders = api_main._list_accessible_portal_report_folders_for_user
    api_main._database_export_schema_available = lambda: True
    api_main._database_export_system_folder_schema_available = lambda: True
    api_main._get_database_export_system_folder_for_user = lambda user_id: dict(folder) if user_id == USER_ID else None
    api_main._list_accessible_portal_report_folders_for_user = lambda user_id: []
    try:
        user = {"user_id": USER_ID, "username": "alice", "is_admin": False, "is_active": True}
        legacy = api_main._user_database_exports_response(user, _FakeRequest("/user/database/exports"))
        assert legacy.status_code == 303, legacy.status_code
        assert legacy.headers.get("Location") == "/user/reports/database-exports/ffffffff-ffff-ffff-ffff-ffffffffffff", legacy.headers
        reports = api_main._user_reports_response(user).body.decode("utf-8")
        assert api_main.DATABASE_EXPORT_SYSTEM_FOLDER_NAME in reports, reports
        assert "/user/reports/database-exports/ffffffff-ffff-ffff-ffff-ffffffffffff" in reports, reports
    finally:
        api_main._database_export_schema_available = old_schema
        api_main._database_export_system_folder_schema_available = old_folder_schema
        api_main._get_database_export_system_folder_for_user = old_get
        api_main._list_accessible_portal_report_folders_for_user = old_list_folders
    print("PASS: /user/database/exports remains compatible and Reports tab links to the canonical system folder")



# ===========================================================================
# S13 FINAL CLOSURE — the background export replays the DISPLAYED filter
# universe exactly, or refuses.
#
# THE DEFECT. The export snapshot was converted back into a request by a
# hand-written converter that trimmed every value, dropped the ones that became
# empty and emitted `in` lists through `filter__` — the HUMAN textarea parameter,
# which trims and splits on newlines. A view whose exact filter carried a leading
# space, a trailing space, whitespace only or an embedded newline therefore
# DISPLAYED one population and EXPORTED a broader one, and a value list that
# emptied dropped the whole constraint.
#
# THE CONTRACT. `visible query semantics == exported query semantics`, proven by
# building the real rows query from the live parameters and from the replayed
# snapshot and requiring the two to be the same statement with the same bound
# values. Anything that cannot replay to exactly its stored meaning is refused
# BEFORE a client-database connection is opened.
# ===========================================================================
EXPORT_REPLAY_CASES = [
    ("whitespace-only exact value",
     "op__driver_name=in&filter_exact__driver_name=%20%20%20"),
    ("leading-space exact value",
     "op__driver_name=in&filter_exact__driver_name=%20Kowalski"),
    ("trailing-space exact value",
     "op__driver_name=in&filter_exact__driver_name=Kowalski%20"),
    ("multiple exact values",
     "op__driver_name=in&filter_exact__driver_name=Kowalski&filter_exact__driver_name=Nowak"),
    ("delimiter characters inside one value",
     "op__driver_name=in&filter_exact__driver_name=Kowalski%2C%20Nowak"),
    ("wildcard characters",
     "op__driver_name=in&filter_exact__driver_name=%25Kowal_ski%25"),
    ("newline inside one exact value",
     "op__driver_name=in&filter_exact__driver_name=Kowalski%0ANowak"),
    ("unicode",
     "op__driver_name=in&filter_exact__driver_name=%C5%BB%C3%B3%C5%82%C4%87%20%E6%97%A5%E6%9C%AC"),
    ("mixed exact values in one list",
     "op__driver_name=in&filter_exact__driver_name=%20&filter_exact__driver_name=Nowak"
     "&filter_exact__driver_name=Kowalski%20"),
    ("empty-string exact value",
     "op__driver_name=in&filter_exact__driver_name="),
    ("ordinary single-value contains",
     "filter__driver_name=Kowalski&op__driver_name=contains"),
    ("numeric between",
     "op__amount=between&filter__amount=10&filter_to__amount=20"),
    ("valueless blank", "op__driver_name=blank"),
    ("global search", "search=Kowalski"),
    ("date range",
     "dateop__trip_date=range&date_from__trip_date=2026-05-01&date_to__trip_date=2026-05-31"),
    ("exact value plus global search",
     "op__driver_name=in&filter_exact__driver_name=%20Kowalski&search=Kowalski"),
    ("exact value plus a date range and a sort",
     "op__driver_name=in&filter_exact__driver_name=%20Kowalski"
     "&dateop__trip_date=range&date_from__trip_date=2026-05-01&date_to__trip_date=2026-05-31"
     "&sort=amount&direction=desc"),
]


def _test_background_export_replays_the_displayed_filter_universe() -> None:
    dataset, columns = _dataset(), _columns()
    for label, query in EXPORT_REPLAY_CASES:
        params = parse_qs(query, keep_blank_values=True)
        shown_query, shown_values, shown_state, shown_error = api_main._build_portal_database_rows_query(
            dataset, columns, params, unbounded=True,
        )
        assert shown_error is None, (label, shown_error)
        snapshot, _state, snapshot_error = api_main._portal_database_canonical_export_snapshot(
            dataset, columns, params, format_name="csv",
        )
        assert snapshot_error is None and snapshot is not None, (label, snapshot_error)
        replay, refusal = api_main._portal_database_export_snapshot_params(dataset, columns, snapshot)
        assert refusal is None and replay is not None, (label, refusal)
        export_query, export_values, export_state, export_error = api_main._build_portal_database_rows_query(
            dataset, columns, replay, unbounded=True,
        )
        assert export_error is None, (label, export_error)
        # The decisive equality: same statement, same bound values, same
        # canonical record set. A trimmed, split or dropped value fails here.
        assert export_query == shown_query, (label, shown_query, export_query)
        assert export_values == shown_values, (label, shown_values, export_values)
        assert export_state.get("active_filters") == shown_state.get("active_filters"), label
        assert export_state.get("sort") == shown_state.get("sort"), label
        assert export_state.get("direction") == shown_state.get("direction"), label
    print(f"PASS: {len(EXPORT_REPLAY_CASES)} displayed filter shapes export with identical query semantics")


REVIEWED_COMMIT = "722fc437595920afb0780955b8d3a5c052425c86"


def _test_the_reviewed_converter_broadened_a_whitespace_exact_filter() -> None:
    """RED on the reviewed commit, by executing the converter it shipped.

    The correction cannot be demonstrated by the corrected code alone, so the
    superseded function is loaded out of `722fc43` and run against the same
    stored record. It drops the constraint entirely, which is a BROADER export.
    """
    result = subprocess.run(
        ["git", "show", f"{REVIEWED_COMMIT}:api/main.py"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        print("SKIP: the reviewed commit is not reachable in this checkout")
        return
    source = result.stdout
    start = source.index("def _portal_database_snapshot_to_params(")
    end = source.index("\ndef ", start + 1)
    namespace = {"PORTAL_DATABASE_VALUELESS_OPERATORS": api_main.PORTAL_DATABASE_VALUELESS_OPERATORS}
    exec(compile(source[start:end], "<722fc43>", "exec"), namespace)  # noqa: S102
    reviewed = namespace["_portal_database_snapshot_to_params"]

    stored = {"column_name": "driver_name", "operator": "in", "value": "   ", "values": ["   "]}
    dataset, columns = _dataset(), _columns()

    old_params = reviewed({"filters": [stored]})
    old_conditions, old_values, _a, old_error, _e = api_main._build_portal_database_filter_conditions(
        dataset, columns, old_params,
    )
    assert old_error is None, old_error
    assert old_conditions == [], ("the reviewed converter kept the constraint", old_conditions)
    assert old_values == [], old_values

    new_params, refusal = api_main._portal_database_export_snapshot_params(
        dataset, columns, {"filters": [stored]},
    )
    assert refusal is None and new_params is not None, refusal
    new_conditions, new_values, _a2, new_error, _e2 = api_main._build_portal_database_filter_conditions(
        dataset, columns, new_params,
    )
    assert new_error is None, new_error
    assert new_conditions == ['CAST("driver_name" AS TEXT) IN (%s)'], new_conditions
    assert new_values == ["   "], new_values
    print("PASS: the reviewed converter dropped a whitespace-only exact filter; the corrected one carries it verbatim")


def _non_filterable_columns():
    columns = _columns()
    for column in columns:
        if column["column_name"] == "driver_name":
            column["is_filterable"] = False
    return columns


LOSSY_EXPORT_SNAPSHOTS = [
    ("a stored filter that is not a record", _dataset, _columns,
     {"filters": ["op__driver_name=in"]}),
    ("a stored value that is not its own canonical form", _dataset, _columns,
     {"filters": [{"column_name": "driver_name", "operator": "contains", "value": " Kowalski "}]}),
    ("an `in` record that disagrees with its own list", _dataset, _columns,
     {"filters": [{"column_name": "driver_name", "operator": "in", "value": "Nowak", "values": ["Kowalski"]}]}),
    ("an `in` list holding one value twice", _dataset, _columns,
     {"filters": [{"column_name": "driver_name", "operator": "in", "value": "Kowalski", "values": ["Kowalski", "Kowalski"]}]}),
    ("two unmergeable records for one column", _dataset, _columns,
     {"filters": [{"column_name": "driver_name", "operator": "contains", "value": "Kowalski"},
                  {"column_name": "driver_name", "operator": "contains", "value": "Nowak"}]}),
    ("a column that no longer exists", _dataset, _columns,
     {"filters": [{"column_name": "ghost_column", "operator": "contains", "value": "Kowalski"}]}),
    ("a column that is no longer filterable", _dataset, _non_filterable_columns,
     {"filters": [{"column_name": "driver_name", "operator": "in", "value": "Kowalski", "values": ["Kowalski"]}]}),
    ("an operator the column family no longer offers", _dataset, _columns,
     {"filters": [{"column_name": "amount", "operator": "in", "value": "10", "values": ["10"]}]}),
    ("a valueless record carrying a value", _dataset, _columns,
     {"filters": [{"column_name": "driver_name", "operator": "blank", "value": "Kowalski"}]}),
    ("a search that is not its own trimmed form", _dataset, _columns,
     {"search": " Kowalski "}),
    ("revoked can_filter_rows", lambda: _dataset(can_filter_rows=False), _columns,
     {"filters": [{"column_name": "driver_name", "operator": "in", "value": "Kowalski", "values": ["Kowalski"]}]}),
]


def _test_a_lossy_export_snapshot_refuses_before_any_dataset_read() -> None:
    saved_connect = api_main._connect_portal_client_database

    def _forbidden(*args, **kwargs):
        raise AssertionError("a refused export opened a client-database connection")

    api_main._connect_portal_client_database = _forbidden
    try:
        for label, dataset_factory, columns_factory, snapshot in LOSSY_EXPORT_SNAPSHOTS:
            dataset, columns = dataset_factory(), columns_factory()
            params, refusal = api_main._portal_database_export_snapshot_params(dataset, columns, snapshot)
            assert params is None, (label, params)
            assert refusal == api_main.PORTAL_DATABASE_EXPORT_REPLAY_REFUSAL, (label, refusal)
            # The refusal is a user-safe sentence: it names no column, no
            # operator, no value and no SQL.
            assert "driver_name" not in refusal and "SELECT" not in refusal.upper(), (label, refusal)
            assert "Kowalski" not in refusal, (label, refusal)
            # And the real generation path refuses without reading anything.
            rows = api_main._portal_database_iter_export_rows(dataset, columns, dict(snapshot))
            try:
                next(rows)
            except ValueError as exc:
                assert str(exc) == api_main.PORTAL_DATABASE_EXPORT_REPLAY_REFUSAL, (label, exc)
            else:
                raise AssertionError(f"{label}: the export produced rows from a refused snapshot")
    finally:
        api_main._connect_portal_client_database = saved_connect
    print(f"PASS: {len(LOSSY_EXPORT_SNAPSHOTS)} lossy export snapshots fail closed before any dataset read")


def _test_worker_refuses_a_lossy_snapshot_before_generating() -> None:
    job = _running_worker_job()
    job["request_snapshot_json"] = {
        "columns": ["trip_date", "driver_name", "amount"],
        "filters": [{"column_name": "driver_name", "operator": "contains", "value": " Kowalski "}],
    }
    state = {"job": job, "artifacts": [], "attempts": [_attempt_row(job, "active")]}

    def scenario(_fake_s3, _audits):
        def refused():
            # The canonical converter and the query path stay REAL: this proves
            # the worker itself refuses, not a stub.
            api_main._portal_database_export_snapshot_params = saved_params
            api_main._portal_database_iter_export_rows = _forbidden_rows
            return worker.process_job(job)

        logs, result = _capture_worker_logs(lambda: _with_worker_process_helpers(refused))
        assert result is False
        assert "stage=query" in logs, logs
        assert job["safe_error_code"] == "EXPORT_REQUEST_INVALID", job
        assert job["safe_error_message"] == api_main.PORTAL_DATABASE_EXPORT_REPLAY_REFUSAL, job
        assert "driver_name" not in job["safe_error_message"], job

    saved_params = api_main._portal_database_export_snapshot_params

    def _forbidden_rows(*args, **kwargs):
        raise AssertionError("the worker generated rows from a refused snapshot")

    _with_fake_worker_state(state, scenario)
    print("PASS: the background worker refuses a lossy snapshot at the query stage and never generates rows")


def main() -> None:
    _test_canonical_snapshot()
    _test_background_export_replays_the_displayed_filter_universe()
    _test_the_reviewed_converter_broadened_a_whitespace_exact_filter()
    _test_a_lossy_export_snapshot_refuses_before_any_dataset_read()
    _test_report_207_quoted_polish_columns_async_snapshot_and_serializers()
    _test_streaming_serializers_and_cap()
    _test_pre043_schema_capability_and_route_guards()
    _test_schema_capability_metadata_probe()
    _test_route_method_contract_and_prg()
    _test_post043_enqueue_and_exports_routes()
    _test_background_export_row_count_thresholds()
    _test_direct_guessed_async_artifact_routes_denied()
    _test_owner_and_expiry_access()
    _test_worker_claim_and_source_invariants()
    _test_schema_bootstrap_and_migration_contract()
    _test_database_export_system_folder_provisioning()
    _test_database_export_system_folder_lifecycle_ui_and_auth()
    _test_database_exports_compatibility_route_and_reports_entry()
    _test_systemd_proposed_invocations_import_api()
    _test_worker_atomic_publication_and_owner_visibility()
    _test_worker_event_logger_allows_only_static_safe_fields()
    _test_worker_snapshot_validation_exception_is_query_failure()
    _test_worker_refuses_a_lossy_snapshot_before_generating()
    _test_worker_upload_failure_structured_logging_and_redaction()
    _test_worker_portal_safe_failure_remains_generic_and_continues()
    _test_worker_publication_exception_structured_logging()
    _test_worker_publication_failure_rolls_back_artifact_and_keeps_cleanup_ledger()
    _test_worker_idle_loop_stop_event_exits_promptly()
    _test_worker_stop_request_prevents_next_claim()
    _test_worker_stop_during_generation_recovers_without_publication()
    _test_worker_stop_after_upload_cleanup_before_publication()
    _test_worker_stale_recovery_successful_cleanup_not_reselected()
    _test_worker_stale_attempt_cannot_publish_over_reclaimed_claim()
    _test_worker_cleanup_failure_structured_logging_and_redaction()
    _test_failed_cleanup_record_remains_retryable()
    _test_missing_unpublished_attempt_object_finalizes_when_storage_reachable()
    _test_missing_object_does_not_finalize_while_the_producer_may_still_upload()
    _test_cancelled_inflight_upload_cannot_orphan_an_object()
    _test_cleanup_unpublished_attempt_object()
    print("PASS: async Database Explorer export tests complete")


if __name__ == "__main__":
    main()
