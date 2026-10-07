#!/usr/bin/env python3
"""Service-free Stage 1 keyed artifact reconciliation regressions."""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

import requests

from api.client import ArtifactUploadResult
from jobs.mail import reconcile_report_artifacts
from jobs.mail import fetch_reports
from jobs.mail.stage1_artifact_sync import (
    NORMALIZED_ROLE,
    RAW_ROLE,
    STAGE1_ARTIFACT_LOCK_NAMESPACE,
    Stage1ArtifactReconciliationResult,
    Stage1ArtifactSyncError,
    Stage1ArtifactSyncOutcome,
    reconcile_stage1_artifacts_batch,
    stage1_artifact_idempotency_key,
    stage1_artifact_lock_key,
)


RAW_ID = "666ff6cc-aa5b-4c07-8eaa-3a95d3a4bd2c"
SOURCE_SHA = "a" * 64
MIGRATION = Path(__file__).resolve().parents[2] / "db/migrations/049_workflow_b_stage1_artifact_reconciliation.sql"


class Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.rows = []
        self.one = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql, params=()):
        self.conn.sql.append(sql)
        if "pg_try_advisory_lock" in sql:
            self.one = {"pg_try_advisory_lock": self.conn.locked}
        elif "pg_advisory_unlock" in sql:
            self.one = {"pg_advisory_unlock": True}
        elif "FROM ingest.raw_file WHERE id=" in sql:
            self.one = dict(self.conn.row) if self.conn.row else None
        elif "FROM ingest.raw_file WHERE" in sql:
            self.rows = [dict(self.conn.row)] if self.conn.row else []
        elif "FROM artifacts" in sql:
            role = params[-1]
            self.rows = [dict(row) for row in self.conn.artifacts.get(role, [])]
        else:
            self.rows = []
            self.one = None

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.rows


class Connection:
    def __init__(self, row, *, artifacts=None, locked=True):
        self.row = row
        self.artifacts = artifacts or {}
        self.locked = locked
        self.sql = []
        self.closed = False

    def cursor(self):
        return Cursor(self)

    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        self.closed = True


class Client:
    def __init__(self, status="created", *, conflict=False, fail=False):
        self.status = status
        self.conflict = conflict
        self.fail = fail
        self.uploads = []
        self.logs = []

    def upload_artifact(self, path, **kwargs):
        self.uploads.append((path, kwargs))
        if self.conflict:
            response = requests.Response()
            response.status_code = 409
            raise requests.HTTPError("conflict", response=response)
        if self.fail:
            raise requests.Timeout("timeout")
        return ArtifactUploadResult("canonical-artifact", self.status)

    def log(self, *args, **kwargs):
        self.logs.append((args, kwargs))


def row(raw_path: Path, normalized_path: Path | None = None):
    return {
        "raw_file_id": RAW_ID,
        "sha256": SOURCE_SHA,
        "original_filename": "synthetic.csv",
        "report_key": "synthetic",
        "status": "NORMALIZED" if normalized_path else "NEW",
        "persisted": True,
        "raw_path": str(raw_path),
        "normalized_csv_path": str(normalized_path) if normalized_path else None,
        "stage1_normalized_artifact_metadata": {
            "report_key": "synthetic", "sha256": SOURCE_SHA, "date_normalization": {}
        },
    }


def test_key_contract() -> None:
    raw = stage1_artifact_idempotency_key(RAW_ID, SOURCE_SHA, RAW_ROLE)
    normalized = stage1_artifact_idempotency_key(RAW_ID, SOURCE_SHA, NORMALIZED_ROLE)
    assert raw == stage1_artifact_idempotency_key(RAW_ID, SOURCE_SHA, RAW_ROLE)
    assert raw != normalized
    assert raw != stage1_artifact_idempotency_key("different", SOURCE_SHA, RAW_ROLE)
    assert raw != stage1_artifact_idempotency_key(RAW_ID, "b" * 64, RAW_ROLE)
    assert raw != stage1_artifact_idempotency_key(RAW_ID, SOURCE_SHA, RAW_ROLE, contract_version="v2")
    assert len(raw) == 64 and RAW_ID not in raw and SOURCE_SHA not in raw
    canonical = json.dumps(
        ["workflow_b.stage1.raw", "v1", RAW_ID, SOURCE_SHA],
        ensure_ascii=True, separators=(",", ":"),
    )
    assert raw == hashlib.sha256(canonical.encode()).hexdigest()
    assert STAGE1_ARTIFACT_LOCK_NAMESPACE.startswith("workflow_b.stage1.")
    assert stage1_artifact_lock_key(RAW_ID, RAW_ROLE) != stage1_artifact_lock_key(RAW_ID, NORMALIZED_ROLE)

    sql = MIGRATION.read_text()
    assert "ops_control.environment_identity" in sql
    assert "ADD COLUMN IF NOT EXISTS stage1_normalized_artifact_metadata JSONB" in sql
    assert "UPDATE ingest.raw_file" not in sql and "DELETE FROM" not in sql


def test_created_reused_and_no_unkeyed_fallback() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        raw_path = Path(tmp) / "raw.bin"
        raw_path.write_bytes(b"raw")
        for status, outcome in (
            ("created", Stage1ArtifactSyncOutcome.CREATED),
            ("reused", Stage1ArtifactSyncOutcome.REUSED),
        ):
            conn = Connection(row(raw_path))
            client = Client(status)
            result = reconcile_stage1_artifacts_batch(
                client, "run-changes", connection_factory=lambda: conn,
                raw_file_ids=[RAW_ID], roles=[RAW_ROLE], dry_run=False,
            )
            assert result.items[0].outcome == outcome
            kwargs = client.uploads[0][1]
            assert kwargs["idempotency_scope"] == "workflow_b.stage1.raw.v1"
            assert kwargs["structured_response"] is True
            assert kwargs["idempotency_key"] == stage1_artifact_idempotency_key(RAW_ID, SOURCE_SHA, RAW_ROLE)
            assert len(client.uploads) == 1


def test_existing_ambiguous_missing_and_lock() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        raw_path = Path(tmp) / "raw.bin"
        raw_path.write_bytes(b"raw")
        digest = hashlib.sha256(b"raw").hexdigest()
        base = row(raw_path)
        cases = [
            ([{"artifact_id": "a", "sha256": digest}], True, Stage1ArtifactSyncOutcome.LINKED_EXISTING),
            ([{"artifact_id": "a", "sha256": digest}, {"artifact_id": "b", "sha256": digest}], True, Stage1ArtifactSyncOutcome.BLOCKED_AMBIGUOUS_EXISTING_ARTIFACT),
            ([], False, Stage1ArtifactSyncOutcome.SKIPPED_LOCKED),
        ]
        for artifacts, locked, expected in cases:
            conn = Connection(base, artifacts={RAW_ROLE: artifacts}, locked=locked)
            client = Client()
            try:
                result = reconcile_stage1_artifacts_batch(
                    client, "run", connection_factory=lambda: conn,
                    raw_file_ids=[RAW_ID], roles=[RAW_ROLE], dry_run=False,
                )
            except Stage1ArtifactSyncError as exc:
                result = exc.partial_result
            assert result.items[0].outcome == expected
            assert not client.uploads

        missing = dict(base, raw_path=str(Path(tmp) / "missing.bin"))
        conn = Connection(missing)
        try:
            reconcile_stage1_artifacts_batch(
                Client(), "run", connection_factory=lambda: conn,
                raw_file_ids=[RAW_ID], roles=[RAW_ROLE], dry_run=False,
            )
        except Stage1ArtifactSyncError as exc:
            assert exc.partial_result.items[0].outcome == Stage1ArtifactSyncOutcome.BLOCKED_SOURCE_MISSING
        else:
            raise AssertionError("missing durable source was accepted")


def test_timeout_conflict_dry_run_and_crash_retry() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        raw_path = Path(tmp) / "raw.bin"
        raw_path.write_bytes(b"raw")
        for client, expected in (
            (Client(fail=True), Stage1ArtifactSyncOutcome.FAILED_RETRYABLE_UPLOAD),
            (Client(conflict=True), Stage1ArtifactSyncOutcome.FAILED_NON_RETRYABLE_CONFLICT),
        ):
            conn = Connection(row(raw_path))
            try:
                reconcile_stage1_artifacts_batch(
                    client, "run", connection_factory=lambda: conn,
                    raw_file_ids=[RAW_ID], roles=[RAW_ROLE], dry_run=False,
                )
            except Stage1ArtifactSyncError as exc:
                assert exc.partial_result.items[0].outcome == expected
            else:
                raise AssertionError("failure did not propagate")
            assert len(client.uploads) == 1

        dry_client = Client()
        dry = reconcile_stage1_artifacts_batch(
            dry_client, "run", connection_factory=lambda: Connection(row(raw_path)),
            raw_file_ids=[RAW_ID], roles=[RAW_ROLE], dry_run=True,
        )
        assert dry.items[0].outcome == Stage1ArtifactSyncOutcome.SKIPPED_NOT_REQUIRED
        assert not dry_client.uploads

        retry_client = Client("reused")
        retried = reconcile_stage1_artifacts_batch(
            retry_client, "different-run", connection_factory=lambda: Connection(row(raw_path)),
            raw_file_ids=[RAW_ID], roles=[RAW_ROLE], dry_run=False,
        )
        assert retried.items[0].outcome == Stage1ArtifactSyncOutcome.REUSED


def test_manual_entrypoint_defaults_dry_and_never_uses_imap() -> None:
    original = reconcile_report_artifacts.reconcile_stage1_artifacts_batch
    calls = []
    try:
        def fake(*args, **kwargs):
            calls.append(kwargs)
            return Stage1ArtifactReconciliationResult()

        reconcile_report_artifacts.reconcile_stage1_artifacts_batch = fake
        assert isinstance(reconcile_report_artifacts.run(Client(), "run", {}), Stage1ArtifactReconciliationResult)
        assert calls[-1]["dry_run"] is True
        reconcile_report_artifacts.run(Client(), "run", {"execute": True, "limit": 2, "roles": [RAW_ROLE]})
        assert calls[-1]["dry_run"] is False and calls[-1]["limit"] == 2
    finally:
        reconcile_report_artifacts.reconcile_stage1_artifacts_batch = original


def test_normal_stage1_reconciles_before_mailbox_validation() -> None:
    original_reconcile = fetch_reports.reconcile_stage1_artifacts_batch
    original_getenv = fetch_reports.os.getenv
    calls = []
    try:
        def fake_reconcile(*args, **kwargs):
            calls.append(kwargs)
            return Stage1ArtifactReconciliationResult()

        def fake_getenv(name, default=None):
            if name in {"IMAP_HOST", "IMAP_PASSWORD"}:
                return None if name == "IMAP_HOST" else ""
            return default

        fetch_reports.reconcile_stage1_artifacts_batch = fake_reconcile
        fetch_reports.os.getenv = fake_getenv
        try:
            fetch_reports.run(Client(), "run", {"since_days": 1, "mailbox": "synthetic"})
        except RuntimeError as exc:
            assert "IMAP_HOST" in str(exc)
        else:
            raise AssertionError("missing mailbox infrastructure did not fail")
        assert len(calls) == 1 and calls[0]["dry_run"] is False
    finally:
        fetch_reports.reconcile_stage1_artifacts_batch = original_reconcile
        fetch_reports.os.getenv = original_getenv


def test_duplicate_only_mailbox_still_runs_reconciliation() -> None:
    original_reconcile = fetch_reports.reconcile_stage1_artifacts_batch
    original_getenv = fetch_reports.os.getenv
    original_pg = fetch_reports._pg_conn
    original_imap = fetch_reports.imaplib.IMAP4_SSL
    original_search = fetch_reports._search_imap_uids
    original_content = fetch_reports._content_dedup_settings
    original_links = fetch_reports._report_link_settings
    calls = []

    class Stage1Cursor:
        def execute(self, sql, _params=()):
            self.sql = sql

        def fetchone(self):
            return (1,) if "FROM ingest.imap_message" in self.sql else None

    class Stage1Conn:
        def __init__(self):
            self.cur = Stage1Cursor()

        def cursor(self):
            return self.cur

        def commit(self):
            return None

        def close(self):
            return None

    class Imap:
        def login(self, *_args): return None
        def select(self, *_args): return ("OK", [])
        def response(self, *_args): return ("UIDVALIDITY", [b"1"])
        def close(self): return None
        def logout(self): return None

    try:
        fetch_reports.reconcile_stage1_artifacts_batch = lambda *args, **kwargs: (
            calls.append(kwargs) or Stage1ArtifactReconciliationResult()
        )
        fetch_reports.os.getenv = lambda name, default=None: {
            "IMAP_HOST": "synthetic.invalid", "IMAP_PASSWORD": "synthetic",
            "IMAP_USER": "opaque-test-account", "REPORTS_DATA_DIR": "/tmp/synthetic-stage1",
        }.get(name, default)
        fetch_reports._pg_conn = Stage1Conn
        fetch_reports.imaplib.IMAP4_SSL = lambda *_args: Imap()
        fetch_reports._search_imap_uids = lambda *_args, **_kwargs: ([b"1"], ["SINCE synthetic"])
        fetch_reports._content_dedup_settings = lambda: ("off", 0.0, 1.0)
        fetch_reports._report_link_settings = lambda: (set(), 1, 1, True, False, 1)
        fetch_reports.run(Client(), "run", {"since_days": 1, "mailbox": "synthetic"})
        assert len(calls) == 1 and calls[0]["dry_run"] is False
    finally:
        fetch_reports.reconcile_stage1_artifacts_batch = original_reconcile
        fetch_reports.os.getenv = original_getenv
        fetch_reports._pg_conn = original_pg
        fetch_reports.imaplib.IMAP4_SSL = original_imap
        fetch_reports._search_imap_uids = original_search
        fetch_reports._content_dedup_settings = original_content
        fetch_reports._report_link_settings = original_links


def main() -> None:
    test_key_contract()
    test_created_reused_and_no_unkeyed_fallback()
    test_existing_ambiguous_missing_and_lock()
    test_timeout_conflict_dry_run_and_crash_retry()
    test_manual_entrypoint_defaults_dry_and_never_uses_imap()
    test_normal_stage1_reconciles_before_mailbox_validation()
    test_duplicate_only_mailbox_still_runs_reconciliation()
    print("OK - Workflow B Stage 1 artifact synchronization regressions passed")


if __name__ == "__main__":
    main()
