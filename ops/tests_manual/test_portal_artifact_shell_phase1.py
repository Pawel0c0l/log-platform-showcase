#!/usr/bin/env python3
"""Phase 1 (unified portal shell) regression: the Artifacts module renders inside
the shared dark/alpha Log Platform shell instead of its former light teal layout.

Verifies real Artifacts routes/render helpers use the approved shared shell,
the dark/alpha design tokens, the single `/logout` link, and preserve the
combo-filter + copy-to-clipboard progressive-enhancement scripts. Routes,
permissions, and the Artifact Browser token API are unchanged.

Run:

    cd /opt/log-platform
    env PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_portal_artifact_shell_phase1.py
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
             "dbname=", "secret_access_key", "select * from", "from portal_",
             "traceback (most recent call last)")

LIGHT_THEME_MARKERS = ("#176b5d", "--bg: #f7f7f5", "background: #26312f", "--accent: #176b5d")


class _FakeUrl:
    def __init__(self, path="/artifact-explorer", query=""):
        self.path = path
        self.query = query


class _FakeRequest:
    def __init__(self, *, path="/artifact-explorer", query="", cookies=None):
        self.url = _FakeUrl(path, query)
        self.cookies = cookies or {}


def _html(response) -> str:
    return response.body.decode("utf-8")


def _user(*, admin=True):
    return {
        "user_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "username": "alice",
        "display_name": "Alice",
        "is_active": True,
        "is_admin": admin,
        "permissions": [],
    }


def _assert_approved_shell(html: str, label: str, *, expect_nav: bool = True) -> None:
    """Approved shared shell. Supersedes the pre-redesign sidebar/dark-token
    assertions; the Artifacts module identity now comes from the horizontal nav
    and the context bar rather than a `portal-brand-module` caption."""
    assert "lp-shell" in html and "portal-shell" in html and "portal-main" in html, label
    assert "lp-appbar" in html and "lp-context" in html, label
    assert "lp-brand-word" in html and "Log Platform" in html, label
    if expect_nav:
        assert "Artefakty" in html, label
        # Artefakty stays a tools-group surface, not a fourth data-access mode.
        assert 'data-nav-group="tools"' in html, label
    else:
        # A page rendered without a signed-in account keeps the shell frame so
        # the error is not a bare page, and shows only the two entries that are
        # visible to every account. It must never leak an admin-only entry --
        # that is the property worth asserting here, and it matches the
        # pre-redesign sidebar, which also rendered on this page.
        assert 'href="/admin"' not in html, f"{label}: admin nav leaked on a denied page"
        assert 'href="/artifact-explorer"' not in html, f"{label}: operator nav leaked"
        # "Artefakty" still appears as the module label in the context bar --
        # SH-6 keeps context visible on denied states -- so assert on links,
        # not on the word.
    assert "portal-sidebar" not in html, f"{label}: old sidebar still rendered"
    assert "#080b10" not in html and "#ff7300" not in html, f"{label}: superseded palette"
    assert 'href="/logout"' in html, f"{label}: must use the canonical /logout link"
    for marker in LIGHT_THEME_MARKERS:
        assert marker not in html, f"{label}: stale light teal theme marker {marker!r} present"
    low = html.lower()
    for needle in FORBIDDEN:
        assert needle not in low, f"{label}: forbidden content {needle!r} rendered"


def _test_layout_helper_dark_shell() -> None:
    page = api_main._artifact_explorer_layout("Artifact Explorer", "<div class='panel'>x</div>", user=_user())
    html = _html(page)
    _assert_approved_shell(html, "_artifact_explorer_layout")
    # The combo-filter and copy-to-clipboard progressive enhancements moved out
    # of an inline <script> into the shared page-scoped asset architecture in
    # S11. Asserting the versioned asset is what proves they still ship.
    assert "/static/js/artifact-explorer.js?v=" in html, "module script asset must be loaded"
    assert "/static/css/artifact-explorer.css?v=" in html, "module stylesheet asset must be loaded"
    print("PASS: _artifact_explorer_layout renders the approved shared shell")


def _test_folders_index_dark_shell() -> None:
    old_user = api_main.get_current_artifact_user
    old_list = api_main._list_virtual_folders
    api_main.get_current_artifact_user = lambda request: _user()
    api_main._list_virtual_folders = lambda parent: []
    try:
        page = api_main.artifact_explorer_folders_index(_FakeRequest(path="/artifact-explorer/folders"))
    finally:
        api_main.get_current_artifact_user = old_user
        api_main._list_virtual_folders = old_list
    html = _html(page)
    _assert_approved_shell(html, "/artifact-explorer/folders")
    assert "Virtual folders" in html, html
    print("PASS: /artifact-explorer/folders renders the approved shared shell")


def _test_forbidden_dark_shell() -> None:
    page = api_main._artifact_explorer_forbidden("nope")
    html = _html(page)
    assert page.status_code == 403, page.status_code
    _assert_approved_shell(html, "artifact forbidden", expect_nav=False)
    print("PASS: Artifacts forbidden page uses the approved shared shell without navigation")


def main() -> None:
    _test_layout_helper_dark_shell()
    _test_folders_index_dark_shell()
    _test_forbidden_dark_shell()
    print("\nALL PASS: Artifacts module renders in the approved shared shell (scripts preserved)")


if __name__ == "__main__":
    main()
