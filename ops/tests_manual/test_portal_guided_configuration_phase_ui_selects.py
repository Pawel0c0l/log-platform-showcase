#!/usr/bin/env python3
"""Manual tests for guided Portal/Admin configuration controls.

Run:

    cd /opt/log-platform
    env PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_portal_guided_configuration_phase_ui_selects.py
"""
from __future__ import annotations

import sys
import types
from pathlib import Path


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
        self.body = body
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
    def __init__(self, path="/admin/client-access/database", query=""):
        self.path = path
        self.query = query


class _FakeRequest:
    def __init__(self, *, path="/admin/client-access/database", query="", cookies=None):
        self.url = _FakeUrl(path, query)
        self.cookies = cookies or {}


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


def _client(**overrides):
    data = {
        "client_code": "ACME_01",
        "display_name": "Acme Logistics",
        "description": "Primary test client",
        "database_name": "acme_main",
        "is_active": True,
        "assigned_users": 1,
    }
    data.update(overrides)
    return data


def _dataset(**overrides):
    data = {
        "dataset_id": DATASET_ID,
        "client_code": "ACME_01",
        "client_display_name": "Acme Logistics",
        "client_database_name": "acme_main",
        "dataset_name": "Approved trips",
        "slug": "approved-trips",
        "description": "Approved portal dataset",
        "schema_name": "public",
        "table_name": "trips",
        "default_date_column": "trip_start_time",
        "is_active": True,
        "visible_columns": 1,
        "assigned_users": 0,
    }
    data.update(overrides)
    return data


def _catalog_column(name="trip_id", data_type="integer", **overrides):
    data = {
        "dataset_id": DATASET_ID,
        "column_name": name,
        "display_name": api_main._portal_database_column_display_label(name),
        "data_type": data_type,
        "is_visible": True,
        "is_filterable": True,
        "is_sortable": True,
        "is_default_date_column": False,
        "display_order": 10,
    }
    data.update(overrides)
    return data


def _physical_columns():
    return [
        {"column_name": "trip_id", "data_type": "integer", "normalized_data_type": "integer", "ordinal_position": 1, "is_nullable": "NO", "udt_name": "int4", "is_date_like": False},
        {"column_name": "registration", "data_type": "text", "normalized_data_type": "text", "ordinal_position": 2, "is_nullable": "YES", "udt_name": "text", "is_date_like": False},
        {"column_name": "trip_start_time", "data_type": "timestamp without time zone", "normalized_data_type": "timestamp", "ordinal_position": 3, "is_nullable": "YES", "udt_name": "timestamp", "is_date_like": True},
        {"column_name": "payload_json", "data_type": "jsonb", "normalized_data_type": "json", "ordinal_position": 4, "is_nullable": "YES", "udt_name": "jsonb", "is_date_like": False},
    ]


def _assignment_data(columns=None):
    return {
        "dataset": _dataset(),
        "assigned": [],
        "available": [],
        "assigned_groups": [],
        "available_groups": [],
        "columns": columns if columns is not None else [_catalog_column("trip_id", "integer")],
    }


def _patch(name, value):
    old = getattr(api_main, name)
    setattr(api_main, name, value)
    return old


def _restore(patches):
    for name, old in reversed(patches):
        setattr(api_main, name, old)


def _admin_patches(extra=None):
    patches = [("get_current_artifact_user", _patch("get_current_artifact_user", lambda request: _user(admin=True, user_id=ADMIN_ID, username="admin")))]
    if extra:
        patches.extend(extra)
    return patches


def _test_type_normalization() -> None:
    cases = {
        "character varying": "text",
        "varchar": "text",
        "integer": "integer",
        "bigint": "integer",
        "numeric": "numeric",
        "double precision": "numeric",
        "date": "date",
        "timestamp without time zone": "timestamp",
        "timestamp with time zone": "timestamp",
        "boolean": "boolean",
        "jsonb": "json",
    }
    for raw, expected in cases.items():
        assert api_main._normalize_portal_database_data_type(raw) == expected, raw
    print("PASS: data type normalization covers text/integer/numeric/date/timestamp/boolean/json")


def _test_physical_discovery_uses_registered_dataset_and_bound_params() -> None:
    calls = []

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=None):
            calls.append((sql, params))

        def fetchall(self):
            return [
                {"column_name": "registration", "data_type": "text", "ordinal_position": 1, "is_nullable": "YES", "udt_name": "text"},
                {"column_name": "trip_start_time", "data_type": "timestamp without time zone", "ordinal_position": 2, "is_nullable": "YES", "udt_name": "timestamp"},
            ]

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def cursor(self):
            return _Cursor()

    patches = [("_connect_portal_client_database", _patch("_connect_portal_client_database", lambda database_name: _Conn()))]
    try:
        columns = api_main._list_portal_database_physical_columns(_dataset(schema_name="public", table_name="trips"))
    finally:
        _restore(patches)
    sql, params = calls[0]
    assert "information_schema.columns" in sql, sql
    assert params == ("public", "trips"), params
    assert "public.trips" not in sql and "%s" in sql, sql
    assert columns[1]["normalized_data_type"] == "timestamp" and columns[1]["is_date_like"] is True, columns
    print("PASS: physical column discovery uses registered schema/table through bound parameters")


def _test_admin_detail_renders_physical_column_picker_without_row_values() -> None:
    patches = _admin_patches([
        ("_get_portal_database_dataset", _patch("_get_portal_database_dataset", lambda dataset_id: _dataset() if dataset_id == DATASET_ID else None)),
        ("_safe_list_portal_database_physical_columns", _patch("_safe_list_portal_database_physical_columns", lambda dataset: (_physical_columns(), None))),
        ("_list_portal_database_dataset_columns", _patch("_list_portal_database_dataset_columns", lambda dataset_id: [_catalog_column("trip_id", "integer"), _catalog_column("trip_start_time", "timestamp", is_default_date_column=True, display_order=20)])),
        ("_get_portal_database_dataset_assignment_data", _patch("_get_portal_database_dataset_assignment_data", lambda dataset_id: _assignment_data(columns=[_catalog_column("trip_id", "integer")]))),
        ("_list_portal_clients", _patch("_list_portal_clients", lambda: [_client()])),
    ])
    try:
        page = api_main.admin_portal_database_dataset_detail(DATASET_ID, _FakeRequest(path=f"/admin/client-access/database/{DATASET_ID}"))
    finally:
        _restore(patches)
    html = _html(page)
    assert page.status_code == 200, page.status_code
    assert "Available physical columns" in html and 'name="column_names" multiple' in html, html
    assert '<option value="registration">registration - text</option>' in html, html
    assert '<option value="trip_id">' not in html, html
    assert 'id="data_type" name="data_type"' in html and '<option value="timestamp"' in html, html
    assert 'id="default_date_column" name="default_date_column"' in html and "trip_start_time - timestamp" in html, html
    assert "TOP SECRET ROW VALUE" not in html and "storage_key" not in html and "dsn" not in html.lower(), html
    print("PASS: admin dataset detail renders guided picker, excludes cataloged columns, and does not render row values")


def _test_guided_route_is_admin_only() -> None:
    old = _patch("get_current_artifact_user", lambda request: None)
    try:
        unauth = api_main.admin_portal_add_discovered_database_dataset_columns(DATASET_ID, _FakeRequest(), column_names=["registration"])
    finally:
        api_main.get_current_artifact_user = old
    assert unauth.status_code == 303, unauth.status_code

    old = _patch("get_current_artifact_user", lambda request: _user(admin=False))
    try:
        forbidden = api_main.admin_portal_add_discovered_database_dataset_columns(DATASET_ID, _FakeRequest(), column_names=["registration"])
    finally:
        api_main.get_current_artifact_user = old
    assert forbidden.status_code == 403 and "Access denied" in _html(forbidden), _html(forbidden)
    print("PASS: non-admin and unauthenticated users cannot use guided column add")


def _test_guided_multi_add_creates_multiple_catalog_columns() -> None:
    calls = []

    def fake_upsert(**kwargs):
        calls.append(kwargs)
        return None

    patches = _admin_patches([
        ("_get_portal_database_dataset", _patch("_get_portal_database_dataset", lambda dataset_id: _dataset() if dataset_id == DATASET_ID else None)),
        ("_list_portal_database_physical_columns", _patch("_list_portal_database_physical_columns", lambda dataset: _physical_columns())),
        ("_list_portal_database_dataset_columns", _patch("_list_portal_database_dataset_columns", lambda dataset_id: [_catalog_column("trip_id", "integer")])),
        ("_upsert_portal_database_dataset_column", _patch("_upsert_portal_database_dataset_column", fake_upsert)),
        ("_portal_audit_event_safe", _patch("_portal_audit_event_safe", lambda **kwargs: None)),
    ])
    try:
        response = api_main.admin_portal_add_discovered_database_dataset_columns(
            DATASET_ID,
            _FakeRequest(),
            column_names=["registration", "trip_start_time"],
            is_visible="1",
            is_filterable="1",
            is_sortable="1",
            default_date_column="trip_start_time",
        )
    finally:
        _restore(patches)
    assert response.status_code == 303, response.status_code
    assert [call["column_name"] for call in calls] == ["registration", "trip_start_time"], calls
    assert calls[0]["display_name"] == "Registration" and calls[0]["data_type"] == "text", calls
    assert calls[1]["display_name"] == "Trip Start Time" and calls[1]["data_type"] == "timestamp", calls
    assert calls[1]["is_default_date_column"] is True, calls
    assert calls[0]["display_order"] == 20 and calls[1]["display_order"] == 30, calls
    print("PASS: selecting multiple discovered columns creates multiple catalog rows with generated labels and appended order")


def _test_guided_multi_add_validation() -> None:
    patches = [
        ("_get_portal_database_dataset", _patch("_get_portal_database_dataset", lambda dataset_id: _dataset() if dataset_id == DATASET_ID else None)),
        ("_list_portal_database_physical_columns", _patch("_list_portal_database_physical_columns", lambda dataset: _physical_columns())),
        ("_list_portal_database_dataset_columns", _patch("_list_portal_database_dataset_columns", lambda dataset_id: [_catalog_column("trip_id", "integer")])),
    ]
    try:
        empty_added, empty_error = api_main._add_portal_database_dataset_columns_from_physical(
            dataset_id=DATASET_ID,
            column_names=[],
            is_visible=True,
            is_filterable=True,
            is_sortable=True,
        )
        dupe_added, dupe_error = api_main._add_portal_database_dataset_columns_from_physical(
            dataset_id=DATASET_ID,
            column_names=["trip_id"],
            is_visible=True,
            is_filterable=True,
            is_sortable=True,
        )
        default_added, default_error = api_main._add_portal_database_dataset_columns_from_physical(
            dataset_id=DATASET_ID,
            column_names=["registration"],
            is_visible=True,
            is_filterable=True,
            is_sortable=True,
            default_date_column="registration",
        )
    finally:
        _restore(patches)
    assert not empty_added and "Select at least one" in empty_error, empty_error
    assert not dupe_added and "already in the portal catalog" in dupe_error, dupe_error
    assert not default_added and "date or timestamp" in default_error, default_error
    print("PASS: guided add validates empty selections, already-cataloged columns, and non-date defaults")


def _test_default_date_selector_only_offers_date_like_columns() -> None:
    html = api_main._portal_database_default_date_select(
        selected=None,
        catalog_columns=[_catalog_column("registration", "text"), _catalog_column("trip_start_time", "timestamp")],
        physical_columns=_physical_columns(),
    )
    assert "trip_start_time - timestamp" in html, html
    assert "registration -" not in html, html
    print("PASS: default date selector only offers date/time-like catalog columns")


def _test_manual_column_add_still_works() -> None:
    calls = []

    def fake_upsert(**kwargs):
        calls.append(kwargs)
        return None

    patches = _admin_patches([
        ("_get_portal_database_dataset", _patch("_get_portal_database_dataset", lambda dataset_id: _dataset() if dataset_id == DATASET_ID else None)),
        ("_upsert_portal_database_dataset_column", _patch("_upsert_portal_database_dataset_column", fake_upsert)),
        ("_list_portal_database_dataset_columns", _patch("_list_portal_database_dataset_columns", lambda dataset_id: [])),
        ("_portal_audit_event_safe", _patch("_portal_audit_event_safe", lambda **kwargs: None)),
    ])
    try:
        response = api_main.admin_portal_save_database_dataset_column(
            DATASET_ID,
            _FakeRequest(),
            column_name="manual_column",
            display_name="Manual Column",
            data_type="double precision",
            display_order=100,
            is_visible="1",
            is_filterable="1",
            is_sortable=None,
            is_default_date_column=None,
        )
    finally:
        _restore(patches)
    assert response.status_code == 303, response.status_code
    assert calls[0]["column_name"] == "manual_column" and calls[0]["data_type"] == "numeric", calls
    assert calls[0]["is_sortable"] is False, calls
    print("PASS: existing manual single-column add/update still works with normalized data type")


def _test_report_folder_filter_suggestions_render() -> None:
    patches = [("_get_artifact_browser_facets", _patch("_get_artifact_browser_facets", lambda: {"workflow_name": ["workflow-a"], "report_type": ["monthly"], "file_ext": ["pdf"], "tags": ["finance"]}))]
    try:
        html = api_main._portal_report_filter_form_fields({"search_query_json": {"report_type": ["monthly"]}})
    finally:
        _restore(patches)
    assert 'list="workflow_name_options"' in html and '<option value="workflow-a"></option>' in html, html
    assert '<option value="monthly"></option>' in html and '<option value="finance"></option>' in html, html
    print("PASS: report folder filter fields render safe existing-value suggestions")


def _test_permission_semantics_helpers_unchanged() -> None:
    assert hasattr(api_main, "_list_accessible_portal_database_datasets_for_user")
    assert hasattr(api_main, "_get_accessible_portal_report_folder_for_user")
    assert hasattr(api_main, "_portal_report_artifact_access")
    print("PASS: permission/effective access helper entry points remain present")


def main() -> None:
    _test_type_normalization()
    _test_physical_discovery_uses_registered_dataset_and_bound_params()
    _test_admin_detail_renders_physical_column_picker_without_row_values()
    _test_guided_route_is_admin_only()
    _test_guided_multi_add_creates_multiple_catalog_columns()
    _test_guided_multi_add_validation()
    _test_default_date_selector_only_offers_date_like_columns()
    _test_manual_column_add_still_works()
    _test_report_folder_filter_suggestions_render()
    _test_permission_semantics_helpers_unchanged()


if __name__ == "__main__":
    main()
