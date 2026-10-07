#!/usr/bin/env python3
"""Manual regression tests for Admin/User Portal Phase 2C report folders.

Run:

    cd /opt/log-platform
    env PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_portal_report_folders_phase2c.py
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
FOLDER_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
ARTIFACT_ID = "dddddddd-dddd-dddd-dddd-dddddddddddd"


class _FakeUrl:
    def __init__(self, path="/admin/report-folders", query=""):
        self.path = path
        self.query = query


class _FakeRequest:
    def __init__(self, *, path="/admin/report-folders", query="", cookies=None):
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


def _folder(**overrides):
    data = {
        "folder_id": FOLDER_ID,
        "client_code": "ACME_01",
        "client_display_name": "Acme Logistics",
        "folder_name": "Monthly reports",
        "slug": "monthly-reports",
        "description": "Customer monthly reports",
        "search_query_json": {"report_type": ["monthly"], "file_ext": ["pdf"]},
        "is_active": True,
        "can_preview": True,
        "can_download": True,
        "assigned_users": 1,
    }
    data.update(overrides)
    return data


def _artifact(**overrides):
    data = {
        "artifact_id": ARTIFACT_ID,
        "display_filename": "monthly-report.pdf",
        "original_filename": "source-monthly.pdf",
        "client_code": "ACME_01",
        "report_type": "monthly",
        "stage_name": "stage2",
        "artifact_role": "cleaned",
        "file_ext": "pdf",
        "size_bytes": 2048,
        "created_at_local": "2026-05-28 10:00",
        "storage_key": "secret/internal/key.pdf",
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


def _admin_patches(extra=None):
    patches = [
        ("get_current_artifact_user", _patch("get_current_artifact_user", lambda request: _user(admin=True, user_id=ADMIN_ID, username="admin"))),
    ]
    if extra:
        patches.extend(extra)
    return patches


def _test_admin_auth_boundaries() -> None:
    old = _patch("get_current_artifact_user", lambda request: None)
    try:
        unauth = api_main.admin_portal_report_folders(_FakeRequest())
    finally:
        api_main.get_current_artifact_user = old
    assert unauth.status_code == 303, unauth.status_code
    assert unauth.headers["Location"].startswith("/artifact-explorer/login"), unauth.headers

    old = _patch("get_current_artifact_user", lambda request: _user(admin=False))
    try:
        forbidden = api_main.admin_portal_report_folders(_FakeRequest())
    finally:
        api_main.get_current_artifact_user = old
    assert forbidden.status_code == 403, forbidden.status_code
    assert "Access denied" in _html(forbidden), _html(forbidden)
    print("PASS: /admin/report-folders requires authenticated admin access")


def _test_admin_dashboard_and_create_form() -> None:
    patches = _admin_patches([
        ("_list_portal_report_folders", _patch("_list_portal_report_folders", lambda: [_folder()])),
        ("_list_portal_clients", _patch("_list_portal_clients", lambda: [_client()])),
    ])
    try:
        dashboard = api_main.admin_portal_report_folders(_FakeRequest())
        form = api_main.admin_portal_new_report_folder_form(_FakeRequest(path="/admin/report-folders/new"))
    finally:
        _restore(patches)
    assert "Report folders" in _html(dashboard) and "Monthly reports" in _html(dashboard), _html(dashboard)
    assert "Create report folder" in _html(form) and "Safe artifact filters" in _html(form), _html(form)
    print("PASS: admin can view report-folder dashboard and create form")


def _test_create_validation_and_redirect() -> None:
    calls = []

    def fake_create(**kwargs):
        calls.append(kwargs)
        return FOLDER_ID

    patches = _admin_patches([
        ("_list_portal_clients", _patch("_list_portal_clients", lambda: [_client()])),
        ("_create_portal_report_folder", _patch("_create_portal_report_folder", fake_create)),
    ])
    try:
        ok = api_main.admin_portal_create_report_folder(
            request=_FakeRequest(path="/admin/report-folders/new"),
            client_code="acme_01",
            folder_name="Monthly reports",
            slug="monthly-reports",
            description="Customer monthly reports",
            is_active="1",
            can_preview="1",
            can_download="1",
            workflow_name="workflow_b",
            stage_name="stage2",
            artifact_role="cleaned",
            report_type="monthly",
            file_ext="pdf",
            tag="reviewed",
            search="monthly",
            date_from="2026-01-01T00:00:00+00:00",
            date_to=None,
        )
    finally:
        _restore(patches)
    assert ok.status_code == 303 and ok.headers["Location"] == f"/admin/report-folders/{FOLDER_ID}", ok.headers
    created = calls[0]
    assert created["client_code"] == "ACME_01", created
    assert created["search_query_json"]["report_type"] == ["monthly"], created
    assert created["search_query_json"]["tag"] == ["reviewed"], created

    try:
        api_main._validate_portal_report_folder_search_query({"client_code": "ACME_01"})
    except ValueError as exc:
        assert "Unknown report folder filter key" in str(exc), exc
    else:
        raise AssertionError("client_code filter unexpectedly accepted")

    for kwargs, expected in [
        ({"client_code": "MISSING", "folder_name": "Name", "slug": "good-slug", "search_query_json": {}}, "Client not found"),
        ({"client_code": "ACME_01", "folder_name": "Name", "slug": "monthly-reports", "search_query_json": {}}, "already exists"),
        ({"client_code": "ACME_01", "folder_name": "Name", "slug": "bad slug", "search_query_json": {}}, "Slug"),
    ]:
        patches = [
            ("_get_portal_client", _patch("_get_portal_client", lambda code: _client(client_code=code) if code == "ACME_01" else None)),
            ("_portal_report_folder_slug_exists", _patch("_portal_report_folder_slug_exists", lambda client_code, slug, exclude_folder_id=None: slug == "monthly-reports")),
        ]
        try:
            try:
                api_main._create_portal_report_folder(
                    client_code=kwargs["client_code"],
                    folder_name=kwargs["folder_name"],
                    slug=kwargs["slug"],
                    description=None,
                    search_query_json=kwargs["search_query_json"],
                    is_active=True,
                    can_preview=True,
                    can_download=True,
                    actor_user_id=ADMIN_ID,
                )
            except ValueError as exc:
                assert expected in str(exc), (expected, exc)
            else:
                raise AssertionError(f"{expected} case unexpectedly succeeded")
        finally:
            _restore(patches)
    print("PASS: create validates filters, client, slug uniqueness, and redirects after success")


def _test_edit_and_admin_preview() -> None:
    calls = []

    def fake_update(**kwargs):
        calls.append(kwargs)
        return None

    patches = _admin_patches([
        ("_get_portal_report_folder", _patch("_get_portal_report_folder", lambda folder_id: _folder() if folder_id == FOLDER_ID else None)),
        ("_list_portal_clients", _patch("_list_portal_clients", lambda: [_client()])),
        ("_get_portal_report_folder_assignment_data", _patch("_get_portal_report_folder_assignment_data", lambda folder_id: {"folder": _folder(), "assigned": [], "available": []})),
        ("_update_portal_report_folder", _patch("_update_portal_report_folder", fake_update)),
        ("_portal_report_folder_artifacts", _patch("_portal_report_folder_artifacts", lambda folder, limit=50, offset=0: {"data": [_artifact()], "meta": {"total": 1}})),
    ])
    try:
        detail = api_main.admin_portal_report_folder_detail(FOLDER_ID, _FakeRequest(path=f"/admin/report-folders/{FOLDER_ID}"))
        update = api_main.admin_portal_update_report_folder(
            FOLDER_ID,
            _FakeRequest(path=f"/admin/report-folders/{FOLDER_ID}"),
            client_code="ACME_01",
            folder_name="Updated reports",
            slug="updated-reports",
            description="Updated",
            is_active="1",
            can_preview=None,
            can_download="1",
            workflow_name="workflow_b",
            stage_name="stage2",
            artifact_role="cleaned",
            report_type="monthly",
            file_ext="pdf",
            tag=None,
            search=None,
            date_from=None,
            date_to=None,
        )
        preview = api_main.admin_portal_report_folder_preview(FOLDER_ID, _FakeRequest(path=f"/admin/report-folders/{FOLDER_ID}/preview"))
    finally:
        _restore(patches)
    assert detail.status_code == 200 and "Assigned users" in _html(detail), _html(detail)
    assert update.status_code == 303 and update.headers["Location"] == f"/admin/report-folders/{FOLDER_ID}", update.headers
    assert calls[0]["folder_name"] == "Updated reports" and calls[0]["can_preview"] is False, calls
    preview_html = _html(preview)
    assert "monthly-report.pdf" in preview_html, preview_html
    assert "secret/internal/key.pdf" not in preview_html, preview_html
    print("PASS: admin can edit folders and preview matching reports without storage keys")


def _test_assignment_gate() -> None:
    calls = []

    def fake_assign(**kwargs):
        calls.append(kwargs)
        if kwargs["user_id"] == "no-report-access":
            return "User must have report access to this folder's client before assignment."
        return None

    patches = _admin_patches([
        ("_get_portal_report_folder", _patch("_get_portal_report_folder", lambda folder_id: _folder() if folder_id == FOLDER_ID else None)),
        ("_list_portal_clients", _patch("_list_portal_clients", lambda: [_client()])),
        ("_get_portal_report_folder_assignment_data", _patch("_get_portal_report_folder_assignment_data", lambda folder_id: {"folder": _folder(), "assigned": [], "available": [_user()]})),
        ("_assign_portal_report_folder_user", _patch("_assign_portal_report_folder_user", fake_assign)),
        ("_remove_portal_report_folder_user", _patch("_remove_portal_report_folder_user", lambda **kwargs: calls.append(kwargs) or None)),
    ])
    try:
        ok = api_main.admin_portal_assign_report_folder_user(FOLDER_ID, _FakeRequest(), user_id=USER_ID)
        denied = api_main.admin_portal_assign_report_folder_user(FOLDER_ID, _FakeRequest(), user_id="no-report-access")
        removed = api_main.admin_portal_remove_report_folder_user(FOLDER_ID, USER_ID, _FakeRequest())
    finally:
        _restore(patches)
    assert ok.status_code == 303, ok.status_code
    assert denied.status_code == 400 and "must have report access" in _html(denied), _html(denied)
    assert removed.status_code == 303, removed.status_code
    assert any(call.get("user_id") == USER_ID for call in calls if isinstance(call, dict)), calls
    print("PASS: report folder assignment requires existing report access to the client")


def _test_user_reports_and_folder_detail() -> None:
    patches = [
        ("get_current_artifact_user", _patch("get_current_artifact_user", lambda request: _user(admin=False, user_id=USER_ID))),
        ("_list_accessible_portal_report_folders_for_user", _patch("_list_accessible_portal_report_folders_for_user", lambda user_id: [_folder()] if user_id == USER_ID else [])),
        ("_get_accessible_portal_report_folder_for_user", _patch("_get_accessible_portal_report_folder_for_user", lambda user_id, folder_id: _folder() if user_id == USER_ID and folder_id == FOLDER_ID else None)),
        ("_portal_report_folder_artifacts", _patch("_portal_report_folder_artifacts", lambda folder, limit=100, offset=0: {"data": [_artifact()], "meta": {"total": 1}})),
    ]
    try:
        # S15 moved the `Raporty` nav slot to the Report Explorer library; the
        # pre-redesign folder index this stage asserts is unchanged and is
        # still served, now at its compatibility route.
        landing = api_main.user_portal_report_folders_legacy(
            _FakeRequest(path="/user/reports/folders"))
        detail = api_main.user_portal_report_folder(FOLDER_ID, _FakeRequest(path=f"/user/reports/folders/{FOLDER_ID}"))
        guessed = api_main.user_portal_report_folder("unassigned", _FakeRequest(path="/user/reports/folders/unassigned"))
    finally:
        _restore(patches)
    assert "Monthly reports" in _html(landing) and "Acme Logistics" in _html(landing), _html(landing)
    assert "monthly-report.pdf" in _html(detail), _html(detail)
    assert "secret/internal/key.pdf" not in _html(detail), _html(detail)
    assert guessed.status_code == 403 and "not available" in _html(guessed), _html(guessed)
    print("PASS: user Reports Explorer shows assigned folders and blocks guessed folder URLs")


def _test_portal_preview_and_download_access() -> None:
    patches = [
        ("get_current_artifact_user", _patch("get_current_artifact_user", lambda request: _user(admin=False, user_id=USER_ID))),
        ("_portal_report_artifact_access", _patch("_portal_report_artifact_access", lambda user, folder_id, artifact_id, action: None)),
    ]
    try:
        denied_preview = api_main.user_portal_report_artifact_preview(ARTIFACT_ID, _FakeRequest(), folder_id=FOLDER_ID)
        try:
            api_main.user_portal_report_artifact_download(ARTIFACT_ID, _FakeRequest(), folder_id=FOLDER_ID)
        except _HTTPException as exc:
            denied_download = exc
        else:
            raise AssertionError("download unexpectedly allowed")
    finally:
        _restore(patches)
    assert denied_preview.status_code == 403, denied_preview.status_code
    assert denied_download.status_code == 403, denied_download.status_code

    stream = _StreamingResponse([b"ok"])
    patches = [
        ("get_current_artifact_user", _patch("get_current_artifact_user", lambda request: _user(admin=False, user_id=USER_ID))),
        ("_portal_report_artifact_access", _patch("_portal_report_artifact_access", lambda user, folder_id, artifact_id, action: (_folder(), {"storage_key": "object", "display_filename": "monthly-report.pdf"}, _artifact()))),
        ("_artifact_explorer_preview_for_row", _patch("_artifact_explorer_preview_for_row", lambda row, artifact: {"preview_type": "text", "text": "hello", "truncated": False})),
        ("_stream_artifact_download", _patch("_stream_artifact_download", lambda row, disposition="attachment": stream)),
    ]
    try:
        preview = api_main.user_portal_report_artifact_preview(ARTIFACT_ID, _FakeRequest(), folder_id=FOLDER_ID)
        download = api_main.user_portal_report_artifact_download(ARTIFACT_ID, _FakeRequest(), folder_id=FOLDER_ID)
    finally:
        _restore(patches)
    assert "hello" in _html(preview), _html(preview)
    assert download is stream, download
    print("PASS: portal preview/download wrappers enforce portal folder access and do not use Artifact Explorer RBAC")


def _test_existing_token_behavior_still_works() -> None:
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
    print("PASS: existing Artifact Browser token auth behavior remains intact")


def main() -> None:
    _test_admin_auth_boundaries()
    _test_admin_dashboard_and_create_form()
    _test_create_validation_and_redirect()
    _test_edit_and_admin_preview()
    _test_assignment_gate()
    _test_user_reports_and_folder_detail()
    _test_portal_preview_and_download_access()
    _test_existing_token_behavior_still_works()


if __name__ == "__main__":
    main()
