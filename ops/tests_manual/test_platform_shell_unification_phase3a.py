#!/usr/bin/env python3
"""Phase 3A tests: unified Log Platform shell and module navigation.

Verifies the UI presents one platform ("Log Platform") with subordinate modules,
while preserving routes, the single session cookie, permissions, dark-alpha
design tokens, and Artifact Browser token-API behavior.

Run:

    cd /opt/log-platform
    env PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_platform_shell_unification_phase3a.py
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

FORBIDDEN = ("postgres://", "password=", "api_write_token", "api_read_token",
             "storage_key", "dbname=", "secret_access_key", "select * from",
             "from portal_", "traceback (most recent call last)")


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


def _no_secrets(html: str, label: str) -> None:
    low = html.lower()
    for needle in FORBIDDEN:
        assert needle not in low, f"{label}: forbidden content {needle!r} rendered"


# --------------------------------------------------------------------------
# 1-3: login + single session cookie
# --------------------------------------------------------------------------
def _test_login_renders_platform_brand() -> None:
    old = _with_current_user(None)
    try:
        page = api_main.platform_login_page(_FakeRequest(path="/login"), "/user")
    finally:
        api_main.get_current_artifact_user = old
    html = _html(page)
    assert page.status_code == 200, page.status_code
    assert "Log Platform" in html, html
    assert 'action="/login"' in html, "login form must post to canonical /login"
    assert "Artifact Explorer Login" in html, "compatibility <title> substring preserved"
    _no_secrets(html, "/login")
    print("PASS: /login renders Log Platform branding and posts to canonical /login")


def _test_legacy_login_compatible() -> None:
    old = _with_current_user(None)
    try:
        page = api_main.artifact_explorer_login_page(_FakeRequest(path="/artifact-explorer/login"), next="/artifact-explorer")
    finally:
        api_main.get_current_artifact_user = old
    html = _html(page)
    assert page.status_code == 200, page.status_code
    assert "Log Platform" in html and "Artifact Explorer Login" in html, html
    print("PASS: /artifact-explorer/login remains compatible")


def _test_single_session_cookie() -> None:
    old_auth = api_main.authenticate_artifact_user
    try:
        api_main.authenticate_artifact_user = lambda u, p: _user(admin=True) if (u, p) == ("alice", "pw") else None
        resp = api_main.artifact_explorer_login("alice", "pw", "/admin/users")
    finally:
        api_main.authenticate_artifact_user = old_auth
    assert resp.status_code == 303 and resp.headers["Location"] == "/admin/users", resp.headers
    cookie = resp.headers.get("Set-Cookie", "")
    assert cookie.startswith(api_main.ARTIFACT_EXPLORER_SESSION_COOKIE + "="), cookie
    assert "Path=/" in cookie, cookie
    print("PASS: login keeps the single root-scoped session cookie")


# --------------------------------------------------------------------------
# 4-8: unified shell + navigation
# --------------------------------------------------------------------------
def _render_user_pages():
    old = _with_current_user(_user())
    old_rf = api_main._list_accessible_portal_report_folders_for_user
    old_ds = api_main._list_accessible_portal_database_datasets_for_user
    api_main._list_accessible_portal_report_folders_for_user = lambda user_id: []
    api_main._list_accessible_portal_database_datasets_for_user = lambda user_id: []
    try:
        # S15 moved the `Raporty` nav slot to the Report Explorer library; the
        # pre-redesign folder index this stage asserts is unchanged and is
        # still served, now at its compatibility route.
        reports = api_main.user_portal_report_folders_legacy(
            _FakeRequest(path="/user/reports/folders"))
        database = api_main.user_portal_database(_FakeRequest(path="/user/database"))
    finally:
        api_main.get_current_artifact_user = old
        api_main._list_accessible_portal_report_folders_for_user = old_rf
        api_main._list_accessible_portal_database_datasets_for_user = old_ds
    return _html(reports), _html(database)


def _render_admin_pages():
    old = _with_current_user(_user(admin=True))
    saved = {
        "_list_portal_users": api_main._list_portal_users,
        "_list_portal_clients": api_main._list_portal_clients,
        "_list_portal_user_client_dashboard_rows": api_main._list_portal_user_client_dashboard_rows,
        "_list_portal_report_folders": api_main._list_portal_report_folders,
    }
    api_main._list_portal_users = lambda: []
    api_main._list_portal_clients = lambda: []
    api_main._list_portal_user_client_dashboard_rows = lambda: []
    api_main._list_portal_report_folders = lambda: []
    try:
        users = api_main.admin_portal_users(_FakeRequest(path="/admin/users"))
        report_folders = api_main.admin_portal_report_folders(_FakeRequest(path="/admin/report-folders"))
    finally:
        api_main.get_current_artifact_user = old
        for name, fn in saved.items():
            setattr(api_main, name, fn)
    return _html(users), _html(report_folders)


def _assert_unified_shell(html: str, module_label: str, label: str) -> None:
    """The approved shared shell (SHL-001/002).

    Supersedes the pre-redesign sidebar assertions: module identity now comes
    from the horizontal navigation plus the client context bar, not from a
    `portal-brand-module` caption inside a 268 px rail.
    """
    assert "lp-shell" in html and "portal-shell" in html, label
    assert "lp-appbar" in html and "lp-context" in html, label
    assert "lp-main" in html and "portal-main" in html, label
    assert "lp-brand-word" in html and "Log Platform" in html, label
    # The active module is named in the context bar and marked in the nav.
    assert module_label in html, f"{label}: module {module_label!r} not named in the shell"
    assert 'aria-current="page"' in html, f"{label}: active nav item must be semantic"
    # The old shell must not survive behind the new one.
    assert "portal-sidebar" not in html, f"{label}: old sidebar still rendered"
    assert "portal-breadcrumb" not in html, f"{label}: old breadcrumb strip still rendered"
    assert "#080b10" not in html and "#ff7300" not in html, f"{label}: superseded palette present"
    assert 'href="/logout"' in html, f"{label}: logout link should prefer /logout"
    assert "/artifact-explorer/logout" not in html, f"{label}: stale logout link present"
    _no_secrets(html, label)


def _test_admin_pages_unified_shell() -> None:
    users, report_folders = _render_admin_pages()
    _assert_unified_shell(users, "Administracja", "/admin/users")
    _assert_unified_shell(report_folders, "Administracja", "/admin/report-folders")
    print("PASS: /admin pages render the unified Log Platform shell")


def _test_user_pages_unified_shell() -> None:
    reports, database = _render_user_pages()
    _assert_unified_shell(reports, "Raporty", "/user/reports")
    _assert_unified_shell(database, "Dane", "/user/database")
    # SUPERSEDED EXPECTATION, REPLACED RATHER THAN DROPPED.
    #
    # This line used to assert the pre-redesign English module captions
    # (`Reports Explorer` / `Client Database Explorer`). `_portal_layout` no
    # longer renders `nav_items` labels at all: the shared shell builds its
    # primary navigation from `portal_shell.primary_label_for` and the approved
    # Polish vocabulary (`shell.nav.reports` = `Raporty`), which
    # `_assert_unified_shell` above already requires of both pages. The English
    # captions were the identity of the sidebar THIS STAGE REMOVED, so asserting
    # them asserted the shell the stage exists to replace — and the assertion
    # had been failing on `/user/database` since that redesign landed,
    # independently of S15.
    #
    # What the line was really protecting — that the two user modules are
    # distinguishable, each names itself, and each offers the other — is
    # asserted structurally here instead, which is strictly more than a
    # substring check.
    for html, own_href, other_href, other_label in (
        (reports, "/user/reports", "/user/database", "Dane"),
        (database, "/user/database", "/user/reports", "Raporty"),
    ):
        assert f'href="{own_href}" aria-current="page"' in html, \
            f"the active user module must be marked current: {own_href}"
        assert f'href="{other_href}"' in html, \
            f"the other user module must stay reachable: {other_href}"
        assert f">{other_label}</a>" in html, \
            f"the other user module must be named: {other_label}"
        assert f'href="{other_href}" aria-current="page"' not in html, \
            "only one navigation item may be current"
    print("PASS: /user pages render the unified Log Platform shell")


def _test_admin_nav_has_artifacts_link() -> None:
    users, _ = _render_admin_pages()
    assert 'href="/artifact-explorer"' in users, "admin nav must link the Artifacts/Artifact Explorer module"
    # The redesign renames the nav label to the approved Polish term.
    assert "Artefakty" in users, users
    print("PASS: admin navigation includes the Artifacts module link")


def _test_user_nav_hides_admin_items() -> None:
    reports, database = _render_user_pages()
    for html in (reports, database):
        assert 'href="/admin/users"' not in html, "regular user nav must not expose admin routes"
        assert 'href="/artifact-explorer"' not in html, "regular user nav must not expose the Artifact Explorer admin module"
        assert "Technical Artifact Explorer" not in html, html
    print("PASS: regular user navigation hides admin-only items")


# --------------------------------------------------------------------------
# 9-12: routes, artifact module positioning, token API, no secrets
# --------------------------------------------------------------------------
def _test_artifact_explorer_module_positioning() -> None:
    # The Artifacts module renders inside the approved shared shell. Pass an
    # admin user, since Artefakty is an operator surface and is admin-only.
    page = api_main._artifact_explorer_layout("Artifact Explorer", "<p>body</p>", user=_user(admin=True))
    html = _html(page)
    assert "Log Platform" in html and "Artefakty" in html, html
    # Approved shared shell, not the old sidebar and not the older light teal.
    assert "lp-shell" in html and "lp-appbar" in html and "portal-main" in html, html
    assert "portal-sidebar" not in html, html
    assert "#080b10" not in html and "#ff7300" not in html, html
    assert "#176b5d" not in html and "--bg: #f7f7f5" not in html, "old light teal theme must be gone"
    # Artefakty is a tools-group entry, never a fourth data-access mode.
    assert 'data-nav-group="tools"' in html, html
    # still links the existing artifact routes and the platform modules
    assert 'href="/artifact-explorer"' in html and 'href="/artifact-explorer/folders"' in html, html
    assert 'href="/admin"' in html and 'href="/user"' in html and 'href="/logout"' in html, html
    # Combo + copy progressive enhancement preserved.
    #
    # SUPERSEDED EXPECTATION, REPLACED RATHER THAN DROPPED. These two markers
    # used to appear because the behaviour shipped as an inline `<script>` in
    # this layout. The asset-extraction stage moved it verbatim into
    # `api/static/js/artifact-explorer.js`, which the layout now loads with a
    # cache-busted `defer` tag — so the inline markers are absent by design and
    # this assertion had been failing since that move, independently of S15.
    #
    # The requirement is that the behaviour is still SHIPPED, so that is what is
    # asserted: the module is loaded by the page, and it still implements both
    # enhancements. Reading the served asset is a stronger check than the
    # substring it replaces.
    assert 'src="/static/js/artifact-explorer.js' in html, html
    _artifact_js = (REPO_ROOT / "api" / "static" / "js" / "artifact-explorer.js").read_text(
        encoding="utf-8"
    )
    assert "data-combo" in _artifact_js and "data-copy-value" in _artifact_js, \
        "artifact combo/copy progressive enhancement must be preserved"
    _no_secrets(html, "artifact-explorer layout")
    print("PASS: Artifact Explorer renders inside the unified Log Platform shell")


def _test_routes_and_token_api_unchanged() -> None:
    for name in (
        "platform_login_page", "platform_login_route", "platform_logout_get_route",
        "platform_logout_post_route", "artifact_explorer_login_page",
        "artifact_explorer_login_route", "artifact_explorer_logout_get_route",
        "artifact_explorer_logout_post_route",
    ):
        assert callable(getattr(api_main, name)), name
    # Artifact Browser token API auth helper is untouched.
    assert callable(api_main.require_token)
    assert api_main.ARTIFACT_EXPLORER_SESSION_COOKIE == "artifact_explorer_session"
    assert api_main.ARTIFACT_EXPLORER_SESSION_COOKIE_PATH == "/"
    print("PASS: existing routes and Artifact Browser token API behavior unchanged")


def _test_forbidden_page_unified_and_clean() -> None:
    old = _with_current_user(_user())
    try:
        resp = api_main.admin_portal_users(_FakeRequest(path="/admin/users"))
    finally:
        api_main.get_current_artifact_user = old
    html = _html(resp)
    assert resp.status_code == 403, resp.status_code
    assert "Access denied" in html, html
    assert "Log Platform" in html and "portal-shell" in html, html
    _no_secrets(html, "forbidden page")
    print("PASS: 403 page uses the unified shell and exposes no internal details")


def main() -> None:
    _test_login_renders_platform_brand()
    _test_legacy_login_compatible()
    _test_single_session_cookie()
    _test_admin_pages_unified_shell()
    _test_user_pages_unified_shell()
    _test_admin_nav_has_artifacts_link()
    _test_user_nav_hides_admin_items()
    _test_artifact_explorer_module_positioning()
    _test_routes_and_token_api_unchanged()
    _test_forbidden_page_unified_and_clean()
    print("\nALL PASS: Phase 3A unified Log Platform shell and module navigation (routes/permissions preserved)")


if __name__ == "__main__":
    main()
