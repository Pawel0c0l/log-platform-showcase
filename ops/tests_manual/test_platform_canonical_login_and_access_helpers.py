#!/usr/bin/env python3
"""Phase 1 / 2A foundation tests: canonical login/logout aliases and central
effective-access helper wrappers.

These tests are browser-free. They stub FastAPI/boto3/psycopg imports (like the
other portal manual tests) and drive an in-memory fake database so the new
central helpers can be proven to match the existing inline access logic on the
same data.

Run:

    cd /opt/log-platform
    env PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_platform_canonical_login_and_access_helpers.py
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
    psycopg.connect = lambda *args, **kwargs: None
    rows = types.ModuleType("psycopg.rows")
    rows.dict_row = object()

    sys.modules.setdefault("fastapi", fastapi)
    sys.modules.setdefault("fastapi.responses", responses)
    sys.modules.setdefault("boto3", boto3)
    sys.modules.setdefault("psycopg", psycopg)
    sys.modules.setdefault("psycopg.rows", rows)


_install_import_stubs()

import api.main as api_main  # noqa: E402


# --------------------------------------------------------------------------
# Fake request + patch helpers
# --------------------------------------------------------------------------
class _FakeUrl:
    def __init__(self, path="/login", query=""):
        self.path = path
        self.query = query


class _FakeRequest:
    def __init__(self, *, path="/login", query="", cookies=None):
        self.url = _FakeUrl(path, query)
        self.cookies = cookies or {}


def _html(response) -> str:
    return response.body.decode("utf-8")


def _patch(name, value):
    old = getattr(api_main, name)
    setattr(api_main, name, value)
    return old


def _restore(patches):
    for name, old in reversed(patches):
        setattr(api_main, name, old)


# --------------------------------------------------------------------------
# In-memory fake database that answers BOTH the new helper queries and the
# existing inline access queries from one shared model.
# --------------------------------------------------------------------------
CLIENT_CAPS = ("can_view_reports", "can_view_database", "can_export_database")
DATASET_CAPS = ("can_view_rows", "can_filter_rows", "can_export_rows")


class FakeModel:
    def __init__(self):
        self.clients = {}        # client_code -> {"is_active": bool}
        self.users = {}          # user_id -> {"is_active": bool}
        self.user_clients = []   # {"user_id","client_code", caps...}
        self.groups = {}         # group_id -> {"is_active": bool}
        self.group_users = []    # {"group_id","user_id"}
        self.group_clients = []  # {"group_id","client_code", caps...}
        self.datasets = {}       # dataset_id -> {"client_code","is_active", ...}
        self.dataset_users = []  # {"user_id","dataset_id", caps...}
        self.dataset_groups = [] # {"group_id","dataset_id", caps...}

    # ---- effective computations matching real SQL semantics ----
    def _direct_client_caps(self, uid, code):
        rows = [r for r in self.user_clients if r["user_id"] == uid and r["client_code"] == code]
        if not rows:
            return {c: None for c in CLIENT_CAPS}
        return {c: any(bool(r.get(c)) for r in rows) for c in CLIENT_CAPS}

    def _group_client_caps(self, uid, code):
        matched = []
        for gu in self.group_users:
            if gu["user_id"] != uid:
                continue
            g = self.groups.get(gu["group_id"])
            if not g or not g.get("is_active"):
                continue
            for gc in self.group_clients:
                if gc["group_id"] == gu["group_id"] and gc["client_code"] == code:
                    matched.append(gc)
        if not matched:
            return {c: None for c in CLIENT_CAPS}
        return {c: any(bool(gc.get(c)) for gc in matched) for c in CLIENT_CAPS}

    def _eff_client(self, uid, code):
        d = self._direct_client_caps(uid, code)
        g = self._group_client_caps(uid, code)
        return {c: bool(d.get(c)) or bool(g.get(c)) for c in CLIENT_CAPS}

    def _direct_dataset_caps(self, uid, did):
        rows = [r for r in self.dataset_users if r["user_id"] == uid and r["dataset_id"] == did]
        if not rows:
            return {c: None for c in DATASET_CAPS}
        return {c: any(bool(r.get(c)) for r in rows) for c in DATASET_CAPS}

    def _group_dataset_caps(self, uid, did):
        matched = []
        for gu in self.group_users:
            if gu["user_id"] != uid:
                continue
            g = self.groups.get(gu["group_id"])
            if not g or not g.get("is_active"):
                continue
            for dg in self.dataset_groups:
                if dg["group_id"] == gu["group_id"] and dg["dataset_id"] == did:
                    matched.append(dg)
        if not matched:
            return {c: None for c in DATASET_CAPS}
        return {c: any(bool(dg.get(c)) for dg in matched) for c in DATASET_CAPS}

    def _eff_dataset(self, uid, did):
        d = self._direct_dataset_caps(uid, did)
        g = self._group_dataset_caps(uid, did)
        return {c: bool(d.get(c)) or bool(g.get(c)) for c in DATASET_CAPS}

    def _dataset_accessible(self, uid, did, *, require_user_active: bool):
        ds = self.datasets.get(did)
        if not ds:
            return False
        code = ds.get("client_code")
        client = self.clients.get(code)
        user_active = bool(self.users.get(uid, {}).get("is_active"))
        ok = (
            bool(ds.get("is_active"))
            and bool(client and client.get("is_active"))
            and self._eff_client(uid, code)["can_view_database"]
            and self._eff_dataset(uid, did)["can_view_rows"]
        )
        if require_user_active:
            ok = ok and user_active
        return ok

    def _dataset_row(self, did):
        ds = self.datasets.get(did, {})
        return {
            "dataset_id": did,
            "client_code": ds.get("client_code"),
            "client_display_name": ds.get("client_code"),
            "client_database_name": None,
            "dataset_name": ds.get("dataset_name", f"Dataset {did}"),
            "slug": ds.get("slug", did),
            "description": None,
            "schema_name": "public",
            "table_name": "t",
            "default_date_column": None,
            "is_active": ds.get("is_active"),
            "visible_columns": 1,
            "assigned_users": 1,
            "can_view_rows": self._eff_dataset("__caller__", did).get("can_view_rows"),
        }


class FakeCursor:
    def __init__(self, model: FakeModel):
        self.model = model
        self._rows: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @staticmethod
    def _norm(sql: str) -> str:
        return " ".join(str(sql).lower().split())

    def execute(self, sql, params=None):
        s = self._norm(sql)
        p = tuple(params or ())
        m = self.model
        self._rows = []

        # A. new helper: client active lookup
        if "select is_active from portal_clients where client_code" in s:
            code = p[0]
            if code in m.clients:
                self._rows = [{"is_active": m.clients[code]["is_active"]}]
            return

        # E. new dataset helper: user active lookup
        if "select is_active from artifact_users where user_id" in s:
            uid = p[0]
            if uid in m.users:
                self._rows = [{"is_active": m.users[uid]["is_active"]}]
            return

        # F. new dataset helper: simple dataset lookup
        if "from portal_database_datasets where dataset_id" in s and "pdd" not in s:
            did = p[0]
            if did in m.datasets:
                self._rows = [{
                    "client_code": m.datasets[did]["client_code"],
                    "is_active": m.datasets[did]["is_active"],
                }]
            return

        # K. Phase 2D set-based client helper (used by the switched
        # _portal_user_has_database_access_to_client). Mutually exclusive with the
        # SELECT 1 gate (D): no "select 1", no effective_dataset_access.
        if (
            "with effective_client_access" in s
            and "join portal_clients pc" in s
            and "effective_dataset_access" not in s
            and "select 1" not in s
        ):
            uid = p[0]
            client_filter = None
            if "client_code = any(%s)" in s:
                client_filter = {str(x) for x in p[1]}
            out = []
            for code, client in m.clients.items():
                direct = m._direct_client_caps(uid, code)
                group = m._group_client_caps(uid, code)
                via_direct = any(bool(direct.get(c)) for c in CLIENT_CAPS)
                via_group = any(bool(group.get(c)) for c in CLIENT_CAPS)
                if not (via_direct or via_group):
                    continue
                if client_filter is not None and code not in client_filter:
                    continue
                eff = m._eff_client(uid, code)
                out.append({
                    "client_code": code,
                    "client_name": code,
                    "client_active": client["is_active"],
                    "can_view_reports": eff["can_view_reports"],
                    "can_view_database": eff["can_view_database"],
                    "can_export_database": eff["can_export_database"],
                    "source_direct": via_direct,
                    "source_group": via_group,
                })
            self._rows = out
            return

        # The inline CTE queries (D/I/J) embed the same table/aggregate
        # substrings used by the small helper queries (B/C/G/H), so they must be
        # matched FIRST.

        # D. inline _portal_user_has_database_access_to_client
        if (
            "with effective_client_access" in s
            and "select 1" in s
            and "from effective_client_access eca" in s
            and "effective_dataset_access" not in s
            and "can_view_database is true" in s
        ):
            uid, code = p[0], p[1]
            client = m.clients.get(code)
            ok = m._eff_client(uid, code)["can_view_database"] and bool(client and client.get("is_active"))
            self._rows = [{"?column?": 1}] if ok else []
            return

        # I. inline _get_portal_database_dataset_for_user (single dataset)
        if (
            "with effective_client_access" in s
            and "effective_dataset_access" in s
            and "where pdd.dataset_id = %s" in s
        ):
            uid, did = p[0], p[-1]
            if m._dataset_accessible(uid, did, require_user_active=True):
                self._rows = [m._dataset_row(did)]
            return

        # J. inline _list_accessible_portal_database_datasets_for_user (list)
        if (
            "with effective_client_access" in s
            and "effective_dataset_access" in s
            and "order by pc.display_name" in s
        ):
            uid = p[0]
            self._rows = [
                m._dataset_row(did)
                for did in m.datasets
                if m._dataset_accessible(uid, did, require_user_active=False)
            ]
            return

        # B. new helper: direct client caps
        if "from portal_user_clients" in s and "bool_or(can_view_reports)" in s:
            uid, code = p[0], p[1]
            self._rows = [m._direct_client_caps(uid, code)]
            return

        # C. new helper: group client caps
        if "from portal_group_users pgu" in s and "bool_or(pgc.can_view_reports)" in s:
            uid, code = p[0], p[1]
            self._rows = [m._group_client_caps(uid, code)]
            return

        # G. new helper: direct dataset caps
        if "from portal_database_dataset_users" in s and "bool_or(can_view_rows)" in s:
            uid, did = p[0], p[1]
            self._rows = [m._direct_dataset_caps(uid, did)]
            return

        # H. new helper: group dataset caps
        if "from portal_database_dataset_groups pddg" in s and "bool_or(pddg.can_view_rows)" in s:
            uid, did = p[0], p[1]
            self._rows = [m._group_dataset_caps(uid, did)]
            return

        raise AssertionError(f"FakeCursor received an unrecognized query:\n{s}")

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeConn:
    def __init__(self, model: FakeModel):
        self.model = model

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return FakeCursor(self.model)


def _with_model(model: FakeModel):
    return _patch("db_conn", lambda: FakeConn(model))


# --------------------------------------------------------------------------
# Part A — canonical login/logout aliases
# --------------------------------------------------------------------------
def _test_login_page_alias_matches() -> None:
    old = _patch("get_current_artifact_user", lambda request: None)
    try:
        canonical = api_main.platform_login_page(_FakeRequest(path="/login"), next="/artifact-explorer")
        legacy = api_main.artifact_explorer_login_page(_FakeRequest(path="/artifact-explorer/login"), next="/artifact-explorer")
    finally:
        api_main.get_current_artifact_user = old
    c_html, l_html = _html(canonical), _html(legacy)
    assert canonical.status_code == 200 and legacy.status_code == 200
    assert c_html == l_html, "canonical /login must render the identical login page"
    # Branding now reads as Log Platform, but the legacy substring is preserved.
    assert "Log Platform" in c_html, c_html
    assert "Artifact Explorer Login" in c_html, c_html
    # No secret/DSN leakage on the login page.
    for forbidden in ("POSTGRES_PASSWORD", "password=", "API_WRITE_TOKEN", "dbname="):
        assert forbidden not in c_html, forbidden
    print("PASS: /login renders the same platform login page as /artifact-explorer/login")


def _test_login_post_alias_matches() -> None:
    captured = {}

    def fake_auth(username, password):
        captured["username"] = username
        return {"user_id": "uuuuuuuu-uuuu-uuuu-uuuu-uuuuuuuuuuuu", "username": username, "is_active": True, "is_admin": False}

    patches = [
        ("authenticate_artifact_user", _patch("authenticate_artifact_user", fake_auth)),
        ("_portal_audit_event_safe", _patch("_portal_audit_event_safe", lambda *a, **k: None)),
    ]
    try:
        canonical = api_main.platform_login_route(
            request=_FakeRequest(path="/login"), username="alice", password="pw", next="/user"
        )
        legacy = api_main.artifact_explorer_login_route(
            request=_FakeRequest(path="/artifact-explorer/login"), username="alice", password="pw", next="/user"
        )
    finally:
        _restore(patches)
    assert canonical.status_code == 303 and legacy.status_code == 303
    assert canonical.headers.get("Location") == "/user" == legacy.headers.get("Location")
    cookie = canonical.headers.get("Set-Cookie", "")
    assert cookie.startswith(api_main.ARTIFACT_EXPLORER_SESSION_COOKIE + "="), cookie
    assert legacy.headers.get("Set-Cookie", "").startswith(api_main.ARTIFACT_EXPLORER_SESSION_COOKIE + "="), legacy.headers
    print("PASS: POST /login authenticates and sets the same session cookie as the legacy route")


def _test_logout_alias_matches() -> None:
    patches = [
        ("get_current_artifact_user", _patch("get_current_artifact_user", lambda request: None)),
        ("_portal_audit_event_safe", _patch("_portal_audit_event_safe", lambda *a, **k: None)),
    ]
    try:
        canonical_get = api_main.platform_logout_get_route(_FakeRequest(path="/logout"))
        canonical_post = api_main.platform_logout_post_route(_FakeRequest(path="/logout"))
        legacy_get = api_main.artifact_explorer_logout_get_route(_FakeRequest(path="/artifact-explorer/logout"))
    finally:
        _restore(patches)
    for resp in (canonical_get, canonical_post, legacy_get):
        assert resp.status_code == 303, resp.status_code
        assert resp.headers.get("Location") == "/artifact-explorer/login", resp.headers
        cookie = resp.headers.get("Set-Cookie", "")
        assert cookie.startswith(api_main.ARTIFACT_EXPLORER_SESSION_COOKIE + "="), cookie
        assert "Max-Age=0" in cookie, cookie
        assert "Path=/" in cookie, cookie
    print("PASS: /logout clears the same single session cookie as the legacy logout route")


def _test_legacy_login_routes_still_exist() -> None:
    for name in (
        "artifact_explorer_login_page",
        "artifact_explorer_login_route",
        "artifact_explorer_logout_get_route",
        "artifact_explorer_logout_post_route",
        "platform_login_page",
        "platform_login_route",
        "platform_logout_get_route",
        "platform_logout_post_route",
    ):
        assert callable(getattr(api_main, name)), name
    print("PASS: legacy and canonical login/logout handlers both exist")


def _test_portal_unauth_redirects_to_login() -> None:
    patches = [
        ("get_current_artifact_user", _patch("get_current_artifact_user", lambda request: None)),
        ("_portal_audit_event_safe", _patch("_portal_audit_event_safe", lambda *a, **k: None)),
    ]
    try:
        user_redirect = api_main.user_portal_home(_FakeRequest(path="/user"))
        admin_redirect = api_main.admin_portal_home(_FakeRequest(path="/admin"))
    finally:
        _restore(patches)
    for resp in (user_redirect, admin_redirect):
        assert resp.status_code == 303, resp.status_code
        assert "login" in resp.headers.get("Location", ""), resp.headers
    print("PASS: unauthenticated /user and /admin redirect to a safe login route")


def _test_single_cookie_model() -> None:
    assert api_main.ARTIFACT_EXPLORER_SESSION_COOKIE == "artifact_explorer_session"
    assert api_main.ARTIFACT_EXPLORER_SESSION_COOKIE_PATH == "/"
    # The token API auth helper is untouched and remains separate from UI login.
    assert callable(api_main.require_token)
    print("PASS: one session cookie name/path; token API auth remains separate")


# --------------------------------------------------------------------------
# Part C/D — central effective-access helper parity
# --------------------------------------------------------------------------
U = "11111111-1111-1111-1111-111111111111"
C = "ACME_01"
G = "gggggggg-gggg-gggg-gggg-gggggggggggg"
D = "dddddddd-dddd-dddd-dddd-dddddddddddd"


def _base_model(*, client_active=True, user_active=True) -> FakeModel:
    m = FakeModel()
    m.clients[C] = {"is_active": client_active}
    m.users[U] = {"is_active": user_active}
    return m


def _caps(view_reports=False, view_database=False, export_database=False):
    return {
        "can_view_reports": view_reports,
        "can_view_database": view_database,
        "can_export_database": export_database,
    }


def _assert_client_parity(model: FakeModel, *, expect_db: bool, expect_reports: bool, label: str) -> None:
    old = _with_model(model)
    try:
        helper = api_main._get_effective_client_access_for_user(U, C)
        inline_has_db = api_main._portal_user_has_database_access_to_client(U, C)
    finally:
        api_main.db_conn = old
    assert helper["can_view_database"] == expect_db, (label, helper)
    assert helper["can_view_reports"] == expect_reports, (label, helper)
    client_active = model.clients[C]["is_active"]
    assert (helper["can_view_database"] and helper["client_is_active"]) == inline_has_db, (label, helper, inline_has_db)
    assert helper["client_is_active"] == client_active, (label, helper)


def _test_client_access_direct() -> None:
    m = _base_model()
    m.user_clients.append({"user_id": U, "client_code": C, **_caps(view_database=True)})
    old = _with_model(m)
    try:
        helper = api_main._get_effective_client_access_for_user(U, C)
    finally:
        api_main.db_conn = old
    assert helper["via_direct"] is True and helper["via_group"] is False, helper
    _assert_client_parity(m, expect_db=True, expect_reports=False, label="direct")
    print("PASS: effective client helper matches inline direct client access")


def _test_client_access_group() -> None:
    m = _base_model()
    m.groups[G] = {"is_active": True}
    m.group_users.append({"group_id": G, "user_id": U})
    m.group_clients.append({"group_id": G, "client_code": C, **_caps(view_database=True)})
    old = _with_model(m)
    try:
        helper = api_main._get_effective_client_access_for_user(U, C)
    finally:
        api_main.db_conn = old
    assert helper["via_group"] is True and helper["via_direct"] is False, helper
    _assert_client_parity(m, expect_db=True, expect_reports=False, label="group")
    print("PASS: effective client helper matches inline group-derived client access")


def _test_client_access_additive_or() -> None:
    m = _base_model()
    m.user_clients.append({"user_id": U, "client_code": C, **_caps(view_reports=True)})
    m.groups[G] = {"is_active": True}
    m.group_users.append({"group_id": G, "user_id": U})
    m.group_clients.append({"group_id": G, "client_code": C, **_caps(view_database=True)})
    _assert_client_parity(m, expect_db=True, expect_reports=True, label="additive-or")
    print("PASS: direct + group remains additive OR across capabilities")


def _test_client_access_inactive_group_ignored() -> None:
    m = _base_model()
    m.groups[G] = {"is_active": False}
    m.group_users.append({"group_id": G, "user_id": U})
    m.group_clients.append({"group_id": G, "client_code": C, **_caps(view_database=True)})
    _assert_client_parity(m, expect_db=False, expect_reports=False, label="inactive-group")
    print("PASS: inactive group does not grant client access (parity preserved)")


def _test_client_access_none_and_inactive_client() -> None:
    m = _base_model()
    _assert_client_parity(m, expect_db=False, expect_reports=False, label="no-grants")
    m2 = _base_model(client_active=False)
    m2.user_clients.append({"user_id": U, "client_code": C, **_caps(view_database=True)})
    # effective db is True but client inactive -> has_database must be False
    _assert_client_parity(m2, expect_db=True, expect_reports=False, label="inactive-client")
    print("PASS: no-grant and inactive-client cases match inline behavior")


def _assert_dataset_parity(model: FakeModel, *, expect_accessible: bool, expect_in_list: bool, label: str) -> None:
    old = _with_model(model)
    try:
        details = api_main._get_effective_dataset_access_details_for_user(U, D)
        existing_wrapper = api_main._get_effective_dataset_access_for_user(U, D)
        inline_single = api_main._get_portal_database_dataset_for_user(D, U)
        inline_list = api_main._list_accessible_portal_database_datasets_for_user(U)
    finally:
        api_main.db_conn = old
    assert details["accessible"] == expect_accessible, (label, details)
    assert (inline_single is not None) == expect_accessible, (label, inline_single)
    # The pre-existing minimal wrapper must agree on accessibility too.
    assert (existing_wrapper is not None) == expect_accessible, (label, existing_wrapper)
    in_list = any(str(row.get("dataset_id")) == D for row in inline_list)
    assert in_list == expect_in_list, (label, inline_list)


def _dataset_model(*, client_db_direct=False, client_db_group=False, rows_direct=False, rows_group=False,
                   dataset_active=True, client_active=True, user_active=True) -> FakeModel:
    m = _base_model(client_active=client_active, user_active=user_active)
    m.datasets[D] = {"client_code": C, "is_active": dataset_active, "dataset_name": "Trips", "slug": "trips"}
    if client_db_direct:
        m.user_clients.append({"user_id": U, "client_code": C, **_caps(view_database=True)})
    if rows_direct:
        m.dataset_users.append({"user_id": U, "dataset_id": D, "can_view_rows": True, "can_filter_rows": True, "can_export_rows": False})
    if client_db_group or rows_group:
        m.groups[G] = {"is_active": True}
        m.group_users.append({"group_id": G, "user_id": U})
    if client_db_group:
        m.group_clients.append({"group_id": G, "client_code": C, **_caps(view_database=True)})
    if rows_group:
        m.dataset_groups.append({"group_id": G, "dataset_id": D, "can_view_rows": True, "can_filter_rows": True, "can_export_rows": False})
    return m


def _test_dataset_access_direct() -> None:
    m = _dataset_model(client_db_direct=True, rows_direct=True)
    old = _with_model(m)
    try:
        helper = api_main._get_effective_dataset_access_details_for_user(U, D)
    finally:
        api_main.db_conn = old
    assert helper["can_view_rows"] is True and helper["via_direct"] is True, helper
    assert helper["client_can_view_database"] is True, helper
    _assert_dataset_parity(m, expect_accessible=True, expect_in_list=True, label="dataset-direct")
    print("PASS: effective dataset helper matches inline user-facing direct dataset access")


def _test_dataset_access_group() -> None:
    m = _dataset_model(client_db_group=True, rows_group=True)
    old = _with_model(m)
    try:
        helper = api_main._get_effective_dataset_access_details_for_user(U, D)
    finally:
        api_main.db_conn = old
    assert helper["can_view_rows"] is True and helper["via_group"] is True, helper
    _assert_dataset_parity(m, expect_accessible=True, expect_in_list=True, label="dataset-group")
    print("PASS: effective dataset helper matches inline group-derived dataset access")


def _test_dataset_access_requires_client_prerequisite() -> None:
    # Has dataset rows but NO client can_view_database -> not accessible.
    m = _dataset_model(client_db_direct=False, rows_direct=True)
    old = _with_model(m)
    try:
        helper = api_main._get_effective_dataset_access_details_for_user(U, D)
    finally:
        api_main.db_conn = old
    assert helper["can_view_rows"] is True and helper["client_can_view_database"] is False, helper
    assert helper["accessible"] is False, helper
    _assert_dataset_parity(m, expect_accessible=False, expect_in_list=False, label="dataset-missing-client-db")
    print("PASS: dataset access still requires client can_view_database (semantics preserved)")


def _test_dataset_access_inactive_dataset() -> None:
    m = _dataset_model(client_db_direct=True, rows_direct=True, dataset_active=False)
    _assert_dataset_parity(m, expect_accessible=False, expect_in_list=False, label="dataset-inactive")
    print("PASS: inactive dataset is not accessible (parity preserved)")


def _test_dataset_access_inactive_user_single() -> None:
    m = _dataset_model(client_db_direct=True, rows_direct=True, user_active=False)
    old = _with_model(m)
    try:
        helper = api_main._get_effective_dataset_access_details_for_user(U, D)
        inline_single = api_main._get_portal_database_dataset_for_user(D, U)
    finally:
        api_main.db_conn = old
    # Single-dataset accessor gates on active user; helper mirrors that exactly.
    assert helper["accessible"] is False, helper
    assert inline_single is None, inline_single
    print("PASS: inactive user blocks single-dataset access; helper matches accessor gate")


def main() -> None:
    _test_login_page_alias_matches()
    _test_login_post_alias_matches()
    _test_logout_alias_matches()
    _test_legacy_login_routes_still_exist()
    _test_portal_unauth_redirects_to_login()
    _test_single_cookie_model()

    _test_client_access_direct()
    _test_client_access_group()
    _test_client_access_additive_or()
    _test_client_access_inactive_group_ignored()
    _test_client_access_none_and_inactive_client()

    _test_dataset_access_direct()
    _test_dataset_access_group()
    _test_dataset_access_requires_client_prerequisite()
    _test_dataset_access_inactive_dataset()
    _test_dataset_access_inactive_user_single()

    print("\nALL PASS: canonical login aliases + central access helper parity")


if __name__ == "__main__":
    main()
