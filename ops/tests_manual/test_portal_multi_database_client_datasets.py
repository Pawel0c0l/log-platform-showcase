#!/usr/bin/env python3
"""Manual tests for multi-database Portal Database Explorer support.

Run:

    cd /opt/log-platform
    env PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_portal_multi_database_client_datasets.py
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


class _HTMLResponse:
    def __init__(self, content, status_code=200, headers=None, media_type=None):
        self.body = str(content).encode("utf-8")
        self.status_code = status_code
        self.headers = headers or {}
        self.media_type = media_type or "text/html"


class _StreamingResponse:
    def __init__(self, body, media_type=None, headers=None):
        self.body = body
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

ADMIN_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
USER_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
DATASET_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"


class _FakeUrl:
    def __init__(self, path="/", query=""):
        self.path = path
        self.query = query


class _FakeRequest:
    def __init__(self, *, path="/", query="", cookies=None):
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
        "client_code": "ALPHA00001",
        "display_name": "Alpha",
        "description": "Alpha test client",
        "database_name": "alpha_main",
        "is_active": True,
        "assigned_users": 1,
    }
    data.update(overrides)
    return data


def _dataset(**overrides):
    data = {
        "dataset_id": DATASET_ID,
        "client_code": "ALPHA00001",
        "client_display_name": "Alpha",
        "client_database_name": "alpha_main",
        "dataset_name": "Client trips",
        "slug": "client-trips",
        "description": "Trips from client DB",
        "schema_name": "public",
        "table_name": "client_trips",
        "default_date_column": "start_timestamp",
        "is_active": True,
        "visible_columns": 2,
        "assigned_users": 1,
        "can_view_rows": True,
        "can_filter_rows": True,
        "can_export_rows": True,
    }
    data.update(overrides)
    return data


def _column(name="record_id", data_type="text", **overrides):
    data = {
        "dataset_id": DATASET_ID,
        "column_name": name,
        "display_name": api_main._portal_database_column_display_label(name),
        "data_type": data_type,
        "is_visible": True,
        "is_filterable": True,
        "is_sortable": True,
        "is_default_date_column": name == "start_timestamp",
        "display_order": 10,
    }
    data.update(overrides)
    return data


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


class _ClientCursor:
    def __init__(self, calls):
        self.calls = calls
        self.rows = []
        self.one = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if "information_schema.schemata" in sql:
            self.rows = [{"schema_name": "public"}, {"schema_name": "telematics_reports"}]
        elif "information_schema.tables" in sql and "LIMIT 1" not in sql:
            self.rows = [{"table_name": "client_trips", "table_type": "BASE TABLE"}]
        elif "information_schema.columns" in sql:
            self.rows = [
                {"column_name": "record_id", "data_type": "uuid", "ordinal_position": 1, "is_nullable": "NO", "udt_name": "uuid"},
                {"column_name": "start_timestamp", "data_type": "timestamp without time zone", "ordinal_position": 2, "is_nullable": "YES", "udt_name": "timestamp"},
            ]
        elif "count(*) AS total" in sql:
            self.one = {"total": 1}
        elif "FROM" in sql:
            self.rows = [{"record_id": "r1", "start_timestamp": "2026-05-01T08:00:00"}]
        else:
            self.rows = []

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.one


class _ClientConn:
    def __init__(self, calls):
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return _ClientCursor(self.calls)


def _test_database_name_validation() -> None:
    assert api_main._validate_portal_client_database_name("alpha_main") is None
    assert api_main._validate_portal_client_database_name("client_alpha00001") is None
    assert api_main._validate_portal_client_database_name("alpha.main")
    assert api_main._validate_portal_client_database_name("alpha main")
    assert api_main._validate_portal_client_database_name("postgres://alpha")
    assert api_main._validate_portal_client_database_name("alpha_main;drop")
    print("PASS: portal client database_name validation accepts safe DB names and rejects DSNs/SQL-like values")


def _test_client_form_renders_database_name_field() -> None:
    response = api_main._portal_client_form_response(_user(admin=True), client=_client())
    html = _html(response)
    assert 'id="database_name" name="database_name"' in html and 'value="alpha_main"' in html, html
    assert "not a DSN" in html and "must not contain credentials" in html, html
    assert "postgres://" not in html and "password" not in html.lower(), html
    print("PASS: admin client form renders safe database_name field and help text")


def _test_metadata_discovery_uses_client_database_connection() -> None:
    database_names = []
    calls = []
    patches = [
        ("_get_portal_client", _patch("_get_portal_client", lambda client_code: _client() if client_code == "ALPHA00001" else None)),
        ("_connect_portal_client_database", _patch("_connect_portal_client_database", lambda database_name: database_names.append(database_name) or _ClientConn(calls))),
    ]
    try:
        schemas = api_main._list_portal_dataset_schemas("ALPHA00001")
        tables = api_main._list_portal_dataset_tables("ALPHA00001", "public")
        columns = api_main._list_portal_dataset_columns("ALPHA00001", "public", "client_trips")
    finally:
        _restore(patches)
    assert database_names == ["alpha_main", "alpha_main", "alpha_main"], database_names
    assert schemas == ["telematics_reports", "public"], schemas
    assert tables[0]["table_name"] == "client_trips", tables
    assert columns[1]["column_name"] == "start_timestamp" and columns[1]["is_date_like"] is True, columns
    assert all("information_schema" in sql or "SET " in sql for sql, _params in calls), calls
    print("PASS: schema/table/column discovery connects to mapped client database, not logdb")


def _test_dataset_create_warns_when_client_database_missing() -> None:
    patches = _admin_patches([
        ("_list_portal_clients", _patch("_list_portal_clients", lambda: [_client(database_name=None)])),
        ("_get_portal_client", _patch("_get_portal_client", lambda client_code: _client(database_name=None))),
    ])
    try:
        response = api_main.admin_portal_new_database_dataset_form(_FakeRequest(path="/admin/client-access/database/new", query="client_code=ALPHA00001"))
    finally:
        _restore(patches)
    html = _html(response)
    assert "does not have a database name configured" in html, html
    assert "Set the client database name before registering datasets" in html, html
    assert "postgres://" not in html and "password" not in html.lower(), html
    print("PASS: dataset create page shows sanitized admin diagnostic when selected client has no database mapping")


def _test_row_browsing_and_export_helpers_use_client_database() -> None:
    database_names = []
    calls = []
    patches = [("_connect_portal_client_database", _patch("_connect_portal_client_database", lambda database_name: database_names.append(database_name) or _ClientConn(calls)))]
    try:
        total, _state, error = api_main._count_portal_database_rows(_dataset(), [_column(), _column("start_timestamp", "timestamp")], {})
        rows, _state, row_error = api_main._list_portal_database_rows(_dataset(), [_column(), _column("start_timestamp", "timestamp")], {}, limit=10, offset=0)
    finally:
        _restore(patches)
    assert error is None and row_error is None, (error, row_error)
    assert total == 1 and rows[0]["record_id"] == "r1", (total, rows)
    assert database_names == ["alpha_main", "alpha_main"], database_names
    assert any('FROM "public"."client_trips"' in sql for sql, _params in calls), calls
    assert not any("logdb" in sql.lower() for sql, _params in calls), calls
    print("PASS: row count/list execution uses the mapped client database connection")


def _test_permissions_checked_before_user_row_connection() -> None:
    def fail_connect(database_name):
        raise AssertionError("client database should not be opened before access is granted")

    patches = [
        ("get_current_artifact_user", _patch("get_current_artifact_user", lambda request: _user(admin=False, user_id=USER_ID))),
        ("_get_portal_database_dataset_for_user", _patch("_get_portal_database_dataset_for_user", lambda dataset_id, user_id: None)),
        ("_connect_portal_client_database", _patch("_connect_portal_client_database", fail_connect)),
        ("_portal_audit_event_safe", _patch("_portal_audit_event_safe", lambda **kwargs: None)),
    ]
    try:
        rows = api_main.user_portal_database_dataset(DATASET_ID, _FakeRequest(path=f"/user/database/datasets/{DATASET_ID}"))
        export = api_main.user_portal_database_dataset_export(DATASET_ID, _FakeRequest(path=f"/user/database/datasets/{DATASET_ID}/export"))
    finally:
        _restore(patches)
    # Approved stage S9 permission state; the boundary itself is unchanged — the
    # client database is never opened for a dataset the account may not view.
    assert "Nie masz dost\u0119pu do tego zbioru danych" in _html(rows), _html(rows)
    assert "Nie masz dost\u0119pu do tego zbioru danych" in _html(export), _html(export)
    print("PASS: user-facing row/export routes enforce portal access before opening client DB")


def _test_admin_guided_add_still_admin_only_and_no_regular_metadata() -> None:
    old = _patch("get_current_artifact_user", lambda request: _user(admin=False))
    try:
        forbidden = api_main.admin_portal_add_discovered_database_dataset_columns(DATASET_ID, _FakeRequest(), column_names=["record_id"])
    finally:
        api_main.get_current_artifact_user = old
    assert forbidden.status_code == 403 and "Access denied" in _html(forbidden), _html(forbidden)
    assert not hasattr(api_main, "user_portal_database_metadata_schemas")
    assert not hasattr(api_main, "user_portal_database_metadata_tables")
    assert not hasattr(api_main, "user_portal_database_metadata_columns")
    print("PASS: regular users cannot access metadata discovery routes")


def _test_deactivated_dataset_still_denied() -> None:
    list_sql = []
    single_sql = []

    class _ListCursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=None):
            list_sql.append(sql)

        def fetchall(self):
            return []

    class _SingleCursor(_ListCursor):
        def execute(self, sql, params=None):
            single_sql.append(sql)

        def fetchone(self):
            return None

    class _Conn:
        def __init__(self, cursor_cls):
            self.cursor_cls = cursor_cls

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def cursor(self):
            return self.cursor_cls()

    patches = [("db_conn", _patch("db_conn", lambda: _Conn(_ListCursor)))]
    try:
        assert api_main._list_accessible_portal_database_datasets_for_user(USER_ID) == []
    finally:
        _restore(patches)
    patches = [("db_conn", _patch("db_conn", lambda: _Conn(_SingleCursor)))]
    try:
        assert api_main._get_portal_database_dataset_for_user(DATASET_ID, USER_ID) is None
    finally:
        _restore(patches)
    assert "pdd.is_active IS TRUE" in list_sql[0], list_sql[0]
    assert "pdd.is_active IS TRUE" in single_sql[0], single_sql[0]
    print("PASS: inactive/deactivated datasets remain hidden and denied")


def main() -> None:
    _test_database_name_validation()
    _test_client_form_renders_database_name_field()
    _test_metadata_discovery_uses_client_database_connection()
    _test_dataset_create_warns_when_client_database_missing()
    _test_row_browsing_and_export_helpers_use_client_database()
    _test_permissions_checked_before_user_row_connection()
    _test_admin_guided_add_still_admin_only_and_no_regular_metadata()
    _test_deactivated_dataset_still_denied()


if __name__ == "__main__":
    main()
