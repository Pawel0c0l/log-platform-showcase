#!/usr/bin/env python3
"""Manual regression tests for artifact layout Phase 1.

Run:

    cd /opt/log-platform
    python3 ops/tests_manual/test_artifact_layout_phase1.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from artifacts.layout import build_artifact_display_filename, build_artifact_object_key, sanitize_artifact_component  # noqa: E402
from jobs.reports.stage2.job_stage2 import _upload_stage2_artifact  # noqa: E402


RAW_FILE_ID = "11e44cf1-7421-47b6-8d5e-f75675b37d88"


def _test_layout_helper() -> None:
    created_at = datetime(2026, 5, 12, 22, 11, 44, tzinfo=timezone.utc)
    display = build_artifact_display_filename(
        report_type="report_207",
        created_at=created_at,
        raw_file_id=RAW_FILE_ID,
        artifact_role="cleaned",
        ext="csv",
    )
    assert display == "report_207__20260512T221144Z__ac3866ec__cleaned.csv", display

    key = build_artifact_object_key(
        workflow_name="workflow_b",
        stage_name="stage_2_clean",
        run_id="666ff6cc-aa5b-4c07-8eaa-3a95d3a4bd2c",
        artifact_role="cleaned",
        ext="csv",
        created_at=created_at,
        report_type="report_207",
        raw_file_id=RAW_FILE_ID,
    )
    assert key == (
        "workflow_b/stage_2_clean/yyyy=2026/mm=05/dd=12/"
        "run_id=666ff6cc-aa5b-4c07-8eaa-3a95d3a4bd2c/"
        "report_type=report_207/cleaned/"
        "report_207__20260512T221144Z__ac3866ec__cleaned.csv"
    ), key

    unknown_key = build_artifact_object_key(
        workflow_name="workflow_b",
        stage_name="stage_1_fetch",
        run_id="run-1",
        artifact_role="raw",
        ext=".xls",
        created_at=created_at,
    )
    assert "/report_type=" not in unknown_key, unknown_key
    assert unknown_key.endswith("/raw/unknown__20260512T221144Z__na__raw.xls"), unknown_key

    assert sanitize_artifact_component(" Raport 207 / Łódź.xlsx ") == "raport_207_odz.xlsx"
    print("PASS: artifact layout helper is deterministic")


class _FakeS3:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, str]] = []

    def upload_file(self, tmp_name: str, bucket: str, key: str) -> None:
        self.uploads.append((bucket, key))


class _FakeCursor:
    def __init__(self, queries: list[tuple[str, object]]) -> None:
        self.queries = queries

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def execute(self, sql: str, params=None) -> None:
        self.queries.append((sql, params))

    def fetchone(self):
        return {"exists": 1}


class _FakeConn:
    def __init__(self, queries: list[tuple[str, object]]) -> None:
        self.queries = queries

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def cursor(self):
        return _FakeCursor(self.queries)

    def commit(self) -> None:
        return None


def _test_api_upload_metadata() -> None:
    try:
        from fastapi.testclient import TestClient
    except ModuleNotFoundError:
        print("SKIP: API upload test requires FastAPI test dependencies")
        return

    import api.main as api_main

    queries: list[tuple[str, object]] = []
    fake_s3 = _FakeS3()
    old_write_token = api_main.WRITE_TOKEN
    old_s3 = api_main.s3
    old_db_conn = api_main.db_conn
    api_main.WRITE_TOKEN = "test-write"
    api_main.s3 = fake_s3
    api_main.db_conn = lambda: _FakeConn(queries)
    try:
        client = TestClient(api_main.app)
        response = client.post(
            "/artifacts/upload",
            headers={"Authorization": "Bearer test-write"},
            data={
                "kind": "REPORT",
                "run_id": "666ff6cc-aa5b-4c07-8eaa-3a95d3a4bd2c",
                "raw_file_id": RAW_FILE_ID,
                "workflow_name": "workflow_b",
                "stage_name": "stage_2_clean",
                "artifact_role": "cleaned",
                "report_type": "report_207",
                "client_code": "CLIENT_A",
                "original_filename": "original report 207.xls",
                "metadata_json": json.dumps({"source": "manual-test"}),
            },
            files={"file": ("cleaned.csv", b"a;b\n1;2\n", "text/csv")},
        )
    finally:
        api_main.WRITE_TOKEN = old_write_token
        api_main.s3 = old_s3
        api_main.db_conn = old_db_conn

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["layout_version"] == 2, payload
    assert payload["workflow_name"] == "workflow_b", payload
    assert payload["stage_name"] == "stage_2_clean", payload
    assert payload["artifact_role"] == "cleaned", payload
    assert payload["report_type"] == "report_207", payload
    assert payload["client_code"] == "CLIENT_A", payload
    assert payload["display_filename"].endswith("__ac3866ec__cleaned.csv"), payload
    assert payload["original_filename"] == "original report 207.xls", payload
    assert "/report_type=report_207/cleaned/" in payload["storage_key"], payload
    assert fake_s3.uploads[0][1] == payload["storage_key"], fake_s3.uploads

    insert_params = queries[-1][1]
    assert insert_params[11:19] == (
        "workflow_b",
        "stage_2_clean",
        "cleaned",
        "report_207",
        "CLIENT_A",
        payload["display_filename"],
        "original report 207.xls",
        "csv",
    ), insert_params
    assert insert_params[19] == 2, insert_params
    assert json.loads(insert_params[20]) == {"source": "manual-test"}, insert_params
    print("PASS: API upload accepts semantic metadata and stores layout fields")


def _test_api_upload_client_backward_compatibility() -> None:
    import tempfile
    from api.client import LogPlatformClient

    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"artifact_id": "artifact-id"}

    def fake_post(url, headers, data, files, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["data"] = dict(data)
        captured["files"] = files
        captured["timeout"] = timeout
        return Response()

    import api.client as client_mod

    old_post = client_mod.requests.post
    client_mod.requests.post = fake_post
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".txt") as tmp:
            tmp.write("probe")
            tmp.flush()
            client = LogPlatformClient("http://api", write_token="write")
            artifact_id = client.upload_artifact(tmp.name, client_code="CLIENT_A")
            assert artifact_id == "artifact-id"
            assert captured["data"]["client_code"] == "CLIENT_A", captured

            captured.clear()
            client.upload_artifact(tmp.name)
            assert "client_code" not in captured["data"], captured
    finally:
        client_mod.requests.post = old_post

    print("PASS: LogPlatformClient upload_artifact accepts optional client_code and remains backward-compatible")


class _FakeStage2Client:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def upload_artifact(self, path: str, **kwargs) -> str:
        self.calls.append({"path": path, **kwargs})
        return "artifact-id"


def _test_stage2_upload_metadata() -> None:
    client = _FakeStage2Client()
    path = Path("/tmp/report_207_cleaned.csv")
    artifact_id = _upload_stage2_artifact(
        client,
        path=path,
        run_id="666ff6cc-aa5b-4c07-8eaa-3a95d3a4bd2c",
        raw_file_id=RAW_FILE_ID,
        report_type="report_207",
        artifact_role="cleaned",
        original_filename="road speed source.xls",
    )
    assert artifact_id == "artifact-id"
    call = client.calls[0]
    assert call["workflow_name"] == "workflow_b", call
    assert call["stage_name"] == "stage_2_clean", call
    assert call["artifact_role"] == "cleaned", call
    assert call["report_type"] == "report_207", call
    assert call["raw_file_id"] == RAW_FILE_ID, call
    assert call["original_filename"] == "road speed source.xls", call
    print("PASS: Stage 2 cleaned upload sends Workflow B layout metadata")


def main() -> None:
    _test_layout_helper()
    _test_api_upload_metadata()
    _test_api_upload_client_backward_compatibility()
    _test_stage2_upload_metadata()


if __name__ == "__main__":
    main()
