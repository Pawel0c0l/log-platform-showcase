#!/usr/bin/env python3
"""Manual regression tests for Phase 4A portal audit coverage.

Run:

    cd /opt/log-platform
    env PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_portal_audit_phase4a.py
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
        self.body = b"".join(body) if not isinstance(body, (bytes, bytearray)) else bytes(body)
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
TARGET_ID = "dddddddd-dddd-dddd-dddd-dddddddddddd"
DATASET_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
FOLDER_ID = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
ARTIFACT_ID = "ffffffff-ffff-ffff-ffff-ffffffffffff"


class _FakeUrl:
    def __init__(self, path="/admin/audit", query=""):
        self.path = path
        self.query = query


class _FakeClient:
    host = "127.0.0.1"


class _FakeRequest:
    def __init__(self, *, path="/admin/audit", query=""):
        self.url = _FakeUrl(path, query)
        self.cookies = {}
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


def _target_user(**overrides):
    data = _user(user_id=TARGET_ID, username="client.user")
    data.update({"display_name": "Client User", "roles": []})
    data.update(overrides)
    return data


def _dataset(**overrides):
    data = {
        "dataset_id": DATASET_ID,
        "client_code": "ACME_01",
        "client_display_name": "Acme Logistics",
        "dataset_name": "Approved trips",
        "slug": "approved-trips",
        "default_date_column": "trip_date",
        "can_view_rows": True,
        "can_filter_rows": True,
        "can_export_rows": False,
    }
    data.update(overrides)
    return data


def _columns():
    return [
        {"column_name": "trip_date", "display_name": "Trip date", "data_type": "date", "is_visible": True, "is_filterable": True, "is_sortable": True, "display_order": 10},
        {"column_name": "driver_name", "display_name": "Driver", "data_type": "text", "is_visible": True, "is_filterable": True, "is_sortable": True, "display_order": 20},
    ]


def _folder(**overrides):
    data = {
        "folder_id": FOLDER_ID,
        "folder_name": "Monthly reports",
        "client_code": "ACME_01",
        "client_display_name": "Acme Logistics",
        "description": "Reports",
        "can_preview": True,
        "can_download": True,
    }
    data.update(overrides)
    return data


def _artifact(**overrides):
    data = {"artifact_id": ARTIFACT_ID, "display_filename": "report.csv", "report_type": "monthly", "file_ext": "csv"}
    data.update(overrides)
    return data


def _patch(name, value):
    old = getattr(api_main, name)
    setattr(api_main, name, value)
    return old


def _restore(patches):
    for name, old in reversed(patches):
        setattr(api_main, name, old)


def _capture_audit():
    events = []
    return events, _patch("_portal_audit_event_safe", lambda **kwargs: events.append(kwargs))


def _test_sanitizer_and_event_types() -> None:
    unsafe = api_main._sanitize_portal_audit_metadata({
        "password": "plain",
        "api_token": "secret-token",
        "query_sql": "select * from x",
        "safe": "value",
        "nested": {"cookie": "abc", "field": "ok"},
    })
    assert unsafe["password"] == "[redacted]", unsafe
    assert unsafe["api_token"] == "[redacted]", unsafe
    assert unsafe["query_sql"] == "[redacted]", unsafe
    assert unsafe["nested"]["cookie"] == "[redacted]", unsafe
    assert unsafe["safe"] == "value", unsafe
    required = {
        "auth_login_success", "auth_login_failed", "auth_logout", "admin_access_denied",
        "admin_user_created", "portal_client_created", "report_folder_viewed",
        "report_artifact_preview_success", "database_rows_viewed", "database_export_success",
    }
    assert required.issubset(api_main.PORTAL_AUDIT_EVENT_TYPES), required - api_main.PORTAL_AUDIT_EVENT_TYPES
    print("PASS: audit sanitizer redacts unsafe metadata and event allowlist includes Phase 4A events")


def _test_admin_audit_filters_and_pagination() -> None:
    old_user = _patch("get_current_artifact_user", lambda request: _user(admin=False))
    try:
        denied = api_main.admin_portal_audit(_FakeRequest(path="/admin/audit"))
    finally:
        api_main.get_current_artifact_user = old_user
    assert denied.status_code == 403, denied.status_code

    captured = {}
    event = {
        "created_at": "2026-05-28T10:00:00+00:00",
        "event_type": "database_rows_viewed",
        "actor_username": "alice",
        "client_display_name": "Acme Logistics",
        "dataset_name": "Approved trips",
        "ip_address": "127.0.0.1",
        "metadata_json": {"page": 1, "limit": 100, "row_count": 2, "filter_keys": ["driver_name"]},
    }

    def fake_search(filters, page=1, limit=100):
        captured.update({"filters": filters, "page": page, "limit": limit})
        return [event], 501

    patches = [
        ("get_current_artifact_user", _patch("get_current_artifact_user", lambda request: _user(admin=True, user_id=ADMIN_ID, username="admin"))),
        ("_search_portal_audit_events", _patch("_search_portal_audit_events", fake_search)),
    ]
    try:
        page = api_main.admin_portal_audit(_FakeRequest(path="/admin/audit", query=f"event_type=database_rows_viewed&actor=ali&client_code=ACME_01&dataset_id={DATASET_ID}&date_from=2026-05-01&limit=9999"))
        bad_date = api_main.admin_portal_audit(_FakeRequest(path="/admin/audit", query="date_from=not-a-date"))
    finally:
        _restore(patches)
    html = _html(page)
    assert page.status_code == 200, page.status_code
    assert captured["filters"]["event_type"] == "database_rows_viewed", captured
    assert captured["filters"]["dataset_id"] == DATASET_ID, captured
    assert captured["limit"] == api_main.PORTAL_AUDIT_MAX_PAGE_SIZE, captured
    assert "Next" in html and "database_rows_viewed" in html and "filters: driver_name" in html, html
    assert "Date from must be an ISO date or datetime" in _html(bad_date), _html(bad_date)
    print("PASS: /admin/audit is admin-only and supports safe filters, invalid-date errors, and max pagination")


def _test_auth_and_admin_action_events() -> None:
    events, old_audit = _capture_audit()
    patches = [
        ("_portal_audit_event_safe", old_audit),
        ("authenticate_artifact_user", _patch("authenticate_artifact_user", lambda username, password: _user(user_id=USER_ID, username=username) if password == "pw" else None)),
        ("get_current_artifact_user", _patch("get_current_artifact_user", lambda request: _user(admin=True, user_id=ADMIN_ID, username="admin"))),
        ("_list_portal_roles", _patch("_list_portal_roles", lambda: [])),
        ("_portal_username_exists", _patch("_portal_username_exists", lambda username: False)),
        ("_create_portal_user", _patch("_create_portal_user", lambda **kwargs: TARGET_ID)),
        ("_get_portal_user", _patch("_get_portal_user", lambda user_id: _target_user(user_id=user_id))),
        ("_reset_portal_user_password", _patch("_reset_portal_user_password", lambda **kwargs: None)),
    ]
    try:
        api_main.artifact_explorer_login("alice", "pw", "/user", _FakeRequest(path="/artifact-explorer/login"))
        api_main.artifact_explorer_login("alice", "bad", "/user", _FakeRequest(path="/artifact-explorer/login"))
        api_main.admin_portal_create_user(_FakeRequest(path="/admin/users/new"), "client.user", "pw", "pw", "Client User", "on", [])
        api_main.admin_portal_reset_user_password(TARGET_ID, _FakeRequest(path=f"/admin/users/{TARGET_ID}/password"), "newpw", "newpw")
    finally:
        _restore(patches)
    names = [event["event_type"] for event in events]
    assert "auth_login_success" in names and "auth_login_failed" in names, names
    assert "admin_user_created" in names and "admin_user_password_reset" in names, names
    assert "newpw" not in str(events) and "pw" not in str([event.get("metadata") for event in events]), events
    print("PASS: login success/failure and admin user create/password reset events are logged without passwords")


def _test_client_report_database_and_row_events() -> None:
    events, old_audit = _capture_audit()
    patches = [
        ("_portal_audit_event_safe", old_audit),
        ("get_current_artifact_user", _patch("get_current_artifact_user", lambda request: _user(admin=True, user_id=ADMIN_ID, username="admin"))),
        ("_validate_portal_client_code", _patch("_validate_portal_client_code", lambda code: None)),
        ("_portal_client_exists", _patch("_portal_client_exists", lambda code: False)),
        ("_create_portal_client", _patch("_create_portal_client", lambda **kwargs: kwargs["client_code"])),
        ("_get_accessible_portal_report_folder_for_user", _patch("_get_accessible_portal_report_folder_for_user", lambda user_id, folder_id: _folder())),
        ("_portal_report_folder_artifacts", _patch("_portal_report_folder_artifacts", lambda folder, limit, offset: {"data": [_artifact()], "meta": {"total": 1}})),
        ("_portal_report_artifacts_table", _patch("_portal_report_artifacts_table", lambda folder, artifacts, user_actions: "<table></table>")),
        ("_portal_report_artifact_access", _patch("_portal_report_artifact_access", lambda user, folder_id, artifact_id, action: (_folder(), {"artifact_id": artifact_id}, _artifact()))),
        ("_artifact_explorer_preview_for_row", _patch("_artifact_explorer_preview_for_row", lambda row, artifact: {"preview_type": "text", "text": "ok"})),
        ("_artifact_explorer_render_preview", _patch("_artifact_explorer_render_preview", lambda preview, artifact_id, can_download=False: "<pre>ok</pre>")),
        ("_get_portal_database_dataset_for_user", _patch("_get_portal_database_dataset_for_user", lambda dataset_id, user_id: _dataset())),
        ("_get_portal_database_visible_columns", _patch("_get_portal_database_visible_columns", lambda dataset_id: _columns())),
        ("_count_portal_database_rows", _patch("_count_portal_database_rows", lambda dataset, columns, params: (1, {"sort": "trip_date", "direction": "desc"}, None))),
        ("_list_portal_database_rows", _patch("_list_portal_database_rows", lambda dataset, columns, params, limit, offset, display_columns=None: ([{"trip_date": "2026-05-28", "driver_name": "Alice", "secret_value": "hidden"}], {"sort": "trip_date", "direction": "desc"}, None))),
    ]
    try:
        api_main.admin_portal_create_client(_FakeRequest(path="/admin/client-access/clients/new"), "ACME_01", "Acme Logistics", "", "on")
        api_main._user_report_folder_response(_user(user_id=USER_ID), FOLDER_ID, _FakeRequest(path=f"/user/reports/folders/{FOLDER_ID}"))
        api_main._portal_report_preview_response(_user(user_id=USER_ID), FOLDER_ID, ARTIFACT_ID, _FakeRequest(path=f"/user/reports/artifacts/{ARTIFACT_ID}/preview"))
        api_main.user_portal_database_dataset(DATASET_ID, _FakeRequest(path=f"/user/database/datasets/{DATASET_ID}", query="filter__driver_name=Ali&op__driver_name=contains"))
    finally:
        _restore(patches)
    names = [event["event_type"] for event in events]
    assert "portal_client_created" in names, names
    assert "report_folder_viewed" in names and "report_artifact_preview_success" in names, names
    assert "database_rows_viewed" in names, names
    row_event = next(event for event in events if event["event_type"] == "database_rows_viewed")
    metadata = row_event.get("metadata") or row_event.get("metadata_json") or {}
    assert metadata.get("filter_keys") == ["driver_name"], metadata
    assert "Alice" not in str(metadata) and "hidden" not in str(metadata), metadata
    print("PASS: client, report, preview, and database row browsing events are logged without row values")


def _test_export_events_still_use_sanitized_helper() -> None:
    events, old_audit = _capture_audit()
    patches = [
        ("_portal_audit_event_safe", old_audit),
        ("get_current_artifact_user", _patch("get_current_artifact_user", lambda request: _user(user_id=USER_ID))),
        ("_get_portal_database_dataset_for_user", _patch("_get_portal_database_dataset_for_user", lambda dataset_id, user_id: _dataset(can_export_rows=True))),
        ("_get_portal_database_visible_columns", _patch("_get_portal_database_visible_columns", lambda dataset_id: _columns())),
        ("_count_portal_database_rows", _patch("_count_portal_database_rows", lambda dataset, columns, params: (1, {"sort": "trip_date", "direction": "desc"}, None))),
        ("_list_portal_database_rows", _patch("_list_portal_database_rows", lambda dataset, columns, params, limit, offset, display_columns=None: ([{"trip_date": "2026-05-28", "driver_name": "Alice", "raw_sql": "no"}], {"sort": "trip_date", "direction": "desc"}, None))),
    ]
    try:
        response = api_main.user_portal_database_dataset_export(DATASET_ID, _FakeRequest(path=f"/user/database/datasets/{DATASET_ID}/export", query="format=csv&filter__driver_name=Ali"))
    finally:
        _restore(patches)
    assert response.status_code == 200, response.status_code
    names = [event["event_type"] for event in events]
    assert "database_export_success" in names, names
    success = next(event for event in events if event["event_type"] == "database_export_success")
    metadata = success.get("metadata") or success.get("metadata_json") or {}
    assert metadata.get("filter_keys") == ["driver_name"], metadata
    assert "raw_sql" not in str(metadata).lower(), metadata
    print("PASS: database export success still logs through sanitized audit metadata")


def _test_artifact_browser_token_behavior_still_works() -> None:
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
    print("PASS: Artifact Browser bearer token behavior remains unchanged")


def main() -> None:
    _test_sanitizer_and_event_types()
    _test_admin_audit_filters_and_pagination()
    _test_auth_and_admin_action_events()
    _test_client_report_database_and_row_events()
    _test_export_events_still_use_sanitized_helper()
    _test_artifact_browser_token_behavior_still_works()
    print("PASS: Phase 4A portal audit manual regression checks completed")


if __name__ == "__main__":
    main()
