#!/usr/bin/env python3
"""Focused, service-free regressions for the artifact idempotency contract."""
from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.artifacts.layout import build_idempotent_artifact_object_key  # noqa: E402
from api.client import ArtifactUploadResult, LogPlatformClient  # noqa: E402


MIGRATION = ROOT / "db/migrations/047_artifact_upload_idempotency.sql"


def test_object_key() -> None:
    first = build_idempotent_artifact_object_key("stage2", "opaque-retry-token", ".CSV")
    second = build_idempotent_artifact_object_key("stage2", "opaque-retry-token", "csv")
    changed = build_idempotent_artifact_object_key("stage2", "another-token", "csv")
    assert first == second and first != changed
    assert first.startswith("idempotent/v1/") and first.endswith(".csv")
    assert "opaque" not in first and "stage2" not in first
    digest = hashlib.sha256(b"stage2\0opaque-retry-token").hexdigest()
    assert f"/{digest[:2]}/{digest[2:4]}/{digest}.csv" in first


def test_migration_contract() -> None:
    sql = MIGRATION.read_text()
    assert "ops_control.environment_identity" in sql
    assert "idempotency_scope VARCHAR(128)" in sql
    assert "idempotency_key VARCHAR(256)" in sql
    assert "btrim(idempotency_scope" in sql
    assert "btrim(idempotency_key" in sql
    assert "WHERE idempotency_key IS NOT NULL" in sql
    assert "UPDATE artifacts" not in sql and "DELETE FROM artifacts" not in sql


class Response:
    def __init__(self, payload: dict):
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


def test_client_retry_and_response() -> None:
    calls: list[dict] = []

    def post(*args, **kwargs):
        calls.append(dict(kwargs["data"]))
        if len(calls) == 1:
            from requests import Timeout
            raise Timeout("lost response")
        return Response({"artifact_id": "canonical-id", "idempotency_status": "reused"})

    with tempfile.NamedTemporaryFile() as tmp, patch("api.client.requests.post", side_effect=post):
        client = LogPlatformClient("http://api", write_token="token")
        result = client.upload_artifact(
            tmp.name,
            idempotency_scope="stage2",
            idempotency_key="opaque-retry-token",
            structured_response=True,
        )
        assert result == ArtifactUploadResult("canonical-id", "reused")
        assert calls[0] == calls[1]
        assert calls[0]["idempotency_scope"] == "stage2"
        assert calls[0]["idempotency_key"] == "opaque-retry-token"

    with tempfile.NamedTemporaryFile() as tmp, patch(
        "api.client.requests.post", return_value=Response({"artifact_id": "legacy-id"})
    ):
        assert LogPlatformClient("http://api", write_token="token").upload_artifact(tmp.name) == "legacy-id"


def test_client_validation() -> None:
    with tempfile.NamedTemporaryFile() as tmp:
        client = LogPlatformClient("http://api", write_token="token")
        for kwargs in (
            {"idempotency_scope": "scope"},
            {"idempotency_scope": " ", "idempotency_key": "key"},
            {"idempotency_scope": "scope", "idempotency_key": " "},
            {"idempotency_scope": "s" * 129, "idempotency_key": "key"},
        ):
            try:
                client.upload_artifact(tmp.name, **kwargs)
            except ValueError:
                pass
            else:
                raise AssertionError(f"expected ValueError for {kwargs!r}")


def test_fingerprint_and_authorization() -> None:
    import api.main as api_main
    from fastapi import HTTPException

    expected = {
        "sha256": "abc", "kind": "REPORT", "content_type": "text/csv",
        "workflow_name": "workflow_b", "stage_name": "stage_2_clean",
        "artifact_role": "cleaned", "report_type": "report_207",
        "client_code": None, "raw_file_id": None, "layout_version": 2,
    }
    existing = {**expected, "filename": "old.csv", "metadata_json": {"label": "old"}}
    assert api_main._artifact_idempotency_compatible(existing, expected)
    for field in api_main._IDEMPOTENCY_FINGERPRINT_FIELDS:
        incompatible = dict(existing)
        incompatible[field] = "different"
        assert not api_main._artifact_idempotency_compatible(incompatible, expected), field

    old_token = api_main.WRITE_TOKEN
    api_main.WRITE_TOKEN = "write-token"
    try:
        for auth in (None, "Bearer wrong"):
            try:
                api_main.require_token(auth, "write")
            except HTTPException as exc:
                assert exc.status_code in (401, 403)
            else:
                raise AssertionError("write authorization unexpectedly bypassed")
        api_main.require_token("Bearer write-token", "write")
    finally:
        api_main.WRITE_TOKEN = old_token


def main() -> None:
    test_object_key()
    test_migration_contract()
    test_client_retry_and_response()
    test_client_validation()
    test_fingerprint_and_authorization()
    print("OK - artifact upload idempotency focused regressions passed")


if __name__ == "__main__":
    main()
