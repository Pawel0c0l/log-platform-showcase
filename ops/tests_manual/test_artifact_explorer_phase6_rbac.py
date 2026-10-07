#!/usr/bin/env python3
"""Manual regression tests for Artifact Explorer Phase 6 local RBAC.

Run:

    cd /opt/log-platform
    env PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_artifact_explorer_phase6_rbac.py
"""
from __future__ import annotations

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


ARTIFACT_ID = "22222222-2222-2222-2222-222222222222"
RUN_ID = "666ff6cc-aa5b-4c07-8eaa-3a95d3a4bd2c"


class _FakeUrl:
    def __init__(self, path="/artifact-explorer", query=""):
        self.path = path
        self.query = query


class _FakeRequest:
    def __init__(self, *, cookies=None, path="/artifact-explorer", query=""):
        self.cookies = cookies or {}
        self.url = _FakeUrl(path, query)


def _html(response) -> str:
    return response.body.decode("utf-8")


def _artifact(**overrides):
    artifact = {
        "artifact_id": ARTIFACT_ID,
        "run_id": RUN_ID,
        "created_at": datetime(2026, 5, 12, 22, 11, 44, tzinfo=timezone.utc).isoformat(),
        "kind": "REPORT",
        "filename": "report.csv",
        "display_filename": "report.csv",
        "content_type": "text/csv",
        "size_bytes": 100,
        "sha256": "abc",
        "storage_backend": "S3",
        "storage_key": "workflow_b/stage_2_clean/key.csv",
        "raw_file_id": None,
        "workflow_name": "workflow_b",
        "stage_name": "stage_2_clean",
        "artifact_role": "cleaned",
        "report_type": "report_207",
        "client_code": "TEST_CLIENT",
        "file_ext": "csv",
        "layout_version": 2,
        "metadata_json": {},
        "description": None,
        "manual_metadata_json": {},
        "tags": ["reviewed"],
    }
    artifact.update(overrides)
    return artifact


def _row(**overrides):
    row = dict(_artifact(**overrides))
    row["created_at"] = datetime(2026, 5, 12, 22, 11, 44, tzinfo=timezone.utc)
    return row


def _user(*permissions, admin=False):
    return {
        "user_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "username": "alice",
        "display_name": "Alice",
        "is_active": True,
        "is_admin": admin,
        "permissions": list(permissions),
    }


def _perm(**overrides):
    permission = {
        "can_view": True,
        "can_preview": True,
        "can_download": False,
        "can_edit_annotations": False,
        "workflow_name": None,
        "stage_name": None,
        "artifact_role": None,
        "report_type": None,
        "client_code": None,
        "file_ext": None,
        "layout_version": None,
        "tag": None,
    }
    permission.update(overrides)
    return permission


def _test_password_hashing() -> None:
    password_hash = api_main.hash_artifact_password("secret")
    assert "secret" not in password_hash, password_hash
    assert password_hash.startswith("pbkdf2_sha256$"), password_hash
    assert api_main.verify_artifact_password("secret", password_hash)
    assert not api_main.verify_artifact_password("wrong", password_hash)
    print("PASS: password hashes verify and do not store plaintext")


def _test_login_logout_routes() -> None:
    html = _html(api_main.artifact_explorer_login_page(_FakeRequest()))
    assert "Artifact Explorer Login" in html, html

    old_auth = api_main.authenticate_artifact_user
    try:
        api_main.authenticate_artifact_user = lambda username, password: _user(admin=True) if (username, password) == ("alice", "pw") else None
        ok = api_main.artifact_explorer_login("alice", "pw", "/artifact-explorer?search=x")
        assert ok.status_code == 303, ok.status_code
        assert ok.headers["Location"] == "/artifact-explorer?search=x", ok.headers
        assert "artifact_explorer_session=" in ok.headers["Set-Cookie"], ok.headers

        bad = api_main.artifact_explorer_login("alice", "bad", "/artifact-explorer")
        assert bad.status_code == 200, bad.status_code
        assert "Invalid username or password" in _html(bad), _html(bad)
    finally:
        api_main.authenticate_artifact_user = old_auth

    logout = api_main.artifact_explorer_logout_post()
    assert logout.status_code == 303, logout.status_code
    assert "Max-Age=0" in logout.headers["Set-Cookie"], logout.headers
    print("PASS: login/logout routes create and clear sessions")


def _test_rbac_matching() -> None:
    artifact = _artifact()
    assert api_main.user_can_access_artifact(_user(_perm()), artifact, "view")
    assert api_main.user_can_access_artifact(_user(_perm(workflow_name="workflow_b")), artifact, "view")
    assert not api_main.user_can_access_artifact(_user(_perm(workflow_name="workflow_a")), artifact, "view")
    assert api_main.user_can_access_artifact(_user(_perm(stage_name="stage_2_clean")), artifact, "view")
    assert not api_main.user_can_access_artifact(_user(_perm(stage_name="stage_1_fetch")), artifact, "view")
    assert api_main.user_can_access_artifact(_user(_perm(artifact_role="cleaned")), artifact, "view")
    assert not api_main.user_can_access_artifact(_user(_perm(artifact_role="raw")), artifact, "view")
    assert api_main.user_can_access_artifact(_user(_perm(report_type="report_207")), artifact, "view")
    assert not api_main.user_can_access_artifact(_user(_perm(report_type="report_602")), artifact, "view")
    assert api_main.user_can_access_artifact(_user(_perm(client_code="TEST_CLIENT")), artifact, "view")
    assert not api_main.user_can_access_artifact(_user(_perm(client_code="OTHER")), artifact, "view")
    assert api_main.user_can_access_artifact(_user(_perm(file_ext="csv")), artifact, "view")
    assert not api_main.user_can_access_artifact(_user(_perm(file_ext="pdf")), artifact, "view")
    assert api_main.user_can_access_artifact(_user(_perm(layout_version=2)), artifact, "view")
    assert not api_main.user_can_access_artifact(_user(_perm(layout_version=1)), artifact, "view")
    assert api_main.user_can_access_artifact(_user(_perm(tag="reviewed")), artifact, "view")
    assert not api_main.user_can_access_artifact(_user(_perm(tag="missing")), artifact, "view")
    assert api_main.user_can_access_artifact(_user(admin=True), artifact, "download")
    print("PASS: RBAC helper matches wildcard, dimensions, tags, and admin bypass")


def _test_permission_where_clause() -> None:
    user = _user(_perm(client_code="TEST_CLIENT"))
    old_schema = api_main._database_export_schema_available
    try:
        api_main._database_export_schema_available = lambda: True
        clause, params = api_main.build_artifact_permission_where_clause(user, "view")
        assert "artifact_user_roles" in clause, clause
        assert "arp.can_view IS TRUE" in clause, clause
        assert "arp.client_code IS NULL OR arp.client_code = artifacts.client_code" in clause, clause
        assert params == [user["user_id"], user["user_id"]], params
        assert "owner_user_id" in clause, clause
        assert "NOT (artifacts.workflow_name = 'database_explorer'" in clause, clause

        api_main._database_export_schema_available = lambda: False
        legacy_clause, legacy_params = api_main.build_artifact_permission_where_clause(user, "view")
        assert "artifact_user_roles" in legacy_clause, legacy_clause
        assert "owner_user_id" not in legacy_clause, legacy_clause
        assert "expires_at" not in legacy_clause and "expired_at" not in legacy_clause, legacy_clause
        assert "NOT (artifacts.workflow_name = 'database_explorer'" not in legacy_clause, legacy_clause
        assert legacy_params == [user["user_id"]], legacy_params
        assert api_main.build_artifact_permission_where_clause(_user(admin=True), "view") == (None, [])
    finally:
        api_main._database_export_schema_available = old_schema
    print("PASS: RBAC SQL permission clause is generated for post-043 and legacy pre-043 schemas")


def _test_ui_access() -> None:
    unauth = api_main.artifact_explorer_index(request=_FakeRequest(path="/artifact-explorer", query="search=x"))
    assert unauth.status_code == 303, unauth.status_code
    assert "/artifact-explorer/login" in unauth.headers["Location"], unauth.headers

    allowed_user = _user(_perm(can_download=True, can_edit_annotations=True))
    view_only_user = _user(_perm(can_preview=False, can_download=False, can_edit_annotations=False))
    no_view_user = _user(_perm(workflow_name="workflow_a"))

    old_current = api_main.get_current_artifact_user
    old_facets = api_main._get_artifact_browser_facets
    old_kind_counts = api_main._get_artifact_kind_counts
    old_list = api_main._list_artifact_browser_items
    old_row = api_main._get_artifact_browser_row
    old_run = api_main._get_run_summary
    old_raw = api_main._get_raw_file_summary
    old_get_folders = api_main._get_artifact_virtual_folders
    old_preview = api_main._artifact_explorer_preview_for_row
    old_stream = api_main._stream_artifact_download
    old_update = api_main._upsert_artifact_metadata_override
    try:
        api_main.get_current_artifact_user = lambda request: allowed_user
        api_main._get_artifact_browser_facets = lambda **kwargs: {key: [] for key in api_main.ARTIFACT_BROWSER_FACET_FIELDS}
        api_main._get_artifact_kind_counts = lambda **kwargs: []
        api_main._list_artifact_browser_items = lambda **kwargs: {
            "data": [_artifact()],
            "meta": {"limit": 50, "offset": 0, "count": 1, "total": 1},
        }
        html = _html(api_main.artifact_explorer_index(request=_FakeRequest()))
        assert "report.csv" in html, html
        assert "Download" in html, html

        api_main.get_current_artifact_user = lambda request: view_only_user
        api_main._get_artifact_browser_row = lambda artifact_id: _row()
        api_main._get_run_summary = lambda run_id: {"run_id": run_id, "status": "SUCCESS"}
        api_main._get_raw_file_summary = lambda raw_file_id: None
        api_main._get_artifact_virtual_folders = lambda artifact_id: []
        api_main._artifact_explorer_preview_for_row = lambda row, artifact: (_ for _ in ()).throw(AssertionError("preview should not be built"))
        html = _html(api_main.artifact_explorer_detail(ARTIFACT_ID, request=_FakeRequest()))
        assert "Preview is not allowed" in html, html
        assert "cannot edit" in html, html

        try:
            api_main.artifact_explorer_download_artifact(ARTIFACT_ID, request=_FakeRequest())
        except _HTTPException as exc:
            assert exc.status_code == 403, exc.status_code
        else:
            raise AssertionError("download without permission should fail")

        try:
            api_main.artifact_explorer_update_metadata(ARTIFACT_ID, "desc", "{}", request=_FakeRequest())
        except _HTTPException as exc:
            assert exc.status_code == 403, exc.status_code
        else:
            raise AssertionError("annotation edit without permission should fail")

        api_main.get_current_artifact_user = lambda request: no_view_user
        try:
            api_main.artifact_explorer_detail(ARTIFACT_ID, request=_FakeRequest())
        except _HTTPException as exc:
            assert exc.status_code == 403, exc.status_code
        else:
            raise AssertionError("detail without view permission should fail")

        api_main.get_current_artifact_user = lambda request: allowed_user
        api_main._stream_artifact_download = lambda row, disposition="attachment": _StreamingResponse(b"ok")
        response = api_main.artifact_explorer_download_artifact(ARTIFACT_ID, request=_FakeRequest())
        assert isinstance(response, _StreamingResponse), response
        api_main._upsert_artifact_metadata_override = lambda *args, **kwargs: {"ok": True}
        api_main._artifact_explorer_preview_for_row = lambda row, artifact: {"preview_type": "text", "text": "ok"}
        html = _html(api_main.artifact_explorer_update_metadata(ARTIFACT_ID, "desc", "{}", request=_FakeRequest()))
        assert "Manual metadata saved" in html, html
    finally:
        api_main.get_current_artifact_user = old_current
        api_main._get_artifact_browser_facets = old_facets
        api_main._get_artifact_kind_counts = old_kind_counts
        api_main._list_artifact_browser_items = old_list
        api_main._get_artifact_browser_row = old_row
        api_main._get_run_summary = old_run
        api_main._get_raw_file_summary = old_raw
        api_main._get_artifact_virtual_folders = old_get_folders
        api_main._artifact_explorer_preview_for_row = old_preview
        api_main._stream_artifact_download = old_stream
        api_main._upsert_artifact_metadata_override = old_update
    print("PASS: UI routes enforce login, view, preview, download, and edit permissions")


def _test_token_api_unchanged() -> None:
    old_read = api_main.READ_TOKEN
    old_write = api_main.WRITE_TOKEN
    old_list = api_main._list_artifact_browser_items
    old_update = api_main._upsert_artifact_metadata_override
    try:
        api_main.READ_TOKEN = "read-token"
        api_main.WRITE_TOKEN = "write-token"
        api_main._list_artifact_browser_items = lambda **kwargs: {"data": [], "meta": {"limit": 50, "offset": 0, "count": 0, "total": 0}}
        result = api_main.artifact_browser_list_artifacts(authorization="Bearer read-token")
        assert result["data"] == [], result
        api_main._upsert_artifact_metadata_override = lambda *args, **kwargs: {"ok": True}
        assert api_main.artifact_browser_update_artifact_metadata(
            ARTIFACT_ID,
            {"description": "x", "metadata_json": {}},
            authorization="Bearer write-token",
        ) == {"ok": True}
    finally:
        api_main.READ_TOKEN = old_read
        api_main.WRITE_TOKEN = old_write
        api_main._list_artifact_browser_items = old_list
        api_main._upsert_artifact_metadata_override = old_update
    print("PASS: token-based Artifact Browser API remains machine-level access")


def main() -> None:
    _test_password_hashing()
    _test_login_logout_routes()
    _test_rbac_matching()
    _test_permission_where_clause()
    _test_ui_access()
    _test_token_api_unchanged()


if __name__ == "__main__":
    main()
