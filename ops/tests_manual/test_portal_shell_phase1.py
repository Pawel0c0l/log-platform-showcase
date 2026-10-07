#!/usr/bin/env python3
"""Manual regression tests for User/Admin Portal Phase 1 shell.

Run:

    cd /opt/log-platform
    env PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_portal_shell_phase1.py
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


class _FakeUrl:
    def __init__(self, path="/user", query=""):
        self.path = path
        self.query = query


class _FakeRequest:
    def __init__(self, *, path="/user", query="", cookies=None):
        self.url = _FakeUrl(path, query)
        self.cookies = cookies or {}


def _html(response) -> str:
    return response.body.decode("utf-8")


def _user(*, admin=False):
    return {
        "user_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "username": "alice",
        "display_name": "Alice",
        "is_active": True,
        "is_admin": admin,
        "permissions": [],
    }


def _with_current_user(user):
    old = api_main.get_current_artifact_user
    api_main.get_current_artifact_user = lambda request: user
    return old


def _test_unauthenticated_user_redirects_to_login() -> None:
    old = _with_current_user(None)
    try:
        response = api_main.user_portal_home(_FakeRequest(path="/user"))
    finally:
        api_main.get_current_artifact_user = old
    assert response.status_code == 303, response.status_code
    assert response.headers["Location"].startswith("/artifact-explorer/login"), response.headers
    assert "next=%2Fuser" in response.headers["Location"], response.headers
    print("PASS: unauthenticated /user redirects to login with next path")


def _test_authenticated_user_portal_pages() -> None:
    old = _with_current_user(_user())
    old_report_folders = api_main._list_accessible_portal_report_folders_for_user
    old_datasets = api_main._list_accessible_portal_database_datasets_for_user
    api_main._list_accessible_portal_report_folders_for_user = lambda user_id: []
    api_main._list_accessible_portal_database_datasets_for_user = lambda user_id: []
    try:
        home = api_main.user_portal_home(_FakeRequest(path="/user"))
        # S15 moved the `Raporty` nav slot to the Report Explorer library; the
        # pre-redesign folder index this stage asserts is unchanged and is
        # still served, now at its compatibility route.
        reports = api_main.user_portal_report_folders_legacy(
            _FakeRequest(path="/user/reports/folders"))
        database = api_main.user_portal_database(_FakeRequest(path="/user/database"))
    finally:
        api_main.get_current_artifact_user = old
        api_main._list_accessible_portal_report_folders_for_user = old_report_folders
        api_main._list_accessible_portal_database_datasets_for_user = old_datasets
    assert home.status_code == 303 and home.headers["Location"] == "/user/reports", home.headers
    reports_html = _html(reports)
    database_html = _html(database)
    assert "Reports Explorer" in reports_html, reports_html
    assert "No report folders have been assigned yet" in reports_html, reports_html
    assert "Technical Artifact Explorer" not in reports_html, reports_html
    # Approved stage S9 renamed the catalogue to its approved Polish title and
    # replaced the placeholder copy with the approved catalogue-empty state.
    assert "Zbiory danych klient\u00f3w" in database_html, database_html
    assert "Nie masz przypisanych zbior\u00f3w danych" in database_html, database_html
    print("PASS: authenticated non-admin can access user portal placeholders")


def _test_non_admin_cannot_access_admin() -> None:
    old = _with_current_user(_user())
    try:
        response = api_main.admin_portal_users(_FakeRequest(path="/admin/users"))
    finally:
        api_main.get_current_artifact_user = old
    assert response.status_code == 403, response.status_code
    assert "Access denied" in _html(response), _html(response)
    print("PASS: authenticated non-admin gets clean forbidden page for admin portal")


def _test_admin_portal_pages() -> None:
    old_user = _with_current_user(_user(admin=True))
    old_list = api_main._list_portal_users
    old_clients = api_main._list_portal_clients
    old_user_clients = api_main._list_portal_user_client_dashboard_rows
    old_report_folders = api_main._list_portal_report_folders
    api_main._list_portal_users = lambda: []
    api_main._list_portal_clients = lambda: []
    api_main._list_portal_user_client_dashboard_rows = lambda: []
    api_main._list_portal_report_folders = lambda: []
    try:
        home = api_main.admin_portal_home(_FakeRequest(path="/admin"))
        users = api_main.admin_portal_users(_FakeRequest(path="/admin/users"))
        client_access = api_main.admin_portal_client_access(_FakeRequest(path="/admin/client-access"))
        report_folders = api_main.admin_portal_report_folders(_FakeRequest(path="/admin/report-folders"))
        audit = api_main.admin_portal_audit(_FakeRequest(path="/admin/audit"))
    finally:
        api_main.get_current_artifact_user = old_user
        api_main._list_portal_users = old_list
        api_main._list_portal_clients = old_clients
        api_main._list_portal_user_client_dashboard_rows = old_user_clients
        api_main._list_portal_report_folders = old_report_folders
    assert home.status_code == 303 and home.headers["Location"] == "/admin/users", home.headers
    assert "Users" in _html(users), _html(users)
    assert "Create user" in _html(users), _html(users)
    assert "does not grant arbitrary SQL access" in _html(client_access), _html(client_access)
    assert "Report Folders" in _html(report_folders), _html(report_folders)
    # The redesign renames the Artifacts nav entry to the approved Polish term
    # and keeps it admin-only.
    assert "Artefakty" in _html(report_folders), _html(report_folders)
    assert 'href="/artifact-explorer"' in _html(report_folders), _html(report_folders)
    assert "Audit" in _html(audit), _html(audit)
    print("PASS: admin can access admin portal placeholders")


def _test_login_allows_portal_next_paths_and_root_cookie() -> None:
    old_auth = api_main.authenticate_artifact_user
    try:
        api_main.authenticate_artifact_user = lambda username, password: _user(admin=True) if (username, password) == ("alice", "pw") else None
        response = api_main.artifact_explorer_login("alice", "pw", "/admin/users")
    finally:
        api_main.authenticate_artifact_user = old_auth
    assert response.status_code == 303, response.status_code
    assert response.headers["Location"] == "/admin/users", response.headers
    assert "artifact_explorer_session=" in response.headers["Set-Cookie"], response.headers
    assert "Path=/;" in response.headers["Set-Cookie"], response.headers
    print("PASS: login accepts portal next paths and sets a root-scoped UI session cookie")


def _test_existing_artifact_login_page_still_renders() -> None:
    response = api_main.artifact_explorer_login_page(_FakeRequest(path="/artifact-explorer/login"), next="/artifact-explorer")
    assert response.status_code == 200, response.status_code
    assert "Artifact Explorer Login" in _html(response), _html(response)
    print("PASS: existing Artifact Explorer login page still renders")


def main() -> None:
    _test_unauthenticated_user_redirects_to_login()
    _test_authenticated_user_portal_pages()
    _test_non_admin_cannot_access_admin()
    _test_admin_portal_pages()
    _test_login_allows_portal_next_paths_and_root_cookie()
    _test_existing_artifact_login_page_still_renders()


if __name__ == "__main__":
    main()
