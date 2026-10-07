#!/usr/bin/env python3
"""Manual stabilization tests for Phase 5A portal polish.

Run:

    cd /opt/log-platform
    env PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_portal_stabilization_phase5a.py

This test intentionally uses import stubs and monkeypatches so it can verify
rendering and access-control wiring without a live database or object store.
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
        self.routes = []

    def get(self, *args, **kwargs):
        self.routes.append(("GET", args[0] if args else ""))
        return lambda fn: fn

    def post(self, *args, **kwargs):
        self.routes.append(("POST", args[0] if args else ""))
        return lambda fn: fn

    def patch(self, *args, **kwargs):
        self.routes.append(("PATCH", args[0] if args else ""))
        return lambda fn: fn

    def delete(self, *args, **kwargs):
        self.routes.append(("DELETE", args[0] if args else ""))
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
FOLDER_ID = "dddddddd-dddd-dddd-dddd-dddddddddddd"


class _FakeUrl:
    def __init__(self, path="/admin", query=""):
        self.path = path
        self.query = query


class _FakeClient:
    host = "127.0.0.1"


class _FakeRequest:
    def __init__(self, *, path="/admin", query="", cookies=None):
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
        "roles": [],
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
        "can_export_rows": False,
        "visible_columns": 2,
    }
    data.update(overrides)
    return data


def _folder(**overrides):
    data = {
        "folder_id": FOLDER_ID,
        "client_code": "ACME_01",
        "client_display_name": "Acme Logistics",
        "folder_name": "Monthly reports",
        "slug": "monthly-reports",
        "description": "Reports",
        "search_query_json": {"report_type": ["monthly"]},
        "is_active": True,
        "can_preview": True,
        "can_download": True,
    }
    data.update(overrides)
    return data


def _columns():
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


def _patch(name, value):
    old = getattr(api_main, name)
    setattr(api_main, name, value)
    return old


def _restore(patches):
    for name, old in reversed(patches):
        setattr(api_main, name, old)


def _assert_no_sensitive_debug_text(html: str) -> None:
    lowered = html.lower()
    forbidden = ["traceback", "api_key", "access_token", "password_hash", "minio_secret", "select *"]
    for token in forbidden:
        assert token not in lowered, token


def test_admin_and_user_navigation_are_consistent():
    admin_labels = [item["label"] for item in api_main._admin_portal_nav()]
    for label in ["Users", "Groups", "Client Access", "Database Access", "Report Folders", "Audit"]:
        assert label in admin_labels, admin_labels
    user_labels = [item["label"] for item in api_main._portal_user_nav()]
    assert user_labels == ["Reports Explorer", "Client Database Explorer"], user_labels

    html = _html(
        api_main._portal_layout(
            "Database Access",
            "<p>ok</p>",
            user=_user(admin=True),
            portal_label="Admin Portal",
            active_key="database-access",
            nav_items=api_main._admin_portal_nav(),
        )
    )
    assert 'href="/admin/client-access/database"' in html, html
    # The approved shell marks the active entry with `aria-current`, so the state
    # is semantic and not carried by an `active` class or by colour alone. The
    # Administration section bar carries the granular entry; the app bar marks
    # `Administracja` as the active primary module.
    assert (
        '<a class="lp-subnav-item" href="/admin/client-access/database" aria-current="page">'
        in html
    ), html
    assert 'href="/admin" aria-current="page"' in html, html
    print("PASS: admin and user navigation labels are consistent")


def test_admin_pages_render_empty_states_without_debug_text():
    patches = [
        ("_list_portal_users", _patch("_list_portal_users", lambda: [])),
        ("_list_portal_groups", _patch("_list_portal_groups", lambda: [])),
        ("_list_portal_clients", _patch("_list_portal_clients", lambda: [])),
        ("_list_portal_user_client_dashboard_rows", _patch("_list_portal_user_client_dashboard_rows", lambda: [])),
        ("_list_portal_database_datasets", _patch("_list_portal_database_datasets", lambda: [])),
        ("_list_portal_report_folders", _patch("_list_portal_report_folders", lambda: [])),
        ("_search_portal_audit_events", _patch("_search_portal_audit_events", lambda filters, page, limit: ([], 0))),
    ]
    try:
        admin = _user(admin=True, user_id=ADMIN_ID, username="admin")
        pages = [
            api_main._admin_users_response(admin),
            api_main._admin_groups_response(admin),
            api_main._admin_client_access_response(admin),
            api_main._admin_database_access_response(admin),
            api_main._admin_report_folders_response(admin),
            api_main._admin_audit_response(admin, _FakeRequest(path="/admin/audit")),
        ]
    finally:
        _restore(patches)
    joined = "\n".join(_html(page) for page in pages)
    for expected in ["No local UI users", "No portal groups", "No portal clients", "No database datasets", "No report folders", "Filters"]:
        assert expected in joined, expected
    _assert_no_sensitive_debug_text(joined)
    print("PASS: admin portal pages render clean empty states")


def test_user_pages_use_effective_access_helpers_and_hide_debug_text():
    calls = []
    patches = [
        ("_list_accessible_portal_report_folders_for_user", _patch("_list_accessible_portal_report_folders_for_user", lambda user_id: calls.append(("reports", user_id)) or [])),
        ("_list_accessible_portal_database_datasets_for_user", _patch("_list_accessible_portal_database_datasets_for_user", lambda user_id: calls.append(("database", user_id)) or [_dataset(can_export_rows=False)])),
    ]
    try:
        portal_user = _user()
        reports_html = _html(api_main._user_reports_response(portal_user))
        database_html = _html(api_main._user_database_response(portal_user))
    finally:
        _restore(patches)
    assert ("reports", USER_ID) in calls, calls
    assert ("database", USER_ID) in calls, calls
    assert "No report folders" in reports_html, reports_html
    # Approved stage S9 copy: the neutral view-only permission badge and the
    # access-rules prose that `PBC` 2.2 keeps in the catalogue.
    assert "Tylko podgl\u0105d" in database_html, database_html
    assert "Zasady dost\u0119pu" in database_html, database_html
    _assert_no_sensitive_debug_text(reports_html + database_html)
    print("PASS: user portal pages use effective access helpers and clean copy")


def test_database_row_browser_export_buttons_follow_can_export_rows():
    def fake_count(dataset, columns, params):
        return 1, {"sort": "trip_date", "direction": "desc", "active_filters": []}, None

    def fake_list(dataset, columns, params, *, limit, offset, display_columns=None):
        return [{"trip_date": "2026-05-29", "driver_name": "Alice"}], {"sort": "trip_date", "direction": "desc", "active_filters": []}, None

    base_patches = [
        ("_get_portal_database_visible_columns", _patch("_get_portal_database_visible_columns", lambda dataset_id: _columns())),
        ("_count_portal_database_rows", _patch("_count_portal_database_rows", fake_count)),
        ("_list_portal_database_rows", _patch("_list_portal_database_rows", fake_list)),
        ("_portal_audit_event_safe", _patch("_portal_audit_event_safe", lambda **kwargs: None)),
    ]
    try:
        patches = [("_get_portal_database_dataset_for_user", _patch("_get_portal_database_dataset_for_user", lambda dataset_id, user_id: _dataset(can_export_rows=False)))]
        try:
            no_export_html = _html(api_main._portal_database_row_browser_response(_user(), DATASET_ID, _FakeRequest(path=f"/user/database/datasets/{DATASET_ID}")))
        finally:
            _restore(patches)

        patches = [("_get_portal_database_dataset_for_user", _patch("_get_portal_database_dataset_for_user", lambda dataset_id, user_id: _dataset(can_export_rows=True)))]
        try:
            export_html = _html(api_main._portal_database_row_browser_response(_user(), DATASET_ID, _FakeRequest(path=f"/user/database/datasets/{DATASET_ID}")))
        finally:
            _restore(patches)
    finally:
        _restore(base_patches)

    # Approved stage S9 keyed Polish copy; the action stays absent, not disabled.
    assert "Tylko podgl\u0105d" in no_export_html, no_export_html
    assert "Export is not enabled" not in no_export_html, no_export_html
    assert "db-export-form" not in no_export_html, no_export_html
    # Pre-existing staleness relative to approved stage S8, which replaced the
    # two export buttons with one panel carrying a format choice and a single
    # submit. The capability semantics this test owns are unchanged.
    assert "db-export-form" in export_html, export_html
    assert ">XLSX</" in export_html and ">CSV</" in export_html, export_html
    assert "Pobierz XLSX" in export_html, export_html
    _assert_no_sensitive_debug_text(no_export_html + export_html)
    print("PASS: row browser export buttons respect can_export_rows")


def test_portal_route_inventory_still_contains_key_surfaces():
    routes = set(getattr(api_main.app, "routes", []))
    expected = {
        ("GET", "/admin/groups"),
        ("GET", "/admin/audit"),
        ("GET", "/admin/client-access/database"),
        ("GET", "/user/reports"),
        ("GET", "/user/database"),
        ("GET", "/user/database/datasets/{dataset_id}/export"),
    }
    missing = expected - routes
    assert not missing, missing
    print("PASS: portal route inventory still contains key admin and user surfaces")


def main() -> int:
    test_admin_and_user_navigation_are_consistent()
    test_admin_pages_render_empty_states_without_debug_text()
    test_user_pages_use_effective_access_helpers_and_hide_debug_text()
    test_database_row_browser_export_buttons_follow_can_export_rows()
    test_portal_route_inventory_still_contains_key_surfaces()
    print("PASS: Phase 5A portal stabilization manual checks completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
