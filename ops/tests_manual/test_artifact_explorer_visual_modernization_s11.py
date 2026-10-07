#!/usr/bin/env python3
"""S11 — `ARTIFACT_EXPLORER_VISUAL_MODERNIZATION` regression.

Artifact Explorer adopts the approved shared platform shell, the shared token
layer and the `ART-001` operator treatment without changing what it is or what
anyone is allowed to do. This suite renders real Artifact Explorer responses and
asserts the approved contract on the rendered HTML:

* `AR-1` — `TRYB OPERATORA` in the app bar and a neutral (non-accent) context
  rule; the context bar never invents a client;
* `AR-2` — the screen states in words that Artifacts is not a client-data
  access mode;
* `AR-3` — the artifact-kind rail with counts;
* `AR-4` — the `Artefakty` nav item is absent without the operator permission,
  and the module's permission state renders without leaking operator links;
* `AR-5` — every action on the screen is read or inspect.

The count assertions are the load-bearing security ones: an artifact-kind count
is an information-disclosure surface, so this file constructs a store holding
artifacts the account may not view and proves the rail can neither name those
kinds nor reveal their volume.

Run:

    cd /opt/log-platform
    env PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_artifact_explorer_visual_modernization_s11.py
"""
from __future__ import annotations

import re
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
from api.portal_ui import i18n as portal_i18n  # noqa: E402

FORBIDDEN = (
    "postgres://",
    "password=",
    "api_write_token",
    "api_read_token",
    "dbname=",
    "secret_access_key",
    "traceback (most recent call last)",
)

# Every colour the retired inline Artifacts stylesheet hard-coded. None of them
# may reappear anywhere the module renders: they were dark-only literals that
# stayed dark under the shared light theme.
RETIRED_LITERAL_COLOURS = (
    "#0f141d",
    "#0b1119",
    "#141d2a",
    "#c3cad6",
    "#dbe1ea",
    "#ffb4b4",
    "#fde68a",
    "#160a00",
    "rgba(255, 115, 0",
)

STATIC_DIR = REPO_ROOT / "api" / "static"


# ---------------------------------------------------------------------------
# Fixture store
# ---------------------------------------------------------------------------

# `alpha` is the only workflow the operator account below is granted. The store
# deliberately holds far more artifacts than that account may view, in kinds it
# may not view at all, so "the rail leaked the global volume" is a failure the
# assertions can actually catch.
VISIBLE_WORKFLOW = "alpha"
HIDDEN_WORKFLOW = "restricted"
HIDDEN_KIND = "audit_log"
HIDDEN_KIND_VOLUME = 47


def _artifact(index: int, *, kind: str, workflow: str, expired: bool = False) -> dict:
    return {
        "artifact_id": f"00000000-0000-0000-0000-{index:012d}",
        "kind": kind,
        "workflow_name": workflow,
        "stage_name": "stage1",
        "artifact_role": "output",
        "report_type": "monthly",
        "client_code": "ALPHA00001",
        "layout_version": 2,
        "run_id": f"11111111-1111-1111-1111-{index:012d}",
        "raw_file_id": None,
        "filename": f"file_{index}.csv",
        "display_filename": f"file_{index}.csv",
        "original_filename": f"orig_{index}.csv",
        "storage_key": f"artifacts/file_{index}.csv",
        "bucket_name": "artifacts",
        "content_type": "text/csv",
        "file_ext": "csv",
        "size_bytes": 1024 * (index + 1),
        "sha256": f"9f2c41ab8e7d{index:04d}" + "0" * 40,
        "metadata_json": {},
        "owner_user_id": None,
        "expires_at": None,
        "expired_at": "2026-01-01T00:00:00+00:00" if expired else None,
        "created_at": f"2026-08-{(index % 28) + 1:02d} 04:00",
        "description": None,
        "manual_metadata_json": {},
        "tags": [],
    }


def _store() -> list[dict]:
    items: list[dict] = []
    index = 0
    for _ in range(4):
        items.append(_artifact(index, kind="query_snapshot", workflow=VISIBLE_WORKFLOW))
        index += 1
    for _ in range(2):
        items.append(_artifact(index, kind="export_file", workflow=VISIBLE_WORKFLOW))
        index += 1
    # Removed by retention, still visible to the account: proves the `USUNIETY`
    # state renders and that expiry does not silently drop the row from view.
    items.append(_artifact(index, kind="export_file", workflow=VISIBLE_WORKFLOW, expired=True))
    index += 1
    for _ in range(HIDDEN_KIND_VOLUME):
        items.append(_artifact(index, kind=HIDDEN_KIND, workflow=HIDDEN_WORKFLOW))
        index += 1
    return items


STORE = _store()


def _operator_user() -> dict:
    """A non-administrator Artifact Explorer account with one scoped grant.

    `permissions` is supplied inline, which is the shape `list_user_permissions`
    already honours, so `user_can_access_artifact` runs its real RBAC path.
    """
    return {
        "user_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        "username": "operator",
        "display_name": "Ola Nowak",
        "is_active": True,
        "is_admin": False,
        "permissions": [
            {
                "can_view": True,
                "can_preview": True,
                "can_download": False,
                "can_edit_annotations": False,
                "workflow_name": VISIBLE_WORKFLOW,
                "stage_name": None,
                "artifact_role": None,
                "report_type": None,
                "client_code": None,
                "file_ext": None,
                "layout_version": None,
                "tag": None,
            }
        ],
    }


def _admin_user() -> dict:
    return {
        "user_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "username": "alice",
        "display_name": "Anna Kowalska",
        "is_active": True,
        "is_admin": True,
        "permissions": [],
    }


def _authorized(user: dict, action: str = "view") -> list[dict]:
    """The rows this account may actually see, via the production predicate."""
    return [a for a in STORE if api_main.user_can_access_artifact(user, a, action)]


def _expected_kind_counts(user: dict) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for artifact in _authorized(user):
        counts[artifact["kind"]] = counts.get(artifact["kind"], 0) + 1
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


# ---------------------------------------------------------------------------
# Fake database
# ---------------------------------------------------------------------------


class _Cursor:
    """Routes the module's real SQL by shape and answers from `STORE`.

    Row visibility is resolved with `user_can_access_artifact` — the same
    function the route uses per row — so "the rail agrees with the list" is a
    property of the production authorization code, not of this fake. Every
    statement is recorded so the tests can also assert the *SQL* carries the
    permission predicate, which is what protects the real database.
    """

    def __init__(self, state: "_DbState"):
        self.state = state
        self._rows: list[dict] = []
        self._one: dict | None = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def execute(self, sql, params=None):
        text = " ".join(str(sql).split())
        self.state.statements.append((text, list(params or [])))
        visible = [a for a in _authorized(self.state.user) if self.state.matches(a)]
        if text.startswith("SELECT kind, COUNT(*) AS total FROM artifacts"):
            counts: dict[str, int] = {}
            # The rail describes the whole authorized universe, not the filtered
            # page, so it ignores the route's other filters exactly like the SQL.
            for artifact in _authorized(self.state.user):
                counts[artifact["kind"]] = counts.get(artifact["kind"], 0) + 1
            self._rows = [
                {"kind": kind, "total": total}
                for kind, total in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
            ]
        elif text.startswith("SELECT COUNT(*) AS total FROM artifacts"):
            self._one = {"total": len(visible)}
        elif text.startswith("SELECT DISTINCT tag"):
            self._rows = []
        elif text.startswith("SELECT DISTINCT"):
            field = text.split()[2]
            values = sorted({str(a.get(field)) for a in _authorized(self.state.user) if a.get(field)})
            self._rows = [{"value": value} for value in values]
        else:
            self._rows = list(visible)

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._rows


class _Conn:
    def __init__(self, state: "_DbState"):
        self.state = state

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def cursor(self):
        return _Cursor(self.state)


class _DbState:
    def __init__(self, user: dict, *, kind_filter: list[str] | None = None):
        self.user = user
        self.kind_filter = kind_filter or []
        self.statements: list[tuple[str, list]] = []

    def matches(self, artifact: dict) -> bool:
        if self.kind_filter:
            return str(artifact.get("kind")) in {str(k) for k in self.kind_filter}
        return True


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


class _Patched:
    """Installs the fake database and the account for one rendered response."""

    def __init__(self, user: dict, *, kind_filter: list[str] | None = None):
        self.state = _DbState(user, kind_filter=kind_filter)
        self.user = user
        self._saved: dict = {}

    def __enter__(self) -> "_Patched":
        self._saved = {
            "db_conn": api_main.db_conn,
            "get_current_artifact_user": api_main.get_current_artifact_user,
            "_database_export_schema_available": api_main._database_export_schema_available,
            "_artifact_browser_row": api_main._artifact_browser_row,
        }
        api_main.db_conn = lambda: _Conn(self.state)
        api_main.get_current_artifact_user = lambda request: self.user
        api_main._database_export_schema_available = lambda: False
        api_main._artifact_browser_row = lambda row: dict(row)
        return self

    def __exit__(self, exc_type, exc, tb):
        for name, value in self._saved.items():
            setattr(api_main, name, value)
        return None


def _render_root(user: dict, **kwargs):
    kind = kwargs.get("kind") or []
    with _Patched(user, kind_filter=kind) as patched:
        response = api_main.artifact_explorer_index(_FakeRequest(), **kwargs)
    return _html(response), patched.state


def _tr(key: str) -> str:
    return portal_i18n.t(key)


# ---------------------------------------------------------------------------
# Shared assertions
# ---------------------------------------------------------------------------


def _assert_no_leaks(html: str, label: str) -> None:
    low = html.lower()
    for needle in FORBIDDEN:
        assert needle not in low, f"{label}: forbidden content {needle!r} rendered"
    for colour in RETIRED_LITERAL_COLOURS:
        assert colour not in html, f"{label}: retired hard-coded colour {colour!r} still emitted"
    assert "<style>" not in html, f"{label}: module still emits an inline stylesheet"


def _assert_shared_shell(html: str, label: str) -> None:
    assert "lp-shell" in html and "lp-appbar" in html and "lp-context" in html, label
    assert "lp-main" in html and 'id="lp-main"' in html, label
    assert "lp-skip-link" in html, f"{label}: skip link missing"
    assert "portal-sidebar" not in html, f"{label}: legacy independent sidebar present"
    # Shared static asset contract, versioned, plus the module's own two assets.
    for asset in ("css/tokens.css", "css/portal.css", "js/theme.js", "js/shell.js"):
        assert f"/static/{asset}?v=" in html, f"{label}: shared asset {asset} not loaded"
    for asset in ("css/artifact-explorer.css", "js/artifact-explorer.js"):
        assert f"/static/{asset}?v=" in html, f"{label}: module asset {asset} not loaded"
    # Database Explorer's grid layer must not ride along on an Artifacts page.
    for asset in ("data-grid.css", "data-grid.js", "data-grid-selection.js", "data-grid-export.js"):
        assert asset not in html, f"{label}: Database Explorer module asset {asset} leaked"
    # Shared theme + responsive navigation contract (S1/S10).
    assert "data-theme-switcher" in html and 'data-theme-option="auto"' in html, f"{label}: theme control missing"
    assert 'data-theme-option="light"' in html and 'data-theme-option="dark"' in html, label
    assert "data-nav-toggle" in html and 'aria-controls="lp-nav-drawer"' in html, f"{label}: responsive nav missing"
    assert "data-nav-drawer" in html and "data-nav-scrim" in html, f"{label}: nav drawer missing"
    assert html.count("<h1") == 1, f"{label}: exactly one h1 required"
    _assert_no_leaks(html, label)


def _assert_operator_identity(html: str, label: str) -> None:
    badge = _tr("shell.context.operator_mode")
    assert badge == "TRYB OPERATORA", "approved operator-mode copy changed"
    assert f'<span class="lp-badge-technical">{badge}</span>' in html, f"{label}: operator badge missing"
    assert html.count(badge) == 1, f"{label}: operator badge must appear exactly once"
    # `AR-1`: neutral, non-accent context rule marks the technical surface.
    assert 'data-surface="operator"' in html, f"{label}: neutral context rule not applied"
    # `AR-2`: the screen says in words that this is not a client-data mode.
    assert _tr("art.page.subtitle") in html, f"{label}: approved subtitle missing"
    assert "nie jest trybem dostępu do danych klienta" in html, label
    # Neutral context rule: no fabricated client identity in the shell.
    assert "lp-client-code" not in html, f"{label}: shell invented a client code"
    assert "lp-context-sep" not in html, f"{label}: shell rendered a client/module context pair"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _test_root_list_shell_and_operator_identity() -> None:
    html, _ = _render_root(_admin_user())
    _assert_shared_shell(html, "/artifact-explorer")
    _assert_operator_identity(html, "/artifact-explorer")
    assert f">{_tr('art.page.title')}</h1>" in html or _tr("art.page.title") in html
    # `Artefakty` stays in the tools group — never a fourth data-access mode.
    assert 'data-nav-group="tools"' in html and 'href="/artifact-explorer"' in html
    assert 'data-nav-group="work"' in html
    # One coherent hierarchy: the retired duplicate header nav is gone and the
    # section bar is the single "all artifacts / folders" concept.
    assert ">All artifacts</a>" not in html, "legacy duplicate header navigation still rendered"
    assert ">Virtual folders</a>" not in html, "legacy duplicate header navigation still rendered"
    assert _tr("shell.subnav.artifacts.all") in html and _tr("shell.subnav.artifacts.folders") in html
    assert _tr("art.action.refresh") in html, "approved `Odśwież katalog` action missing"
    print("PASS: root list renders the shared shell with the approved operator identity")


def _test_detail_folders_use_same_shell() -> None:
    user = _admin_user()
    artifact = STORE[0]

    saved = {
        "_get_artifact_browser_row": api_main._get_artifact_browser_row,
        "_get_run_summary": api_main._get_run_summary,
        "_get_raw_file_summary": api_main._get_raw_file_summary,
        "_get_artifact_virtual_folders": api_main._get_artifact_virtual_folders,
        "_artifact_explorer_preview_for_row": api_main._artifact_explorer_preview_for_row,
        "_list_virtual_folders": api_main._list_virtual_folders,
        "_get_virtual_folder_detail": api_main._get_virtual_folder_detail,
        "list_manual_virtual_folders_with_paths": api_main.list_manual_virtual_folders_with_paths,
    }
    folder = {
        "folder_id": "ffffffff-ffff-ffff-ffff-ffffffffffff",
        "folder_name": "Wsady",
        "folder_type": "manual",
        "description": None,
        "parent_folder_id": None,
        "search_query_json": {},
    }
    api_main._get_artifact_browser_row = lambda artifact_id: dict(artifact)
    api_main._get_run_summary = lambda run_id: None
    api_main._get_raw_file_summary = lambda raw_file_id: None
    api_main._get_artifact_virtual_folders = lambda artifact_id: []
    api_main._artifact_explorer_preview_for_row = lambda row, art: None
    api_main._list_virtual_folders = lambda parent: [folder]
    api_main.list_manual_virtual_folders_with_paths = lambda: []
    api_main._get_virtual_folder_detail = lambda folder_id, **kwargs: {
        "folder": folder,
        "breadcrumbs": [folder],
        "child_folders": [],
        "artifacts": {"data": _authorized(user)[:2], "meta": {"limit": 50, "offset": 0, "count": 2, "total": 2}},
    }
    try:
        with _Patched(user):
            detail = _html(api_main.artifact_explorer_detail(artifact["artifact_id"], _FakeRequest()))
            folders_root = _html(api_main.artifact_explorer_folders_index(_FakeRequest()))
            folder_detail = _html(
                api_main.artifact_explorer_folder_detail(folder["folder_id"], _FakeRequest(), 50, 0, "created_at_desc")
            )
    finally:
        for name, value in saved.items():
            setattr(api_main, name, value)

    for label, html in (
        ("artifact detail", detail),
        ("folders root", folders_root),
        ("folder detail", folder_detail),
    ):
        _assert_shared_shell(html, label)
        _assert_operator_identity(html, label)
    # The folder surfaces mark their own section entry rather than claiming the
    # list's; there is exactly one current entry in the section bar.
    assert folders_root.count('class="lp-subnav-item" href="/artifact-explorer/folders" aria-current="page"') == 1
    assert folder_detail.count('aria-current="page"') >= 1
    print("PASS: artifact detail, folders root and folder detail share the modernized shell")


def _test_operator_badge_is_context_not_authorization() -> None:
    """`TRYB OPERATORA` labels the surface. It never decides access."""
    operator = _operator_user()
    html, _ = _render_root(operator)
    assert _tr("shell.context.operator_mode") in html, "badge must render for a non-admin operator too"
    # ... and the badge did not grant this non-admin the admin-only nav entries.
    # The page's own rail/refresh links point at `/artifact-explorer`, so the
    # assertion is about navigation items specifically, not about the string.
    nav_hrefs = re.findall(r'<a class="lp-nav-item" data-nav-group="[^"]+" href="([^"]+)"', html)
    assert nav_hrefs, "shared navigation must still render"
    assert "/artifact-explorer" not in nav_hrefs, "nav visibility must stay admin-gated (`AR-4`)"
    assert "/admin" not in nav_hrefs, "operator badge must not imply administration access"
    assert "lp-subnav-item" not in html, "section navigation must stay admin-gated"

    # Ordinary non-Artifacts pages do not inherit the operator identity.
    plain = _html(
        api_main._portal_layout(
            "Zbiory danych",
            "<p>x</p>",
            user=_admin_user(),
            portal_label="Data",
            active_key="database",
        )
    )
    assert _tr("shell.context.operator_mode") not in plain, "operator badge leaked onto a non-Artifacts page"
    assert 'data-surface="operator"' not in plain, "neutral operator rule leaked onto a non-Artifacts page"
    print("PASS: operator mode is context only and does not spread or authorize")


def _test_nav_visibility_and_permission_state() -> None:
    """`AR-4` — no `Artefakty` entry without the operator permission."""
    non_admin = api_main._platform_primary_nav_items(_operator_user(), "reports")
    assert all(item.key != "artifacts" for item in non_admin), "Artefakty nav leaked to a non-operator"
    admin = api_main._platform_primary_nav_items(_admin_user(), "files")
    assert any(item.key == "artifacts" and item.active for item in admin), "operator lost the Artefakty entry"

    forbidden = api_main._artifact_explorer_forbidden("Only Artifact Explorer admins can manage virtual folders.")
    html = _html(forbidden)
    assert forbidden.status_code == 403
    _assert_no_leaks(html, "permission state")
    assert "lp-shell" in html and "lp-appbar" in html, "permission state must render inside the safe shell"
    assert 'href="/artifact-explorer"' not in html and 'href="/admin"' not in html, "permission state leaked operator links"
    print("PASS: navigation visibility and the permission state keep the current authorization model")


def _test_kind_rail_dimension_and_counts() -> None:
    """`AR-3` — the rail uses the persisted `kind` dimension, with counts."""
    user = _operator_user()
    html, state = _render_root(user)
    assert _tr("art.rail.heading") in html and "Rodzaje artefaktów" in html
    assert f'aria-label="{_tr("art.rail.aria")}"' in html, "rail needs an accessible name"
    assert _tr("art.rail.note") in html, "rail must state the operator/immutability rule"

    expected = _expected_kind_counts(user)
    assert expected, "fixture must produce at least one authorized kind"
    for kind, count in expected:
        assert f'<span class="art-rail-name">{kind}</span>' in html, f"rail missing kind {kind}"
        assert f'<span class="art-rail-count">{count}</span>' in html, f"rail missing count for {kind}"
    rendered = re.findall(r'<span class="art-rail-name">([^<]+)</span>', html)
    assert rendered == [kind for kind, _ in expected], (rendered, expected)

    # The dimension is `artifacts.kind`, not a lookalike technical field.
    count_sql = [sql for sql, _ in state.statements if sql.startswith("SELECT kind, COUNT(*)")]
    assert len(count_sql) == 1, count_sql
    assert "GROUP BY kind" in count_sql[0], count_sql[0]
    for lookalike in ("artifact_role", "report_type", "file_ext", "workflow_name"):
        assert f"GROUP BY {lookalike}" not in count_sql[0]
    # One bounded, read-only aggregate — no new persistence, no new API surface.
    assert "LIMIT %s" in count_sql[0], count_sql[0]
    assert not any(
        verb in sql for sql, _ in state.statements for verb in ("INSERT ", "UPDATE ", "DELETE ", "CREATE ")
    ), "S11 must issue no write statements"
    print("PASS: kind rail uses the approved `kind` dimension with bounded read-only counts")


def _test_kind_counts_are_rbac_scoped() -> None:
    """Mandatory: counts must never become an RBAC side channel."""
    user = _operator_user()
    html, state = _render_root(user)

    authorized = _authorized(user)
    hidden = [a for a in STORE if a not in authorized]
    assert len(hidden) >= HIDDEN_KIND_VOLUME, "fixture must hold artifacts the account cannot view"
    assert len(STORE) > len(authorized), "global store must exceed the authorized universe"

    # The rail names only kinds the account can browse...
    rendered = re.findall(r'<span class="art-rail-name">([^<]+)</span>', html)
    assert HIDDEN_KIND not in rendered, "rail named a kind the account cannot view"
    assert HIDDEN_KIND not in html, "an inaccessible kind leaked into the page"

    # ...and its counts sum to exactly the authorized universe, not the store.
    counts = [int(value) for value in re.findall(r'<span class="art-rail-count">(\d+)</span>', html)]
    assert sum(counts) == len(authorized), (counts, len(authorized))
    assert sum(counts) != len(STORE), "rail leaked the global artifact volume"
    assert str(HIDDEN_KIND_VOLUME) not in [str(value) for value in counts]
    assert str(len(STORE)) not in [str(value) for value in counts]

    # The SQL itself carries the same permission predicate the list is built
    # from, with the same parameters — this is what protects the real database.
    clause, params = api_main.build_artifact_permission_where_clause(user, "view")
    assert clause, "a non-admin account must produce a permission predicate"
    normalized = " ".join(clause.split())
    count_sql, count_params = next(
        (sql, sql_params) for sql, sql_params in state.statements if sql.startswith("SELECT kind, COUNT(*)")
    )
    assert normalized in count_sql, count_sql
    assert count_params[: len(params)] == params, (count_params, params)

    # An administrator sees the whole store; the same code path, no special case.
    admin_html, _ = _render_root(_admin_user())
    admin_counts = [int(value) for value in re.findall(r'<span class="art-rail-count">(\d+)</span>', admin_html)]
    assert sum(admin_counts) == len(STORE), (admin_counts, len(STORE))
    assert HIDDEN_KIND in admin_html, "administrator must still see every kind"
    print("PASS: kind counts are scoped to the authorized universe and leak no hidden volume")


def _test_kind_rail_url_round_trip() -> None:
    """Rail navigation is server-authoritative, bookmarkable and composable."""
    user = _admin_user()
    html, _ = _render_root(
        user,
        client_code=["ALPHA00001"],
        search="file",
        sort="created_at_desc",
        limit=50,
        offset=50,
    )
    links = re.findall(r'<a class="art-rail-item" href="([^"]+)"', html)
    assert links, "rail must render links"
    target = next(link for link in links if "kind=query_snapshot" in link)
    # Unrelated filters ride along; only paging resets.
    assert "client_code=ALPHA00001" in target and "search=file" in target
    assert "sort=created_at_desc" in target
    assert "offset=0" in target and "offset=50" not in target
    # One filtering grammar for the kind dimension: the exact selection wins and
    # the typed substring is not left behind to contradict it.
    assert "kind_search=" not in target
    assert target.startswith("/artifact-explorer?")

    # Active state is semantic, not colour-only.
    active_html, _ = _render_root(user, kind=["query_snapshot"])
    assert active_html.count('aria-current="true"') == 1, "exactly one kind may be current"
    active_entry = re.search(
        r'<a class="art-rail-item" href="[^"]*kind=query_snapshot[^"]*" aria-current="true">'
        r'<span class="art-rail-name">query_snapshot</span>',
        active_html,
    )
    assert active_entry, "the selected kind must carry aria-current"

    # Two kinds is a legitimate filter-form state no single rail entry claims.
    multi_html, _ = _render_root(user, kind=["query_snapshot", "export_file"])
    assert 'aria-current="true"' not in multi_html, "a multi-kind filter must not mark one rail entry current"

    # An unknown or forged kind is safe: no rail entry activates, and the value
    # travels as an ordinary parameterized filter rather than into the SQL text.
    forged_html, forged_state = _render_root(user, kind=["'; DROP TABLE artifacts;--"])
    assert 'aria-current="true"' not in forged_html
    assert "DROP TABLE" not in " ".join(sql for sql, _ in forged_state.statements)
    print("PASS: kind navigation round-trips through canonical, composable query parameters")


def _test_list_capabilities_preserved() -> None:
    """Filtering, sorting, pagination and the copy affordance still work."""
    user = _admin_user()
    html, _ = _render_root(user, client_code=["ALPHA00001"], sort="size_bytes_desc", limit=50, offset=0)
    assert 'data-artifact-filters="1"' in html and 'action="/artifact-explorer"' in html
    assert 'data-combo="kind"' in html and 'data-combo="client_code"' in html
    assert 'value="ALPHA00001"' in html, "active filter must round-trip into the form"
    assert _tr("art.search.placeholder") in html, "approved search placeholder missing"
    # Sort survives modernization and now carries the approved semantic state.
    assert 'aria-sort="descending"' in html, "active sort must be programmatically exposed"
    assert html.count('aria-sort="descending"') == 1 and 'aria-sort="none"' in html
    assert "sort=size_bytes_asc" in html, "sort toggle link missing"
    assert "Showing" in html and "Offset" in html, "pagination summary missing"
    # Copyable technical cells keep a cursor/label affordance, never colour only.
    assert "data-copy-value=" in html and 'title="Click to copy' in html
    assert "data-copy-status" in html and 'aria-live="polite"' in html
    print("PASS: list filtering, sorting, pagination and copy affordances are preserved")


def _test_approved_columns_and_states() -> None:
    """`ART-001` column contract, without dropping existing technical columns."""
    html, _ = _render_root(_admin_user())
    for key in ("created", "kind", "name", "client", "hash", "size", "state"):
        assert _tr(f"art.col.{key}") in html, f"approved column {key} missing"
    assert _tr("art.action.inspect") in html, "approved `Inspekcja` action missing"
    # Existing technical metadata is not redacted by the visual system change.
    for legacy in ("Workflow", "Stage", "Role", "Report type", "Tags", "Description", "Run", "Raw file"):
        assert f">{legacy}<" in html or f"{legacy}</a>" in html, f"technical column {legacy} was dropped"
    assert _tr("art.state.verified") in html, "verified state badge missing"
    assert _tr("art.state.removed") in html, "removed state badge missing for the retired artifact"
    print("PASS: approved ART-001 columns render without redacting existing technical metadata")


def _test_rbac_gated_controls_and_immutability() -> None:
    """`AR-5` — every rendered action is read or inspect, and stays gated."""
    operator = _operator_user()
    html, _ = _render_root(operator)
    # The grant is view+preview, never download: the download control must be
    # absent, not merely styled away (a hidden-but-focusable control is a bug).
    assert ">Download</a>" not in html, "download control rendered without the grant"
    assert ">Preview</a>" in html, "preview control lost for an account that has the grant"
    assert _tr("art.action.inspect") in html

    admin_html, _ = _render_root(_admin_user())
    assert ">Download</a>" in admin_html, "administrator lost the download control"

    # S11 introduces no artifact-content mutation anywhere in the module.
    for label, page in (("operator", html), ("admin", admin_html)):
        low = page.lower()
        for banned in (
            "/delete\"",
            "delete artifact",
            "replace file",
            "overwrite",
            "rerun job",
            "artifacts/{artifact_id}/delete",
        ):
            assert banned not in low, f"{label}: S11 introduced a mutation affordance ({banned})"
        # Scoped to the WORKING AREA. The shared shell's app bar now carries the
        # approved stage S13 theme-preference form, which is a per-account UI
        # preference on every portal page and is not an artifact affordance;
        # what this test owns is that the artifact list itself offers no
        # mutation. Checking the whole document would make an unrelated shell
        # control look like an Artifact Explorer regression.
        work_area = page[page.index("<main"):page.index("</main>")]
        assert "<form" not in work_area or 'method="post"' not in work_area, (
            f"{label}: list must not carry a POST form"
        )
        assert 'data-theme-form' not in work_area, f"{label}: shell chrome must stay in the app bar"
    print("PASS: read/inspect only — permission gates and artifact immutability are unchanged")


def _test_module_stylesheet_is_token_only() -> None:
    """The module follows AUTO / light / dark through the shared token layer."""
    css = (STATIC_DIR / "css" / "artifact-explorer.css").read_text(encoding="utf-8")
    for colour in RETIRED_LITERAL_COLOURS:
        assert colour not in css, f"module stylesheet still hard-codes {colour!r}"
    hexes = set(re.findall(r"#[0-9a-fA-F]{3,8}\b", css))
    assert not hexes, f"module stylesheet must define no literal colours: {sorted(hexes)}"
    assert "prefers-color-scheme" not in css, "module must not carry a second theme palette"
    assert 'data-theme' not in css, "module must not carry a second theme palette"
    for token in (
        "--lp-surface-raised",
        "--lp-text-primary",
        "--lp-border-default",
        "--lp-accent-tint",
        "--lp-state-positive-bg",
        "--lp-state-neutral-bg",
        "--lp-width-rail-compact",
    ):
        assert token in css, f"module stylesheet must use the shared token {token}"
    # Approved neutral operator rule lives in the shared shell, once.
    portal_css = (STATIC_DIR / "css" / "portal.css").read_text(encoding="utf-8")
    assert '.lp-context[data-surface="operator"] .lp-context-rule' in portal_css
    js = (STATIC_DIR / "js" / "artifact-explorer.js").read_text(encoding="utf-8")
    assert "data-combo" in js and "data-copy-value" in js, "module JS lost its progressive enhancements"
    print("PASS: module stylesheet is token-only and works in light, dark and AUTO")


def _test_translation_keys_resolve() -> None:
    """Approved Polish copy travels through the existing translation catalogue."""
    keys = portal_i18n.available_keys()
    expected = {
        "art.page.title": "Artefakty",
        "art.page.subtitle": "inspekcja artefaktów systemu · nie jest trybem dostępu do danych klienta",
        "art.rail.heading": "Rodzaje artefaktów",
        "art.search.placeholder": "Nazwa, hash lub referencja",
        "art.action.refresh": "Odśwież katalog",
        "art.action.inspect": "Inspekcja",
        "art.col.created": "Utworzono",
        "art.col.kind": "Rodzaj",
        "art.col.name": "Nazwa",
        "art.col.client": "Klient",
        "art.col.hash": "Hash",
        "art.col.size": "Rozmiar",
        "art.col.state": "Stan",
        "art.state.verified": "ZWERYFIKOWANY",
        "art.state.removed": "USUNIĘTY",
        "shell.context.operator_mode": "TRYB OPERATORA",
    }
    for key, value in expected.items():
        assert key in keys, f"missing translation key {key}"
        assert portal_i18n.t(key) == value, (key, portal_i18n.t(key), value)
    print("PASS: approved ART-001 copy resolves through the shared translation catalogue")


def main() -> None:
    _test_root_list_shell_and_operator_identity()
    _test_detail_folders_use_same_shell()
    _test_operator_badge_is_context_not_authorization()
    _test_nav_visibility_and_permission_state()
    _test_kind_rail_dimension_and_counts()
    _test_kind_counts_are_rbac_scoped()
    _test_kind_rail_url_round_trip()
    _test_list_capabilities_preserved()
    _test_approved_columns_and_states()
    _test_rbac_gated_controls_and_immutability()
    _test_module_stylesheet_is_token_only()
    _test_translation_keys_resolve()
    print("\nALL PASS: ARTIFACT_EXPLORER_VISUAL_MODERNIZATION (S11)")


if __name__ == "__main__":
    main()
