#!/usr/bin/env python3
"""Manual regression tests for Admin/User Portal Phase 3A database catalog.

Run:

    cd /opt/log-platform
    env PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_portal_database_catalog_phase3a.py
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
        "dataset_name": "Approved trips",
        "slug": "approved-trips",
        "description": "Approved portal dataset",
        "schema_name": "public",
        "table_name": "trips",
        "default_date_column": "trip_date",
        "is_active": True,
        "visible_columns": 2,
        "assigned_users": 1,
    }
    data.update(overrides)
    return data


def _column(**overrides):
    data = {
        "dataset_id": DATASET_ID,
        "column_name": "trip_date",
        "display_name": "Trip date",
        "data_type": "date",
        "is_visible": True,
        "is_filterable": True,
        "is_sortable": True,
        "is_default_date_column": True,
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
    patches = [
        ("get_current_artifact_user", _patch("get_current_artifact_user", lambda request: _user(admin=True, user_id=ADMIN_ID, username="admin"))),
    ]
    if extra:
        patches.extend(extra)
    return patches


def _assignment_data():
    assigned = [_user(admin=False, user_id=USER_ID, username="alice") | {
        "can_view_rows": True,
        "can_filter_rows": True,
        "can_export_rows": False,
    }]
    available = [_user(admin=False, user_id="dddddddd-dddd-dddd-dddd-dddddddddddd", username="bob")]
    return {"dataset": _dataset(), "assigned": assigned, "available": available, "columns": [_column()]}


def _test_admin_auth_boundaries() -> None:
    old = _patch("get_current_artifact_user", lambda request: None)
    try:
        unauth = api_main.admin_portal_database_access(_FakeRequest())
    finally:
        api_main.get_current_artifact_user = old
    assert unauth.status_code == 303, unauth.status_code
    assert unauth.headers["Location"].startswith("/artifact-explorer/login"), unauth.headers

    old = _patch("get_current_artifact_user", lambda request: _user(admin=False))
    try:
        forbidden = api_main.admin_portal_database_access(_FakeRequest())
    finally:
        api_main.get_current_artifact_user = old
    assert forbidden.status_code == 403, forbidden.status_code
    assert "Access denied" in _html(forbidden), _html(forbidden)
    print("PASS: /admin/client-access/database requires authenticated admin access")


def _test_dashboard_and_create_form_render() -> None:
    patches = _admin_patches([
        ("_list_portal_database_datasets", _patch("_list_portal_database_datasets", lambda: [_dataset()])),
        ("_list_portal_clients", _patch("_list_portal_clients", lambda: [_client()])),
    ])
    try:
        dashboard = api_main.admin_portal_database_access(_FakeRequest())
        form = api_main.admin_portal_new_database_dataset_form(_FakeRequest(path="/admin/client-access/database/new"))
    finally:
        _restore(patches)
    assert "Database datasets" in _html(dashboard) and "Approved trips" in _html(dashboard), _html(dashboard)
    assert "Create database dataset" in _html(form) and "Physical table reference" in _html(form), _html(form)
    print("PASS: admin can view database dashboard and create form")


def _test_identifier_and_duplicate_validation() -> None:
    patches = [
        ("_portal_database_dataset_client_error", _patch("_portal_database_dataset_client_error", lambda client_code: None)),
        ("_portal_database_dataset_slug_exists", _patch("_portal_database_dataset_slug_exists", lambda client_code, slug, exclude_dataset_id=None: slug == "dupe")),
        ("_portal_database_dataset_physical_exists", _patch("_portal_database_dataset_physical_exists", lambda client_code, schema_name, table_name, exclude_dataset_id=None: table_name == "trips")),
    ]
    try:
        bad_schema = api_main._validate_portal_database_dataset_inputs(
            client_code="ACME_01",
            dataset_name="Trips",
            slug="good-slug",
            schema_name="public;drop",
            table_name="safe_table",
            default_date_column=None,
        )
        bad_default = api_main._validate_portal_database_dataset_inputs(
            client_code="ACME_01",
            dataset_name="Trips",
            slug="good-slug",
            schema_name="public",
            table_name="safe_table",
            default_date_column="created at",
        )
        dupe_slug = api_main._validate_portal_database_dataset_inputs(
            client_code="ACME_01",
            dataset_name="Trips",
            slug="dupe",
            schema_name="public",
            table_name="safe_table",
            default_date_column=None,
        )
        dupe_table = api_main._validate_portal_database_dataset_inputs(
            client_code="ACME_01",
            dataset_name="Trips",
            slug="good-slug",
            schema_name="public",
            table_name="trips",
            default_date_column=None,
        )
    finally:
        _restore(patches)
    assert "Schema name must be" in bad_schema, bad_schema
    assert "Default date column must be" in bad_default, bad_default
    assert "slug already exists" in dupe_slug, dupe_slug
    assert "physical table is already registered" in dupe_table, dupe_table
    print("PASS: safe identifiers, duplicate slugs, and duplicate physical tables are validated cleanly")


def _test_default_date_column_uses_physical_allowlist() -> None:
    patches = [
        ("_portal_database_dataset_client_error", _patch("_portal_database_dataset_client_error", lambda client_code: None)),
        ("_portal_database_dataset_slug_exists", _patch("_portal_database_dataset_slug_exists", lambda client_code, slug, exclude_dataset_id=None: False)),
        ("_portal_database_dataset_physical_exists", _patch("_portal_database_dataset_physical_exists", lambda client_code, schema_name, table_name, exclude_dataset_id=None: False)),
        ("_list_portal_dataset_columns", _patch("_list_portal_dataset_columns", lambda client_code, schema_name, table_name: [
            {"column_name": "Created At", "is_date_like": True},
            {"column_name": "Driver Name", "is_date_like": False},
        ])),
    ]
    try:
        ok = api_main._validate_portal_database_dataset_inputs(
            client_code="ACME_01",
            dataset_name="Trips",
            slug="good-slug",
            schema_name="public",
            table_name="safe_table",
            default_date_column="Created At",
        )
        bad = api_main._validate_portal_database_dataset_inputs(
            client_code="ACME_01",
            dataset_name="Trips",
            slug="good-slug",
            schema_name="public",
            table_name="safe_table",
            default_date_column="Driver Name",
        )
    finally:
        _restore(patches)
    assert ok is None, ok
    assert bad == "Default date column must be a discovered date or timestamp column.", bad
    print("PASS: default date column accepts discovered non-simple date columns only")


def _test_create_and_edit_dataset_routes() -> None:
    calls = []

    def fake_create(**kwargs):
        calls.append(("create", kwargs))
        if kwargs["schema_name"] == "bad_schema":
            raise ValueError("Schema name must be a single safe identifier")
        return DATASET_ID

    def fake_update(**kwargs):
        calls.append(("update", kwargs))
        return None

    patches = _admin_patches([
        ("_create_portal_database_dataset", _patch("_create_portal_database_dataset", fake_create)),
        ("_get_portal_database_dataset", _patch("_get_portal_database_dataset", lambda dataset_id: _dataset() if dataset_id == DATASET_ID else None)),
        ("_update_portal_database_dataset", _patch("_update_portal_database_dataset", fake_update)),
        ("_list_portal_clients", _patch("_list_portal_clients", lambda: [_client()])),
        ("_get_portal_database_dataset_assignment_data", _patch("_get_portal_database_dataset_assignment_data", lambda dataset_id: _assignment_data())),
    ])
    try:
        ok = api_main.admin_portal_create_database_dataset(
            request=_FakeRequest(path="/admin/client-access/database/new"),
            client_code="acme_01",
            dataset_name="Approved trips",
            slug="approved-trips",
            description="Approved portal dataset",
            schema_name="public",
            table_name="trips",
            default_date_column="trip_date",
            is_active="1",
        )
        invalid = api_main.admin_portal_create_database_dataset(
            request=_FakeRequest(path="/admin/client-access/database/new"),
            client_code="acme_01",
            dataset_name="Bad",
            slug="bad",
            description=None,
            schema_name="bad_schema",
            table_name="trips",
            default_date_column=None,
            is_active="1",
        )
        detail = api_main.admin_portal_database_dataset_detail(DATASET_ID, _FakeRequest(path=f"/admin/client-access/database/{DATASET_ID}"))
        update = api_main.admin_portal_update_database_dataset(
            DATASET_ID,
            _FakeRequest(path=f"/admin/client-access/database/{DATASET_ID}"),
            client_code="acme_01",
            dataset_name="Updated trips",
            slug="updated-trips",
            description="Updated",
            schema_name="public",
            table_name="trips_v2",
            default_date_column="updated_at",
            is_active=None,
        )
    finally:
        _restore(patches)
    assert ok.status_code == 303 and ok.headers["Location"] == f"/admin/client-access/database/{DATASET_ID}", ok.headers
    assert invalid.status_code == 400 and "Schema name must be" in _html(invalid), _html(invalid)
    assert detail.status_code == 200 and "Visible columns" in _html(detail), _html(detail)
    assert update.status_code == 303, update.headers
    assert ("update", {
        "dataset_id": DATASET_ID,
        "client_code": "ACME_01",
        "dataset_name": "Updated trips",
        "slug": "updated-trips",
        "description": "Updated",
        "schema_name": "public",
        "table_name": "trips_v2",
        "default_date_column": "updated_at",
        "is_active": False,
        "actor_user_id": ADMIN_ID,
    }) in calls, calls
    print("PASS: admin can create, view, and edit dataset metadata")


def _test_column_management_routes() -> None:
    calls = []

    def fake_upsert(**kwargs):
        calls.append(("upsert", kwargs))
        return "Column name must be a single safe identifier" if kwargs["column_name"] == "bad column" else None

    def fake_remove(**kwargs):
        calls.append(("remove", kwargs))
        return None

    patches = _admin_patches([
        ("_get_portal_database_dataset", _patch("_get_portal_database_dataset", lambda dataset_id: _dataset() if dataset_id == DATASET_ID else None)),
        ("_upsert_portal_database_dataset_column", _patch("_upsert_portal_database_dataset_column", fake_upsert)),
        ("_remove_portal_database_dataset_column", _patch("_remove_portal_database_dataset_column", fake_remove)),
        ("_list_portal_clients", _patch("_list_portal_clients", lambda: [_client()])),
        ("_get_portal_database_dataset_assignment_data", _patch("_get_portal_database_dataset_assignment_data", lambda dataset_id: _assignment_data())),
    ])
    try:
        ok = api_main.admin_portal_save_database_dataset_column(
            DATASET_ID,
            _FakeRequest(),
            column_name="trip_date",
            display_name="Trip date",
            data_type="date",
            display_order=10,
            is_visible="1",
            is_filterable="1",
            is_sortable="1",
            is_default_date_column="1",
        )
        invalid = api_main.admin_portal_save_database_dataset_column(
            DATASET_ID,
            _FakeRequest(),
            column_name="bad column",
            display_name="Bad",
            data_type=None,
            display_order=100,
            is_visible="1",
            is_filterable="1",
            is_sortable="1",
            is_default_date_column=None,
        )
        remove = api_main.admin_portal_remove_database_dataset_column(DATASET_ID, "trip_date", _FakeRequest())
    finally:
        _restore(patches)
    assert ok.status_code == 303 and remove.status_code == 303, (ok.status_code, remove.status_code)
    assert invalid.status_code == 400 and "Column name must be" in _html(invalid), _html(invalid)
    assert ("upsert", {
        "dataset_id": DATASET_ID,
        "column_name": "trip_date",
        "display_name": "Trip date",
        "data_type": "date",
        "is_visible": True,
        "is_filterable": True,
        "is_sortable": True,
        "is_default_date_column": True,
        "display_order": 10,
    }) in calls, calls
    assert ("remove", {"dataset_id": DATASET_ID, "column_name": "trip_date"}) in calls, calls
    print("PASS: admin can add/update/remove catalog columns and mark a default date column")


def _test_discovered_column_save_uses_physical_allowlist() -> None:
    calls = []
    physical_columns = [
        {"column_name": "registration", "normalized_data_type": "text", "is_date_like": False},
        {"column_name": "Driver Name", "normalized_data_type": "text", "is_date_like": False},
        {"column_name": "Driver-ID", "normalized_data_type": "text", "is_date_like": False},
        {"column_name": "Średnia prędkość", "normalized_data_type": "numeric", "is_date_like": False},
        {"column_name": "2026_value", "normalized_data_type": "numeric", "is_date_like": False},
        {"column_name": "Created At", "normalized_data_type": "timestamp", "is_date_like": True},
    ]

    def fake_upsert(**kwargs):
        calls.append(kwargs)
        return None

    patches = [
        ("_get_portal_database_dataset", _patch("_get_portal_database_dataset", lambda dataset_id: _dataset() if dataset_id == DATASET_ID else None)),
        ("_list_portal_database_physical_columns", _patch("_list_portal_database_physical_columns", lambda dataset: physical_columns)),
        ("_list_portal_database_dataset_columns", _patch("_list_portal_database_dataset_columns", lambda dataset_id: [])),
        ("_upsert_portal_database_dataset_column", _patch("_upsert_portal_database_dataset_column", fake_upsert)),
    ]
    try:
        added, error = api_main._add_portal_database_dataset_columns_from_physical(
            dataset_id=DATASET_ID,
            column_names=["registration", "Driver Name", "Driver-ID", "Średnia prędkość", "2026_value"],
            is_visible=True,
            is_filterable=True,
            is_sortable=True,
        )
        added_with_default, default_error = api_main._add_portal_database_dataset_columns_from_physical(
            dataset_id=DATASET_ID,
            column_names=["Created At"],
            is_visible=True,
            is_filterable=True,
            is_sortable=True,
            default_date_column="Created At",
        )
        rejected, reject_error = api_main._add_portal_database_dataset_columns_from_physical(
            dataset_id=DATASET_ID,
            column_names=["bad_column; DROP TABLE users; --"],
            is_visible=True,
            is_filterable=True,
            is_sortable=True,
        )
    finally:
        _restore(patches)
    assert error is None, error
    assert added == ["registration", "Driver Name", "Driver-ID", "Średnia prędkość", "2026_value"], added
    assert default_error is None and added_with_default == ["Created At"], (added_with_default, default_error)
    assert rejected == [] and "not available" in reject_error, reject_error
    assert any(call["column_name"] == "registration" for call in calls), calls
    assert any(call["column_name"] == "Driver Name" and call.get("allow_physical_column_name") is True for call in calls), calls
    assert any(call["column_name"] == "Created At" and call.get("is_default_date_column") is True for call in calls), calls
    print("PASS: discovered column save accepts physical non-simple names and rejects non-available names")


def _test_user_assignment_routes() -> None:
    calls = []

    def fake_assign(**kwargs):
        calls.append(("assign", kwargs))
        if kwargs["user_id"] == "no-access":
            return "User must have database access to this dataset's client before assignment."
        return None

    def fake_update(**kwargs):
        calls.append(("update", kwargs))
        return None

    def fake_remove(**kwargs):
        calls.append(("remove", kwargs))
        return None

    patches = _admin_patches([
        ("_get_portal_database_dataset", _patch("_get_portal_database_dataset", lambda dataset_id: _dataset() if dataset_id == DATASET_ID else None)),
        ("_assign_portal_database_dataset_user", _patch("_assign_portal_database_dataset_user", fake_assign)),
        ("_update_portal_database_dataset_user_permissions", _patch("_update_portal_database_dataset_user_permissions", fake_update)),
        ("_remove_portal_database_dataset_user", _patch("_remove_portal_database_dataset_user", fake_remove)),
        ("_list_portal_clients", _patch("_list_portal_clients", lambda: [_client()])),
        ("_get_portal_database_dataset_assignment_data", _patch("_get_portal_database_dataset_assignment_data", lambda dataset_id: _assignment_data())),
    ])
    try:
        assign = api_main.admin_portal_assign_database_dataset_user(DATASET_ID, _FakeRequest(), user_id=USER_ID)
        denied = api_main.admin_portal_assign_database_dataset_user(DATASET_ID, _FakeRequest(), user_id="no-access")
        update = api_main.admin_portal_update_database_dataset_user_permissions(
            DATASET_ID,
            USER_ID,
            _FakeRequest(),
            can_view_rows="1",
            can_filter_rows=None,
            can_export_rows="1",
        )
        remove = api_main.admin_portal_remove_database_dataset_user(DATASET_ID, USER_ID, _FakeRequest())
    finally:
        _restore(patches)
    assert assign.status_code == 303 and update.status_code == 303 and remove.status_code == 303, (assign.status_code, update.status_code, remove.status_code)
    assert denied.status_code == 400 and "must have database access" in _html(denied), _html(denied)
    assert ("assign", {"dataset_id": DATASET_ID, "user_id": USER_ID, "granted_by": ADMIN_ID}) in calls, calls
    assert ("update", {"dataset_id": DATASET_ID, "user_id": USER_ID, "can_view_rows": True, "can_filter_rows": False, "can_export_rows": True}) in calls, calls
    assert ("remove", {"dataset_id": DATASET_ID, "user_id": USER_ID}) in calls, calls
    print("PASS: admin can assign datasets only through guarded dataset assignment routes")


def _test_user_database_dataset_cards() -> None:
    patches = [
        ("get_current_artifact_user", _patch("get_current_artifact_user", lambda request: _user(admin=False, user_id=USER_ID, username="alice"))),
        ("_list_accessible_portal_database_datasets_for_user", _patch("_list_accessible_portal_database_datasets_for_user", lambda user_id: [_dataset(can_filter_rows=True, can_export_rows=False)] if user_id == USER_ID else [])),
    ]
    try:
        page = api_main.user_portal_database(_FakeRequest(path="/user/database"))
    finally:
        _restore(patches)
    html = _html(page)
    # Approved stage S9: `DB-001` is one comparison table, not a card grid.
    assert "Approved trips" in html and '<td class="db-cat-num lp-mono">2</td>' in html, html
    assert "Otw\u00f3rz arkusz" in html and "db-cat-table" in html, html
    # Permission flags render as neutral configuration badges, never as errors.
    assert "Filtrowanie" in html and "Tylko podgl\u0105d" in html, html
    print("PASS: /user/database shows assigned datasets as one comparison table with permissions and open actions")


def _test_existing_artifact_token_behavior_still_works() -> None:
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
    print("PASS: Artifact Browser token auth behavior remains unchanged")


def main() -> None:
    _test_admin_auth_boundaries()
    _test_dashboard_and_create_form_render()
    _test_identifier_and_duplicate_validation()
    _test_default_date_column_uses_physical_allowlist()
    _test_create_and_edit_dataset_routes()
    _test_column_management_routes()
    _test_discovered_column_save_uses_physical_allowlist()
    _test_user_assignment_routes()
    _test_user_database_dataset_cards()
    _test_existing_artifact_token_behavior_still_works()


if __name__ == "__main__":
    main()
