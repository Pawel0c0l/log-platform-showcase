#!/usr/bin/env python3
"""Manual regressions for Stage 2 low-confidence pending-review notifications.

Run:

    cd /opt/log-platform
    PYTHONPATH="$PWD" python3 ops/tests_manual/test_stage2_pending_review_notification.py
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    import pandas  # noqa: F401
except ModuleNotFoundError:
    pandas_stub = types.ModuleType("pandas")
    pandas_stub.DataFrame = object
    pandas_stub.Series = object
    sys.modules["pandas"] = pandas_stub

from jobs.common.emailer import send_html_email  # noqa: E402
from jobs.reports.stage2 import job_stage2  # noqa: E402


class FakeCursor:
    def __init__(self, rows=None) -> None:
        self.calls = []
        self.rows = rows or []

    def execute(self, sql, params) -> None:
        self.calls.append((sql, params))

    def fetchall(self):
        return self.rows


class FakeClient:
    def __init__(self) -> None:
        self.uploads = []
        self.logs = []

    def upload_artifact(self, filepath, **kwargs):
        self.uploads.append({"filepath": filepath, **kwargs})
        return f"artifact-{len(self.uploads)}"

    def log(self, level, type_, source, message, run_id=None, context=None, error=None):
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
        return len(self.logs)


def _detection(report_type="report_207", score=0.42):
    return SimpleNamespace(
        report_type=report_type,
        detect_score=score,
        candidates_top3=[
            {"report_type": report_type, "score": score},
            {"report_type": "n104_1", "score": 0.13},
        ],
        pending_reason="low_detection_confidence",
    )


class _EnvPatch:
    def __init__(self, values: dict[str, str | None]) -> None:
        self.values = values
        self.previous = {}

    def __enter__(self):
        for key, value in self.values.items():
            self.previous[key] = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return self

    def __exit__(self, exc_type, exc, tb):
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_low_confidence_artifact_uses_pending_review_report_type() -> None:
    cur = FakeCursor()
    client = FakeClient()
    pending_reviews = []
    path = Path("/tmp/stage2/normalized/report.csv")

    job_stage2._record_low_confidence_pending_review(
        client,
        cur,
        path=path,
        run_id="run-1",
        raw_file_id="raw-1",
        original_filename="original.xls",
        detection=_detection(),
        pending_reviews=pending_reviews,
    )

    assert cur.calls, "expected raw_file persistence"
    params = cur.calls[0][1]
    assert params[0] == "PENDING_REVIEW", params
    assert params[1] == "report_207", params
    assert params[4] == "low_detection_confidence", params

    upload = client.uploads[0]
    assert upload["run_id"] == "run-1", upload
    assert upload["raw_file_id"] == "raw-1", upload
    assert upload["report_type"] == "PENDING_REVIEW", upload
    assert upload["artifact_role"] == "debug_sample", upload
    assert upload["metadata"]["detected_candidate_report_type"] == "report_207", upload
    assert upload["metadata"]["detect_score"] == 0.42, upload
    assert pending_reviews[0]["artifact_id"] == "artifact-1", pending_reviews
    print("PASS: low-confidence artifact report_type is PENDING_REVIEW while raw_file keeps candidate")


def test_notification_not_sent_for_zero_pending_reviews() -> None:
    calls = []
    client = FakeClient()
    original_sender = job_stage2.send_html_email
    job_stage2._LOW_CONFIDENCE_NOTIFICATION_SENT_RUNS.clear()
    try:
        job_stage2.send_html_email = lambda **kwargs: calls.append(kwargs)
        job_stage2._notify_low_confidence_pending_reviews(client, run_id="run-empty", rows=[])
    finally:
        job_stage2.send_html_email = original_sender
        job_stage2._LOW_CONFIDENCE_NOTIFICATION_SENT_RUNS.clear()

    assert calls == [], calls
    assert any(log["context"].get("reason") == "zero_rows" for log in client.logs), client.logs
    print("PASS: no notification is sent when there are zero low-confidence rows")


def test_notification_sent_once_with_single_html_table_for_multiple_rows() -> None:
    calls = []
    original_sender = job_stage2.send_html_email
    job_stage2._LOW_CONFIDENCE_NOTIFICATION_SENT_RUNS.clear()
    rows = [
        {
            "run_id": "run-2",
            "raw_file_id": "raw-1",
            "filename": "/tmp/a.csv",
            "original_filename": "a.xls",
            "detected_candidate_report_type": "report_207",
            "detect_score": 0.41,
            "pending_reason": "low_detection_confidence",
            "artifact_id": "artifact-a",
        },
        {
            "run_id": "run-2",
            "raw_file_id": "raw-2",
            "filename": "/tmp/b.csv",
            "original_filename": "b.xls",
            "detected_candidate_report_type": "n104_1",
            "detect_score": 0.33,
            "pending_reason": "low_detection_confidence",
            "artifact_id": "artifact-b",
        },
    ]
    with _EnvPatch(
        {
            "AUTOMATION_SMTP_HOST": "smtp.example.test",
            "STAGE2_PENDING_REVIEW_NOTIFY_TO": "ops@example.test",
            "ARTIFACT_EXPLORER_BASE_URL": "https://ops.example.test",
        }
    ):
        try:
            job_stage2.send_html_email = lambda **kwargs: calls.append(kwargs)
            job_stage2._notify_low_confidence_pending_reviews(FakeClient(), run_id="run-2", rows=rows)
            job_stage2._notify_low_confidence_pending_reviews(FakeClient(), run_id="run-2", rows=rows)
        finally:
            job_stage2.send_html_email = original_sender
            job_stage2._LOW_CONFIDENCE_NOTIFICATION_SENT_RUNS.clear()

    assert len(calls) == 1, calls
    assert calls[0]["to_addrs"] == ["ops@example.test"], calls[0]
    assert "(2)" in calls[0]["subject"], calls[0]["subject"]
    html = calls[0]["html_body"]
    assert html.count("<table") == 1, html
    assert html.count("<tr>") == 3, html
    assert "https://ops.example.test/artifact-explorer/artifacts/artifact-a" in html, html
    assert "https://ops.example.test/artifact-explorer/artifacts/artifact-b" in html, html
    print("PASS: one notification with a single HTML table covers multiple low-confidence rows")


def test_notification_failure_is_logged_without_raising() -> None:
    original_sender = job_stage2.send_html_email
    job_stage2._LOW_CONFIDENCE_NOTIFICATION_SENT_RUNS.clear()
    client = FakeClient()
    with _EnvPatch({"AUTOMATION_SMTP_HOST": "smtp.example.test"}):
        try:
            def _raise(**kwargs):
                raise RuntimeError("smtp unavailable")

            job_stage2.send_html_email = _raise
            job_stage2._notify_low_confidence_pending_reviews(
                client,
                run_id="run-fail",
                rows=[
                    {
                        "run_id": "run-fail",
                        "filename": "/tmp/a.csv",
                        "original_filename": "a.xls",
                        "detected_candidate_report_type": "report_207",
                        "detect_score": 0.41,
                        "pending_reason": "low_detection_confidence",
                        "artifact_id": "artifact-a",
                    }
                ],
            )
        finally:
            job_stage2.send_html_email = original_sender
            job_stage2._LOW_CONFIDENCE_NOTIFICATION_SENT_RUNS.clear()

    assert any("notification email failed" in log["message"] for log in client.logs), client.logs
    print("PASS: notification failure logs warning and does not raise")


def test_missing_smtp_host_logs_skip_without_sending() -> None:
    calls = []
    original_sender = job_stage2.send_html_email
    job_stage2._LOW_CONFIDENCE_NOTIFICATION_SENT_RUNS.clear()
    client = FakeClient()
    with _EnvPatch({"AUTOMATION_SMTP_HOST": None}):
        try:
            job_stage2.send_html_email = lambda **kwargs: calls.append(kwargs)
            job_stage2._notify_low_confidence_pending_reviews(
                client,
                run_id="run-missing-smtp",
                rows=[
                    {
                        "run_id": "run-missing-smtp",
                        "filename": "/tmp/a.csv",
                        "original_filename": "a.xls",
                        "detected_candidate_report_type": "report_207",
                        "detect_score": 0.41,
                        "pending_reason": "low_detection_confidence",
                        "artifact_id": "artifact-a",
                    }
                ],
            )
        finally:
            job_stage2.send_html_email = original_sender
            job_stage2._LOW_CONFIDENCE_NOTIFICATION_SENT_RUNS.clear()

    assert calls == [], calls
    assert any(
        log["message"] == "Stage2 pending-review notification skipped"
        and log["context"].get("reason") == "missing_smtp_config"
        and log["context"].get("missing_env") == ["AUTOMATION_SMTP_HOST"]
        for log in client.logs
    ), client.logs
    print("PASS: missing SMTP host logs skip with env var name and does not send")


def test_db_eligible_rows_drive_one_notification_attempt() -> None:
    rows = [
        {
            "run_id": "run-db",
            "raw_file_id": "raw-1",
            "filename": "/tmp/a.csv",
            "original_filename": "a.xls",
            "detected_candidate_report_type": "report_207",
            "detect_score": "0.41",
            "pending_reason": "low_detection_confidence",
            "artifact_id": "artifact-a",
        },
        {
            "run_id": "run-db",
            "raw_file_id": "raw-2",
            "filename": "/tmp/b.csv",
            "original_filename": "b.xls",
            "detected_candidate_report_type": "n104_1",
            "detect_score": "0.33",
            "pending_reason": "low_detection_confidence",
            "artifact_id": "artifact-b",
        },
    ]
    cur = FakeCursor(rows=rows)
    fetched = job_stage2._fetch_low_confidence_pending_review_rows_for_run(cur, run_id="run-db")
    assert fetched == rows, fetched
    assert "rf.stage2_status = 'PENDING_REVIEW'" in cur.calls[0][0], cur.calls[0][0]
    assert cur.calls[0][1] == ("run-db", "low_detection_confidence"), cur.calls[0]

    calls = []
    original_sender = job_stage2.send_html_email
    job_stage2._LOW_CONFIDENCE_NOTIFICATION_SENT_RUNS.clear()
    with _EnvPatch({"AUTOMATION_SMTP_HOST": "smtp.example.test"}):
        try:
            job_stage2.send_html_email = lambda **kwargs: calls.append(kwargs)
            job_stage2._notify_low_confidence_pending_reviews(
                FakeClient(),
                run_id="run-db",
                rows=fetched,
                db_eligible_count=len(fetched),
                memory_collected_count=0,
            )
        finally:
            job_stage2.send_html_email = original_sender
            job_stage2._LOW_CONFIDENCE_NOTIFICATION_SENT_RUNS.clear()

    assert len(calls) == 1, calls
    assert "(2)" in calls[0]["subject"], calls[0]
    assert calls[0]["html_body"].count("<tr>") == 3, calls[0]["html_body"]
    print("PASS: two DB-derived low-confidence pending-review rows cause one email attempt")


class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.started_tls = False
        self.login_args = None
        self.messages = []
        self.quit_called = False
        FakeSMTP.instances.append(self)

    def starttls(self, context):
        self.started_tls = True

    def login(self, username, password):
        self.login_args = (username, password)

    def send_message(self, message):
        self.messages.append(message)

    def quit(self):
        self.quit_called = True


def test_emailer_uses_env_smtp_config_and_sends_html_message() -> None:
    FakeSMTP.instances = []
    with _EnvPatch(
        {
            "AUTOMATION_SMTP_HOST": "smtp.example.test",
            "AUTOMATION_SMTP_PORT": "2525",
            "AUTOMATION_SMTP_USERNAME": "robot@example.test",
            "AUTOMATION_SMTP_PASSWORD": "secret",
            "AUTOMATION_SMTP_USE_TLS": "true",
            "AUTOMATION_SMTP_FROM": "automations@example.invalid",
        }
    ):
        send_html_email(
            to_addrs=["owner@example.invalid"],
            subject="Subject",
            html_body="<p>Hello <strong>HTML</strong></p>",
            text_body="Hello text",
            smtp_factory=FakeSMTP,
        )

    smtp = FakeSMTP.instances[0]
    assert smtp.host == "smtp.example.test", smtp.host
    assert smtp.port == 2525, smtp.port
    assert smtp.started_tls is True
    assert smtp.login_args == ("robot@example.test", "secret")
    assert smtp.quit_called is True
    assert len(smtp.messages) == 1
    msg = smtp.messages[0]
    assert msg["From"] == "automations@example.invalid", msg
    assert msg["To"] == "owner@example.invalid", msg
    assert "text/html" in msg.as_string(), msg.as_string()
    assert "Hello text" in msg.as_string(), msg.as_string()
    print("PASS: emailer builds HTML+plain email from env SMTP config")


def main() -> None:
    test_low_confidence_artifact_uses_pending_review_report_type()
    test_notification_not_sent_for_zero_pending_reviews()
    test_notification_sent_once_with_single_html_table_for_multiple_rows()
    test_notification_failure_is_logged_without_raising()
    test_missing_smtp_host_logs_skip_without_sending()
    test_db_eligible_rows_drive_one_notification_attempt()
    test_emailer_uses_env_smtp_config_and_sends_html_message()


if __name__ == "__main__":
    main()
