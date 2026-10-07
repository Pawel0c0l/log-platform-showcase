#!/usr/bin/env python3
"""Service-free regressions for Workflow B Stage 1 message transactions/downloads."""
from __future__ import annotations

import tempfile
from email.message import EmailMessage
from http.client import IncompleteRead
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.client import ArtifactUploadResult
from jobs.mail import fetch_reports as job
from jobs.mail.stage1_artifact_sync import (
    Stage1ArtifactReconciliationResult,
    Stage1ArtifactSyncError,
)
from jobs.mail.stage1_batch_contract import Stage1BatchError, Stage1Outcome


class Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.one = None

    def execute(self, sql, params=()):
        compact = " ".join(sql.split())
        self.one = None
        if "SELECT id FROM ingest.imap_message" in compact:
            uid = int(params[3])
            row = self.conn.committed_messages.get(uid) or self.conn.staged_messages.get(uid)
            self.one = (row,) if row is not None else None
        elif "INSERT INTO ingest.imap_message" in compact:
            uid = int(params[3])
            if uid in self.conn.committed_messages or uid in self.conn.staged_messages:
                self.one = None
            else:
                message_id = self.conn.next_message_id
                self.conn.next_message_id += 1
                self.conn.staged_messages[uid] = message_id
                self.one = (message_id,)
        elif "FROM artifacts" in compact:
            self.one = None

    def fetchone(self):
        return self.one


class Connection:
    def __init__(self):
        self.cursor_obj = Cursor(self)
        self.committed_messages: dict[int, int] = {}
        self.staged_messages: dict[int, int] = {}
        self.committed_raw: dict[str, dict] = {}
        self.staged_raw: dict[str, dict] = {}
        self.next_message_id = 1
        self.next_raw_id = 1
        self.commits: list[tuple[int, ...]] = []
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commits.append(tuple(sorted(self.staged_messages)))
        self.committed_messages.update(self.staged_messages)
        self.committed_raw.update(self.staged_raw)
        self.staged_messages.clear()
        self.staged_raw.clear()

    def rollback(self):
        self.rollbacks += 1
        self.staged_messages.clear()
        self.staged_raw.clear()

    def close(self):
        self.closed = True


class Imap:
    def __init__(self, messages):
        self.messages = messages

    def login(self, *_args): return None
    def select(self, *_args): return ("OK", [])
    def response(self, *_args): return ("UIDVALIDITY", [b"77"])
    def uid(self, _command, uid, _query):
        return ("OK", [(b'INTERNALDATE "21-Jul-2026 12:00:00 +0200"', self.messages[int(uid)])])
    def close(self): return None
    def logout(self): return None


class Client:
    def __init__(self):
        self.logs = []
        self.artifacts: dict[tuple[str, str], str] = {}
        self.upload_calls = []

    def log(self, level, _kind, _source, message, *, run_id=None, context=None, **_kwargs):
        self.logs.append({"level": level, "message": message, "run_id": run_id, "context": context or {}})

    def upload_artifact(self, path, **kwargs):
        key = (kwargs["idempotency_scope"], kwargs["idempotency_key"])
        self.upload_calls.append((path, dict(kwargs)))
        if key in self.artifacts:
            return ArtifactUploadResult(self.artifacts[key], "reused")
        artifact_id = f"artifact-{len(self.artifacts) + 1}"
        self.artifacts[key] = artifact_id
        return ArtifactUploadResult(artifact_id, "created")


def attachment_message(payload: bytes, filename: str = "report.csv") -> bytes:
    msg = EmailMessage()
    msg["Subject"] = "Report"
    msg["Message-ID"] = f"<{filename}@safe.invalid>"
    msg.set_content("attached")
    msg.add_attachment(payload, maintype="application", subtype="octet-stream", filename=filename)
    return msg.as_bytes()


def link_message(url: str, subject: str = "Report limit e-mail") -> bytes:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg.set_content(f"Download: {url}")
    return msg.as_bytes()


class Patch:
    def __init__(self, **values):
        self.values = values
        self.originals = {}

    def __enter__(self):
        for name, value in self.values.items():
            self.originals[name] = getattr(job, name)
            setattr(job, name, value)
        return self

    def __exit__(self, *_args):
        for name, value in self.originals.items():
            setattr(job, name, value)


def persist_candidate(cur, **kwargs):
    digest = kwargs["sha256"]
    if digest in cur.conn.committed_raw or digest in cur.conn.staged_raw:
        row = cur.conn.committed_raw.get(digest) or cur.conn.staged_raw[digest]
        return {"action": "SKIP_EXISTING_SHA", "raw_file_id": row["raw_file_id"], "existing_status": "NORMALIZED"}
    raw_id = f"00000000-0000-4000-8000-{cur.conn.next_raw_id:012d}"
    cur.conn.next_raw_id += 1
    cur.conn.staged_raw[digest] = {
        "raw_file_id": raw_id,
        "imap_message_id": kwargs["imap_message_id"],
        "raw_path": kwargs["raw_path"],
    }
    return {"action": "NEW", "raw_file_id": raw_id}


def _run_batch(conn, client, messages, uids, data_dir, downloader):
    env = {
        "IMAP_HOST": "imap.safe.invalid",
        "IMAP_PASSWORD": "synthetic",
        "IMAP_USER": "opaque-test-account",
        "REPORTS_DATA_DIR": str(data_dir),
    }
    with Patch(
        reconcile_stage1_artifacts_batch=lambda *_a, **_k: Stage1ArtifactReconciliationResult(),
        _pg_conn=lambda: conn,
        _search_imap_uids=lambda *_a, **_k: ([str(uid).encode() for uid in uids], ["synthetic"]),
        _content_dedup_settings=lambda: ("off", 0.0, 1.0),
        _report_link_settings=lambda: ({"fleetmail.telematics-provider.example"}, 1_000_000, 1, True, False, 1),
        _persist_raw_file_candidate=persist_candidate,
        _download_url_to_bytes=downloader,
    ):
        original_getenv = job.os.getenv
        original_imap = job.imaplib.IMAP4_SSL
        try:
            job.os.getenv = lambda name, default=None: env.get(name, default)
            job.imaplib.IMAP4_SSL = lambda *_args: Imap(messages)
            return job.fetch_reports_batch(client, "run-stage1", {})
        finally:
            job.os.getenv = original_getenv
            job.imaplib.IMAP4_SSL = original_imap


def test_partial_commit_mixed_batch_retry_and_no_duplicates() -> None:
    conn = Connection()
    client = Client()
    messages = {
        1: attachment_message(b"name,value\nfirst,1\n"),
        2: link_message("https://fleetmail.telematics-provider.example/report.csv?token=do-not-log"),
        3: link_message("https://notifications.example.org/cancelEmail?id=secret-token"),
    }

    def timeout(*_args, **_kwargs):
        raise job.ReportDownloadError(
            "read timeout at https://fleetmail.telematics-provider.example/report.csv?token=do-not-log",
            expected_bytes=121,
            received_bytes=79,
            retry_attempt=2,
            cleanup_result="temporary_removed",
            exception_type="ReadTimeout",
        )

    with tempfile.TemporaryDirectory() as tmp:
        try:
            _run_batch(conn, client, messages, [1, 2, 3], Path(tmp), timeout)
        except Stage1BatchError as exc:
            partial = exc.partial_result
        else:
            raise AssertionError("mixed retryable batch unexpectedly succeeded")

        assert set(conn.committed_messages) == {1, 3}, conn.committed_messages
        assert len(conn.committed_raw) == 1, conn.committed_raw
        assert conn.rollbacks == 1
        assert partial.attachments_created == 1 and partial.raw_files_created == 1
        assert partial.retryable_work_remains
        assert partial.count(Stage1Outcome.CREATED) == 1
        assert partial.count(Stage1Outcome.SKIPPED_EXPECTED_LINK) == 1
        assert partial.count(Stage1Outcome.FAILED_RETRYABLE_DOWNLOAD) == 1
        failed = next(item for item in partial.items if item.outcome == Stage1Outcome.FAILED_RETRYABLE_DOWNLOAD)
        assert failed.imap_uid == 2 and failed.exception_type == "ReadTimeout"
        assert failed.expected_bytes == 121 and failed.received_bytes == 79
        assert failed.retry_attempt == 2 and failed.transaction_scope == "imap_message"
        assert "do-not-log" not in (failed.exception_message or "")
        failure_log = next(log for log in client.logs if "download failed" in log["message"].lower())
        required = {
            "run_id", "imap_uid", "message_identity", "client_code", "report_type",
            "raw_file_id", "download_host", "exception_type", "exception_message",
            "expected_bytes", "received_bytes", "retry_attempt", "retryable",
            "transaction_scope", "cleanup_result",
        }
        assert required.issubset(failure_log["context"]), failure_log
        assert "do-not-log" not in str(failure_log["context"])
        assert any(log["message"].startswith("IMAP fetch_reports completed") for log in client.logs)

        def success(*_args, **_kwargs):
            return b"name,value\nthird,3\n", "report.csv", "text/csv"

        retried = _run_batch(conn, client, messages, [2], Path(tmp), success)
        assert not retried.retryable_work_remains
        assert set(conn.committed_messages) == {1, 2, 3}
        assert len(conn.committed_raw) == 2
        raw_count = len(conn.committed_raw)
        upload_count = len(client.artifacts)
        repeated = _run_batch(conn, client, messages, [1, 2, 3], Path(tmp), success)
        assert repeated.count(Stage1Outcome.REUSED_MESSAGE) == 3
        assert len(conn.committed_raw) == raw_count
        assert len(client.artifacts) == upload_count


def test_incomplete_content_length_removes_part() -> None:
    class Response:
        headers = {"Content-Length": "10", "Content-Type": "text/csv"}
        url = "https://fleetmail.telematics-provider.example/report.csv"
        status_code = 200
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def raise_for_status(self): return None
        def iter_content(self, **_kwargs):
            yield b"12345"
            raise IncompleteRead(b"", 5)

    original_get = job.requests.get
    original_temp = job.tempfile.NamedTemporaryFile
    with tempfile.TemporaryDirectory() as tmp:
        try:
            job.requests.get = lambda *_a, **_k: Response()
            job.tempfile.NamedTemporaryFile = lambda **kwargs: original_temp(dir=tmp, **kwargs)
            try:
                job._download_url_to_bytes(
                    "https://fleetmail.telematics-provider.example/report.csv",
                    timeout_s=1,
                    max_bytes=100,
                    verify_tls=True,
                    attempts=1,
                )
            except job.ReportDownloadError as exc:
                assert exc.expected_bytes == 10 and exc.received_bytes == 5
                assert exc.cleanup_result == "temporary_removed"
                assert exc.exception_type == "IncompleteRead"
            else:
                raise AssertionError("incomplete Content-Length was accepted")
            assert not list(Path(tmp).glob("*.part")), list(Path(tmp).iterdir())
        finally:
            job.requests.get = original_get
            job.tempfile.NamedTemporaryFile = original_temp


def test_artifact_write_then_lost_metadata_response_is_deterministic() -> None:
    class ArtifactCursor:
        def execute(self, *_args, **_kwargs): return None
        def fetchone(self): return None

    class LostResponseClient(Client):
        def __init__(self):
            super().__init__()
            self.fail = True
            self.keys = []
        def upload_artifact(self, path, **kwargs):
            self.keys.append((kwargs["idempotency_scope"], kwargs["idempotency_key"]))
            if self.fail:
                self.fail = False
                raise TimeoutError("object stored but metadata response was lost")
            return ArtifactUploadResult("canonical-artifact", "reused")

    counters = {
        "artifact_upload_failed": 0, "artifact_upload_skipped_existing": 0,
        "artifacts_raw_uploaded": 0, "artifacts_normalized_uploaded": 0,
    }
    client = LostResponseClient()
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "source.csv"
        source.write_bytes(b"safe")
        upload = [{
            "path": source,
            "raw_file_id": "11e594f4-8195-4c10-8301-5d0bf0447a22",
            "artifact_role": "raw",
            "original_filename": "source.csv",
            "uid": 10,
            "message_identity": "opaque-message",
            "source_sha256": "a" * 64,
        }]
        try:
            job._upload_stage1_artifacts(client, ArtifactCursor(), run_id="run", uploads=upload, counters=counters)
        except Stage1ArtifactSyncError as exc:
            assert exc.partial_result.retryable_work_remains
        else:
            raise AssertionError("lost artifact metadata response was accepted")
        result = job._upload_stage1_artifacts(
            client, ArtifactCursor(), run_id="run-retry", uploads=upload, counters=counters
        )
        assert result.items[0].idempotency_status == "reused"
        assert client.keys[0] == client.keys[1]
        assert source.exists()


def main() -> None:
    test_partial_commit_mixed_batch_retry_and_no_duplicates()
    test_incomplete_content_length_removes_part()
    test_artifact_write_then_lost_metadata_response_is_deterministic()
    print("OK - Workflow B Stage 1 transaction/download regressions passed")


if __name__ == "__main__":
    main()
