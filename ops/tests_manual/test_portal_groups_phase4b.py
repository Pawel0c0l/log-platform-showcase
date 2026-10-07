#!/usr/bin/env python3
"""Manual regression tests for Phase 4B portal groups.

Run:

    cd /opt/log-platform
    env PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_portal_groups_phase4b.py

This file focuses on the Phase 4B recovery surface and is intended to be run
alongside the earlier portal, Artifact Explorer, and Artifact Browser manual
regression scripts listed in the task acceptance criteria.
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
GROUP_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
DATASET_ID = "dddddddd-dddd-dddd-dddd-dddddddddddd"
FOLDER_ID = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
ARTIFACT_ID = "ffffffff-ffff-ffff-ffff-ffffffffffff"
CLIENT_CODE = "ACME_01"


class _FakeUrl:
    def __init__(self, path="/admin/groups", query=""):
        self.path = path
        self.query = query


class _FakeClient:
    host = "127.0.0.1"


class _FakeRequest:
    def __init__(self, *, path="/admin/groups", query="", cookies=None):
        self.url = _FakeUrl(path, query)
        self.cookies = cookies or {}
        self.headers = {"user-agent": "manual-test"}
        self.client = _FakeClient()


def _html(response) -> str:
    return response.body.decode("utf-8")


def _user(*, admin=False, user_id=USER_ID, username="alice", active=True):
    return {
        "user_id": user_id,
        "username": username,
        "display_name": username.title(),
        "is_active": active,
        "is_admin": admin,
        "roles": [],
        "permissions": [],
    }


def _group(**overrides):
    data = {
        "group_id": GROUP_ID,
        "group_name": "Operations",
        "description": "Ops users",
        "is_active": True,
        "users_count": 1,
        "clients_count": 1,
        "report_folders_count": 1,
        "datasets_count": 1,
    }
    data.update(overrides)
    return data


def _dataset(**overrides):
    data = {
        "dataset_id": DATASET_ID,
        "client_code": CLIENT_CODE,
        "client_display_name": "Acme Logistics",
        "dataset_name": "Trips",
        "slug": "trips",
        "description": "Trips",
        "schema_name": "public",
        "table_name": "client_trips",
        "default_date_column": "trip_date",
        "is_active": True,
        "can_view_rows": True,
        "can_filter_rows": True,
        "can_export_rows": True,
    }
    data.update(overrides)
    return data


def _folder(**overrides):
    data = {
        "folder_id": FOLDER_ID,
        "client_code": CLIENT_CODE,
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


def _patch(name, value):
    old = getattr(api_main, name)
    setattr(api_main, name, value)
    return old


def _restore(patches):
    for name, old in reversed(patches):
        setattr(api_main, name, old)


def _capture_audit(patches):
    events = []
    patches.append(("_portal_audit_event_safe", _patch("_portal_audit_event_safe", lambda **kwargs: events.append(kwargs))))
    return events


def _admin_patches(extra=None):
    patches = [("get_current_artifact_user", _patch("get_current_artifact_user", lambda request: _user(admin=True, user_id=ADMIN_ID, username="admin")))]
    if extra:
        patches.extend(extra)
    return patches


def _test_admin_groups_auth_boundaries() -> None:
    patches = [("_portal_audit_event_safe", _patch("_portal_audit_event_safe", lambda **kwargs: None))]
    patches.append(("get_current_artifact_user", _patch("get_current_artifact_user", lambda request: None)))
    try:
        unauth = api_main.admin_portal_groups(_FakeRequest())
    finally:
        _restore(patches)
    assert unauth.status_code == 303, unauth.status_code
    assert unauth.headers["Location"].startswith("/artifact-explorer/login"), unauth.headers

    patches = [("_portal_audit_event_safe", _patch("_portal_audit_event_safe", lambda **kwargs: None))]
    patches.append(("get_current_artifact_user", _patch("get_current_artifact_user", lambda request: _user(admin=False))))
    try:
        forbidden = api_main.admin_portal_groups(_FakeRequest())
    finally:
        _restore(patches)
    assert forbidden.status_code == 403, forbidden.status_code
    assert "Access denied" in _html(forbidden), _html(forbidden)
    print("PASS: /admin/groups requires authenticated admin access")


def _test_group_create_edit_duplicate_and_audit() -> None:
    patches = _admin_patches([
        ("_create_portal_group", _patch("_create_portal_group", lambda **kwargs: GROUP_ID)),
        ("_get_portal_group", _patch("_get_portal_group", lambda group_id: _group(group_id=group_id))),
    ])
    events = _capture_audit(patches)
    try:
        created = api_main.admin_portal_create_group(_FakeRequest(), group_name=" Operations ", description="Ops", is_active="1")
        assert created.status_code == 303, created.status_code
        assert created.headers["Location"] == f"/admin/groups/{GROUP_ID}", created.headers
        assert events[-1]["event_type"] == "portal_group_created", events
    finally:
        _restore(patches)

    group_states = iter([_group(group_id=GROUP_ID, is_active=True), _group(group_id=GROUP_ID, group_name="Ops 2", description="Changed", is_active=False)])
    patches = _admin_patches([
        ("_get_portal_group", _patch("_get_portal_group", lambda group_id: next(group_states))),
        ("_update_portal_group", _patch("_update_portal_group", lambda **kwargs: None)),
    ])
    events = _capture_audit(patches)
    try:
        updated = api_main.admin_portal_update_group(GROUP_ID, _FakeRequest(), group_name="Ops 2", description="Changed", is_active=None)
        assert updated.status_code == 303, updated.status_code
        assert events[-1]["event_type"] == "portal_group_updated", events
        assert "is_active" in events[-1]["metadata"]["changed_fields"], events[-1]
    finally:
        _restore(patches)

    patches = _admin_patches([
        ("_create_portal_group", _patch("_create_portal_group", lambda **kwargs: (_ for _ in ()).throw(ValueError("Group name already exists.")))),
    ])
    try:
        duplicate = api_main.admin_portal_create_group(_FakeRequest(), group_name="Ops", description="", is_active="1")
        html = _html(duplicate)
        assert duplicate.status_code == 400, duplicate.status_code
        assert "Group name already exists." in html, html
        assert "Traceback" not in html, html
    finally:
        _restore(patches)
    print("PASS: group create/edit and duplicate validation are clean")


def _test_group_membership_and_client_routes() -> None:
    patches = _admin_patches([
        ("_get_portal_group", _patch("_get_portal_group", lambda group_id: _group(group_id=group_id))),
        ("_assign_portal_group_user", _patch("_assign_portal_group_user", lambda **kwargs: None)),
        ("_remove_portal_group_user", _patch("_remove_portal_group_user", lambda **kwargs: None)),
        ("_assign_portal_group_client", _patch("_assign_portal_group_client", lambda **kwargs: None)),
        ("_update_portal_group_client_permissions", _patch("_update_portal_group_client_permissions", lambda **kwargs: None)),
        ("_remove_portal_group_client", _patch("_remove_portal_group_client", lambda **kwargs: None)),
        ("_portal_audit_user_metadata_by_id", _patch("_portal_audit_user_metadata_by_id", lambda user_id, prefix="target": {f"{prefix}_user_id": user_id})),
    ])
    events = _capture_audit(patches)
    try:
        assert api_main.admin_portal_assign_group_user(GROUP_ID, _FakeRequest(), user_id=USER_ID).status_code == 303
        assert api_main.admin_portal_remove_group_user(GROUP_ID, USER_ID, _FakeRequest()).status_code == 303
        assert api_main.admin_portal_assign_group_client(GROUP_ID, _FakeRequest(), client_code=CLIENT_CODE.lower()).status_code == 303
        assert api_main.admin_portal_update_group_client_permissions(
            GROUP_ID,
            CLIENT_CODE,
            _FakeRequest(),
            can_view_database="1",
            can_view_reports=None,
            can_export_database="1",
        ).status_code == 303
        assert api_main.admin_portal_remove_group_client(GROUP_ID, CLIENT_CODE, _FakeRequest()).status_code == 303
    finally:
        _restore(patches)
    event_types = [event["event_type"] for event in events]
    assert event_types == [
        "portal_group_user_assigned",
        "portal_group_user_removed",
        "portal_group_client_assigned",
        "portal_group_client_permissions_updated",
        "portal_group_client_removed",
    ], event_types
    flags = events[3]["metadata"]["permission_flags"]
    assert flags == {"can_view_database": True, "can_view_reports": False, "can_export_database": True}, flags
    print("PASS: group membership and client access routes audit changes")


def _test_inactive_group_and_missing_client_access_validation() -> None:
    patches = [
        ("_get_portal_group", _patch("_get_portal_group", lambda group_id: _group(is_active=False))),
        ("_get_portal_client", _patch("_get_portal_client", lambda client_code: {"client_code": client_code, "is_active": True})),
    ]
    try:
        assert api_main._assign_portal_group_client(group_id=GROUP_ID, client_code=CLIENT_CODE, granted_by=ADMIN_ID) == "Only active groups can receive client access."
    finally:
        _restore(patches)

    patches = [
        ("_get_portal_group", _patch("_get_portal_group", lambda group_id: _group(is_active=True))),
        ("_get_portal_report_folder", _patch("_get_portal_report_folder", lambda folder_id: _folder())),
        ("_get_portal_database_dataset", _patch("_get_portal_database_dataset", lambda dataset_id: _dataset())),
        ("_portal_group_has_client_access", _patch("_portal_group_has_client_access", lambda *args, **kwargs: False)),
    ]
    try:
        assert api_main._assign_portal_report_folder_group(folder_id=FOLDER_ID, group_id=GROUP_ID, granted_by=ADMIN_ID) == "Group must have report access to this folder's client before assignment."
        assert api_main._assign_portal_database_dataset_group(dataset_id=DATASET_ID, group_id=GROUP_ID, granted_by=ADMIN_ID) == "Group must have database access to this dataset's client before assignment."
    finally:
        _restore(patches)
    print("PASS: inactive groups and missing client-level access do not grant folder/dataset access")


def _test_report_folder_group_routes_and_ui() -> None:
    patches = _admin_patches([
        ("_get_portal_report_folder", _patch("_get_portal_report_folder", lambda folder_id: _folder())),
        ("_get_portal_group", _patch("_get_portal_group", lambda group_id: _group(group_id=group_id))),
        ("_assign_portal_report_folder_group", _patch("_assign_portal_report_folder_group", lambda **kwargs: None)),
        ("_remove_portal_report_folder_group", _patch("_remove_portal_report_folder_group", lambda **kwargs: None)),
        ("_get_portal_report_folder_assignment_data", _patch("_get_portal_report_folder_assignment_data", lambda folder_id: {
            "assigned": [],
            "available": [],
            "assigned_groups": [_group()],
            "available_groups": [_group(group_id="99999999-9999-9999-9999-999999999999", group_name="Dispatch")],
        })),
        ("_list_portal_clients", _patch("_list_portal_clients", lambda: [{"client_code": CLIENT_CODE, "display_name": "Acme Logistics", "is_active": True}])),
    ])
    events = _capture_audit(patches)
    try:
        assert api_main.admin_portal_assign_report_folder_group(FOLDER_ID, _FakeRequest(), group_id=GROUP_ID).status_code == 303
        assert api_main.admin_portal_remove_report_folder_group(FOLDER_ID, GROUP_ID, _FakeRequest()).status_code == 303
        html = _html(api_main._portal_report_folder_form_response(_user(admin=True), folder=_folder()))
    finally:
        _restore(patches)
    assert "Assigned users" in html and "Assigned groups" in html, html
    assert "/admin/report-folders/" in html and "/groups" in html, html
    assert [event["event_type"] for event in events] == ["report_folder_group_assigned", "report_folder_group_removed"], events
    print("PASS: report folder group routes and UI are wired")


def _test_database_dataset_group_routes_and_ui() -> None:
    patches = _admin_patches([
        ("_get_portal_database_dataset", _patch("_get_portal_database_dataset", lambda dataset_id: _dataset())),
        ("_get_portal_group", _patch("_get_portal_group", lambda group_id: _group(group_id=group_id))),
        ("_assign_portal_database_dataset_group", _patch("_assign_portal_database_dataset_group", lambda **kwargs: None)),
        ("_update_portal_database_dataset_group_permissions", _patch("_update_portal_database_dataset_group_permissions", lambda **kwargs: None)),
        ("_remove_portal_database_dataset_group", _patch("_remove_portal_database_dataset_group", lambda **kwargs: None)),
        ("_get_portal_database_dataset_assignment_data", _patch("_get_portal_database_dataset_assignment_data", lambda dataset_id: {
            "columns": [],
            "assigned": [],
            "available": [],
            "assigned_groups": [_group(can_view_rows=True, can_filter_rows=False, can_export_rows=True)],
            "available_groups": [_group(group_id="99999999-9999-9999-9999-999999999999", group_name="Dispatch")],
        })),
        ("_list_portal_clients", _patch("_list_portal_clients", lambda: [{"client_code": CLIENT_CODE, "display_name": "Acme Logistics", "is_active": True}])),
    ])
    events = _capture_audit(patches)
    try:
        assert api_main.admin_portal_assign_database_dataset_group(DATASET_ID, _FakeRequest(), group_id=GROUP_ID).status_code == 303
        assert api_main.admin_portal_update_database_dataset_group_permissions(
            DATASET_ID,
            GROUP_ID,
            _FakeRequest(),
            can_view_rows="1",
            can_filter_rows=None,
            can_export_rows="1",
        ).status_code == 303
        assert api_main.admin_portal_remove_database_dataset_group(DATASET_ID, GROUP_ID, _FakeRequest()).status_code == 303
        html = _html(api_main._portal_database_dataset_form_response(_user(admin=True), dataset=_dataset()))
    finally:
        _restore(patches)
    assert "Assigned users" in html and "Assigned groups" in html, html
    assert "/admin/client-access/database/" in html and "/groups" in html, html
    event_types = [event["event_type"] for event in events]
    assert event_types == [
        "database_dataset_group_assigned",
        "database_dataset_group_permissions_updated",
        "database_dataset_group_removed",
    ], event_types
    assert events[1]["metadata"]["permission_flags"] == {"can_view_rows": True, "can_filter_rows": False, "can_export_rows": True}
    print("PASS: database dataset group routes, flags, and UI are wired")


def _test_effective_access_sql_uses_direct_and_group_union() -> None:
    # The user-facing report-folder list and accessor now delegate to the
    # centralized set-based helper (Phase 2E); the additive direct+group union SQL
    # lives there.
    report_list = inspect.getsource(api_main._list_effective_report_folder_access_for_user)
    report_get = report_list
    # The user-facing dataset list now delegates to the centralized set-based
    # helper (Phase 2C); the additive direct+group union SQL lives there.
    database_list = inspect.getsource(api_main._list_effective_dataset_access_for_user)
    database_get = inspect.getsource(api_main._get_portal_database_dataset_for_user)
    # The admin summary's folder/dataset counts now delegate to dedicated count
    # helpers (Phase 2G); the additive direct+group union SQL lives there.
    folder_count = inspect.getsource(api_main._count_effective_report_folders_for_user)
    dataset_count = inspect.getsource(api_main._count_effective_database_datasets_for_user)

    for source in (report_list, report_get):
        assert "portal_report_folder_users" in source, source
        assert "portal_report_folder_groups" in source, source
        assert "pg.is_active IS TRUE" in source, source
        assert "eca.can_view_reports IS TRUE" in source, source

    for source in (database_list, database_get):
        assert "portal_database_dataset_users" in source, source
        assert "portal_database_dataset_groups" in source, source
        assert "bool_or(can_view_rows)" in source, source
        assert "bool_or(can_filter_rows)" in source, source
        assert "bool_or(can_export_rows)" in source, source
        assert "eca.can_view_database IS TRUE" in source, source

    for source in (folder_count, dataset_count):
        assert "portal_user_clients" in source and "portal_group_clients" in source, source
        assert "effective_client_access" in source, source
        assert "pg.is_active IS TRUE" in source, source
    assert "eca.can_view_reports IS TRUE" in folder_count, folder_count
    assert "eca.can_view_database IS TRUE" in dataset_count, dataset_count
    print("PASS: effective access SQL combines direct and active group permissions additively")


def _test_user_wrappers_enforce_effective_access() -> None:
    patches = [
        ("_get_accessible_portal_report_folder_for_user", _patch("_get_accessible_portal_report_folder_for_user", lambda user_id, folder_id: None)),
    ]
    try:
        assert api_main._portal_report_artifact_access(_user(), FOLDER_ID, ARTIFACT_ID, action="preview") is None
    finally:
        _restore(patches)

    patches = _admin_patches([
        ("get_current_artifact_user", _patch("get_current_artifact_user", lambda request: _user(admin=False))),
        ("_get_portal_database_dataset_for_user", _patch("_get_portal_database_dataset_for_user", lambda dataset_id, user_id: None)),
        ("_portal_audit_event_safe", _patch("_portal_audit_event_safe", lambda **kwargs: None)),
    ])
    try:
        denied = api_main.user_portal_database_dataset(DATASET_ID, _FakeRequest(path=f"/user/database/datasets/{DATASET_ID}"))
        assert denied.status_code == 404, denied.status_code
    finally:
        _restore(patches)

    patches = [
        ("get_current_artifact_user", _patch("get_current_artifact_user", lambda request: _user(admin=False))),
        ("_get_portal_database_dataset_for_user", _patch("_get_portal_database_dataset_for_user", lambda dataset_id, user_id: _dataset(can_export_rows=False))),
        ("_portal_audit_event_safe", _patch("_portal_audit_event_safe", lambda **kwargs: None)),
    ]
    try:
        denied = api_main.user_portal_database_dataset_export(DATASET_ID, _FakeRequest(path=f"/user/database/datasets/{DATASET_ID}/export", query="format=csv"))
        assert denied.status_code == 403, denied.status_code
        assert "Export is not enabled" in _html(denied), _html(denied)
    finally:
        _restore(patches)
    print("PASS: report preview/download, row browser, and export wrappers enforce effective access")


def _test_audit_event_catalog_and_security_boundaries() -> None:
    required = {
        "portal_group_created",
        "portal_group_updated",
        "portal_group_user_assigned",
        "portal_group_user_removed",
        "portal_group_client_assigned",
        "portal_group_client_permissions_updated",
        "portal_group_client_removed",
        "report_folder_group_assigned",
        "report_folder_group_removed",
        "database_dataset_group_assigned",
        "database_dataset_group_permissions_updated",
        "database_dataset_group_removed",
    }
    missing = required - api_main.PORTAL_AUDIT_EVENT_TYPES
    assert not missing, missing

    code = Path(api_main.__file__).read_text()
    assert "portal_group_clients" in code, "group access must be explicit through portal_group_clients"
    assert "artifact_roles" in code, "technical Artifact Explorer RBAC remains separate"
    assert "portal_groups" in code and "artifact_user_roles" in code, "portal groups must not replace artifact roles"
    assert "connection_string" in api_main.PORTAL_AUDIT_UNSAFE_KEY_PATTERNS
    print("PASS: audit event catalog and security boundaries are present")


def main() -> int:
    tests = [
        _test_admin_groups_auth_boundaries,
        _test_group_create_edit_duplicate_and_audit,
        _test_group_membership_and_client_routes,
        _test_inactive_group_and_missing_client_access_validation,
        _test_report_folder_group_routes_and_ui,
        _test_database_dataset_group_routes_and_ui,
        _test_effective_access_sql_uses_direct_and_group_union,
        _test_user_wrappers_enforce_effective_access,
        _test_audit_event_catalog_and_security_boundaries,
    ]
    for test in tests:
        test()
    print("PASS: Phase 4B portal groups manual regression checks completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
