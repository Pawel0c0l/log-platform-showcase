#!/usr/bin/env python3
"""Manual regression tests for Admin Portal Phase 2B client access foundation.

Run:

    cd /opt/log-platform
    env PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_portal_client_access_phase2b.py
"""
from __future__ import annotations

import inspect
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


class _Route:
    """What FastAPI would have recorded: the path and the handler bound to it."""

    def __init__(self, path: str, endpoint, methods: tuple[str, ...]):
        self.path = path
        self.endpoint = endpoint
        self.methods = set(methods)


class _App:
    """A FastAPI stand-in that REMEMBERS its routing table.

    The stub used to throw registrations away, so a test could only ask what a
    handler does, never which URL actually reaches it. S15 moved the
    pre-redesign folder index off `/user/reports` and put the Report Explorer
    library there; proving that swap happened the right way round needs the
    path-to-handler binding, so the stub now keeps it.
    """

    def __init__(self, *args, **kwargs):
        self.routes: list[_Route] = []

    def _record(self, method: str):
        def decorator_factory(path: str = "", *args, **kwargs):
            def decorator(fn):
                self.routes.append(_Route(path, fn, (method,)))
                return fn
            return decorator
        return decorator_factory

    def get(self, *args, **kwargs):
        return self._record("GET")(*args, **kwargs)

    def post(self, *args, **kwargs):
        return self._record("POST")(*args, **kwargs)

    def patch(self, *args, **kwargs):
        return self._record("PATCH")(*args, **kwargs)

    def delete(self, *args, **kwargs):
        return self._record("DELETE")(*args, **kwargs)

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


ADMIN_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
USER_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


class _FakeUrl:
    def __init__(self, path="/admin/client-access", query=""):
        self.path = path
        self.query = query


class _FakeRequest:
    def __init__(self, *, path="/admin/client-access", query="", cookies=None):
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


def _assignment(**overrides):
    data = _client()
    data.update({
        "can_view_database": True,
        "can_view_reports": True,
        "can_export_database": False,
        "granted_by_username": "admin",
    })
    data.update(overrides)
    return data


def _patch(name, value):
    old = getattr(api_main, name)
    setattr(api_main, name, value)
    return old


def _restore(patches):
    for name, old in reversed(patches):
        setattr(api_main, name, old)


def _test_admin_auth_boundaries() -> None:
    old = _patch("get_current_artifact_user", lambda request: None)
    try:
        unauth = api_main.admin_portal_client_access(_FakeRequest())
    finally:
        api_main.get_current_artifact_user = old
    assert unauth.status_code == 303, unauth.status_code
    assert unauth.headers["Location"].startswith("/artifact-explorer/login"), unauth.headers

    old = _patch("get_current_artifact_user", lambda request: _user(admin=False))
    try:
        forbidden = api_main.admin_portal_client_access(_FakeRequest())
    finally:
        api_main.get_current_artifact_user = old
    assert forbidden.status_code == 403, forbidden.status_code
    assert "Access denied" in _html(forbidden), _html(forbidden)
    print("PASS: /admin/client-access requires authenticated admin access")


def _test_dashboard_and_create_form_render() -> None:
    patches = [
        ("get_current_artifact_user", _patch("get_current_artifact_user", lambda request: _user(admin=True, user_id=ADMIN_ID, username="admin"))),
        ("_list_portal_clients", _patch("_list_portal_clients", lambda: [_client()])),
        ("_list_portal_user_client_dashboard_rows", _patch("_list_portal_user_client_dashboard_rows", lambda: [{
            "user_id": USER_ID,
            "username": "alice",
            "display_name": "Alice",
            "is_admin": False,
            "clients": [_client()],
        }])),
    ]
    try:
        dashboard = api_main.admin_portal_client_access(_FakeRequest())
        form = api_main.admin_portal_new_client_form(_FakeRequest(path="/admin/client-access/clients/new"))
    finally:
        _restore(patches)
    dashboard_html = _html(dashboard)
    form_html = _html(form)
    assert "Client registry" in dashboard_html and "ACME_01" in dashboard_html, dashboard_html
    assert "does not grant arbitrary SQL access" in dashboard_html, dashboard_html
    assert "Create client" in form_html and "Client code" in form_html, form_html
    print("PASS: admin dashboard and create-client form render")


def _test_create_client_validation_and_redirect() -> None:
    created = {}

    def fake_create(**kwargs):
        created.update(kwargs)
        return kwargs["client_code"]

    patches = [
        ("get_current_artifact_user", _patch("get_current_artifact_user", lambda request: _user(admin=True, user_id=ADMIN_ID, username="admin"))),
        ("_portal_client_exists", _patch("_portal_client_exists", lambda code: code == "DUPLICATE")),
        ("_create_portal_client", _patch("_create_portal_client", fake_create)),
    ]
    try:
        ok = api_main.admin_portal_create_client(
            request=_FakeRequest(path="/admin/client-access/clients/new"),
            client_code=" acme_01 ",
            display_name="Acme Logistics",
            description="Primary test client",
            is_active="1",
        )
        duplicate = api_main.admin_portal_create_client(
            request=_FakeRequest(path="/admin/client-access/clients/new"),
            client_code="duplicate",
            display_name="Duplicate",
            description=None,
            is_active="1",
        )
        invalid = api_main.admin_portal_create_client(
            request=_FakeRequest(path="/admin/client-access/clients/new"),
            client_code="bad code",
            display_name="Bad",
            description=None,
            is_active="1",
        )
        missing_name = api_main.admin_portal_create_client(
            request=_FakeRequest(path="/admin/client-access/clients/new"),
            client_code="EMPTYNAME",
            display_name=" ",
            description=None,
            is_active="1",
        )
    finally:
        _restore(patches)

    assert ok.status_code == 303 and ok.headers["Location"] == "/admin/client-access/clients/ACME_01", ok.headers
    assert created["client_code"] == "ACME_01" and created["is_active"] is True, created
    assert duplicate.status_code == 400 and "Client code already exists" in _html(duplicate), _html(duplicate)
    assert invalid.status_code == 400 and "Client code must be" in _html(invalid), _html(invalid)
    assert missing_name.status_code == 400 and "Display name is required" in _html(missing_name), _html(missing_name)
    print("PASS: client create normalizes codes, validates input, and redirects after success")


def _test_edit_client() -> None:
    calls = []

    def fake_update(**kwargs):
        calls.append(kwargs)
        return None

    patches = [
        ("get_current_artifact_user", _patch("get_current_artifact_user", lambda request: _user(admin=True, user_id=ADMIN_ID, username="admin"))),
        ("_get_portal_client", _patch("_get_portal_client", lambda code: _client(client_code=code) if code == "ACME_01" else None)),
        ("_update_portal_client", _patch("_update_portal_client", fake_update)),
    ]
    try:
        detail = api_main.admin_portal_client_detail("acme_01", _FakeRequest(path="/admin/client-access/clients/ACME_01"))
        update = api_main.admin_portal_update_client(
            "acme_01",
            _FakeRequest(path="/admin/client-access/clients/ACME_01"),
            display_name="Acme Updated",
            description="Updated",
            is_active=None,
        )
    finally:
        _restore(patches)
    assert detail.status_code == 200 and "Edit client" in _html(detail), _html(detail)
    assert update.status_code == 303 and update.headers["Location"] == "/admin/client-access/clients/ACME_01", update.headers
    assert calls == [{"client_code": "ACME_01", "display_name": "Acme Updated", "description": "Updated", "database_name": "", "is_active": False}], calls
    print("PASS: admin can view and edit a portal client")


def _test_manage_user_client_access() -> None:
    calls = []

    def assignments(user_id):
        assert user_id == USER_ID, user_id
        return {
            "user": _user(admin=False, user_id=USER_ID, username="alice"),
            "assigned": [_assignment()],
            "available": [_client(client_code="BETA_02", display_name="Beta Transport", description=None, assigned_users=0)],
        }

    def fake_assign(**kwargs):
        calls.append(("assign", kwargs))
        return None

    def fake_update(**kwargs):
        calls.append(("update", kwargs))
        return None

    def fake_remove(**kwargs):
        calls.append(("remove", kwargs))
        return None

    patches = [
        ("get_current_artifact_user", _patch("get_current_artifact_user", lambda request: _user(admin=True, user_id=ADMIN_ID, username="admin"))),
        ("_get_portal_user_client_assignments", _patch("_get_portal_user_client_assignments", assignments)),
        ("_assign_portal_user_client", _patch("_assign_portal_user_client", fake_assign)),
        ("_update_portal_user_client_permissions", _patch("_update_portal_user_client_permissions", fake_update)),
        ("_remove_portal_user_client", _patch("_remove_portal_user_client", fake_remove)),
    ]
    try:
        page = api_main.admin_portal_user_client_access(USER_ID, _FakeRequest(path=f"/admin/client-access/users/{USER_ID}"))
        assign = api_main.admin_portal_assign_client_to_user(USER_ID, _FakeRequest(), client_code="beta_02")
        duplicate_safe = api_main.admin_portal_assign_client_to_user(USER_ID, _FakeRequest(), client_code="acme_01")
        update = api_main.admin_portal_update_user_client_permissions(
            USER_ID,
            "acme_01",
            _FakeRequest(),
            can_view_database="1",
            can_view_reports=None,
            can_export_database="1",
        )
        remove = api_main.admin_portal_remove_client_from_user(USER_ID, "acme_01", _FakeRequest())
    finally:
        _restore(patches)

    page_html = _html(page)
    assert "Assigned clients" in page_html and "ACME_01" in page_html and "BETA_02" in page_html, page_html
    assert assign.status_code == 303 and duplicate_safe.status_code == 303, (assign.status_code, duplicate_safe.status_code)
    assert update.status_code == 303 and remove.status_code == 303, (update.status_code, remove.status_code)
    assert ("assign", {"user_id": USER_ID, "client_code": "BETA_02", "granted_by": ADMIN_ID}) in calls, calls
    assert ("assign", {"user_id": USER_ID, "client_code": "ACME_01", "granted_by": ADMIN_ID}) in calls, calls
    assert ("update", {"user_id": USER_ID, "client_code": "ACME_01", "can_view_database": True, "can_view_reports": False, "can_export_database": True}) in calls, calls
    assert ("remove", {"user_id": USER_ID, "client_code": "ACME_01"}) in calls, calls
    print("PASS: admin can assign, safely re-assign, update flags, and remove client access")


def _test_user_portal_database_uses_dataset_cards_after_phase3a() -> None:
    seen = []

    def fake_datasets(user_id):
        seen.append(user_id)
        return [{
            "dataset_id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
            "client_code": "DB_ONLY",
            "client_display_name": "Database Client",
            "dataset_name": "Approved trips",
            "slug": "approved-trips",
            "description": "Approved portal dataset",
            "visible_columns": 3,
            "default_date_column": "trip_date",
            "can_filter_rows": True,
            "can_export_rows": False,
        }]

    patches = [
        ("get_current_artifact_user", _patch("get_current_artifact_user", lambda request: _user(admin=False, user_id=USER_ID, username="alice"))),
        ("_list_accessible_portal_database_datasets_for_user", _patch("_list_accessible_portal_database_datasets_for_user", fake_datasets)),
        ("_list_accessible_portal_report_folders_for_user", _patch("_list_accessible_portal_report_folders_for_user", lambda user_id: [])),
    ]
    try:
        database = api_main.user_portal_database(_FakeRequest(path="/user/database"))
        # Since S15, `/user/reports` is the Report Explorer library and needs a
        # database. The pre-redesign folder index this suite asserts against
        # moved, unchanged, to the compatibility route `/user/reports/folders`
        # (`docs/39` §11), which is the surface that never needed one.
        folders = api_main.user_portal_report_folders_legacy(
            _FakeRequest(path="/user/reports/folders")
        )
    finally:
        _restore(patches)

    database_html = _html(database)
    folders_html = _html(folders)
    assert "Approved trips" in database_html and "Database Client" in database_html, database_html
    # Approved stage S9 replaced the card grid with the `DB-001` comparison table.
    assert "Otw\u00f3rz arkusz" in database_html and "db-cat-table" in database_html, database_html
    assert "No report folders have been assigned yet" in folders_html, folders_html
    assert USER_ID in seen, seen

    # A `can_view_database`-only account reaches the Database Explorer and the
    # folder surface it was granted, and neither page leaks the other client's
    # data. The S15 library owns `/user/reports` and is a different handler —
    # asserted on the routing table so this holds without a database.
    routes = {
        route.path: route.endpoint
        for route in api_main.app.routes
        if getattr(route, "path", None) in {"/user/reports", "/user/reports/folders"}
    }
    assert routes.get("/user/reports") is api_main.user_portal_reports, routes
    assert routes.get("/user/reports/folders") is api_main.user_portal_report_folders_legacy, routes
    assert "_report_explorer_pages.library" in inspect.getsource(api_main.user_portal_reports)

    # Authorization is unchanged on BOTH: no portal user, no page.
    denied = _patch("get_current_artifact_user", lambda request: None)
    try:
        for handler, path in (
            (api_main.user_portal_database, "/user/database"),
            (api_main.user_portal_reports, "/user/reports"),
            (api_main.user_portal_report_folders_legacy, "/user/reports/folders"),
        ):
            response = handler(_FakeRequest(path=path))
            assert response.status_code in (302, 303, 401, 403), (path, response.status_code)
    finally:
        api_main.get_current_artifact_user = denied
    print("PASS: the database page renders assigned datasets and the folder surface "
          "stays reachable at its compatibility route after S15")


def _test_existing_admin_users_artifact_and_token_behavior_still_work() -> None:
    patches = [
        ("get_current_artifact_user", _patch("get_current_artifact_user", lambda request: _user(admin=True, user_id=ADMIN_ID, username="admin"))),
        ("_list_portal_users", _patch("_list_portal_users", lambda: [])),
    ]
    try:
        users = api_main.admin_portal_users(_FakeRequest(path="/admin/users"))
    finally:
        _restore(patches)
    assert users.status_code == 200 and "Users" in _html(users), _html(users)

    login = api_main.artifact_explorer_login_page(_FakeRequest(path="/artifact-explorer/login"), next="/artifact-explorer")
    assert login.status_code == 200 and "Artifact Explorer Login" in _html(login), _html(login)

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
    print("PASS: existing admin users, Artifact Explorer login, and token auth behavior remain intact")


def main() -> None:
    _test_admin_auth_boundaries()
    _test_dashboard_and_create_form_render()
    _test_create_client_validation_and_redirect()
    _test_edit_client()
    _test_manage_user_client_access()
    _test_user_portal_database_uses_dataset_cards_after_phase3a()
    _test_existing_admin_users_artifact_and_token_behavior_still_work()


if __name__ == "__main__":
    main()
