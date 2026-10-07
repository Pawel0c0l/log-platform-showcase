#!/usr/bin/env python3
"""Service-free focused regressions for the Workflow B Stage 2 batch contract."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from api.client import ArtifactUploadResult
from jobs.reports.stage2 import job_stage2
from jobs.reports.stage2.batch_contract import (
    STAGE2_CLEANED_ARTIFACT_CONTRACT_VERSION,
    STAGE2_CLEANED_ARTIFACT_IDEMPOTENCY_SCOPE,
    STAGE2_LOCK_NAMESPACE,
    Stage2BatchError,
    Stage2BatchResult,
    Stage2ItemResult,
    Stage2Outcome,
    cleaned_artifact_idempotency_key,
    has_batch_failures,
    is_retryable_historical_state,
    stage2_advisory_lock_key,
)


RAW_ID = "666ff6cc-aa5b-4c07-8eaa-3a95d3a4bd2c"
SOURCE_SHA = "a" * 64
MIGRATION = Path(__file__).resolve().parents[2] / "db/migrations/048_workflow_b_stage2_batch_contract.sql"


def test_key_contract() -> None:
    key = cleaned_artifact_idempotency_key(RAW_ID, SOURCE_SHA)
    assert key == cleaned_artifact_idempotency_key(RAW_ID, SOURCE_SHA)
    assert len(key) == 64 and key.isalnum()
    assert RAW_ID not in key and SOURCE_SHA not in key
    assert "customer" not in STAGE2_CLEANED_ARTIFACT_IDEMPOTENCY_SCOPE
    assert cleaned_artifact_idempotency_key("different", SOURCE_SHA) != key
    assert cleaned_artifact_idempotency_key(RAW_ID, "b" * 64) != key
    assert cleaned_artifact_idempotency_key(RAW_ID, SOURCE_SHA, contract_version="v2") != key
    canonical = json.dumps(
        ["workflow_b.stage2.cleaned", STAGE2_CLEANED_ARTIFACT_CONTRACT_VERSION, RAW_ID, SOURCE_SHA],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    assert key == __import__("hashlib").sha256(canonical.encode()).hexdigest()


def test_migration_is_additive_and_guarded() -> None:
    sql = MIGRATION.read_text()
    assert "ops_control.environment_identity" in sql
    assert "ADD COLUMN IF NOT EXISTS stage2_outcome_category" in sql
    assert "ADD COLUMN IF NOT EXISTS stage2_retryable" in sql
    assert "ADD COLUMN IF NOT EXISTS stage2_cleaned_artifact_id" in sql
    assert "REFERENCES artifacts(artifact_id) ON DELETE SET NULL" in sql
    assert "UPDATE ingest.raw_file" not in sql


class UploadClient:
    def __init__(self, status: str = "created"):
        self.status = status
        self.calls = []

    def upload_artifact(self, path, **kwargs):
        self.calls.append((path, kwargs))
        return ArtifactUploadResult("canonical-artifact", self.status)


def test_keyed_upload_created_and_reused() -> None:
    with tempfile.NamedTemporaryFile(suffix=".csv") as output:
        for status in ("created", "reused"):
            client = UploadClient(status)
            result = job_stage2._upload_stage2_artifact(
                client,
                path=Path(output.name),
                run_id="run-changes-but-key-does-not",
                raw_file_id=RAW_ID,
                report_type="report_112",
                artifact_role="cleaned",
                original_filename="ignored-by-key.csv",
                metadata={"source_identity": SOURCE_SHA},
            )
            assert result == ArtifactUploadResult("canonical-artifact", status)
            kwargs = client.calls[0][1]
            assert kwargs["idempotency_scope"] == STAGE2_CLEANED_ARTIFACT_IDEMPOTENCY_SCOPE
            assert kwargs["idempotency_key"] == cleaned_artifact_idempotency_key(RAW_ID, SOURCE_SHA)
            assert kwargs["structured_response"] is True
            assert kwargs["raw_file_id"] == RAW_ID
            assert len(client.calls) == 1


def test_missing_identity_never_falls_back() -> None:
    with tempfile.NamedTemporaryFile(suffix=".csv") as output:
        client = UploadClient()
        try:
            job_stage2._upload_stage2_artifact(
                client, path=Path(output.name), run_id="run", raw_file_id=RAW_ID,
                report_type="report_112", artifact_role="cleaned",
                original_filename="x.csv", metadata={},
            )
        except RuntimeError as exc:
            assert "immutable source identity" in str(exc)
        else:
            raise AssertionError("missing source identity was accepted")
        assert client.calls == []


def test_results_and_failure_policy() -> None:
    result = Stage2BatchResult(discovered_candidate_count=3, eligible_count=3, items=[
        Stage2ItemResult(RAW_ID, Stage2Outcome.SUCCEEDED_CREATED, artifact_id="a"),
        Stage2ItemResult("raw-2", Stage2Outcome.PENDING_HUMAN_REVIEW, review_required=True),
        Stage2ItemResult("raw-3", Stage2Outcome.FAILED_RETRYABLE, retryable=True),
    ])
    payload = result.to_dict()
    assert payload["attempted_count"] == 3
    assert payload["succeeded_created_count"] == 1
    assert payload["pending_review_count"] == 1
    assert payload["retryable_failure_count"] == 1
    assert payload["artifact_created_count"] == 1
    assert payload["retryable_work_remains"] is True
    assert payload["operator_action_required"] is True
    assert has_batch_failures(result)
    error = Stage2BatchError(result)
    assert error.result is result and error.partial_result is result
    assert Stage2BatchResult().to_dict()["attempted_count"] == 0


def test_lock_namespace_and_historical_policy() -> None:
    assert STAGE2_LOCK_NAMESPACE.startswith("workflow_b.stage2.")
    assert stage2_advisory_lock_key(RAW_ID) == stage2_advisory_lock_key(RAW_ID)
    assert stage2_advisory_lock_key(RAW_ID) != stage2_advisory_lock_key("different")
    assert is_retryable_historical_state("PENDING_REVIEW", "stage2_exception")
    assert not is_retryable_historical_state("PENDING_REVIEW", "low_detection_confidence")
    assert not is_retryable_historical_state("PENDING_REVIEW", "unknown_old_reason")


class LockCursor:
    def __init__(self, acquired: bool):
        self.acquired = acquired
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql, params):
        self.executed.append((sql, params))

    def fetchone(self):
        return {"pg_try_advisory_lock": self.acquired}


class LockConnection:
    def __init__(self, acquired: bool):
        self.cursor_obj = LockCursor(acquired)

    def cursor(self):
        return self.cursor_obj


def test_lock_loser_and_release_contract() -> None:
    loser = LockConnection(False)
    assert job_stage2._acquire_item_lock(loser, RAW_ID) is False
    assert "pg_try_advisory_lock" in loser.cursor_obj.executed[0][0]
    assert loser.cursor_obj.executed[0][1] == (stage2_advisory_lock_key(RAW_ID),)
    job_stage2._release_item_lock(loser, RAW_ID)
    assert "pg_advisory_unlock" in loser.cursor_obj.executed[-1][0]


def test_public_run_returns_result_and_propagates_partial_failure() -> None:
    original = job_stage2.process_stage2_batch
    logged = []

    class Client:
        def log(self, *args, **kwargs):
            logged.append((args, kwargs))

    zero = Stage2BatchResult()
    try:
        job_stage2.process_stage2_batch = lambda *_args, **_kwargs: zero
        assert job_stage2.run(Client(), "run", {}) is zero
        assert logged[-1][1]["context"]["attempted_count"] == 0

        partial = Stage2BatchResult(items=[
            Stage2ItemResult(RAW_ID, Stage2Outcome.FAILED_NON_RETRYABLE)
        ])

        def fail(*_args, **_kwargs):
            raise Stage2BatchError(partial)

        job_stage2.process_stage2_batch = fail
        try:
            job_stage2.run(Client(), "run", {"input_files": []})
        except Stage2BatchError as exc:
            assert exc.partial_result is partial
        else:
            raise AssertionError("typed batch failure was swallowed")
    finally:
        job_stage2.process_stage2_batch = original


def main() -> None:
    test_key_contract()
    test_migration_is_additive_and_guarded()
    test_keyed_upload_created_and_reused()
    test_missing_identity_never_falls_back()
    test_results_and_failure_policy()
    test_lock_namespace_and_historical_policy()
    test_lock_loser_and_release_contract()
    test_public_run_returns_result_and_propagates_partial_failure()
    print("OK - Workflow B Stage 2 batch contract focused regressions passed")


if __name__ == "__main__":
    main()
