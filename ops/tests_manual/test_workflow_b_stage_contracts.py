#!/usr/bin/env python3
"""Service-free Stage 1/2/3 typed contract and orchestration-readiness checks."""
from __future__ import annotations

import json
from email.message import EmailMessage

from jobs.mail import fetch_reports
from jobs.mail.stage1_artifact_sync import (
    RAW_ROLE,
    Stage1ArtifactReconciliationResult,
    Stage1ArtifactSyncItemResult,
    Stage1ArtifactSyncOutcome,
)
from jobs.mail.stage1_batch_contract import (
    Stage1BatchError,
    Stage1BatchResult,
    Stage1ItemResult,
    Stage1Outcome,
)
from jobs.reports.stage2.batch_contract import (
    Stage2BatchError,
    Stage2BatchResult,
    Stage2ItemResult,
    Stage2Outcome,
)
from jobs.reports.stage3 import job_stage3
from jobs.reports.stage3.batch_contract import (
    Stage3BatchError,
    Stage3BatchResult,
    Stage3ItemResult,
    Stage3Outcome,
)


RAW_ID = "666ff6cc-aa5b-4c07-8eaa-3a95d3a4bd2c"


class Client:
    def __init__(self):
        self.logs = []

    def log(self, *args, **kwargs):
        self.logs.append((args, kwargs))


class PlatformConnection:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def rollback(self):
        return None


class PatchStage3:
    def __init__(self, **values):
        self.values = values
        self.originals = {}

    def __enter__(self):
        for name, value in self.values.items():
            self.originals[name] = getattr(job_stage3, name)
            setattr(job_stage3, name, value)
        return self

    def __exit__(self, *_args):
        for name, value in self.originals.items():
            setattr(job_stage3, name, value)


def test_stage1_results_no_work_dedup_and_artifacts() -> None:
    no_mail = Stage1BatchResult(mailbox_check_completed=True)
    assert no_mail.successful_no_work
    assert not no_mail.downstream_stage2_work_may_exist

    dedup = Stage1BatchResult(
        mailbox_check_completed=True,
        messages_inspected=2,
        messages_matched=2,
        messages_deduplicated=2,
        messages_skipped=2,
        items=[
            Stage1ItemResult(Stage1Outcome.REUSED_MESSAGE, message_identity="opaque-a"),
            Stage1ItemResult(Stage1Outcome.REUSED_MESSAGE, message_identity="opaque-b"),
        ],
    )
    assert dedup.successful_no_work and dedup.count(Stage1Outcome.REUSED_MESSAGE) == 2

    reconciliation = Stage1ArtifactReconciliationResult(
        records_discovered=1,
        records_inspected=1,
        items=[
            Stage1ArtifactSyncItemResult(
                RAW_ID,
                RAW_ROLE,
                Stage1ArtifactSyncOutcome.REUSED,
                artifact_id="artifact-raw",
                idempotency_status="reused",
            )
        ],
    )
    created = Stage1BatchResult(
        mailbox_check_completed=True,
        messages_inspected=1,
        messages_matched=1,
        attachments_discovered=1,
        attachments_attempted=1,
        attachments_created=1,
        raw_files_created=1,
        reconciliation=reconciliation,
        items=[Stage1ItemResult(Stage1Outcome.CREATED, raw_file_id=RAW_ID)],
    )
    assert created.downstream_stage2_work_may_exist
    assert created.raw_artifacts_reused == 1
    json.dumps(created.to_dict())


def test_stage1_failure_is_not_no_work_and_exposes_partial_result() -> None:
    partial = Stage1BatchResult(
        mailbox_check_completed=False,
        items=[
            Stage1ItemResult(
                Stage1Outcome.FAILED_RETRYABLE_MAIL_FETCH,
                retryable=True,
                error_category="mailbox_connection_or_search",
            )
        ],
    )
    error = Stage1BatchError(partial)
    assert error.result is partial and error.partial_result is partial
    assert partial.retryable_work_remains and not partial.successful_no_work


def test_stage1_public_wrapper_returns_typed_result() -> None:
    original = fetch_reports.fetch_reports_batch
    expected = Stage1BatchResult(mailbox_check_completed=True)
    try:
        fetch_reports.fetch_reports_batch = lambda *_args, **_kwargs: expected
        assert fetch_reports.run(Client(), "run", {"since_days": 1, "mailbox": "synthetic"}) is expected
    finally:
        fetch_reports.fetch_reports_batch = original


def test_stage1_mailbox_zero_no_attachment_and_hard_failures() -> None:
    originals = {
        name: getattr(fetch_reports, name)
        for name in (
            "reconcile_stage1_artifacts_batch", "_pg_conn", "_search_imap_uids",
            "_content_dedup_settings", "_report_link_settings",
        )
    }
    original_imap = fetch_reports.imaplib.IMAP4_SSL
    original_getenv = fetch_reports.os.getenv

    class Cursor:
        def __init__(self):
            self.sql = ""

        def execute(self, sql, _params=()):
            self.sql = sql

        def fetchone(self):
            if "FROM ingest.imap_message" in self.sql:
                return None
            if "INSERT INTO ingest.imap_message" in self.sql:
                return (1,)
            return None

    class Connection:
        def __init__(self):
            self.cur = Cursor()

        def cursor(self):
            return self.cur

        def commit(self):
            return None

        def close(self):
            return None

    class Imap:
        def __init__(self, message_bytes=None):
            self.message_bytes = message_bytes

        def login(self, *_args): return None
        def select(self, *_args): return ("OK", [])
        def response(self, *_args): return ("UIDVALIDITY", [b"1"])
        def uid(self, *_args): return ("OK", [(b"meta", self.message_bytes)])
        def close(self): return None
        def logout(self): return None

    try:
        fetch_reports.reconcile_stage1_artifacts_batch = lambda *_args, **_kwargs: Stage1ArtifactReconciliationResult()
        fetch_reports._pg_conn = Connection
        fetch_reports._content_dedup_settings = lambda: ("off", 0.0, 1.0)
        fetch_reports._report_link_settings = lambda: (set(), 1, 1, True, False, 1)
        fetch_reports.os.getenv = lambda name, default=None: {
            "IMAP_HOST": "synthetic.invalid", "IMAP_PASSWORD": "synthetic",
            "IMAP_USER": "opaque-account", "REPORTS_DATA_DIR": "/tmp/synthetic-stage1-contract",
        }.get(name, default)

        fetch_reports.imaplib.IMAP4_SSL = lambda *_args: Imap()
        fetch_reports._search_imap_uids = lambda *_args, **_kwargs: ([], ["SINCE synthetic"])
        zero = fetch_reports.fetch_reports_batch(Client(), "run", {})
        assert zero.mailbox_check_completed and zero.messages_inspected == 0 and zero.successful_no_work

        message = EmailMessage()
        message["Subject"] = "synthetic"
        message.set_content("no attachment")
        fetch_reports.imaplib.IMAP4_SSL = lambda *_args: Imap(message.as_bytes())
        fetch_reports._search_imap_uids = lambda *_args, **_kwargs: ([b"1"], ["SINCE synthetic"])
        unsupported = fetch_reports.fetch_reports_batch(Client(), "run", {})
        assert unsupported.mailbox_check_completed
        assert unsupported.messages_skipped == 1
        assert not unsupported.downstream_stage2_work_may_exist

        fetch_reports.imaplib.IMAP4_SSL = lambda *_args: (_ for _ in ()).throw(ConnectionError("synthetic"))
        try:
            fetch_reports.fetch_reports_batch(Client(), "run", {})
        except Stage1BatchError as exc:
            assert not exc.partial_result.mailbox_check_completed
            assert exc.partial_result.retryable_work_remains
        else:
            raise AssertionError("mailbox connection failure was accepted")

        fetch_reports.imaplib.IMAP4_SSL = lambda *_args: Imap()
        fetch_reports._search_imap_uids = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("search"))
        try:
            fetch_reports.fetch_reports_batch(Client(), "run", {})
        except Stage1BatchError as exc:
            assert not exc.partial_result.mailbox_check_completed
        else:
            raise AssertionError("mailbox search failure was accepted")
    finally:
        for name, value in originals.items():
            setattr(fetch_reports, name, value)
        fetch_reports.imaplib.IMAP4_SSL = original_imap
        fetch_reports.os.getenv = original_getenv


def _candidate(raw_id: str) -> job_stage3.Candidate:
    return job_stage3.Candidate(raw_id, "CLIENT", "report_207", None, "a" * 64)


def _load(raw_id: str) -> job_stage3.LoadResult:
    return job_stage3.LoadResult(
        raw_file_id=raw_id,
        client_code="CLIENT",
        report_type="report_207",
        destination_schema="telematics_reports",
        destination_table="report_207",
        data_overwrite=False,
        input_rows=2,
        inserted_rows=1,
        updated_rows=0,
        skipped_rows=1,
        status="OK",
        source_artifact_id="cleaned-artifact",
        cleaned_artifact_id="cleaned-artifact",
    )


def test_stage3_zero_success_partial_and_dry_run() -> None:
    with PatchStage3(
        _platform_pg_conn=lambda: PlatformConnection(),
        _select_stage3_candidates=lambda *_args, **_kwargs: [],
        _stage3_no_pending_diagnostics=lambda *_args, **_kwargs: {},
    ):
        zero = job_stage3.process_stage3_batch(Client(), "run", {})
    assert zero.successful_zero_work and zero.discovered_candidate_count == 0

    candidates = [_candidate("raw-1"), _candidate("raw-2")]
    with PatchStage3(
        _platform_pg_conn=lambda: PlatformConnection(),
        _select_stage3_candidates=lambda *_args, **_kwargs: candidates,
        _process_candidate=lambda *_args, **kwargs: _load(kwargs.get("candidate", _args[3]).raw_file_id),
    ):
        success = job_stage3.process_stage3_batch(Client(), "run", {"limit": 2})
    assert success.to_dict()["loaded_count"] == 2
    assert len(success.successful_load_identities) == 2
    assert success.to_dict()["total_inserted_rows"] == 2

    def one_failure(*args, **_kwargs):
        candidate = args[3]
        if candidate.raw_file_id == "raw-1":
            raise ConnectionError("synthetic client database failure")
        return _load(candidate.raw_file_id)

    with PatchStage3(
        _platform_pg_conn=lambda: PlatformConnection(),
        _select_stage3_candidates=lambda *_args, **_kwargs: candidates,
        _process_candidate=one_failure,
    ):
        try:
            job_stage3.process_stage3_batch(Client(), "run", {})
        except Stage3BatchError as exc:
            partial = exc.partial_result
        else:
            raise AssertionError("partial Stage 3 failure was accepted")
    assert partial.to_dict()["loaded_count"] == 1
    assert partial.retryable_work_remains
    assert len(partial.successful_load_identities) == 1

    dry_summary = {
        "dry_run_status": "OK",
        "destination_schema": "telematics_reports",
        "destination_table": "report_207",
        "source_artifact_id": "cleaned-artifact",
        "would_insert_rows": 1,
        "would_update_rows": 0,
        "would_skip_rows": 1,
        "would_reject_rows": 0,
    }
    with PatchStage3(
        _platform_pg_conn=lambda: PlatformConnection(),
        _select_stage3_candidates=lambda *_args, **_kwargs: [_candidate("raw-dry")],
        _dry_run_candidate=lambda *_args, **_kwargs: dict(dry_summary),
    ):
        dry = job_stage3.process_stage3_batch(Client(), "run", {"dry_run": True, "persist_dry_run_result": False})
    assert dry.to_dict()["dry_run_success_count"] == 1
    assert dry.successful_load_identities == []

    from jobs.common.environment_identity import EnvironmentIdentityError
    for failure, expected in (
        (EnvironmentIdentityError("TEST", "synthetic"), Stage3Outcome.FAILED_ENVIRONMENT_IDENTITY),
        (PermissionError("synthetic"), Stage3Outcome.FAILED_PERMISSION),
    ):
        with PatchStage3(
            _platform_pg_conn=lambda: PlatformConnection(),
            _select_stage3_candidates=lambda *_args, **_kwargs: [_candidate("raw-fail")],
            _process_candidate=lambda *_args, _failure=failure, **_kwargs: (_ for _ in ()).throw(_failure),
        ):
            try:
                job_stage3.process_stage3_batch(Client(), "run", {})
            except Stage3BatchError as exc:
                assert exc.partial_result.items[0].outcome == expected
            else:
                raise AssertionError("hard Stage 3 failure was accepted")


def test_cross_stage_serialization_and_postprocessor_readiness() -> None:
    stage1 = Stage1BatchResult(mailbox_check_completed=True, raw_files_created=1, attachments_created=1)
    stage2 = Stage2BatchResult(
        discovered_candidate_count=1,
        eligible_count=1,
        items=[Stage2ItemResult(RAW_ID, Stage2Outcome.SUCCEEDED_CREATED, artifact_id="cleaned")],
    )
    stage3 = Stage3BatchResult(
        discovered_candidate_count=1,
        eligible_count=1,
        items=[
            Stage3ItemResult(
                RAW_ID,
                "CLIENT",
                "report_207",
                Stage3Outcome.LOADED,
                destination_schema="telematics_reports",
                destination_table="report_207",
                persisted_status="OK",
                source_cleaned_artifact_id="cleaned",
            )
        ],
    )
    payloads = [stage1.to_dict(), stage2.to_dict(), stage3.to_dict()]
    json.dumps(payloads)
    assert stage1.downstream_stage2_work_may_exist
    assert stage2.to_dict()["succeeded_created_count"] == 1
    assert len(stage3.successful_load_identities) == 1

    dry = Stage3BatchResult(items=[
        Stage3ItemResult(RAW_ID, "CLIENT", "report_207", Stage3Outcome.DRY_RUN_SUCCEEDED, dry_run=True)
    ])
    assert dry.successful_load_identities == []

    for result, error_type in (
        (stage1, Stage1BatchError),
        (stage2, Stage2BatchError),
        (stage3, Stage3BatchError),
    ):
        error = error_type(result)
        assert error.partial_result is result

    serialized = json.dumps(payloads).lower()
    for prohibited in ("sender", "recipient", "subject", "registration", "driver", "coordinate", "password"):
        assert prohibited not in serialized


def main() -> None:
    test_stage1_results_no_work_dedup_and_artifacts()
    test_stage1_failure_is_not_no_work_and_exposes_partial_result()
    test_stage1_public_wrapper_returns_typed_result()
    test_stage1_mailbox_zero_no_attachment_and_hard_failures()
    test_stage3_zero_success_partial_and_dry_run()
    test_cross_stage_serialization_and_postprocessor_readiness()
    print("OK - Workflow B Stage 1/2/3 typed contracts are orchestrator-ready.")


if __name__ == "__main__":
    main()
