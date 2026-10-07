#!/usr/bin/env python3
"""Manual regressions for the Stage 2 pending-review notification operator script.

Run:

    cd /opt/log-platform
    PYTHONPATH="$PWD" python3 ops/tests_manual/test_manual_send_stage2_pending_review_notification.py
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ops import manual_send_stage2_pending_review_notification as manual_notify  # noqa: E402


class FakeCursor:
    def __init__(self, rows=None) -> None:
        self.rows = rows or []
        self.calls = []

    def execute(self, sql, params=()) -> None:
        self.calls.append((sql, params))

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeConn:
    def __init__(self, rows=None) -> None:
        self.cursor_obj = FakeCursor(rows=rows)
        self.closed = False

    def cursor(self):
        return self.cursor_obj

    def close(self):
        self.closed = True


class EnvPatch:
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


def _rows():
    return [
        {
            "run_id": "run-1",
            "raw_file_id": "raw-1",
            "filename": "/data/reports/normalized/a.csv",
            "normalized_path": "/data/reports/normalized/a.csv",
            "current_file_path": "/data/reports/raw/a.xlsx",
            "original_filename": "a.xlsx",
            "detected_candidate_report_type": "report_207",
            "detect_score": "0.41",
            "pending_reason": "low_detection_confidence",
            "artifact_id": "artifact-a",
        },
        {
            "run_id": "run-2",
            "raw_file_id": "raw-2",
            "filename": "/data/reports/normalized/b.csv",
            "normalized_path": "/data/reports/normalized/b.csv",
            "current_file_path": "/data/reports/raw/b.xlsx",
            "original_filename": "b.xlsx",
            "detected_candidate_report_type": "n104_1",
            "detect_score": "0.33",
            "pending_reason": "low_detection_confidence",
            "artifact_id": "artifact-b",
        },
    ]


def _run_main(argv, *, rows, email_sender=None):
    conn = FakeConn(rows=rows)
    stdout = io.StringIO()
    stderr = io.StringIO()
    sender = email_sender or (lambda **kwargs: None)
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        rc = manual_notify.main(
            argv,
            connect_fn=lambda: conn,
            email_sender=sender,
            load_env=False,
        )
    return rc, stdout.getvalue(), stderr.getvalue(), conn


def test_dry_run_with_two_pending_rows() -> None:
    def fail_sender(**kwargs):
        raise AssertionError("dry-run must not send email")

    with EnvPatch({"AUTOMATION_SMTP_HOST": None, "ARTIFACT_EXPLORER_BASE_URL": "https://ops.example.test"}):
        rc, out, err, conn = _run_main(["--dry-run"], rows=_rows(), email_sender=fail_sender)

    assert rc == 0, (rc, out, err)
    assert "Stage 2 pending-review rows matched: 2" in out, out
    assert "Dry run only; email was not sent." in out, out
    assert "https://ops.example.test/artifact-explorer/artifacts/artifact-a" in out, out
    assert "rf.stage2_pending_reason = %s" in conn.cursor_obj.calls[0][0], conn.cursor_obj.calls[0][0]
    assert conn.cursor_obj.calls[0][1] == ("low_detection_confidence",), conn.cursor_obj.calls[0]
    assert err == "", err
    print("PASS: dry-run previews two pending rows without SMTP")


def test_zero_rows_exits_successfully() -> None:
    with EnvPatch({"AUTOMATION_SMTP_HOST": None}):
        rc, out, err, conn = _run_main([], rows=[])

    assert rc == 0, (rc, out, err)
    assert "nothing to send" in out, out
    assert err == "", err
    assert conn.closed is True
    print("PASS: zero matching rows exits successfully without SMTP")


def test_missing_smtp_host_exits_with_env_name_only() -> None:
    calls = []
    with EnvPatch(
        {
            "AUTOMATION_SMTP_HOST": None,
            "AUTOMATION_SMTP_USERNAME": "robot@example.test",
            "AUTOMATION_SMTP_PASSWORD": "super-secret",
            "STAGE2_PENDING_REVIEW_NOTIFY_TO": "ops@example.test",
        }
    ):
        rc, out, err, _conn = _run_main([], rows=_rows()[:1], email_sender=lambda **kwargs: calls.append(kwargs))

    assert rc == 2, (rc, out, err)
    assert calls == [], calls
    assert err.strip() == "ERROR: Missing SMTP config: AUTOMATION_SMTP_HOST", err
    assert "super-secret" not in err
    assert "robot@example.test" not in err
    print("PASS: missing SMTP host reports only the missing env var name")


def test_successful_send_with_mocked_emailer() -> None:
    calls = []
    with EnvPatch(
        {
            "AUTOMATION_SMTP_HOST": "smtp.example.test",
            "STAGE2_PENDING_REVIEW_NOTIFY_TO": "ops@example.test;backup@example.test",
            "ARTIFACT_EXPLORER_BASE_URL": "https://ops.example.test",
        }
    ):
        rc, out, err, _conn = _run_main([], rows=_rows(), email_sender=lambda **kwargs: calls.append(kwargs))

    assert rc == 0, (rc, out, err)
    assert len(calls) == 1, calls
    assert calls[0]["to_addrs"] == ["ops@example.test", "backup@example.test"], calls[0]
    assert "(2)" in calls[0]["subject"], calls[0]["subject"]
    assert calls[0]["html_body"].count("<table") == 1, calls[0]["html_body"]
    assert calls[0]["html_body"].count("<tr>") == 3, calls[0]["html_body"]
    assert "https://ops.example.test/artifact-explorer/artifacts/artifact-b" in calls[0]["html_body"]
    assert "Sent Stage 2 pending-review notification with 2 row(s)" in out, out
    assert err == "", err
    print("PASS: successful send uses mocked emailer with one aggregated HTML table")


def main() -> None:
    test_dry_run_with_two_pending_rows()
    test_zero_rows_exits_successfully()
    test_missing_smtp_host_exits_with_env_name_only()
    test_successful_send_with_mocked_emailer()


if __name__ == "__main__":
    main()
