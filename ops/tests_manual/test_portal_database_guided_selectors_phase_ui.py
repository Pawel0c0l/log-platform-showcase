#!/usr/bin/env python3
"""Manual tests for Portal/Admin guided database dataset selectors.

Run:

    cd /opt/log-platform
    env PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_portal_database_guided_selectors_phase_ui.py
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
        "schema_name": "acme_ops",
        "table_name": "trips",
        "default_date_column": "trip_start_time",
        "is_active": True,
        "visible_columns": 1,
        "assigned_users": 1,
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
        "is_default_date_column": name == "trip_start_time",
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


class _MetadataCursor:
    def __init__(self, calls):
        self.calls = calls
        self.rowcount = 1
        self._rows = []
        self._one = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if "information_schema.schemata" in sql:
            self._rows = [{"schema_name": "public"}, {"schema_name": "acme_ops"}, {"schema_name": "other_client"}]
        elif "information_schema.tables" in sql and "LIMIT 1" not in sql:
            self._rows = [{"table_name": "trips", "table_type": "BASE TABLE"}, {"table_name": "drivers", "table_type": "VIEW"}]
        elif "information_schema.columns" in sql:
            self._rows = [
                {"column_name": "trip_id", "data_type": "integer", "ordinal_position": 1, "is_nullable": "NO", "udt_name": "int4"},
                {"column_name": "trip_start_time", "data_type": "timestamp without time zone", "ordinal_position": 2, "is_nullable": "YES", "udt_name": "timestamp"},
            ]
        elif "information_schema.tables" in sql and "LIMIT 1" in sql:
            self._one = {"?column?": 1}
        else:
            self._rows = []

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._one


class _MetadataConn:
    def __init__(self, calls):
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return _MetadataCursor(self.calls)


def _test_metadata_helpers_use_information_schema_and_bound_params() -> None:
    calls = []
    patches = [
        ("_get_portal_client", _patch("_get_portal_client", lambda client_code: _client() if client_code == "ACME_01" else None)),
        ("_connect_portal_client_database", _patch("_connect_portal_client_database", lambda database_name: _MetadataConn(calls))),
    ]
    try:
        schemas = api_main._list_portal_dataset_schemas("ACME_01")
        tables = api_main._list_portal_dataset_tables("ACME_01", "acme_ops")
        columns = api_main._list_portal_dataset_columns("ACME_01", "acme_ops", "trips")
    finally:
        _restore(patches)
    assert schemas[0] == "acme_ops" and "public" in schemas, schemas
    assert tables == [{"table_name": "trips", "table_type": "BASE TABLE"}, {"table_name": "drivers", "table_type": "VIEW"}], tables
    assert columns[1]["column_name"] == "trip_start_time" and columns[1]["is_date_like"] is True, columns
    assert "information_schema.schemata" in calls[0][0] and "pg_catalog" in calls[0][0], calls
    assert calls[1][1] == ("acme_ops",), calls
    assert calls[2][1] == ("acme_ops", "trips"), calls
    assert "acme_ops.trips" not in calls[2][0] and "%s" in calls[2][0], calls[2]
    print("PASS: schema/table/column metadata helpers use information_schema and bound params")


def _test_create_form_renders_cascading_selectors_and_date_options() -> None:
    patches = _admin_patches([
        ("_list_portal_clients", _patch("_list_portal_clients", lambda: [_client(), _client(client_code="INACTIVE", is_active=False)])),
        ("_list_portal_dataset_schemas", _patch("_list_portal_dataset_schemas", lambda client_code=None: ["acme_ops", "public"])),
        ("_list_portal_dataset_tables", _patch("_list_portal_dataset_tables", lambda client_code, schema_name: [{"table_name": "trips", "table_type": "BASE TABLE"}] if schema_name == "acme_ops" else [])),
        ("_list_portal_dataset_columns", _patch("_list_portal_dataset_columns", lambda client_code, schema_name, table_name: _physical_columns())),
    ])
    try:
        response = api_main.admin_portal_new_database_dataset_form(_FakeRequest(path="/admin/client-access/database/new", query="client_code=ACME_01&schema_name=acme_ops&table_name=trips"))
    finally:
        _restore(patches)
    html = _html(response)
    assert response.status_code == 200, response.status_code
    assert '<select id="client_code" name="client_code" required>' in html and "Acme Logistics (ACME_01)" in html, html
    assert "INACTIVE" not in html, html
    assert '<select id="schema_name" name="schema_name">' in html and '<option value="acme_ops" selected>acme_ops</option>' in html, html
    assert '<select id="table_name" name="table_name">' in html and '<option value="trips" selected>trips</option>' in html, html
    assert 'name="schema_name_manual"' in html and 'name="table_name_manual"' in html, html
    assert "Refresh metadata" in html, html
    assert "trip_start_time - timestamp" in html and "registration -" not in html, html
    print("PASS: create dataset form renders client/schema/table selectors, manual fallback, and date-only default options")


def _test_detail_page_renders_physical_columns_and_diagnostics_without_values() -> None:
    patches = _admin_patches([
        ("_get_portal_database_dataset", _patch("_get_portal_database_dataset", lambda dataset_id: _dataset() if dataset_id == DATASET_ID else None)),
        ("_safe_list_portal_database_physical_columns", _patch("_safe_list_portal_database_physical_columns", lambda dataset: (_physical_columns(), None))),
        ("_list_portal_database_dataset_columns", _patch("_list_portal_database_dataset_columns", lambda dataset_id: [_catalog_column("trip_id", "integer")])),
        ("_get_portal_database_dataset_assignment_data", _patch("_get_portal_database_dataset_assignment_data", lambda dataset_id: _assignment_data(columns=[_catalog_column("trip_id", "integer")]))),
        ("_list_portal_clients", _patch("_list_portal_clients", lambda: [_client()])),
        ("_list_portal_dataset_schemas", _patch("_list_portal_dataset_schemas", lambda client_code=None: ["acme_ops"])),
        ("_list_portal_dataset_tables", _patch("_list_portal_dataset_tables", lambda client_code, schema_name: [{"table_name": "trips", "table_type": "BASE TABLE"}])),
        ("_list_portal_dataset_columns", _patch("_list_portal_dataset_columns", lambda client_code, schema_name, table_name: _physical_columns())),
    ])
    try:
        response = api_main.admin_portal_database_dataset_detail(DATASET_ID, _FakeRequest(path=f"/admin/client-access/database/{DATASET_ID}"))
    finally:
        _restore(patches)
    html = _html(response)
    assert "Available physical columns" in html and 'name="column_names" multiple' in html, html
    assert '<option value="registration">registration - text</option>' in html, html
    assert '<option value="trip_id">' not in html, html
    assert "Deactivate dataset" in html and "does not delete the physical database table" in html, html
    assert "TOP SECRET ROW VALUE" not in html and "storage_key" not in html and "dsn" not in html.lower() and "select *" not in html.lower(), html

    diagnostic_patches = _admin_patches([
        ("_get_portal_database_dataset", _patch("_get_portal_database_dataset", lambda dataset_id: _dataset(schema_name="wrong_schema", table_name="wrong_table") if dataset_id == DATASET_ID else None)),
        ("_safe_list_portal_database_physical_columns", _patch("_safe_list_portal_database_physical_columns", lambda dataset: ([], None))),
        ("_list_portal_database_physical_columns", _patch("_list_portal_database_physical_columns", lambda dataset: [])),
        ("_portal_dataset_table_metadata_exists", _patch("_portal_dataset_table_metadata_exists", lambda client_code, schema_name, table_name: False)),
        ("_get_portal_database_dataset_assignment_data", _patch("_get_portal_database_dataset_assignment_data", lambda dataset_id: _assignment_data(columns=[]))),
        ("_list_portal_clients", _patch("_list_portal_clients", lambda: [_client()])),
        ("_list_portal_database_dataset_columns", _patch("_list_portal_database_dataset_columns", lambda dataset_id: [])),
        ("_list_portal_dataset_schemas", _patch("_list_portal_dataset_schemas", lambda client_code=None: ["acme_ops"])),
        ("_list_portal_dataset_tables", _patch("_list_portal_dataset_tables", lambda client_code, schema_name: [])),
        ("_list_portal_dataset_columns", _patch("_list_portal_dataset_columns", lambda client_code, schema_name, table_name: [])),
    ])
    try:
        diagnostic = api_main.admin_portal_database_dataset_detail(DATASET_ID, _FakeRequest(path=f"/admin/client-access/database/{DATASET_ID}"))
    finally:
        _restore(diagnostic_patches)
    diagnostic_html = _html(diagnostic)
    assert "No physical columns discovered." in diagnostic_html, diagnostic_html
    assert "Physical table metadata could not be found" in diagnostic_html, diagnostic_html
    assert "schema/table names are incorrect" in diagnostic_html, diagnostic_html
    assert "select *" not in diagnostic_html.lower() and "password" not in diagnostic_html.lower(), diagnostic_html
    print("PASS: dataset detail shows guided picker when available and sanitized diagnostics when metadata is missing")


def _test_discovery_state_distinguishes_all_cataloged_and_table_missing() -> None:
    patches = [
        ("_list_portal_database_physical_columns", _patch("_list_portal_database_physical_columns", lambda dataset: _physical_columns())),
    ]
    try:
        state = api_main._portal_database_column_discovery_state(
            _dataset(),
            [_catalog_column("trip_id", "integer"), _catalog_column("registration", "text"), _catalog_column("trip_start_time", "timestamp"), _catalog_column("payload_json", "json")],
        )
    finally:
        _restore(patches)
    assert state["status"] == "all_cataloged" and not state["available_columns"], state
    message = api_main._portal_database_discovery_message_html(state, _dataset())
    assert "All physical columns from this table are already cataloged" in message, message

    missing_patches = [
        ("_list_portal_database_physical_columns", _patch("_list_portal_database_physical_columns", lambda dataset: [])),
        ("_portal_dataset_table_metadata_exists", _patch("_portal_dataset_table_metadata_exists", lambda client_code, schema_name, table_name: False)),
    ]
    try:
        missing = api_main._portal_database_column_discovery_state(_dataset(), [])
    finally:
        _restore(missing_patches)
    assert missing["status"] == "table_not_found", missing
    print("PASS: discovery state distinguishes all-cataloged columns from missing physical table metadata")


def _test_guided_multi_add_and_manual_create_still_work() -> None:
    column_calls = []
    create_calls = []

    def fake_upsert(**kwargs):
        column_calls.append(kwargs)
        return None

    def fake_create(**kwargs):
        create_calls.append(kwargs)
        return DATASET_ID

    patches = _admin_patches([
        ("_get_portal_database_dataset", _patch("_get_portal_database_dataset", lambda dataset_id: _dataset() if dataset_id == DATASET_ID else None)),
        ("_list_portal_database_physical_columns", _patch("_list_portal_database_physical_columns", lambda dataset: _physical_columns())),
        ("_list_portal_database_dataset_columns", _patch("_list_portal_database_dataset_columns", lambda dataset_id: [_catalog_column("trip_id", "integer")])),
        ("_upsert_portal_database_dataset_column", _patch("_upsert_portal_database_dataset_column", fake_upsert)),
        ("_create_portal_database_dataset", _patch("_create_portal_database_dataset", fake_create)),
        ("_portal_audit_event_safe", _patch("_portal_audit_event_safe", lambda **kwargs: None)),
    ])
    try:
        added = api_main.admin_portal_add_discovered_database_dataset_columns(
            DATASET_ID,
            _FakeRequest(),
            column_names=["registration", "trip_start_time"],
            is_visible="1",
            is_filterable="1",
            is_sortable="1",
            default_date_column="trip_start_time",
        )
        created = api_main.admin_portal_create_database_dataset(
            _FakeRequest(path="/admin/client-access/database/new"),
            client_code="ACME_01",
            dataset_name="Manual Dataset",
            slug="manual-dataset",
            description="manual fallback",
            schema_name="__manual__",
            table_name="__manual__",
            schema_name_manual="manual_schema",
            table_name_manual="manual_table",
            default_date_column=None,
            is_active="1",
        )
    finally:
        _restore(patches)
    assert added.status_code == 303 and [call["column_name"] for call in column_calls] == ["registration", "trip_start_time"], column_calls
    assert column_calls[1]["data_type"] == "timestamp" and column_calls[1]["is_default_date_column"] is True, column_calls
    assert created.status_code == 303 and create_calls[0]["schema_name"] == "manual_schema" and create_calls[0]["table_name"] == "manual_table", create_calls
    print("PASS: multi-select discovered-column add and manual schema/table fallback still work")


def _test_admin_only_boundaries_for_guided_add_and_deactivation() -> None:
    old = _patch("get_current_artifact_user", lambda request: None)
    try:
        unauth_add = api_main.admin_portal_add_discovered_database_dataset_columns(DATASET_ID, _FakeRequest(), column_names=["registration"])
        unauth_deactivate = api_main.admin_portal_deactivate_database_dataset(DATASET_ID, _FakeRequest())
    finally:
        api_main.get_current_artifact_user = old
    assert unauth_add.status_code == 303 and unauth_deactivate.status_code == 303, (unauth_add.status_code, unauth_deactivate.status_code)

    old = _patch("get_current_artifact_user", lambda request: _user(admin=False))
    try:
        forbidden_add = api_main.admin_portal_add_discovered_database_dataset_columns(DATASET_ID, _FakeRequest(), column_names=["registration"])
        forbidden_deactivate = api_main.admin_portal_deactivate_database_dataset(DATASET_ID, _FakeRequest())
    finally:
        api_main.get_current_artifact_user = old
    assert forbidden_add.status_code == 403 and "Access denied" in _html(forbidden_add), _html(forbidden_add)
    assert forbidden_deactivate.status_code == 403 and "Access denied" in _html(forbidden_deactivate), _html(forbidden_deactivate)
    print("PASS: guided add and dataset deactivation are unavailable to unauthenticated/non-admin users")


def _test_deactivation_is_soft_delete_only() -> None:
    calls = []

    class _Cursor:
        rowcount = 1

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=None):
            calls.append((sql, params))

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def cursor(self):
            return _Cursor()

    patches = [
        ("_get_portal_database_dataset", _patch("_get_portal_database_dataset", lambda dataset_id: _dataset() if dataset_id == DATASET_ID else None)),
        ("db_conn", _patch("db_conn", lambda: _Conn())),
    ]
    try:
        error = api_main._deactivate_portal_database_dataset(dataset_id=DATASET_ID, actor_user_id=ADMIN_ID)
    finally:
        _restore(patches)
    sql = "\n".join(call[0] for call in calls).lower()
    assert error is None, error
    assert "update portal_database_datasets" in sql and "is_active = false" in sql, sql
    assert "drop " not in sql and "alter " not in sql and "delete " not in sql, sql
    assert calls[0][1] == (ADMIN_ID, DATASET_ID), calls
    print("PASS: dataset removal only deactivates portal catalog metadata and does not drop/alter/delete physical data")


def _test_inactive_dataset_access_is_hidden_and_denied() -> None:
    list_calls = []
    single_calls = []

    class _ListCursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=None):
            list_calls.append((sql, params))

        def fetchall(self):
            return []

    class _SingleCursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, params=None):
            single_calls.append((sql, params))

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
        visible = api_main._list_accessible_portal_database_datasets_for_user(USER_ID)
    finally:
        _restore(patches)
    assert visible == [], visible
    assert "pdd.is_active IS TRUE" in list_calls[0][0], list_calls[0][0]

    patches = [("db_conn", _patch("db_conn", lambda: _Conn(_SingleCursor)))]
    try:
        row = api_main._get_portal_database_dataset_for_user(DATASET_ID, USER_ID)
    finally:
        _restore(patches)
    assert row is None, row
    assert "pdd.is_active IS TRUE" in single_calls[0][0], single_calls[0][0]

    route_patches = [
        ("get_current_artifact_user", _patch("get_current_artifact_user", lambda request: _user(admin=False, user_id=USER_ID))),
        ("_list_accessible_portal_database_datasets_for_user", _patch("_list_accessible_portal_database_datasets_for_user", lambda user_id: [])),
        ("_get_portal_database_dataset_for_user", _patch("_get_portal_database_dataset_for_user", lambda dataset_id, user_id: None)),
        ("_portal_audit_event_safe", _patch("_portal_audit_event_safe", lambda **kwargs: None)),
    ]
    try:
        dashboard = api_main.user_portal_database(_FakeRequest(path="/user/database"))
        rows = api_main.user_portal_database_dataset(DATASET_ID, _FakeRequest(path=f"/user/database/datasets/{DATASET_ID}"))
        export = api_main.user_portal_database_dataset_export(DATASET_ID, _FakeRequest(path=f"/user/database/datasets/{DATASET_ID}/export"))
    finally:
        _restore(route_patches)
    # Approved stage S9 copy. The behaviour is unchanged: the dataset is absent
    # from the catalogue entirely and both deep links produce the permission state.
    assert "Nie masz przypisanych zbior\u00f3w danych" in _html(dashboard), _html(dashboard)
    assert "Nie masz dost\u0119pu do tego zbioru danych" in _html(rows), _html(rows)
    assert "Nie masz dost\u0119pu do tego zbioru danych" in _html(export), _html(export)
    print("PASS: inactive/unavailable datasets are hidden from user dashboard and denied for row browsing/export")


def _test_schema_table_change_blocked_when_catalog_columns_exist() -> None:
    patches = [
        ("_get_portal_database_dataset", _patch("_get_portal_database_dataset", lambda dataset_id: _dataset() if dataset_id == DATASET_ID else None)),
        ("_portal_database_dataset_client_error", _patch("_portal_database_dataset_client_error", lambda client_code: None)),
        ("_portal_database_dataset_slug_exists", _patch("_portal_database_dataset_slug_exists", lambda client_code, slug, exclude_dataset_id=None: False)),
        ("_portal_database_dataset_physical_exists", _patch("_portal_database_dataset_physical_exists", lambda client_code, schema_name, table_name, exclude_dataset_id=None: False)),
        ("_list_portal_database_dataset_columns", _patch("_list_portal_database_dataset_columns", lambda dataset_id: [_catalog_column("trip_id", "integer")])),
    ]
    try:
        error = api_main._update_portal_database_dataset(
            dataset_id=DATASET_ID,
            client_code="ACME_01",
            dataset_name="Approved trips",
            slug="approved-trips",
            description="Approved portal dataset",
            schema_name="new_schema",
            table_name="new_table",
            default_date_column="trip_start_time",
            is_active=True,
            actor_user_id=ADMIN_ID,
        )
    finally:
        _restore(patches)
    assert error and "Remove catalog columns before changing" in error, error
    print("PASS: schema/table changes are blocked while catalog columns exist")


def main() -> None:
    _test_metadata_helpers_use_information_schema_and_bound_params()
    _test_create_form_renders_cascading_selectors_and_date_options()
    _test_detail_page_renders_physical_columns_and_diagnostics_without_values()
    _test_discovery_state_distinguishes_all_cataloged_and_table_missing()
    _test_guided_multi_add_and_manual_create_still_work()
    _test_admin_only_boundaries_for_guided_add_and_deactivation()
    _test_deactivation_is_soft_delete_only()
    _test_inactive_dataset_access_is_hidden_and_denied()
    _test_schema_table_change_blocked_when_catalog_columns_exist()


if __name__ == "__main__":
    main()
