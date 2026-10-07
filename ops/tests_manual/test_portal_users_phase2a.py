#!/usr/bin/env python3
"""Manual regression tests for Admin Portal Phase 2A local UI user management.

Run:

    cd /opt/log-platform
    env PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_portal_users_phase2a.py
"""
from __future__ import annotations

import inspect
import sys
import types
from datetime import datetime, timezone
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


USER_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
TARGET_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
ROLE_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"


class _FakeUrl:
    def __init__(self, path="/admin/users", query=""):
        self.path = path
        self.query = query


class _FakeRequest:
    def __init__(self, *, path="/admin/users", query="", cookies=None):
        self.url = _FakeUrl(path, query)
        self.cookies = cookies or {}


def _html(response) -> str:
    return response.body.decode("utf-8")


def _user(*, admin=False, user_id=USER_ID):
    return {
        "user_id": user_id,
        "username": "alice",
        "display_name": "Alice",
        "is_active": True,
        "is_admin": admin,
        "permissions": [],
    }


def _target(**overrides):
    data = {
        "user_id": TARGET_ID,
        "username": "bob",
        "display_name": "Bob",
        "is_active": True,
        "is_admin": False,
        "roles": [{"role_id": ROLE_ID, "role_name": "Analyst"}],
        "created_at": datetime(2026, 5, 28, 8, 0, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 5, 28, 9, 0, tzinfo=timezone.utc),
    }
    data.update(overrides)
    return data


def _role(**overrides):
    data = {"role_id": ROLE_ID, "role_name": "Analyst", "description": "Test role"}
    data.update(overrides)
    return data


def _set_current_user(user):
    old = api_main.get_current_artifact_user
    api_main.get_current_artifact_user = lambda request: user
    return old


def _patch(name, value):
    old = getattr(api_main, name)
    setattr(api_main, name, value)
    return old


def _restore(patches):
    for name, old in reversed(patches):
        setattr(api_main, name, old)


def _test_admin_auth_boundaries() -> None:
    old = _set_current_user(None)
    try:
        unauth = api_main.admin_portal_users(_FakeRequest())
    finally:
        api_main.get_current_artifact_user = old
    assert unauth.status_code == 303, unauth.status_code
    assert unauth.headers["Location"].startswith("/artifact-explorer/login"), unauth.headers

    old = _set_current_user(_user(admin=False))
    try:
        forbidden = api_main.admin_portal_users(_FakeRequest())
    finally:
        api_main.get_current_artifact_user = old
    assert forbidden.status_code == 403, forbidden.status_code
    assert "Access denied" in _html(forbidden), _html(forbidden)
    print("PASS: /admin/users requires authenticated admin access")


def _test_admin_user_list_and_create_form() -> None:
    patches = [
        ("get_current_artifact_user", _patch("get_current_artifact_user", lambda request: _user(admin=True))),
        ("_list_portal_users", _patch("_list_portal_users", lambda: [_target()])),
        ("_list_portal_roles", _patch("_list_portal_roles", lambda: [_role()])),
    ]
    try:
        listing = api_main.admin_portal_users(_FakeRequest())
        form = api_main.admin_portal_new_user_form(_FakeRequest(path="/admin/users/new"))
    finally:
        _restore(patches)
    listing_html = _html(listing)
    form_html = _html(form)
    assert "Create user" in listing_html, listing_html
    assert "bob" in listing_html and "Analyst" in listing_html, listing_html
    assert "Local UI users" in listing_html, listing_html
    assert "Initial assigned roles" in form_html and "Analyst" in form_html, form_html
    print("PASS: admin can view user list and create-user form")


def _test_create_user_hashes_password_and_validates() -> None:
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return TARGET_ID

    patches = [
        ("get_current_artifact_user", _patch("get_current_artifact_user", lambda request: _user(admin=True))),
        ("_list_portal_roles", _patch("_list_portal_roles", lambda: [_role()])),
        ("_portal_username_exists", _patch("_portal_username_exists", lambda username, exclude_user_id=None: username == "taken")),
        ("_create_portal_user", _patch("_create_portal_user", fake_create)),
    ]
    try:
        ok = api_main.admin_portal_create_user(
            request=_FakeRequest(path="/admin/users/new"),
            username=" bob ",
            display_name="Bob",
            password="secret",
            confirm_password="secret",
            is_admin="1",
            role_ids=[ROLE_ID],
        )
        duplicate = api_main.admin_portal_create_user(
            request=_FakeRequest(path="/admin/users/new"),
            username="taken",
            display_name="Taken",
            password="secret",
            confirm_password="secret",
            is_admin=None,
            role_ids=None,
        )
        mismatch = api_main.admin_portal_create_user(
            request=_FakeRequest(path="/admin/users/new"),
            username="charlie",
            display_name="Charlie",
            password="one",
            confirm_password="two",
            is_admin=None,
            role_ids=None,
        )
    finally:
        _restore(patches)

    assert ok.status_code == 303 and ok.headers["Location"] == f"/admin/users/{TARGET_ID}", ok.headers
    assert captured["username"] == "bob", captured
    assert captured["is_admin"] is True, captured
    assert captured["role_ids"] == [ROLE_ID], captured
    assert "secret" not in captured["password_hash"], captured
    assert api_main.verify_artifact_password("secret", captured["password_hash"]), captured
    assert duplicate.status_code == 400 and "Username already exists" in _html(duplicate), _html(duplicate)
    assert mismatch.status_code == 400 and "Password confirmation does not match" in _html(mismatch), _html(mismatch)
    print("PASS: create user validates input and hashes password before persistence")


def _test_detail_update_password_and_roles() -> None:
    calls = []

    def fake_update(**kwargs):
        calls.append(("update", kwargs))
        return None

    def fake_reset(**kwargs):
        calls.append(("reset", kwargs))
        return None

    def fake_assign(**kwargs):
        calls.append(("assign", kwargs))
        return None

    def fake_remove(**kwargs):
        calls.append(("remove", kwargs))
        return None

    patches = [
        ("get_current_artifact_user", _patch("get_current_artifact_user", lambda request: _user(admin=True))),
        ("_get_portal_user", _patch("_get_portal_user", lambda user_id: _target() if user_id == TARGET_ID else None)),
        ("_list_portal_roles", _patch("_list_portal_roles", lambda: [_role(), _role(role_id="dddddddd-dddd-dddd-dddd-dddddddddddd", role_name="Viewer")])),
        ("_update_portal_user", _patch("_update_portal_user", fake_update)),
        ("_reset_portal_user_password", _patch("_reset_portal_user_password", fake_reset)),
        ("_assign_portal_user_role", _patch("_assign_portal_user_role", fake_assign)),
        ("_remove_portal_user_role", _patch("_remove_portal_user_role", fake_remove)),
    ]
    try:
        detail = api_main.admin_portal_user_detail(TARGET_ID, _FakeRequest(path=f"/admin/users/{TARGET_ID}"))
        update = api_main.admin_portal_update_user(TARGET_ID, _FakeRequest(), display_name="Bobby", is_active="1", is_admin="1")
        reset = api_main.admin_portal_reset_user_password(TARGET_ID, _FakeRequest(), new_password="new-secret", confirm_password="new-secret")
        reset_mismatch = api_main.admin_portal_reset_user_password(TARGET_ID, _FakeRequest(), new_password="a", confirm_password="b")
        assign = api_main.admin_portal_assign_user_role(TARGET_ID, _FakeRequest(), role_id="dddddddd-dddd-dddd-dddd-dddddddddddd")
        remove = api_main.admin_portal_remove_user_role(TARGET_ID, ROLE_ID, _FakeRequest())
    finally:
        _restore(patches)

    detail_html = _html(detail)
    assert "Assigned roles" in detail_html and "Roles control access" in detail_html, detail_html
    assert update.status_code == 303 and update.headers["Location"] == f"/admin/users/{TARGET_ID}", update.headers
    assert reset.status_code == 303 and reset.headers["Location"] == f"/admin/users/{TARGET_ID}", reset.headers
    reset_call = [kwargs for kind, kwargs in calls if kind == "reset"][0]
    assert api_main.verify_artifact_password("new-secret", reset_call["password_hash"]), reset_call
    assert reset_mismatch.status_code == 400 and "Password confirmation does not match" in _html(reset_mismatch), _html(reset_mismatch)
    assert assign.status_code == 303 and remove.status_code == 303, (assign.status_code, remove.status_code)
    assert ("assign", {"user_id": TARGET_ID, "role_id": "dddddddd-dddd-dddd-dddd-dddddddddddd"}) in calls, calls
    assert ("remove", {"user_id": TARGET_ID, "role_id": ROLE_ID}) in calls, calls
    print("PASS: detail, safe edits, password reset, and role assignment routes work")


def _test_last_admin_protection() -> None:
    old = _patch("_count_active_admin_users", lambda: 1)
    try:
        error = api_main._validate_last_admin_change(_target(is_admin=True), new_is_active=True, new_is_admin=False)
        inactive_error = api_main._validate_last_admin_change(_target(is_admin=True), new_is_active=False, new_is_admin=True)
    finally:
        api_main._count_active_admin_users = old
    assert error and "last active admin" in error, error
    assert inactive_error and "last active admin" in inactive_error, inactive_error

    old = _patch("_count_active_admin_users", lambda: 2)
    try:
        allowed = api_main._validate_last_admin_change(_target(is_admin=True), new_is_active=True, new_is_admin=False)
    finally:
        api_main._count_active_admin_users = old
    assert allowed is None, allowed
    print("PASS: last-active-admin protection blocks unsafe demotion/deactivation")


def _test_existing_portal_and_artifact_routes_still_work() -> None:
    # `/user/reports` is the S15 Report Explorer library since `891106e`; the
    # pre-redesign folder index it replaced is served, unchanged and with its
    # authorization intact, from the compatibility route `/user/reports/folders`
    # (`docs/39` §11). This suite has no database, so it exercises the surface
    # that never needed one — the folder index — and asserts the S15 route's
    # identity structurally below rather than rendering it.
    patches = [
        ("get_current_artifact_user", _patch("get_current_artifact_user", lambda request: _user(admin=False))),
        ("_list_accessible_portal_report_folders_for_user", _patch("_list_accessible_portal_report_folders_for_user", lambda user_id: [])),
    ]
    try:
        folders = api_main.user_portal_report_folders_legacy(
            _FakeRequest(path="/user/reports/folders")
        )
    finally:
        _restore(patches)
    assert folders.status_code == 200 and "Reports Explorer" in _html(folders), _html(folders)

    # The two surfaces are distinct routes, and the S15 library — not the folder
    # index — owns `/user/reports`. Asserted on the wiring so the check holds
    # without a database and cannot pass by accident if the routes are swapped.
    assert api_main.user_portal_reports is not api_main.user_portal_report_folders_legacy
    reports_source = inspect.getsource(api_main.user_portal_reports)
    assert "_report_explorer_pages.library" in reports_source, reports_source
    folders_source = inspect.getsource(api_main.user_portal_report_folders_legacy)
    assert "_user_reports_response" in folders_source, folders_source
    routes = {
        route.path: route.endpoint
        for route in api_main.app.routes
        if getattr(route, "path", None) in {"/user/reports", "/user/reports/folders"}
    }
    assert routes.get("/user/reports") is api_main.user_portal_reports, routes
    assert routes.get("/user/reports/folders") is api_main.user_portal_report_folders_legacy, routes

    # Both surfaces authorize identically and before doing anything else: an
    # unauthenticated request is turned away by the same guard, not by a
    # difference between the old and the new page.
    denied = _patch("get_current_artifact_user", lambda request: None)
    try:
        for handler, path in (
            (api_main.user_portal_reports, "/user/reports"),
            (api_main.user_portal_report_folders_legacy, "/user/reports/folders"),
        ):
            response = handler(_FakeRequest(path=path))
            assert response.status_code in (302, 303, 401, 403), (path, response.status_code)
    finally:
        api_main.get_current_artifact_user = denied

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
    print("PASS: existing user portal, Artifact Explorer login, and token auth behavior remain intact")


def main() -> None:
    _test_admin_auth_boundaries()
    _test_admin_user_list_and_create_form()
    _test_create_user_hashes_password_and_validates()
    _test_detail_update_password_and_roles()
    _test_last_admin_protection()
    _test_existing_portal_and_artifact_routes_still_work()


if __name__ == "__main__":
    main()
