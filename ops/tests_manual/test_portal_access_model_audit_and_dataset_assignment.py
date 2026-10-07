#!/usr/bin/env python3
"""Manual tests for the portal access model audit + dataset-user visibility fix.

Focus: dataset visibility/access assignment from the admin UI, the eligibility
model (active user with effective client `can_view_database`, direct OR group),
the actionable empty-state diagnostics, and the additive direct/group effective
access used by the user-facing `/user/database`.

Run:

    cd /opt/log-platform
    env PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_portal_access_model_audit_and_dataset_assignment.py
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


class _HTMLResponse:
    def __init__(self, content, status_code=200, headers=None, media_type=None):
        self.body = str(content).encode("utf-8")
        self.status_code = status_code
        self.headers = headers or {}
        self.media_type = media_type or "text/html"


class _StreamingResponse:
    def __init__(self, body, media_type=None, headers=None):
        self.body = body
        self.media_type = media_type
        self.headers = headers or {}
        self.status_code = 200


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
GROUP_ID = "dddddddd-dddd-dddd-dddd-dddddddddddd"
CLIENT_CODE = "ALPHA00001"


class _FakeUrl:
    def __init__(self, path="/", query=""):
        self.path = path
        self.query = query


class _FakeRequest:
    def __init__(self, *, path="/", query="", cookies=None):
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
        "client_code": CLIENT_CODE,
        "display_name": "Alpha",
        "description": "Alpha test client",
        "database_name": "alpha_main",
        "is_active": True,
        "assigned_users": 1,
    }
    data.update(overrides)
    return data


def _dataset(**overrides):
    data = {
        "dataset_id": DATASET_ID,
        "client_code": CLIENT_CODE,
        "client_display_name": "Alpha",
        "client_database_name": "alpha_main",
        "dataset_name": "Client trips",
        "slug": "client-trips",
        "description": "Trips from client DB",
        "schema_name": "public",
        "table_name": "client_trips",
        "default_date_column": "start_timestamp",
        "is_active": True,
        "visible_columns": 2,
        "assigned_users": 1,
        "can_view_rows": True,
        "can_filter_rows": True,
        "can_export_rows": False,
    }
    data.update(overrides)
    return data


def _eligible_user(user_id=USER_ID, username="alice"):
    return {
        "user_id": user_id,
        "username": username,
        "display_name": username.title(),
        "is_active": True,
        "is_admin": False,
    }


def _assignment_data(*, available=None, assigned=None, available_groups=None, assigned_groups=None,
                     eligible_user_count=None, eligible_group_count=None, client=None, columns=None):
    avail = [_eligible_user()] if available is None else available
    return {
        "dataset": _dataset(),
        "client": _client() if client is None else client,
        "assigned": assigned or [],
        "available": avail,
        "assigned_groups": assigned_groups or [],
        "available_groups": available_groups or [],
        "eligible_user_count": len(avail) if eligible_user_count is None else eligible_user_count,
        "eligible_group_count": (len(available_groups or []) if eligible_group_count is None else eligible_group_count),
        "columns": columns if columns is not None else [
            {"column_name": "record_id", "display_name": "Record id", "data_type": "uuid",
             "display_order": 1, "is_visible": True, "is_filterable": True, "is_sortable": True,
             "is_default_date_column": False},
        ],
    }


def _patch(name, value):
    old = getattr(api_main, name)
    setattr(api_main, name, value)
    return old


def _restore(patches):
    for name, old in reversed(patches):
        setattr(api_main, name, old)


def _admin_patches(extra=None):
    patches = [("get_current_artifact_user", _patch("get_current_artifact_user", lambda request: _user(admin=True, user_id=ADMIN_ID, username="admin")))]
    if extra:
        patches.extend(extra)
    return patches


def _capture_audit(patches):
    events: list[dict] = []
    patches.append(("_portal_audit_event_safe", _patch("_portal_audit_event_safe", lambda **kwargs: events.append(kwargs))))
    return events


class _RoutedCursor:
    def __init__(self, router, calls):
        self._router = router
        self._calls = calls
        self._result = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self._calls.append((sql, params))
        self._result = self._router(sql, params)
        self.rowcount = 1 if self._result else 0

    def fetchall(self):
        return self._result if isinstance(self._result, list) else []

    def fetchone(self):
        if isinstance(self._result, list):
            return self._result[0] if self._result else None
        return self._result


class _RoutedConn:
    def __init__(self, router, calls):
        self._router = router
        self._calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return _RoutedCursor(self._router, self._calls)


def _routed_db(router, calls):
    return ("db_conn", _patch("db_conn", lambda: _RoutedConn(router, calls)))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def _test_dataset_detail_renders_assignment_section() -> None:
    patches = _admin_patches([
        ("_get_portal_database_dataset", _patch("_get_portal_database_dataset", lambda dataset_id: _dataset())),
        ("_get_portal_database_dataset_assignment_data", _patch("_get_portal_database_dataset_assignment_data", lambda dataset_id: _assignment_data())),
        ("_list_portal_clients", _patch("_list_portal_clients", lambda: [_client()])),
    ])
    try:
        html = _html(api_main._portal_database_dataset_form_response(_user(admin=True), dataset=_dataset()))
    finally:
        _restore(patches)
    assert "Assigned users" in html and "Assign user" in html, html
    assert "Access diagnostics" in html, html
    assert f'action="/admin/client-access/database/{DATASET_ID}/users"' in html, html
    print("PASS: dataset detail page renders the assignment section, access diagnostics, and correct POST action")


def _test_eligible_user_appears_with_client_database_access() -> None:
    patches = [
        ("_get_portal_database_dataset", _patch("_get_portal_database_dataset", lambda dataset_id: _dataset())),
        ("_list_users_with_database_access_for_client", _patch("_list_users_with_database_access_for_client", lambda client_code: [_eligible_user()] if client_code == CLIENT_CODE else [])),
    ]
    try:
        eligible = api_main._list_users_eligible_for_dataset_assignment(DATASET_ID)
    finally:
        _restore(patches)
    assert [u["user_id"] for u in eligible] == [USER_ID], eligible
    print("PASS: user with effective client database access is eligible for dataset assignment")


def _test_user_without_client_access_not_eligible() -> None:
    patches = [
        ("_get_portal_database_dataset", _patch("_get_portal_database_dataset", lambda dataset_id: _dataset())),
        ("_list_users_with_database_access_for_client", _patch("_list_users_with_database_access_for_client", lambda client_code: [])),
    ]
    try:
        eligible = api_main._list_users_eligible_for_dataset_assignment(DATASET_ID)
    finally:
        _restore(patches)
    assert eligible == [], eligible
    print("PASS: user lacking client database access does not appear as eligible")


def _test_empty_eligibility_shows_actionable_diagnostic() -> None:
    patches = _admin_patches([
        ("_get_portal_database_dataset", _patch("_get_portal_database_dataset", lambda dataset_id: _dataset())),
        ("_get_portal_database_dataset_assignment_data", _patch("_get_portal_database_dataset_assignment_data", lambda dataset_id: _assignment_data(available=[], available_groups=[]))),
        ("_list_portal_clients", _patch("_list_portal_clients", lambda: [_client()])),
    ])
    try:
        html = _html(api_main._portal_database_dataset_form_response(_user(admin=True), dataset=_dataset()))
    finally:
        _restore(patches)
    assert "No eligible users found" in html, html
    assert "Assign client database access" in html, html
    assert 'href="/admin/client-access"' in html, html
    print("PASS: empty eligibility shows actionable diagnostic linking to Client Access")


def _test_eligibility_query_filters_active_user_client_and_view_database() -> None:
    calls: list = []
    router = lambda sql, params: []
    patches = [_routed_db(router, calls)]
    try:
        api_main._list_users_with_database_access_for_client(CLIENT_CODE)
    finally:
        _restore(patches)
    sql = calls[0][0]
    assert "u.is_active IS TRUE" in sql, sql
    assert "pc.is_active IS TRUE" in sql, sql
    assert "can_view_database IS TRUE" in sql, sql
    assert "UNION ALL" in sql and "portal_group_clients" in sql, sql
    print("PASS: eligibility query enforces active user/client and effective can_view_database (direct OR group)")


def _test_assignment_write_requires_client_access() -> None:
    # Active user WITH client DB access -> insert performed, no error.
    inserts: list = []
    router = lambda sql, params: inserts.append((sql, params)) or []
    patches = [
        ("_get_portal_database_dataset", _patch("_get_portal_database_dataset", lambda dataset_id: _dataset())),
        ("_get_portal_user", _patch("_get_portal_user", lambda user_id: _user())),
        ("_portal_user_has_database_access_to_client", _patch("_portal_user_has_database_access_to_client", lambda user_id, client_code: True)),
        _routed_db(router, []),
    ]
    try:
        error = api_main._assign_portal_database_dataset_user(dataset_id=DATASET_ID, user_id=USER_ID, granted_by=ADMIN_ID)
    finally:
        _restore(patches)
    assert error is None, error
    assert any("INSERT INTO portal_database_dataset_users" in sql for sql, _ in inserts), inserts

    # User WITHOUT client DB access -> blocked, no insert.
    blocked_inserts: list = []
    router2 = lambda sql, params: blocked_inserts.append((sql, params)) or []
    patches = [
        ("_get_portal_database_dataset", _patch("_get_portal_database_dataset", lambda dataset_id: _dataset())),
        ("_get_portal_user", _patch("_get_portal_user", lambda user_id: _user())),
        ("_portal_user_has_database_access_to_client", _patch("_portal_user_has_database_access_to_client", lambda user_id, client_code: False)),
        _routed_db(router2, []),
    ]
    try:
        error = api_main._assign_portal_database_dataset_user(dataset_id=DATASET_ID, user_id=USER_ID, granted_by=ADMIN_ID)
    finally:
        _restore(patches)
    assert error and "database access" in error, error
    assert blocked_inserts == [], blocked_inserts
    print("PASS: dataset-user assignment inserts only when client database access exists; otherwise blocked with no write")


def _test_assignment_route_posts_and_audits() -> None:
    captured: dict = {}
    patches = _admin_patches([
        ("_get_portal_database_dataset", _patch("_get_portal_database_dataset", lambda dataset_id: _dataset())),
        ("_assign_portal_database_dataset_user", _patch("_assign_portal_database_dataset_user", lambda **kwargs: captured.update(kwargs))),
        ("_portal_audit_user_metadata_by_id", _patch("_portal_audit_user_metadata_by_id", lambda user_id: {"target_username": "alice"})),
    ])
    events = _capture_audit(patches)
    try:
        response = api_main.admin_portal_assign_database_dataset_user(DATASET_ID, _FakeRequest(), user_id=USER_ID)
    finally:
        _restore(patches)
    assert response.status_code == 303, response.status_code
    assert captured.get("user_id") == USER_ID and captured.get("dataset_id") == DATASET_ID, captured
    assert [e["event_type"] for e in events] == ["database_dataset_user_assigned"], events
    print("PASS: POST dataset-user assignment writes via helper and emits a single audit event")


def _test_assigned_user_sees_dataset_under_user_database() -> None:
    patches = [
        ("_list_accessible_portal_database_datasets_for_user", _patch("_list_accessible_portal_database_datasets_for_user", lambda user_id: [_dataset(can_view_rows=True, can_filter_rows=True, can_export_rows=False)])),
    ]
    try:
        html = _html(api_main._user_database_response(_user()))
    finally:
        _restore(patches)
    assert "Client trips" in html, html
    # Approved stage S9: one comparison-table row per dataset, with the open
    # action and neutral permission badges resolved from the effective grant.
    assert f'href="/user/database/datasets/{DATASET_ID}"' in html and "Otw\u00f3rz arkusz" in html, html
    assert "Filtrowanie" in html and "Tylko podgl\u0105d" in html, html
    print("PASS: assigned user sees the dataset row under /user/database with filter/export state from flags")


def _test_effective_access_helper_reflects_flags() -> None:
    patches = [
        ("_list_accessible_portal_database_datasets_for_user", _patch("_list_accessible_portal_database_datasets_for_user", lambda user_id: [_dataset(can_filter_rows=True, can_export_rows=False)])),
    ]
    try:
        access = api_main._get_effective_dataset_access_for_user(USER_ID, DATASET_ID)
        none_access = api_main._get_effective_dataset_access_for_user(USER_ID, "ffffffff-ffff-ffff-ffff-ffffffffffff")
    finally:
        _restore(patches)
    assert access == {"can_view_rows": True, "can_filter_rows": True, "can_export_rows": False}, access
    assert none_access is None, none_access
    print("PASS: effective dataset access helper reflects can_filter/can_export flags and returns None when not accessible")


def _test_inactive_user_blocked_from_assignment() -> None:
    patches = [
        ("_get_portal_database_dataset", _patch("_get_portal_database_dataset", lambda dataset_id: _dataset())),
        ("_get_portal_user", _patch("_get_portal_user", lambda user_id: _user(user_id=user_id) | {"is_active": False})),
    ]
    try:
        error = api_main._assign_portal_database_dataset_user(dataset_id=DATASET_ID, user_id=USER_ID, granted_by=ADMIN_ID)
    finally:
        _restore(patches)
    assert error and "Active" in error, error
    print("PASS: inactive user is blocked from dataset assignment")


def _test_group_eligibility_and_additive_or_query() -> None:
    # Group eligibility wrapper.
    patches = [
        ("_get_portal_database_dataset", _patch("_get_portal_database_dataset", lambda dataset_id: _dataset())),
        ("_list_groups_with_database_access_for_client", _patch("_list_groups_with_database_access_for_client", lambda client_code: [{"group_id": GROUP_ID, "group_name": "Dispatch", "is_active": True}])),
    ]
    try:
        groups = api_main._list_groups_eligible_for_dataset_assignment(DATASET_ID)
    finally:
        _restore(patches)
    assert [g["group_id"] for g in groups] == [GROUP_ID], groups

    # The user-facing query is additive direct OR group for both client and dataset access.
    calls: list = []
    patches = [_routed_db(lambda sql, params: [], calls)]
    try:
        api_main._list_accessible_portal_database_datasets_for_user(USER_ID)
    finally:
        _restore(patches)
    sql = calls[0][0]
    assert "bool_or(can_view_database)" in sql, sql
    assert "bool_or(can_view_rows)" in sql, sql
    assert "portal_database_dataset_groups" in sql and "portal_group_clients" in sql, sql
    assert sql.count("UNION ALL") >= 2, sql
    print("PASS: group access is eligible and the effective-access query is additive direct OR group")


def _test_multi_database_mapping_preserved_and_dataset_deactivation_denied() -> None:
    calls: list = []
    patches = [_routed_db(lambda sql, params: [], calls)]
    try:
        api_main._list_accessible_portal_database_datasets_for_user(USER_ID)
    finally:
        _restore(patches)
    sql = calls[0][0]
    assert "pc.database_name" in sql, sql
    assert "pdd.is_active IS TRUE" in sql, sql
    assert "pc.is_active IS TRUE" in sql, sql
    print("PASS: row/export routing keeps client database_name mapping and dataset/client deactivation stays denied")


def _test_regular_user_cannot_access_admin_assignment_routes() -> None:
    old = _patch("get_current_artifact_user", lambda request: _user(admin=False))
    try:
        forbidden_assign = api_main.admin_portal_assign_database_dataset_user(DATASET_ID, _FakeRequest(), user_id=USER_ID)
        forbidden_detail = api_main.admin_portal_database_dataset_detail(DATASET_ID, _FakeRequest())
        forbidden_perms = api_main.admin_portal_update_database_dataset_user_permissions(DATASET_ID, USER_ID, _FakeRequest(), can_view_rows="1")
    finally:
        api_main.get_current_artifact_user = old
    for response in (forbidden_assign, forbidden_detail, forbidden_perms):
        assert response.status_code == 403 and "Access denied" in _html(response), _html(response)
    print("PASS: regular users cannot access admin dataset assignment routes")


def _test_no_secret_or_raw_sql_or_row_values_rendered() -> None:
    patches = _admin_patches([
        ("_get_portal_database_dataset", _patch("_get_portal_database_dataset", lambda dataset_id: _dataset())),
        ("_get_portal_database_dataset_assignment_data", _patch("_get_portal_database_dataset_assignment_data", lambda dataset_id: _assignment_data(
            assigned=[{"user_id": USER_ID, "username": "alice", "display_name": "Alice", "can_view_rows": True, "can_filter_rows": True, "can_export_rows": False}],
            assigned_groups=[{"group_id": GROUP_ID, "group_name": "Dispatch", "description": "Dispatch team", "can_view_rows": True, "can_filter_rows": False, "can_export_rows": True}],
            available=[], available_groups=[],
        ))),
        ("_list_portal_clients", _patch("_list_portal_clients", lambda: [_client()])),
    ])
    try:
        html = _html(api_main._portal_database_dataset_form_response(_user(admin=True), dataset=_dataset()))
    finally:
        _restore(patches)
    lowered = html.lower()
    assert "postgres://" not in lowered and "password" not in lowered, html
    assert "secret_ref" not in lowered and "://" not in lowered, html
    # No raw SQL statements rendered (HTML <select> elements are fine; SQL SELECT/FROM are not).
    assert "select * from" not in lowered and "from portal_" not in lowered, html
    # Source labels make direct vs group provenance explicit without leaking data.
    assert "source: direct" in html, html
    assert "effective via group" in html, html
    print("PASS: dataset detail renders provenance labels but no secrets, DSNs, raw SQL, or client row values")


def main() -> None:
    _test_dataset_detail_renders_assignment_section()
    _test_eligible_user_appears_with_client_database_access()
    _test_user_without_client_access_not_eligible()
    _test_empty_eligibility_shows_actionable_diagnostic()
    _test_eligibility_query_filters_active_user_client_and_view_database()
    _test_assignment_write_requires_client_access()
    _test_assignment_route_posts_and_audits()
    _test_assigned_user_sees_dataset_under_user_database()
    _test_effective_access_helper_reflects_flags()
    _test_inactive_user_blocked_from_assignment()
    _test_group_eligibility_and_additive_or_query()
    _test_multi_database_mapping_preserved_and_dataset_deactivation_denied()
    _test_regular_user_cannot_access_admin_assignment_routes()
    _test_no_secret_or_raw_sql_or_row_values_rendered()


if __name__ == "__main__":
    main()
