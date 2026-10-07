#!/usr/bin/env python3
"""Manual regression tests for User Portal Phase 3B database row browsing.

Run:

    cd /opt/log-platform
    env PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_portal_database_rows_phase3b.py
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
    def __init__(self, path=f"/user/database/datasets/{DATASET_ID}", query=""):
        self.path = path
        self.query = query


class _FakeRequest:
    def __init__(self, *, path=f"/user/database/datasets/{DATASET_ID}", query="", cookies=None):
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
        "can_view_rows": True,
        "can_filter_rows": True,
        "can_export_rows": False,
    }
    data.update(overrides)
    return data


def _columns():
    return [
        {
            "dataset_id": DATASET_ID,
            "column_name": "trip_date",
            "display_name": "Trip date",
            "data_type": "date",
            "is_visible": True,
            "is_filterable": True,
            "is_sortable": True,
            "is_default_date_column": True,
            "display_order": 10,
        },
        {
            "dataset_id": DATASET_ID,
            "column_name": "driver_name",
            "display_name": "Driver",
            "data_type": "text",
            "is_visible": True,
            "is_filterable": True,
            "is_sortable": True,
            "is_default_date_column": False,
            "display_order": 20,
        },
        {
            "dataset_id": DATASET_ID,
            "column_name": "internal_secret",
            "display_name": "Hidden",
            "data_type": "text",
            "is_visible": False,
            "is_filterable": False,
            "is_sortable": False,
            "is_default_date_column": False,
            "display_order": 30,
        },
    ]


def _visible_columns():
    return [column for column in _columns() if column["is_visible"]]


def _patch(name, value):
    old = getattr(api_main, name)
    setattr(api_main, name, value)
    return old


def _restore(patches):
    for name, old in reversed(patches):
        setattr(api_main, name, old)


_DEFAULT_DATASET = object()


def _base_user_patches(dataset=_DEFAULT_DATASET, columns=None, rows=None, total=1):
    dataset = _dataset() if dataset is _DEFAULT_DATASET else dataset
    columns = columns if columns is not None else _visible_columns()
    rows = rows if rows is not None else [{"trip_date": "2026-05-28", "driver_name": "Alice", "internal_secret": "hidden"}]
    return [
        ("get_current_artifact_user", _patch("get_current_artifact_user", lambda request: _user(user_id=USER_ID))),
        ("_get_portal_database_dataset_for_user", _patch("_get_portal_database_dataset_for_user", lambda dataset_id, user_id: dataset if dataset_id == DATASET_ID and user_id == USER_ID else None)),
        ("_get_portal_database_visible_columns", _patch("_get_portal_database_visible_columns", lambda dataset_id: columns if dataset_id == DATASET_ID else [])),
        ("_count_portal_database_rows", _patch("_count_portal_database_rows", lambda dataset, columns, params: (total, {"sort": "trip_date", "direction": "desc", "active_filters": []}, None))),
        ("_list_portal_database_rows", _patch("_list_portal_database_rows", lambda dataset, columns, params, limit, offset, display_columns=None: (rows, {"sort": "trip_date", "direction": "desc", "active_filters": []}, None))),
    ]


def _test_auth_and_access_boundaries() -> None:
    old = _patch("get_current_artifact_user", lambda request: None)
    try:
        unauth = api_main.user_portal_database_dataset(DATASET_ID, _FakeRequest())
    finally:
        api_main.get_current_artifact_user = old
    assert unauth.status_code == 303, unauth.status_code
    assert unauth.headers["Location"].startswith("/artifact-explorer/login"), unauth.headers

    patches = _base_user_patches(dataset=None)
    try:
        denied = api_main.user_portal_database_dataset(DATASET_ID, _FakeRequest())
    finally:
        _restore(patches)
    # Approved stage S9 renders the permission state in the portal shell. The
    # status code and the deliberate silence about the requested dataset are
    # unchanged: an unauthorized id must not be distinguishable from an unknown one.
    denied_html = _html(denied)
    assert denied.status_code == 404, denied.status_code
    assert "Nie masz dost\u0119pu do tego zbioru danych" in denied_html, denied_html
    assert "BRAK DOST\u0118PU" in denied_html, denied_html
    print("PASS: dataset row browser requires login and explicit active dataset access")


def _test_assigned_user_can_view_rows() -> None:
    patches = _base_user_patches()
    try:
        page = api_main.user_portal_database_dataset(DATASET_ID, _FakeRequest(query="page=1&limit=100"))
    finally:
        _restore(patches)
    html = _html(page)
    assert page.status_code == 200, page.status_code
    assert "Approved trips" in html and "Alice" in html, html
    assert "Trip date" in html and "Driver" in html, html
    assert "internal_secret" not in html and "<td>hidden</td>" not in html, html
    # Approved stage S9 states the withheld export capability in the approved
    # Polish permission vocabulary; the action remains absent, not disabled.
    assert "Tylko podgl\u0105d" in html, html
    assert "db-export-form" not in html, html
    print("PASS: assigned user can view approved rows and only visible columns")


def _test_query_builder_safety_sort_filter_date_pagination() -> None:
    dataset = _dataset()
    visible = _visible_columns()
    params = {
        "filter__driver_name": ["Ali"],
        "op__driver_name": ["contains"],
        "date_from": ["2026-05-01"],
        "date_to": ["2026-05-31"],
        "sort": ["trip_date"],
        "direction": ["desc"],
    }
    query, values, state, error = api_main._build_portal_database_rows_query(dataset, visible, params, limit=25, offset=50)
    assert error is None, error
    assert 'SELECT "trip_date", "driver_name" FROM "public"."trips"' in query, query
    assert "internal_secret" not in query, query
    assert "ILIKE %s" in query and "LIMIT %s OFFSET %s" in query, query
    assert values == ["%Ali%", "2026-05-01", "2026-05-31", 25, 50], values
    assert state["sort"] == "trip_date" and state["direction"] == "desc", state

    bad_sort = {"sort": ["internal_secret"], "direction": ["asc"]}
    _, _, _, sort_error = api_main._build_portal_database_rows_query(dataset, visible, bad_sort, limit=100, offset=0)
    assert "Sort column is not available" in sort_error, sort_error

    bad_filter = {"filter__driver_name;drop": ["x"]}
    _, _, _, filter_error = api_main._build_portal_database_rows_query(dataset, visible, bad_filter, limit=100, offset=0)
    assert "Filter column is not available" in filter_error, filter_error

    no_filter_access = _dataset(can_filter_rows=False)
    _, _, _, disabled_error = api_main._build_portal_database_rows_query(no_filter_access, visible, params, limit=100, offset=0)
    assert "Filtering is not enabled" in disabled_error, disabled_error

    invalid_date = {"date_from": ["not-a-date"]}
    _, _, _, date_error = api_main._build_portal_database_rows_query(dataset, visible, invalid_date, limit=100, offset=0)
    assert "must be an ISO date or datetime" in date_error, date_error

    page, limit, page_error = api_main._portal_database_parse_page_params({"page": ["-1"], "limit": ["9999"]})
    assert (page, limit, page_error) == (1, api_main.PORTAL_DATABASE_MAX_PAGE_SIZE, None), (page, limit, page_error)
    print("PASS: query builder validates identifiers, filters, date range, sorting, and pagination limits")


def _test_clean_configuration_error() -> None:
    class UndefinedTable(Exception):
        pass

    def raise_undefined(*args, **kwargs):
        raise UndefinedTable("relation does not exist")

    patches = _base_user_patches()
    old_count = _patch("_count_portal_database_rows", raise_undefined)
    patches.append(("_count_portal_database_rows", old_count))
    try:
        page = api_main.user_portal_database_dataset(DATASET_ID, _FakeRequest())
    finally:
        _restore(patches)
    html = _html(page)
    assert page.status_code == 502, page.status_code
    # Approved stage S9 renders `DB-011` inside the sheet: the failure class is
    # named, permissions are stated as correct, and the diagnostic trio is the
    # only thing carried for support. The exception text never reaches the page.
    assert "B\u0141\u0104D \u0179R\u00d3D\u0141A DANYCH" in html, html
    assert "Nie uda\u0142o si\u0119 odczyta\u0107 zbioru" in html, html
    assert "relation does not exist" not in html, html
    assert "UndefinedTable" not in html, html
    print("PASS: missing physical table or column returns a clean user-facing error")


def _test_database_cards_link_to_browser() -> None:
    patches = [
        ("get_current_artifact_user", _patch("get_current_artifact_user", lambda request: _user(user_id=USER_ID))),
        ("_list_accessible_portal_database_datasets_for_user", _patch("_list_accessible_portal_database_datasets_for_user", lambda user_id: [_dataset()])),
    ]
    try:
        page = api_main.user_portal_database(_FakeRequest(path="/user/database"))
    finally:
        _restore(patches)
    html = _html(page)
    assert f'/user/database/datasets/{DATASET_ID}' in html, html
    # Approved stage S9: the comparison-table row carries the open action and the
    # neutral permission badge for the withheld export capability.
    assert "Otw\u00f3rz arkusz" in html, html
    assert "Tylko podgl\u0105d" in html, html
    print("PASS: /user/database rows link to the read-only row browser and show view-only export state")


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
    print("PASS: Artifact Browser token auth behavior remains unchanged")


def main() -> None:
    _test_auth_and_access_boundaries()
    _test_assigned_user_can_view_rows()
    _test_query_builder_safety_sort_filter_date_pagination()
    _test_clean_configuration_error()
    _test_database_cards_link_to_browser()
    _test_existing_token_behavior_still_works()


if __name__ == "__main__":
    main()
