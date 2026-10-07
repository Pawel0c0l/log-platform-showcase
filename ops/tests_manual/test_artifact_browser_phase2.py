#!/usr/bin/env python3
"""Manual regression tests for read-only Artifact Browser API helpers.

The host venv used for manual tests may not include API-container packages
such as FastAPI, so this file installs tiny import stubs before importing
``api.main``.

Run:

    cd /opt/log-platform
    python3 ops/tests_manual/test_artifact_browser_phase2.py
"""
from __future__ import annotations

import asyncio
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


RAW_FILE_ID = "11e44cf1-7421-47b6-8d5e-f75675b37d88"
RUN_ID = "666ff6cc-aa5b-4c07-8eaa-3a95d3a4bd2c"


def _test_query_builder_filters() -> None:
    filters = {
        "workflow_name": "workflow_b",
        "stage_name": "stage_2_clean",
        "artifact_role": "cleaned",
        "report_type": "report_207",
        "original_filename": "source.xls",
        "client_code": "CLIENT_A",
        "run_id": RUN_ID,
        "raw_file_id": RAW_FILE_ID,
        "layout_version": 2,
        "file_ext": "csv",
        "search": "speed",
    }
    sql, params, count_sql, count_params = api_main._build_artifact_browser_queries(
        filters, sort="created_at_desc", limit=20, offset=40
    )
    for clause in (
        "workflow_name = %s",
        "stage_name = %s",
        "artifact_role = %s",
        "report_type = %s",
        "original_filename = %s",
        "client_code = %s",
        "run_id = %s",
        "raw_file_id = %s",
        "layout_version = %s",
        "file_ext = %s",
        "filename ILIKE %s",
        "ORDER BY created_at DESC LIMIT %s OFFSET %s",
    ):
        assert clause in sql, (clause, sql)
    assert count_sql.startswith("SELECT COUNT(*) AS total FROM artifacts WHERE"), count_sql
    assert params[:10] == [
        "workflow_b",
        "stage_2_clean",
        "cleaned",
        "report_207",
        "source.xls",
        "CLIENT_A",
        RUN_ID,
        RAW_FILE_ID,
        2,
        "csv",
    ], params
    assert params[-2:] == [20, 40], params
    assert len(count_params) == len(params) - 2, (count_params, params)
    print("PASS: query builder supports exact filters, search, limit, offset")


def _test_query_builder_multi_value_filters() -> None:
    filters = {
        "workflow_name": ["workflow_b", "workflow_a"],
        "report_type": ["report_207,report_602"],
        "artifact_role": ["cleaned", "debug_sample"],
        "original_filename": ["source.xls,backup.xls"],
        "client_code": ["CLIENT_A", "CLIENT_B"],
        "layout_version": ["1", "2"],
    }
    sql, params, _count_sql, _count_params = api_main._build_artifact_browser_queries(
        filters, sort="created_at_desc", limit=50, offset=0
    )
    assert "workflow_name IN (%s, %s)" in sql, sql
    assert "report_type IN (%s, %s)" in sql, sql
    assert "artifact_role IN (%s, %s)" in sql, sql
    assert "original_filename IN (%s, %s)" in sql, sql
    assert "client_code IN (%s, %s)" in sql, sql
    assert "layout_version IN (%s, %s)" in sql, sql
    assert params[:12] == [
        "workflow_b",
        "workflow_a",
        "cleaned",
        "debug_sample",
        "report_207",
        "report_602",
        "source.xls",
        "backup.xls",
        "CLIENT_A",
        "CLIENT_B",
        1,
        2,
    ], params
    print("PASS: query builder supports repeated and comma-separated multi-value filters")


def _test_query_builder_contains_filters() -> None:
    filters = {
        "original_filename_search": "207",
        "report_type_search": "207",
        "display_filename_search": "207",
        "stage_name_search": "clean",
        "tag_search": "review",
    }
    sql, params, _count_sql, count_params = api_main._build_artifact_browser_queries(
        filters, sort="created_at_desc", limit=50, offset=0
    )
    assert "original_filename ILIKE %s" in sql, sql
    assert "report_type ILIKE %s" in sql, sql
    assert "COALESCE(display_filename, filename) ILIKE %s" in sql, sql
    assert "stage_name ILIKE %s" in sql, sql
    assert "search_tags.tag ILIKE %s" in sql, sql
    assert "207" not in sql, sql
    assert params[:5] == ["%clean%", "%207%", "%207%", "%207%", "%review%"], params
    assert count_params == params[:-2], (count_params, params)
    print("PASS: field-specific contains filters use parameterized ILIKE clauses")


def _test_query_builder_exact_filters_take_precedence_over_search_text() -> None:
    filters = {
        "report_type": ["report_207"],
        "report_type_search": "250",
        "original_filename": ["source.xls", "backup.xls"],
        "original_filename_search": "ignored",
    }
    sql, params, _count_sql, _count_params = api_main._build_artifact_browser_queries(
        filters, sort="created_at_desc", limit=50, offset=0
    )
    assert "report_type = %s" in sql, sql
    assert "report_type ILIKE %s" not in sql, sql
    assert "original_filename IN (%s, %s)" in sql, sql
    assert "original_filename ILIKE %s" not in sql, sql
    assert "%250%" not in params, params
    assert "%ignored%" not in params, params
    assert params[:3] == ["report_207", "source.xls", "backup.xls"], params
    print("PASS: exact selected filters take precedence over typed search text")


def _test_query_builder_empty_contains_filters_do_not_filter() -> None:
    sql, params, _count_sql, _count_params = api_main._build_artifact_browser_queries(
        {"original_filename": [], "original_filename_search": ""},
        sort="created_at_desc",
        limit=50,
        offset=0,
    )
    assert "original_filename = %s" not in sql, sql
    assert "original_filename ILIKE %s" not in sql, sql
    assert params == [50, 0], params
    print("PASS: clearing exact and typed filters removes field-specific filtering")


def _test_invalid_sort_rejected() -> None:
    try:
        api_main._build_artifact_browser_queries({}, sort="created_at;drop", limit=50, offset=0)
    except _HTTPException as exc:
        assert exc.status_code == 400, exc.status_code
    else:
        raise AssertionError("invalid sort should raise HTTP 400")
    print("PASS: invalid sort is rejected safely")


def _test_extended_safe_sort_values() -> None:
    expected_order = {
        "workflow_name_asc": "ORDER BY workflow_name ASC NULLS LAST, created_at DESC",
        "workflow_name_desc": "ORDER BY workflow_name DESC NULLS LAST, created_at DESC",
        "stage_name_asc": "ORDER BY stage_name ASC NULLS LAST, created_at DESC",
        "stage_name_desc": "ORDER BY stage_name DESC NULLS LAST, created_at DESC",
        "artifact_role_asc": "ORDER BY artifact_role ASC NULLS LAST, created_at DESC",
        "artifact_role_desc": "ORDER BY artifact_role DESC NULLS LAST, created_at DESC",
        "report_type_asc": "ORDER BY report_type ASC NULLS LAST, created_at DESC",
        "report_type_desc": "ORDER BY report_type DESC NULLS LAST, created_at DESC",
        "client_code_asc": "ORDER BY client_code ASC NULLS LAST, created_at DESC",
        "client_code_desc": "ORDER BY client_code DESC NULLS LAST, created_at DESC",
        "file_ext_asc": "ORDER BY file_ext ASC NULLS LAST, created_at DESC",
        "file_ext_desc": "ORDER BY file_ext DESC NULLS LAST, created_at DESC",
        "size_bytes_asc": "ORDER BY size_bytes ASC NULLS LAST, created_at DESC",
        "size_bytes_desc": "ORDER BY size_bytes DESC NULLS LAST, created_at DESC",
        "layout_version_asc": "ORDER BY layout_version ASC NULLS LAST, created_at DESC",
        "layout_version_desc": "ORDER BY layout_version DESC NULLS LAST, created_at DESC",
        "filename_asc": "ORDER BY COALESCE(display_filename, filename) ASC NULLS LAST, created_at DESC",
        "filename_desc": "ORDER BY COALESCE(display_filename, filename) DESC NULLS LAST, created_at DESC",
    }
    for sort, order_clause in expected_order.items():
        sql, _params, _count_sql, _count_params = api_main._build_artifact_browser_queries(
            {}, sort=sort, limit=50, offset=0
        )
        assert order_clause in sql, (sort, sql)
    print("PASS: extended safe sort values are accepted through whitelist only")


def _artifact_row(**overrides):
    row = {
        "artifact_id": "22222222-2222-2222-2222-222222222222",
        "run_id": RUN_ID,
        "created_at": datetime(2026, 5, 12, 22, 11, 44, tzinfo=timezone.utc),
        "kind": "REPORT",
        "filename": "report_207__20260512T221144Z__ac3866ec__cleaned.csv",
        "content_type": "text/csv",
        "size_bytes": 12345,
        "sha256": "abc",
        "storage_backend": "S3",
        "storage_key": "workflow_b/stage_2_clean/key.csv",
        "raw_file_id": RAW_FILE_ID,
        "workflow_name": "workflow_b",
        "stage_name": "stage_2_clean",
        "artifact_role": "cleaned",
        "report_type": "report_207",
        "client_code": "CLIENT_A",
        "display_filename": "report_207__20260512T221144Z__ac3866ec__cleaned.csv",
        "original_filename": "source.xls",
        "file_ext": "csv",
        "layout_version": 2,
        "metadata_json": {"source": "test"},
    }
    row.update(overrides)
    return row


def _test_artifact_row_backward_compatibility() -> None:
    old = api_main._artifact_browser_row(
        _artifact_row(
            workflow_name=None,
            stage_name=None,
            artifact_role=None,
            report_type=None,
            client_code=None,
            display_filename=None,
            original_filename=None,
            file_ext=None,
            layout_version=1,
            metadata_json=None,
            raw_file_id=None,
        )
    )
    assert old["workflow_name"] is None, old
    assert old["stage_name"] is None, old
    assert old["client_code"] is None, old
    assert old["layout_version"] == 1, old
    assert old["display_filename"] == old["filename"], old
    assert old["raw_file_id"] is None, old
    print("PASS: old-style artifacts with NULL layout metadata serialize")


class _Cursor:
    def __init__(self, row):
        self.row = row
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self.row


class _Conn:
    def __init__(self, row):
        self.row = row

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def cursor(self):
        return _Cursor(self.row)

    def commit(self):
        return None


def _with_fake_db(row, fn):
    old_db_conn = api_main.db_conn
    api_main.db_conn = lambda: _Conn(row)
    try:
        return fn()
    finally:
        api_main.db_conn = old_db_conn


def _test_detail_lineage_helpers() -> None:
    run_row = {
        "run_id": RUN_ID,
        "source": "jobs.reports.stage2.job_stage2",
        "status": "SUCCESS",
        "started_at": datetime(2026, 5, 12, 22, 0, tzinfo=timezone.utc),
        "ended_at": datetime(2026, 5, 12, 22, 12, tzinfo=timezone.utc),
        "params": {"input_dir": "/tmp", "api_key": "secret-value"},
    }
    run = _with_fake_db(run_row, lambda: api_main._get_run_summary(RUN_ID))
    assert run["run_id"] == RUN_ID, run
    assert run["params"]["api_key"] == "***REDACTED***", run

    raw_row = {
        "id": RAW_FILE_ID,
        "original_filename": "source.xls",
        "report_key": "207",
        "status": "NORMALIZED",
        "normalized_path": "/data/normalized/source.csv",
        "stage2_status": "OK",
        "stage2_report_type": "report_207",
        "stage2_scores": {"final_score": 1.0},
        "stage2_pending_reason": None,
    }
    raw = _with_fake_db(raw_row, lambda: api_main._get_raw_file_summary(RAW_FILE_ID))
    assert raw["id"] == RAW_FILE_ID, raw
    assert raw["stage2_report_type"] == "report_207", raw
    assert api_main._get_raw_file_summary(None) is None
    print("PASS: detail helpers include run and raw_file lineage")


def _test_detail_endpoint_without_raw_file() -> None:
    old_read_token = api_main.READ_TOKEN
    old_get_row = api_main._get_artifact_browser_row
    old_get_run = api_main._get_run_summary
    old_get_raw = api_main._get_raw_file_summary
    old_get_folders = api_main._get_artifact_virtual_folders
    api_main.READ_TOKEN = "read"
    api_main._get_artifact_browser_row = lambda artifact_id: _artifact_row(raw_file_id=None)
    api_main._get_run_summary = lambda run_id: {"run_id": run_id, "status": "SUCCESS"}
    api_main._get_raw_file_summary = lambda raw_file_id: None
    api_main._get_artifact_virtual_folders = lambda artifact_id: []
    try:
        payload = api_main.artifact_browser_get_artifact(
            "22222222-2222-2222-2222-222222222222",
            authorization="Bearer read",
        )
    finally:
        api_main.READ_TOKEN = old_read_token
        api_main._get_artifact_browser_row = old_get_row
        api_main._get_run_summary = old_get_run
        api_main._get_raw_file_summary = old_get_raw
        api_main._get_artifact_virtual_folders = old_get_folders
    assert payload["artifact"]["raw_file_id"] is None, payload
    assert payload["run"]["status"] == "SUCCESS", payload
    assert payload["raw_file"] is None, payload
    print("PASS: detail endpoint handles artifact without raw_file_id")


def _test_missing_artifact_row() -> None:
    old_read_token = api_main.READ_TOKEN
    old_get_row = api_main._get_artifact_browser_row
    api_main.READ_TOKEN = "read"
    api_main._get_artifact_browser_row = lambda artifact_id: None
    try:
        try:
            api_main.artifact_browser_download_artifact(
                "missing",
                authorization="Bearer read",
            )
        except _HTTPException as exc:
            assert exc.status_code == 404, exc.status_code
        else:
            raise AssertionError("missing artifact row should raise 404")
    finally:
        api_main.READ_TOKEN = old_read_token
        api_main._get_artifact_browser_row = old_get_row
    print("PASS: missing artifact row maps to 404")


class _Body:
    def __init__(self, chunks):
        self.chunks = list(chunks)

    def read(self, _size):
        if not self.chunks:
            return b""
        return self.chunks.pop(0)


class _S3:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.uploads = []

    def get_object(self, **kwargs):
        if self.error:
            raise self.error
        return self.response

    def upload_file(self, tmp_name, bucket, key):
        self.uploads.append((bucket, key))


class _S3Error(Exception):
    def __init__(self, status_code):
        self.response = {"ResponseMetadata": {"HTTPStatusCode": status_code}}


def _test_download_streaming() -> None:
    old_s3 = api_main.s3
    api_main.s3 = _S3({"Body": _Body([b"hello", b" world"])})
    try:
        response = api_main._stream_artifact_download(_artifact_row())
        data = b"".join(response.body)
    finally:
        api_main.s3 = old_s3
    assert data == b"hello world", data
    assert response.media_type == "text/csv", response.media_type
    assert "report_207__20260512T221144Z__ac3866ec__cleaned.csv" in response.headers["Content-Disposition"]
    print("PASS: download stream returns bytes and display filename")


def _test_download_missing_object() -> None:
    old_s3 = api_main.s3
    api_main.s3 = _S3(error=_S3Error(404))
    try:
        try:
            api_main._stream_artifact_download(_artifact_row())
        except _HTTPException as exc:
            assert exc.status_code == 404, exc.status_code
        else:
            raise AssertionError("missing S3 object should raise 404")
    finally:
        api_main.s3 = old_s3
    print("PASS: missing object key maps to clear 404")


class _UploadFile:
    def __init__(self, filename: str, data: bytes, content_type: str = "text/plain"):
        self.filename = filename
        self.content_type = content_type
        self._data = data
        self._sent = False

    async def read(self, _size: int):
        if self._sent:
            return b""
        self._sent = True
        return self._data


def _test_upload_endpoint_accepts_optional_client_code() -> None:
    queries = []
    old_write_token = api_main.WRITE_TOKEN
    old_db_conn = api_main.db_conn
    old_s3 = api_main.s3
    api_main.WRITE_TOKEN = "write"
    api_main.db_conn = lambda: _Conn({"exists": 1})
    api_main.s3 = _S3()
    try:
        payload = asyncio.run(
            api_main.upload_artifact(
                authorization="Bearer write",
                file=_UploadFile("sample.csv", b"a;b\n1;2\n", "text/csv"),
                kind="REPORT",
                run_id=RUN_ID,
                raw_file_id=RAW_FILE_ID,
                workflow_name="workflow_b",
                stage_name="stage_2_clean",
                artifact_role="cleaned",
                report_type="report_207",
                client_code="CLIENT_A",
                original_filename="source.xls",
                metadata_json="{}",
            )
        )
        assert payload["client_code"] == "CLIENT_A", payload
        assert api_main.s3.uploads[0][1] == payload["storage_key"], api_main.s3.uploads

        def record_conn():
            conn = _Conn({"exists": 1})
            original_cursor = conn.cursor

            def cursor():
                cur = original_cursor()
                original_execute = cur.execute

                def execute(sql, params=None):
                    queries.append((sql, params))
                    original_execute(sql, params)

                cur.execute = execute
                return cur

            conn.cursor = cursor
            return conn

        api_main.db_conn = record_conn
        payload_without_client = asyncio.run(
            api_main.upload_artifact(
                authorization="Bearer write",
                file=_UploadFile("sample.txt", b"probe"),
                kind="REPORT",
                run_id=RUN_ID,
                workflow_name="workflow_b",
                stage_name="stage_2_clean",
                artifact_role="debug_sample",
                report_type="unknown",
                metadata_json="{}",
            )
        )
    finally:
        api_main.WRITE_TOKEN = old_write_token
        api_main.db_conn = old_db_conn
        api_main.s3 = old_s3

    insert_params = queries[-1][1]
    assert payload_without_client["client_code"] is None, payload_without_client
    assert insert_params[15] is None, insert_params
    print("PASS: upload endpoint accepts client_code and old uploads keep it NULL")


def _test_facets_query_excludes_nulls_and_includes_client_code() -> None:
    migration = (REPO_ROOT / "db" / "migrations" / "023_artifacts_client_code.sql").read_text()
    assert "ADD COLUMN IF NOT EXISTS client_code TEXT" in migration, migration
    assert "idx_artifacts_client_code" in migration, migration

    class FacetCursor:
        def __init__(self):
            self.executed = []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def execute(self, sql, params=None):
            self.executed.append(sql)

        def fetchall(self):
            sql = self.executed[-1]
            if "client_code" in sql:
                return [{"value": "CLIENT_A"}, {"value": "CLIENT_B"}]
            if "layout_version" in sql:
                return [{"value": 1}, {"value": 2}]
            return [{"value": "x"}]

    class FacetConn:
        def __init__(self):
            self.cursor_obj = FacetCursor()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def cursor(self):
            return self.cursor_obj

    conn = FacetConn()
    old_db_conn = api_main.db_conn
    api_main.db_conn = lambda: conn
    try:
        facets = api_main._get_artifact_browser_facets()
    finally:
        api_main.db_conn = old_db_conn
    assert facets["client_code"] == ["CLIENT_A", "CLIENT_B"], facets
    assert facets["layout_version"] == [1, 2], facets
    assert any("WHERE client_code IS NOT NULL" in sql for sql in conn.cursor_obj.executed), conn.cursor_obj.executed
    assert any("BTRIM(client_code) <> ''" in sql for sql in conn.cursor_obj.executed), conn.cursor_obj.executed
    assert any("BTRIM(original_filename) <> ''" in sql for sql in conn.cursor_obj.executed), conn.cursor_obj.executed
    print("PASS: facets include client_code and exclude NULL values")


def main() -> None:
    _test_query_builder_filters()
    _test_query_builder_multi_value_filters()
    _test_query_builder_contains_filters()
    _test_query_builder_exact_filters_take_precedence_over_search_text()
    _test_query_builder_empty_contains_filters_do_not_filter()
    _test_invalid_sort_rejected()
    _test_extended_safe_sort_values()
    _test_artifact_row_backward_compatibility()
    _test_detail_lineage_helpers()
    _test_detail_endpoint_without_raw_file()
    _test_missing_artifact_row()
    _test_download_streaming()
    _test_download_missing_object()
    _test_upload_endpoint_accepts_optional_client_code()
    _test_facets_query_excludes_nulls_and_includes_client_code()


if __name__ == "__main__":
    main()
