#!/usr/bin/env python3
"""Manual regression tests for Workflow B Stage 1 artifact upload metadata."""
from __future__ import annotations

import sys
import tempfile
import hashlib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.mail.fetch_reports import _upload_stage1_artifacts  # noqa: E402
from api.client import ArtifactUploadResult  # noqa: E402
from jobs.mail.stage1_artifact_sync import Stage1ArtifactSyncError  # noqa: E402


RAW_FILE_ID = "11111111-1111-1111-1111-111111111111"
RUN_ID = "22222222-2222-2222-2222-222222222222"


class _Cursor:
    def __init__(self, existing_roles=None):
        self.existing_roles = dict(existing_roles or {})
        self.last_role = None

    def execute(self, _sql, params=None):
        self.last_role = params[1]

    def fetchone(self):
        sha = self.existing_roles.get(self.last_role)
        return (f"artifact-{self.last_role}", sha) if sha else None


class _Client:
    def __init__(self, *, fail_roles=None):
        self.fail_roles = set(fail_roles or [])
        self.uploads = []
        self.logs = []

    def upload_artifact(self, path, **kwargs):
        if kwargs["artifact_role"] in self.fail_roles:
            raise RuntimeError("upload failed")
        self.uploads.append({"path": path, **kwargs})
        return ArtifactUploadResult(f"artifact-{kwargs['artifact_role']}", "created")

    def log(self, level, type_, source, message, *, run_id=None, context=None, error=None):
        self.logs.append(
            {
                "level": level,
                "type": type_,
                "source": source,
                "message": message,
                "run_id": run_id,
                "context": context or {},
                "error": error,
            }
        )


def _counters():
    return {
        "artifacts_raw_uploaded": 0,
        "artifacts_normalized_uploaded": 0,
        "artifact_upload_failed": 0,
        "artifact_upload_skipped_existing": 0,
    }


def _uploads(raw_path: Path, normalized_path: Path):
    return [
        {
            "path": raw_path,
            "raw_file_id": RAW_FILE_ID,
            "artifact_role": "raw",
            "original_filename": "source.xls",
            "uid": 100,
            "source_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
        },
        {
            "path": normalized_path,
            "raw_file_id": RAW_FILE_ID,
            "artifact_role": "normalized",
            "original_filename": "source.xls",
            "uid": 100,
            "source_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
        },
    ]


def _test_stage1_artifact_metadata_and_local_files_unchanged() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        raw_path = Path(tmpdir) / "source.xls"
        normalized_path = Path(tmpdir) / "source.csv"
        raw_path.write_bytes(b"raw-bytes")
        normalized_path.write_bytes(b"a;b\n1;2\n")
        counters = _counters()
        client = _Client()

        _upload_stage1_artifacts(
            client,
            _Cursor(),
            run_id=RUN_ID,
            uploads=_uploads(raw_path, normalized_path),
            counters=counters,
        )

        assert raw_path.read_bytes() == b"raw-bytes"
        assert normalized_path.read_bytes() == b"a;b\n1;2\n"
        assert counters["artifacts_raw_uploaded"] == 1, counters
        assert counters["artifacts_normalized_uploaded"] == 1, counters
        assert len(client.uploads) == 2, client.uploads
        raw_call, normalized_call = client.uploads
        assert raw_call["workflow_name"] == "workflow_b", raw_call
        assert raw_call["stage_name"] == "stage_1_fetch", raw_call
        assert raw_call["artifact_role"] == "raw", raw_call
        assert raw_call["report_type"] == "unknown", raw_call
        assert raw_call["raw_file_id"] == RAW_FILE_ID, raw_call
        assert raw_call["original_filename"] == "source.xls", raw_call
        assert raw_call["structured_response"] is True, raw_call
        assert raw_call["idempotency_scope"] == "workflow_b.stage1.raw.v1", raw_call
        assert normalized_call["artifact_role"] == "normalized", normalized_call
    print("PASS: Stage 1 uploads raw and normalized artifact metadata without changing local files")


def _test_stage1_artifact_upload_skips_existing_role() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        raw_path = Path(tmpdir) / "source.xls"
        normalized_path = Path(tmpdir) / "source.csv"
        raw_path.write_bytes(b"raw")
        normalized_path.write_bytes(b"csv")
        counters = _counters()
        client = _Client()

        _upload_stage1_artifacts(
            client,
            _Cursor(existing_roles={"raw": hashlib.sha256(b"raw").hexdigest()}),
            run_id=RUN_ID,
            uploads=_uploads(raw_path, normalized_path),
            counters=counters,
        )

        assert counters["artifact_upload_skipped_existing"] == 1, counters
        assert counters["artifacts_raw_uploaded"] == 0, counters
        assert counters["artifacts_normalized_uploaded"] == 1, counters
        assert [call["artifact_role"] for call in client.uploads] == ["normalized"], client.uploads
    print("PASS: Stage 1 skips existing raw_file/workflow/stage/role artifact")


def _test_stage1_artifact_upload_failure_is_typed() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        raw_path = Path(tmpdir) / "source.xls"
        normalized_path = Path(tmpdir) / "source.csv"
        raw_path.write_bytes(b"raw")
        normalized_path.write_bytes(b"csv")
        counters = _counters()
        client = _Client(fail_roles={"normalized"})

        try:
            _upload_stage1_artifacts(
                client,
                _Cursor(),
                run_id=RUN_ID,
                uploads=_uploads(raw_path, normalized_path),
                counters=counters,
            )
        except Stage1ArtifactSyncError as exc:
            assert exc.partial_result.retryable_work_remains is True
        else:
            raise AssertionError("artifact upload failure did not raise typed error")

        assert counters["artifacts_raw_uploaded"] == 1, counters
        assert counters["artifact_upload_failed"] == 1, counters
        assert any(log["level"] == "WARNING" and "upload failed" in log["context"]["error"] for log in client.logs), client.logs
    print("PASS: Stage 1 artifact upload failure preserves ingest work and raises typed error")


def main() -> None:
    _test_stage1_artifact_metadata_and_local_files_unchanged()
    _test_stage1_artifact_upload_skips_existing_role()
    _test_stage1_artifact_upload_failure_is_typed()


if __name__ == "__main__":
    main()
