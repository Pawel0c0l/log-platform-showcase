#!/usr/bin/env python3
"""Manual regression tests for Artifact Explorer Phase 7 virtual folders.

Run:

    cd /opt/log-platform
    env PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_artifact_explorer_phase7_virtual_folders.py
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
FOLDER_ID = "33333333-3333-3333-3333-333333333333"
CHILD_FOLDER_ID = "44444444-4444-4444-4444-444444444444"
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
        "can_download": True,
        "can_edit_annotations": True,
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


def _artifact(**overrides):
    artifact = {
        "artifact_id": ARTIFACT_ID,
        "run_id": RUN_ID,
        "created_at": datetime(2026, 5, 13, 10, 5, tzinfo=timezone.utc).isoformat(),
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
    row["created_at"] = datetime(2026, 5, 13, 10, 5, tzinfo=timezone.utc)
    return row


def _folder(**overrides):
    folder = {
        "folder_id": FOLDER_ID,
        "parent_folder_id": None,
        "folder_name": "Raporty FleetWeb",
        "slug": "raporty-fleetweb",
        "description": "FleetWeb report artifacts",
        "folder_type": "manual",
        "search_query_json": {},
        "children_count": 1,
        "artifacts_count": 1,
        "created_by": "api_write_token",
        "created_at": "2026-05-13T10:00:00+00:00",
        "updated_by": "api_write_token",
        "updated_at": "2026-05-13T10:00:00+00:00",
    }
    folder.update(overrides)
    return folder


def _detail_payload(*, artifacts=None):
    return {
        "folder": _folder(),
        "breadcrumbs": [_folder()],
        "child_folders": [_folder(folder_id=CHILD_FOLDER_ID, parent_folder_id=FOLDER_ID, folder_name="Maj 2026", slug="maj-2026")],
        "artifacts": {
            "data": artifacts if artifacts is not None else [_artifact()],
            "meta": {"limit": 50, "offset": 0, "count": 1 if artifacts is None else len(artifacts), "total": 1 if artifacts is None else len(artifacts)},
        },
    }


def _test_migration_and_bootstrap_schema() -> None:
    migration = (REPO_ROOT / "db/migrations/026_artifact_virtual_folders.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS artifact_virtual_folders" in migration, migration
    assert "CREATE TABLE IF NOT EXISTS artifact_virtual_folder_items" in migration, migration
    assert "idx_artifact_virtual_folders_root_slug_unique" in migration, migration
    assert "idx_artifact_virtual_folders_child_name_unique" in migration, migration
    assert "ON DELETE CASCADE" in migration, migration
    mig027 = (REPO_ROOT / "db/migrations/027_artifact_smart_folders.sql").read_text()
    assert "folder_type" in mig027
    assert "search_query_json" in mig027
    assert "manual" in mig027 and "smart" in mig027
    assert "jsonb_typeof" in mig027
    assert "artifact_virtual_folders" in api_main.SCHEMA_SQL, "startup schema missing virtual folders"
    assert "artifact_virtual_folder_items" in api_main.SCHEMA_SQL, "startup schema missing virtual folder items"
    assert "folder_type" in api_main.SCHEMA_SQL and "search_query_json" in api_main.SCHEMA_SQL
    assert "ON CONFLICT (folder_id, artifact_id) DO NOTHING" in Path(api_main.__file__).read_text()
    print("PASS: Phase 7 migration and startup schema define metadata-only virtual folder tables")


def _test_folder_name_and_slug_helpers() -> None:
    assert api_main._slugify_folder_name("Raporty FleetWeb") == "raporty-fleetweb"
    assert api_main._slugify_folder_name("Przekroczenia prędkości") == "przekroczenia-predkosci"
    assert api_main._normalize_folder_name(" Maj 2026 ") == "Maj 2026"
    for bad_name in ("", "a/b", "a\\b", "bad\nname"):
        try:
            api_main._normalize_folder_name(bad_name)
        except _HTTPException as exc:
            assert exc.status_code == 400, exc.status_code
        else:
            raise AssertionError(f"invalid folder name accepted: {bad_name!r}")
    print("PASS: folder names are validated and slugs are URL-safe")


def _test_token_api_routes() -> None:
    old_read = api_main.READ_TOKEN
    old_write = api_main.WRITE_TOKEN
    old_list = api_main._list_virtual_folders
    old_detail = api_main._get_virtual_folder_detail
    old_create = api_main._create_virtual_folder
    old_update = api_main._update_virtual_folder
    old_delete = api_main._delete_virtual_folder
    old_add = api_main._add_artifact_to_virtual_folder
    old_remove = api_main._remove_artifact_from_virtual_folder
    old_db = api_main.db_conn
    old_memberships = api_main._get_artifact_virtual_folders
    try:
        api_main.READ_TOKEN = "read-token"
        api_main.WRITE_TOKEN = "write-token"
        api_main._list_virtual_folders = lambda parent_folder_id=None: [_folder(parent_folder_id=parent_folder_id)]
        api_main._get_virtual_folder_detail = lambda folder_id, **kwargs: _detail_payload()
        api_main._create_virtual_folder = lambda **kwargs: _folder(folder_name=kwargs["folder_name"], parent_folder_id=kwargs.get("parent_folder_id"))
        api_main._update_virtual_folder = lambda folder_id, **kwargs: _folder(folder_id=folder_id, folder_name=kwargs.get("folder_name") or "Updated")
        api_main._delete_virtual_folder = lambda folder_id: {"ok": True, "folder_id": folder_id}
        api_main._add_artifact_to_virtual_folder = lambda folder_id, **kwargs: {"folder_id": folder_id, "artifact_id": kwargs["artifact_id"]}
        api_main._remove_artifact_from_virtual_folder = lambda folder_id, artifact_id: {"ok": True, "folder_id": folder_id, "artifact_id": artifact_id}
        api_main._get_artifact_virtual_folders = lambda artifact_id: [_folder()]

        class _Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def execute(self, *args, **kwargs):
                pass

            def fetchone(self):
                return {"ok": 1}

        class _Conn:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def cursor(self):
                return _Cursor()

        api_main.db_conn = lambda: _Conn()

        assert api_main.artifact_browser_list_virtual_folders(authorization="Bearer read-token")["data"][0]["folder_name"] == "Raporty FleetWeb"
        assert api_main.artifact_browser_get_virtual_folder(FOLDER_ID, authorization="Bearer read-token")["folder"]["folder_id"] == FOLDER_ID
        assert api_main.artifact_browser_create_virtual_folder(
            {"folder_name": "Raporty FleetWeb", "description": "FleetWeb report artifacts", "folder_type": "manual"},
            authorization="Bearer write-token",
        )["slug"] == "raporty-fleetweb"
        assert api_main.artifact_browser_update_virtual_folder(
            FOLDER_ID,
            {"folder_name": "Updated", "description": None},
            authorization="Bearer write-token",
        )["folder_name"] == "Updated"
        assert api_main.artifact_browser_delete_virtual_folder(FOLDER_ID, authorization="Bearer write-token")["ok"]
        assert api_main.artifact_browser_add_virtual_folder_artifact(
            FOLDER_ID,
            {"artifact_id": ARTIFACT_ID},
            authorization="Bearer write-token",
        )["artifact_id"] == ARTIFACT_ID
        assert api_main.artifact_browser_remove_virtual_folder_artifact(
            FOLDER_ID,
            ARTIFACT_ID,
            authorization="Bearer write-token",
        )["ok"]
        assert api_main.artifact_browser_get_artifact_virtual_folders(
            ARTIFACT_ID,
            authorization="Bearer read-token",
        )["data"][0]["folder_id"] == FOLDER_ID
    finally:
        api_main.READ_TOKEN = old_read
        api_main.WRITE_TOKEN = old_write
        api_main._list_virtual_folders = old_list
        api_main._get_virtual_folder_detail = old_detail
        api_main._create_virtual_folder = old_create
        api_main._update_virtual_folder = old_update
        api_main._delete_virtual_folder = old_delete
        api_main._add_artifact_to_virtual_folder = old_add
        api_main._remove_artifact_from_virtual_folder = old_remove
        api_main.db_conn = old_db
        api_main._get_artifact_virtual_folders = old_memberships
    print("PASS: token API routes list/create/update/delete folders and add/remove artifact memberships")


def _test_ui_folder_routes_and_rbac() -> None:
    admin = _user(_perm(), admin=True)
    non_admin = _user(_perm(), admin=False)
    captured = {}

    old_current = api_main.get_current_artifact_user
    old_list = api_main._list_virtual_folders
    old_detail = api_main._get_virtual_folder_detail
    old_create = api_main._create_virtual_folder
    old_update = api_main._update_virtual_folder
    old_delete = api_main._delete_virtual_folder
    old_add = api_main._add_artifact_to_virtual_folder
    old_remove = api_main._remove_artifact_from_virtual_folder
    old_require = api_main._require_virtual_folder
    try:
        api_main.get_current_artifact_user = lambda request: non_admin
        api_main._require_virtual_folder = lambda fid: _folder(folder_id=fid, folder_type="manual")
        api_main._list_virtual_folders = lambda parent_folder_id=None: [_folder(parent_folder_id=parent_folder_id)]
        html = _html(api_main.artifact_explorer_folders_index(request=_FakeRequest(path="/artifact-explorer/folders")))
        assert "Raporty FleetWeb" in html, html
        assert "Create folder" not in html, html

        try:
            response = api_main.artifact_explorer_create_folder("Private", None, None, request=_FakeRequest())
            assert response.status_code == 403, response.status_code
        except _HTTPException as exc:
            assert exc.status_code == 403, exc.status_code

        def _capturing_detail(folder_id, **kwargs):
            captured.update(kwargs)
            return _detail_payload(artifacts=[_artifact(workflow_name="workflow_b")])

        api_main._get_virtual_folder_detail = _capturing_detail
        html = _html(api_main.artifact_explorer_folder_detail(FOLDER_ID, request=_FakeRequest(path=f"/artifact-explorer/folders/{FOLDER_ID}")))
        assert "Folders" in html and "Raporty FleetWeb" in html, html
        assert "Maj 2026" in html, html
        assert "report.csv" in html, html
        assert captured["user"] == non_admin, captured
        assert captured["action"] == "view", captured
        assert "Manage folder" not in html, html

        api_main.get_current_artifact_user = lambda request: admin
        api_main._create_virtual_folder = lambda **kwargs: _folder(folder_name=kwargs["folder_name"], parent_folder_id=kwargs.get("parent_folder_id"))
        response = api_main.artifact_explorer_create_folder("Maj 2026", "", FOLDER_ID, request=_FakeRequest())
        assert response.status_code == 303, response.status_code
        assert response.headers["Location"].startswith("/artifact-explorer/folders/"), response.headers

        api_main._update_virtual_folder = lambda folder_id, **kwargs: _folder(folder_id=folder_id, folder_name=kwargs["folder_name"])
        response = api_main.artifact_explorer_update_folder(FOLDER_ID, "Updated", "desc", request=_FakeRequest())
        assert response.status_code == 303, response.status_code

        api_main._delete_virtual_folder = lambda folder_id: {"ok": True}
        response = api_main.artifact_explorer_delete_folder(FOLDER_ID, "/artifact-explorer/folders", request=_FakeRequest())
        assert response.status_code == 303, response.status_code

        api_main._add_artifact_to_virtual_folder = lambda folder_id, **kwargs: {"folder_id": folder_id, "artifact_id": kwargs["artifact_id"]}
        response = api_main.artifact_explorer_add_artifact_to_folder(ARTIFACT_ID, FOLDER_ID, request=_FakeRequest())
        assert response.status_code == 303, response.status_code

        api_main._remove_artifact_from_virtual_folder = lambda folder_id, artifact_id: {"ok": True}
        response = api_main.artifact_explorer_remove_artifact_from_folder(ARTIFACT_ID, FOLDER_ID, request=_FakeRequest())
        assert response.status_code == 303, response.status_code
    finally:
        api_main.get_current_artifact_user = old_current
        api_main._list_virtual_folders = old_list
        api_main._get_virtual_folder_detail = old_detail
        api_main._create_virtual_folder = old_create
        api_main._update_virtual_folder = old_update
        api_main._delete_virtual_folder = old_delete
        api_main._add_artifact_to_virtual_folder = old_add
        api_main._remove_artifact_from_virtual_folder = old_remove
        api_main._require_virtual_folder = old_require
    print("PASS: UI folder routes load, filter artifacts through view RBAC, and restrict management to admins")


def _test_artifact_detail_folder_membership() -> None:
    admin = _user(_perm(), admin=True)
    old_current = api_main.get_current_artifact_user
    old_row = api_main._get_artifact_browser_row
    old_run = api_main._get_run_summary
    old_raw = api_main._get_raw_file_summary
    old_preview = api_main._artifact_explorer_preview_for_row
    old_folders = api_main._get_artifact_virtual_folders
    old_flat = api_main._list_all_virtual_folders_flat
    try:
        api_main.get_current_artifact_user = lambda request: admin
        api_main._get_artifact_browser_row = lambda artifact_id: _row()
        api_main._get_run_summary = lambda run_id: {"run_id": run_id, "status": "SUCCESS"}
        api_main._get_raw_file_summary = lambda raw_file_id: None
        api_main._artifact_explorer_preview_for_row = lambda row, artifact: {"preview_type": "text", "text": "ok"}
        api_main._get_artifact_virtual_folders = lambda artifact_id: [_folder(path="Raporty FleetWeb")]
        api_main._list_all_virtual_folders_flat = lambda: [_folder(path="Raporty FleetWeb")]
        html = _html(api_main.artifact_explorer_detail(ARTIFACT_ID, request=_FakeRequest()))
        assert "Virtual Folders" in html, html
        assert "Raporty FleetWeb" in html, html
        assert "Add to manual folder" in html, html
        assert "Remove" in html, html
    finally:
        api_main.get_current_artifact_user = old_current
        api_main._get_artifact_browser_row = old_row
        api_main._get_run_summary = old_run
        api_main._get_raw_file_summary = old_raw
        api_main._artifact_explorer_preview_for_row = old_preview
        api_main._get_artifact_virtual_folders = old_folders
        api_main._list_all_virtual_folders_flat = old_flat
    print("PASS: artifact detail shows virtual folder memberships and admin add/remove controls")


def _test_smart_folder_query_validation_and_filters() -> None:
    cleaned = api_main._validate_smart_folder_search_query(
        {"workflow_name": ["workflow_b", "workflow_a"], "report_type": ["report_207"], "search": "speed"}
    )
    assert cleaned["workflow_name"] == ["workflow_b", "workflow_a"]
    try:
        api_main._validate_smart_folder_search_query({"typo_key": []})
    except _HTTPException as exc:
        assert exc.status_code == 400
    else:
        raise AssertionError("unknown key allowed")
    try:
        api_main._validate_smart_folder_search_query({"workflow_name": 3})
    except _HTTPException:
        pass
    else:
        raise AssertionError("invalid type allowed")
    flt = api_main._filters_from_smart_folder_query(
        api_main._validate_smart_folder_search_query({"workflow_name": ["workflow_b"], "date_from": "2026-05-01"})
    )
    assert flt.get("workflow_name") == ["workflow_b"]
    assert flt.get("date_from") is not None
    print("PASS: smart folder search_query_json allowlist and filter mapping behave as expected")


def _test_list_manual_excludes_smart_and_paths_disambiguate() -> None:
    old = api_main._list_all_virtual_folders_flat
    try:
        a = _folder(folder_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", folder_name="Nested")
        a["path"] = "Raporty FleetWeb / Przekroczenia prędkości / Nested"
        b = _folder(
            folder_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            folder_name="SmartBox",
            folder_type="smart",
            search_query_json={"tag": ["reviewed"]},
        )
        b["path"] = "Raporty FleetWeb / SmartBox"
        api_main._list_all_virtual_folders_flat = lambda: [a, b]
        manual = api_main.list_manual_virtual_folders_with_paths()
        assert len(manual) == 1
        assert manual[0]["folder_id"] == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        all_paths = [f["path"] for f in api_main.list_virtual_folders_with_paths()]
        assert "Raporty FleetWeb / Przekroczenia prędkości / Nested" in all_paths
        assert "Raporty FleetWeb / SmartBox" in all_paths
        c = _folder(folder_id="cccccccc-cccc-cccc-cccc-cccccccccccc", folder_name="Maj 2026")
        c["path"] = "Raporty FleetWeb / Przekroczenia prędkości / Maj 2026"
        d = _folder(folder_id="dddddddd-dddd-dddd-dddd-dddddddddddd", folder_name="Maj 2026")
        d["path"] = "Raporty FleetWeb / Inne / Maj 2026"
        api_main._list_all_virtual_folders_flat = lambda: [c, d]
        paths = {x["path"] for x in api_main.list_virtual_folders_with_paths()}
        assert paths == {
            "Raporty FleetWeb / Przekroczenia prędkości / Maj 2026",
            "Raporty FleetWeb / Inne / Maj 2026",
        }
    finally:
        api_main._list_all_virtual_folders_flat = old
    print("PASS: list_virtual_folders_with_paths / list_manual_virtual_folders_with_paths path behavior")


def main() -> None:
    _test_migration_and_bootstrap_schema()
    _test_folder_name_and_slug_helpers()
    _test_token_api_routes()
    _test_ui_folder_routes_and_rbac()
    _test_artifact_detail_folder_membership()
    _test_smart_folder_query_validation_and_filters()
    _test_list_manual_excludes_smart_and_paths_disambiguate()


if __name__ == "__main__":
    main()
