#!/usr/bin/env python3
"""Manual regression tests for Artifact Explorer annotations Phase 5.

Run:

    cd /opt/log-platform
    python3 ops/tests_manual/test_artifact_annotations_phase5.py
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


def _row(*, description=None, manual_metadata_json=None, tags=None):
    return {
        "artifact_id": ARTIFACT_ID,
        "run_id": None,
        "created_at": datetime(2026, 5, 12, 22, 11, 44, tzinfo=timezone.utc),
        "kind": "REPORT",
        "filename": "sample.txt",
        "content_type": "text/plain",
        "size_bytes": 123,
        "sha256": "abc",
        "storage_backend": "S3",
        "storage_key": "workflow_b/stage_2_clean/sample.txt",
        "raw_file_id": None,
        "workflow_name": "workflow_b",
        "stage_name": "stage_2_clean",
        "artifact_role": "debug_sample",
        "report_type": "report_207",
        "client_code": "CLIENT_A",
        "display_filename": "sample.txt",
        "original_filename": "sample.txt",
        "file_ext": "txt",
        "layout_version": 2,
        "metadata_json": {"system": "value"},
        "description": description,
        "manual_metadata_json": manual_metadata_json or {},
        "tags": tags or [],
    }


class _AnnotationState:
    def __init__(self):
        self.exists = True
        self.description = None
        self.manual_metadata_json = {}
        self.tags = set()
        self.executed = []

    def artifact_row(self):
        return _row(
            description=self.description,
            manual_metadata_json=self.manual_metadata_json,
            tags=sorted(self.tags),
        )


class _Cursor:
    def __init__(self, state: _AnnotationState):
        self.state = state
        self.last_sql = ""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def execute(self, sql, params=None):
        self.last_sql = sql
        self.state.executed.append((sql, params))
        if "INSERT INTO artifact_metadata_overrides" in sql:
            self.state.description = params[1]
            import json

            self.state.manual_metadata_json = json.loads(params[2])
        elif "INSERT INTO artifact_tags" in sql:
            self.state.tags.add(params[1])
        elif "DELETE FROM artifact_tags" in sql:
            self.state.tags.discard(params[1])

    def fetchone(self):
        if "SELECT 1 FROM artifacts" in self.last_sql:
            return {"exists": 1} if self.state.exists else None
        if "FROM artifacts" in self.last_sql:
            return self.state.artifact_row() if self.state.exists else None
        return None

    def fetchall(self):
        if "artifact_virtual_folder_items" in self.last_sql:
            return []
        if "FROM artifact_tags" in self.last_sql:
            return [{"value": tag} for tag in sorted(self.state.tags)]
        return [{"value": "x"}]


class _Conn:
    def __init__(self, state: _AnnotationState):
        self.state = state

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def cursor(self):
        return _Cursor(self.state)

    def commit(self):
        return None


def _with_state(state: _AnnotationState, fn):
    old_db_conn = api_main.db_conn
    api_main.db_conn = lambda: _Conn(state)
    try:
        return fn()
    finally:
        api_main.db_conn = old_db_conn


def _html(response) -> str:
    return response.body.decode("utf-8")


def _test_migration_contains_annotation_tables_and_constraints() -> None:
    migration = (REPO_ROOT / "db" / "migrations" / "024_artifact_annotations.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS artifact_metadata_overrides" in migration, migration
    assert "CREATE TABLE IF NOT EXISTS artifact_tags" in migration, migration
    assert "PRIMARY KEY (artifact_id, tag)" in migration, migration
    assert "CHECK (tag = lower(tag))" in migration, migration
    print("PASS: migration defines annotation tables and tag constraints")


def _test_tag_normalization() -> None:
    assert api_main._normalize_artifact_tag(" Reviewed ") == "reviewed"
    for invalid in ("", "bad tag", "x" * 65):
        try:
            api_main._normalize_artifact_tag(invalid)
        except _HTTPException as exc:
            assert exc.status_code == 400, exc.status_code
        else:
            raise AssertionError(f"invalid tag should fail: {invalid!r}")
    print("PASS: tag normalization lowercases and rejects invalid labels")


def _test_metadata_api_auth_and_upsert() -> None:
    state = _AnnotationState()
    old_read_token = api_main.READ_TOKEN
    old_write_token = api_main.WRITE_TOKEN
    api_main.READ_TOKEN = "read"
    api_main.WRITE_TOKEN = "write"
    try:
        try:
            api_main.artifact_browser_update_artifact_metadata(
                ARTIFACT_ID,
                {"description": "Denied", "metadata_json": {}},
                authorization="Bearer read",
            )
        except _HTTPException as exc:
            assert exc.status_code == 403, exc.status_code
        else:
            raise AssertionError("read token should not update metadata")

        payload = _with_state(
            state,
            lambda: api_main.artifact_browser_update_artifact_metadata(
                ARTIFACT_ID,
                {"description": "Reviewed", "metadata_json": {"review_status": "ok"}},
                authorization="Bearer write",
            ),
        )
        assert payload["artifact"]["description"] == "Reviewed", payload
        assert payload["artifact"]["manual_metadata_json"] == {"review_status": "ok"}, payload

        try:
            api_main.artifact_browser_update_artifact_metadata(
                ARTIFACT_ID,
                {"description": "Bad", "metadata_json": []},
                authorization="Bearer write",
            )
        except _HTTPException as exc:
            assert exc.status_code == 400, exc.status_code
        else:
            raise AssertionError("non-object manual metadata should fail")
    finally:
        api_main.READ_TOKEN = old_read_token
        api_main.WRITE_TOKEN = old_write_token
    print("PASS: metadata API requires write token, upserts, and rejects invalid metadata_json")


def _test_missing_artifact_returns_404() -> None:
    state = _AnnotationState()
    state.exists = False
    try:
        _with_state(
            state,
            lambda: api_main._upsert_artifact_metadata_override(
                ARTIFACT_ID,
                description="missing",
                metadata_json={},
                actor="test",
            ),
        )
    except _HTTPException as exc:
        assert exc.status_code == 404, exc.status_code
    else:
        raise AssertionError("missing artifact should raise 404")
    print("PASS: missing artifact returns 404 for metadata writes")


def _test_tags_api_idempotent_add_delete_and_filter() -> None:
    state = _AnnotationState()
    old_write_token = api_main.WRITE_TOKEN
    api_main.WRITE_TOKEN = "write"
    try:
        payload = _with_state(
            state,
            lambda: api_main.artifact_browser_add_artifact_tag(
                ARTIFACT_ID,
                {"tag": "Reviewed"},
                authorization="Bearer write",
            ),
        )
        assert payload["artifact"]["tags"] == ["reviewed"], payload
        _with_state(
            state,
            lambda: api_main.artifact_browser_add_artifact_tag(
                ARTIFACT_ID,
                {"tag": "reviewed"},
                authorization="Bearer write",
            ),
        )
        assert sorted(state.tags) == ["reviewed"], state.tags
        payload = _with_state(
            state,
            lambda: api_main.artifact_browser_delete_artifact_tag(
                ARTIFACT_ID,
                "reviewed",
                authorization="Bearer write",
            ),
        )
        assert payload["artifact"]["tags"] == [], payload
    finally:
        api_main.WRITE_TOKEN = old_write_token

    sql, params, _count_sql, _count_params = api_main._build_artifact_browser_queries(
        {"tag": ["Reviewed", "speeding"]},
        sort="created_at_desc",
        limit=20,
        offset=0,
    )
    assert "EXISTS (SELECT 1 FROM artifact_tags" in sql, sql
    assert params[:2] == ["reviewed", "speeding"], params
    print("PASS: tags API is idempotent and tag filter uses OR semantics")


def _test_facets_include_tags() -> None:
    state = _AnnotationState()
    state.tags.update({"reviewed", "speeding"})
    facets = _with_state(state, api_main._get_artifact_browser_facets)
    assert facets["tags"] == ["reviewed", "speeding"], facets
    print("PASS: facets include tags")


def _test_ui_forms_and_invalid_json_error() -> None:
    old_get_row = api_main._get_artifact_browser_row
    old_get_run = api_main._get_run_summary
    old_get_raw = api_main._get_raw_file_summary
    old_get_folders = api_main._get_artifact_virtual_folders
    old_list_folders = api_main._list_all_virtual_folders_flat
    old_preview = api_main._artifact_explorer_preview_for_row
    api_main._get_artifact_browser_row = lambda artifact_id: _row(
        description="Reviewed <ok>",
        manual_metadata_json={"review_status": "ok"},
        tags=["reviewed"],
    )
    api_main._get_run_summary = lambda run_id: None
    api_main._get_raw_file_summary = lambda raw_file_id: None
    api_main._get_artifact_virtual_folders = lambda artifact_id: []
    api_main._list_all_virtual_folders_flat = lambda: []
    api_main._artifact_explorer_preview_for_row = lambda row, artifact: {
        "artifact_id": ARTIFACT_ID,
        "preview_type": "text",
        "text": "preview",
        "truncated": False,
        "encoding": "utf-8",
    }
    try:
        html = _html(api_main.artifact_explorer_detail(ARTIFACT_ID))
        assert 'action="/artifact-explorer/artifacts/22222222-2222-2222-2222-222222222222/metadata"' in html, html
        assert 'action="/artifact-explorer/artifacts/22222222-2222-2222-2222-222222222222/tags"' in html, html
        assert "Reviewed &lt;ok&gt;" in html, html
        assert "reviewed" in html, html

        html = _html(api_main.artifact_explorer_update_metadata(ARTIFACT_ID, "desc", "{bad"))
        assert "Manual metadata JSON is invalid" in html, html
    finally:
        api_main._get_artifact_browser_row = old_get_row
        api_main._get_run_summary = old_get_run
        api_main._get_raw_file_summary = old_get_raw
        api_main._get_artifact_virtual_folders = old_get_folders
        api_main._list_all_virtual_folders_flat = old_list_folders
        api_main._artifact_explorer_preview_for_row = old_preview
    print("PASS: UI renders annotation forms and invalid JSON error")


def main() -> None:
    _test_migration_contains_annotation_tables_and_constraints()
    _test_tag_normalization()
    _test_metadata_api_auth_and_upsert()
    _test_missing_artifact_returns_404()
    _test_tags_api_idempotent_add_delete_and_filter()
    _test_facets_include_tags()
    _test_ui_forms_and_invalid_json_error()


if __name__ == "__main__":
    main()
