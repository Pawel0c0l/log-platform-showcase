#!/usr/bin/env python3
"""Manual regression tests for the approved Portal/Admin shared shell.

Supersedes the pre-redesign dark-only navy/alpha assertions. The approved
design (design-handoffs/log-platform/approved/v1.0/, screens SHL-001..SHL-003)
replaces the 268 px sidebar with a horizontal app bar, ships the colour layer as
an external token stylesheet instead of inline CSS, and supports AUTO / light /
dark rather than dark only. The functional assertions in this file — access
helper routing, 404 on a denied dataset, audit filtering, dense tables, export
permission messaging — are unchanged and must stay unchanged.

Run:

    cd /opt/log-platform
    env PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_portal_dark_theme_ui.py
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


USER_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
DATASET_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"


class _FakeUrl:
    def __init__(self, path="/user", query=""):
        self.path = path
        self.query = query


class _FakeRequest:
    def __init__(self, *, path="/user", query=""):
        self.url = _FakeUrl(path, query)
        self.cookies = {}
        self.headers = {"user-agent": "manual-test"}


def _html(response) -> str:
    return response.body.decode("utf-8")


def _user(*, admin=False):
    return {
        "user_id": USER_ID,
        "username": "alice",
        "display_name": "Alice",
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
        {"column_name": "trip_date", "display_name": "Trip date", "data_type": "date", "is_visible": True, "is_filterable": True, "is_sortable": True, "is_default_date_column": True, "display_order": 10},
        {"column_name": "driver_name", "display_name": "Driver", "data_type": "text", "is_visible": True, "is_filterable": True, "is_sortable": True, "is_default_date_column": False, "display_order": 20},
    ]


def _patch(name, value):
    old = getattr(api_main, name)
    setattr(api_main, name, value)
    return old


def _restore(patches):
    for name, old in reversed(patches):
        setattr(api_main, name, old)


def _assert_approved_shell(html: str, *, expect_page_head: bool = True) -> None:
    """The approved shared shell, as observable in the rendered document.

    ``expect_page_head`` is False for a table-first surface: the Database
    Explorer row sheet deliberately drops the in-page path and title so the
    table starts directly below the context bar, which already names the client
    and the dataset. Repeating that name above the sheet would push the table
    down for no information the user does not already have.
    """
    # Frame: app bar + context bar + full-width working area.
    assert "lp-shell" in html and "portal-shell" in html, html
    assert "lp-appbar" in html, html
    assert "lp-context" in html, html
    assert "lp-main" in html and "portal-main" in html, html
    if expect_page_head:
        assert "portal-page-header" in html, html
        assert "portal-title" in html, html
    else:
        assert "lp-page-head" not in html, "a table-first page must not render a page head"

    # Horizontal primary navigation replaced the sidebar; the old shell must not
    # survive behind the new one.
    assert "lp-nav-item" in html, html
    assert "portal-sidebar" not in html, html
    assert "portal-topbar" not in html, html

    # The colour layer is an external token stylesheet, not inline CSS, and the
    # superseded navy/alpha values are gone.
    assert "/static/css/tokens.css" in html, html
    assert "/static/css/portal.css" in html, html
    assert "#080b10" not in html, html
    assert "#ff7300" not in html, html

    # Typography is self-hosted: no third-party font CDN may appear.
    lowered = html.lower()
    for host in ("fonts.googleapis.com", "fonts.gstatic.com", "use.typekit", "cdn.jsdelivr"):
        assert host not in lowered, host


def _test_user_reports_dark_empty_state() -> None:
    old = _patch("_list_accessible_portal_report_folders_for_user", lambda user_id: [])
    try:
        html = _html(api_main._user_reports_response(_user()))
    finally:
        api_main._list_accessible_portal_report_folders_for_user = old
    _assert_approved_shell(html)
    assert "Reports Explorer" in html, html
    assert "portal-empty-state" in html, html
    assert "No report folders assigned" in html, html
    print("PASS: user reports page renders approved shell and styled empty state")


def _test_user_database_dark_empty_state() -> None:
    old = _patch("_list_accessible_portal_database_datasets_for_user", lambda user_id: [])
    try:
        html = _html(api_main._user_database_response(_user()))
    finally:
        api_main._list_accessible_portal_database_datasets_for_user = old
    _assert_approved_shell(html)
    # Approved stage S9 catalogue title and catalogue-empty state.
    assert "Zbiory danych klient\u00f3w" in html, html
    assert "db-state-empty-catalogue" in html, html
    assert "Nie masz przypisanych zbior\u00f3w danych" in html, html
    print("PASS: user database page renders approved shell and styled empty state")


def _test_admin_users_groups_dark_shell() -> None:
    patches = [
        ("_list_portal_users", _patch("_list_portal_users", lambda: [])),
        ("_list_portal_groups", _patch("_list_portal_groups", lambda: [])),
    ]
    try:
        users_html = _html(api_main._admin_users_response(_user(admin=True)))
        groups_html = _html(api_main._admin_groups_response(_user(admin=True)))
    finally:
        _restore(patches)
    _assert_approved_shell(users_html)
    _assert_approved_shell(groups_html)
    assert "Create user" in users_html, users_html
    assert "Create group" in groups_html, groups_html
    print("PASS: admin users and groups pages render approved shell")


def _test_admin_audit_dense_dark_table() -> None:
    events = [{
        "created_at": "2026-05-29T10:00:00Z",
        "event_type": "database_rows_viewed",
        "actor_username": "alice",
        "client_display_name": "Acme Logistics",
        "dataset_name": "Approved trips",
        "ip_address": "127.0.0.1",
        "metadata_json": {"page": 1, "limit": 50, "row_count": 1},
    }]
    old = _patch("_search_portal_audit_events", lambda filters, page, limit: (events, 1))
    try:
        html = _html(api_main._admin_audit_response(_user(admin=True), _FakeRequest(path="/admin/audit")))
    finally:
        api_main._search_portal_audit_events = old
    _assert_approved_shell(html)
    assert "portal-filterbar" in html, html
    assert "portal-table portal-table-dense" in html, html
    assert "database_rows_viewed" in html, html
    print("PASS: admin audit keeps filterbar and dense table classes")


def _test_database_dataset_dense_filterbar_and_access_helper() -> None:
    calls = []
    patches = [
        ("_get_portal_database_dataset_for_user", _patch("_get_portal_database_dataset_for_user", lambda dataset_id, user_id: calls.append((dataset_id, user_id)) or _dataset())),
        ("_get_portal_database_visible_columns", _patch("_get_portal_database_visible_columns", lambda dataset_id: _columns())),
        ("_count_portal_database_rows", _patch("_count_portal_database_rows", lambda dataset, columns, params: (1, {"sort": "trip_date", "direction": "desc", "active_filters": []}, None))),
        ("_list_portal_database_rows", _patch("_list_portal_database_rows", lambda dataset, columns, params, limit, offset, display_columns=None: ([{"trip_date": "2026-05-29", "driver_name": "Alice"}], {"sort": "trip_date", "direction": "desc", "active_filters": []}, None))),
        ("_portal_audit_event_safe", _patch("_portal_audit_event_safe", lambda **kwargs: None)),
    ]
    try:
        html = _html(api_main._portal_database_row_browser_response(_user(), DATASET_ID, _FakeRequest(path=f"/user/database/datasets/{DATASET_ID}", query="page=1&limit=50")))
    finally:
        _restore(patches)
    _assert_approved_shell(html, expect_page_head=False)
    assert calls == [(DATASET_ID, USER_ID)], calls
    # S3 replaced the standalone filter form with the column-centric surfaces:
    # the toolbar's collapsed `Filtry` panel and per-column header menus. Both
    # are themed by the module stylesheet rather than an inline <style> blob.
    assert 'class="db-filter-panel"' in html, html
    assert "db-col-menu" in html and "db-col-panel" in html, html
    # The approved data sheet replaced the generic bordered table panel.
    assert 'class="db-table"' in html and "db-toolbar" in html and "db-footer" in html, html
    assert "Tylko podgl\u0105d" in html, html

    denied_calls = []
    patches = [
        ("_get_portal_database_dataset_for_user", _patch("_get_portal_database_dataset_for_user", lambda dataset_id, user_id: denied_calls.append((dataset_id, user_id)) or None)),
        ("_portal_audit_event_safe", _patch("_portal_audit_event_safe", lambda **kwargs: None)),
    ]
    try:
        denied = api_main._portal_database_row_browser_response(_user(), DATASET_ID, _FakeRequest(path=f"/user/database/datasets/{DATASET_ID}"))
    finally:
        _restore(patches)
    denied_html = _html(denied)
    assert denied.status_code == 404, denied.status_code
    assert denied_calls == [(DATASET_ID, USER_ID)], denied_calls
    # Approved stage S9 replaced the generic error block with the permission state.
    assert "db-state-access" in denied_html, denied_html
    print("PASS: database dataset page uses dense UI and still routes through access helper")


def _test_login_page_uses_shared_token_layer() -> None:
    """The sign-in page renders outside the shared shell but on the same tokens.

    It regressed to a NameError once the inline dark CSS constant was removed,
    so this asserts the document actually renders and links the token layer.
    """
    html = _html(api_main._artifact_explorer_login_page(next_path="/user"))
    assert "Artifact Explorer Login" in html, html
    assert "portal-login-shell" in html, html
    assert "portal-login-panel" in html, html
    assert "/static/css/tokens.css" in html, html
    assert "#080b10" not in html and "#ff7300" not in html, html
    # No shell chrome on an unauthenticated page: no navigation, no account.
    assert "lp-nav-item" not in html, html
    print("PASS: sign-in page renders on the approved token layer")


def main() -> None:
    _test_user_reports_dark_empty_state()
    _test_user_database_dark_empty_state()
    _test_admin_users_groups_dark_shell()
    _test_admin_audit_dense_dark_table()
    _test_database_dataset_dense_filterbar_and_access_helper()
    _test_login_page_uses_shared_token_layer()


if __name__ == "__main__":
    main()
