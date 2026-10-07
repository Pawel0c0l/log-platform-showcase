#!/usr/bin/env python3
"""Manual regression tests for Artifact Preview API Phase 3.

Run:

    cd /opt/log-platform
    .venv/bin/python ops/tests_manual/test_artifact_preview_phase3.py
"""
from __future__ import annotations

import io
import json
import sys
import types
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from api.artifacts.preview import (  # noqa: E402
    ArtifactPreviewError,
    build_preview,
)


ARTIFACT_ID = "22222222-2222-2222-2222-222222222222"


def _artifact(file_ext: str, *, content_type: str = "application/octet-stream", filename: str | None = None) -> dict:
    filename = filename or f"artifact.{file_ext}"
    return {
        "artifact_id": ARTIFACT_ID,
        "file_ext": file_ext,
        "filename": filename,
        "display_filename": filename,
        "original_filename": filename,
        "content_type": content_type,
        "size_bytes": 123,
    }


def _test_csv_preview() -> None:
    data = "\ufeffName;Count\nA;1\nB;2\nC;3\n".encode("utf-8")
    preview = build_preview(_artifact("csv", content_type="text/csv"), data, rows_limit=2)
    assert preview["preview_type"] == "table", preview
    assert preview["columns"] == ["Name", "Count"], preview
    assert preview["rows"] == [{"Name": "A", "Count": "1"}, {"Name": "B", "Count": "2"}], preview
    assert preview["row_count_previewed"] == 2, preview
    assert preview["truncated"] is True, preview
    assert preview["delimiter"] == ";", preview
    assert preview["encoding"] == "utf-8-sig", preview
    print("PASS: CSV preview handles semicolon, BOM, strings, and truncation")


def _test_xlsx_preview() -> None:
    try:
        import openpyxl
    except ModuleNotFoundError:
        print("SKIP: XLSX preview test requires openpyxl")
        return

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "First"
    sheet.append(["Name", "Count"])
    sheet.append(["A", 1])
    second = workbook.create_sheet("Second")
    second.append(["Other"])
    second.append(["B"])
    buf = io.BytesIO()
    workbook.save(buf)

    preview = build_preview(_artifact("xlsx"), buf.getvalue(), rows_limit=5)
    assert preview["selected_sheet"] == "First", preview
    assert preview["sheet_names"] == ["First", "Second"], preview
    assert preview["rows"] == [{"Name": "A", "Count": "1"}], preview

    preview_by_name = build_preview(_artifact("xlsx"), buf.getvalue(), rows_limit=5, sheet_name="Second")
    assert preview_by_name["selected_sheet"] == "Second", preview_by_name
    assert preview_by_name["columns"] == ["Other"], preview_by_name

    preview_by_index = build_preview(_artifact("xlsx"), buf.getvalue(), rows_limit=5, sheet_index=1)
    assert preview_by_index["selected_sheet"] == "Second", preview_by_index
    print("PASS: XLSX preview returns first sheet, sheet names, sheet_name, and sheet_index")


def _test_text_preview() -> None:
    preview = build_preview(_artifact("txt", content_type="text/plain"), b"abcdef", text_chars_limit=3)
    assert preview["preview_type"] == "text", preview
    assert preview["text"] == "abc", preview
    assert preview["truncated"] is True, preview
    print("PASS: text preview respects char limit")


def _test_json_preview() -> None:
    valid = build_preview(_artifact("json", content_type="application/json"), b'{"a": 1}', text_chars_limit=20)
    assert valid["preview_type"] == "json", valid
    assert valid["json"] == {"a": 1}, valid
    assert valid["truncated"] is False, valid

    invalid = build_preview(_artifact("json", content_type="application/json"), b'{"a": ', text_chars_limit=4)
    assert invalid["preview_type"] == "json", invalid
    assert invalid["text"] == '{"a"', invalid
    assert "parse_error" in invalid, invalid
    assert invalid["truncated"] is True, invalid
    print("PASS: JSON preview parses valid JSON and reports invalid JSON")


def _test_pdf_preview() -> None:
    preview = build_preview(_artifact("pdf", content_type="application/pdf"), None)
    assert preview["preview_type"] == "pdf_inline", preview
    assert preview["download_url"].endswith(f"/{ARTIFACT_ID}/download"), preview
    assert preview["inline_url"].endswith(f"/{ARTIFACT_ID}/download?disposition=inline"), preview
    print("PASS: PDF preview returns inline metadata only")


def _test_unsupported_extension() -> None:
    try:
        build_preview(_artifact("bin"), b"binary")
    except ArtifactPreviewError as exc:
        assert exc.status_code == 415, exc.status_code
    else:
        raise AssertionError("unsupported extension should raise 415")
    print("PASS: unsupported preview type raises 415")


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


class _S3Error(Exception):
    def __init__(self, status_code):
        self.response = {"ResponseMetadata": {"HTTPStatusCode": status_code}}


class _S3:
    def __init__(self, *, data: bytes | None = None, error: Exception | None = None):
        self.data = data
        self.error = error

    def get_object(self, **kwargs):
        if self.error:
            raise self.error
        return {"Body": io.BytesIO(self.data or b"")}

    def head_object(self, **kwargs):
        if self.error:
            raise self.error
        return {}


def _test_preview_endpoint_auth_and_missing_object() -> None:
    _install_import_stubs()
    import api.main as api_main

    row = {
        "artifact_id": ARTIFACT_ID,
        "run_id": None,
        "created_at": None,
        "kind": "REPORT",
        "filename": "sample.csv",
        "content_type": "text/csv",
        "size_bytes": 10,
        "sha256": "abc",
        "storage_backend": "S3",
        "storage_key": "sample.csv",
        "raw_file_id": None,
        "workflow_name": None,
        "stage_name": None,
        "artifact_role": None,
        "report_type": None,
        "display_filename": "sample.csv",
        "original_filename": "sample.csv",
        "file_ext": "csv",
        "layout_version": 1,
        "metadata_json": {},
    }

    old_read_token = api_main.READ_TOKEN
    old_get_row = api_main._get_artifact_browser_row
    old_s3 = api_main.s3
    api_main.READ_TOKEN = "read"
    api_main._get_artifact_browser_row = lambda artifact_id: row
    try:
        try:
            api_main.artifact_browser_preview_artifact(ARTIFACT_ID, authorization=None)
        except _HTTPException as exc:
            assert exc.status_code == 401, exc.status_code
        else:
            raise AssertionError("preview endpoint should require read auth")

        api_main.s3 = _S3(error=_S3Error(404))
        try:
            api_main.artifact_browser_preview_artifact(ARTIFACT_ID, authorization="Bearer read")
        except _HTTPException as exc:
            assert exc.status_code == 404, exc.status_code
        else:
            raise AssertionError("missing object should raise 404")
    finally:
        api_main.READ_TOKEN = old_read_token
        api_main._get_artifact_browser_row = old_get_row
        api_main.s3 = old_s3
    print("PASS: preview endpoint requires read auth and maps missing object to 404")


def main() -> None:
    _test_csv_preview()
    _test_xlsx_preview()
    _test_text_preview()
    _test_json_preview()
    _test_pdf_preview()
    _test_unsupported_extension()
    _test_preview_endpoint_auth_and_missing_object()


if __name__ == "__main__":
    main()
