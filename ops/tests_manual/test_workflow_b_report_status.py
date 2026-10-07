#!/usr/bin/env python3
"""Offline smoke test for ops/workflow_b_report_status.py.

Run:

    cd /opt/log-platform
    python3 ops/tests_manual/test_workflow_b_report_status.py
"""
from __future__ import annotations

import io
import sys
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ops import workflow_b_report_status as report_status  # noqa: E402


FAILURES: list[str] = []


def _check(label: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    line = f"[{status}] {label}"
    if detail:
        line += f"\n        {detail}"
    print(line)
    if not ok:
        FAILURES.append(label)


def _sample_data():
    ts = datetime(2026, 5, 12, 8, 30, tzinfo=timezone.utc)
    return {
        "registry": [
            {
                "report_type": "d104_1",
                "display_name": "104 ogolny raport podrozy",
                "enabled": True,
                "implementation_status": "implemented",
                "cleaner_module": "jobs.reports.stage2.types.d104_1",
            },
            {
                "report_type": "eco_driving_driver",
                "display_name": "Raport EcoDriving - kierowcy",
                "enabled": True,
                "implementation_status": "todo",
                "cleaner_module": "jobs.reports.stage2.types.eco_driving_driver",
            },
        ],
        "status_by_type": [
            {
                "report_type": "d104_1",
                "stage2_status": "OK",
                "files_count": 3,
                "latest_stage2_at": ts,
            },
            {
                "report_type": "eco_driving_driver",
                "stage2_status": "PENDING_REVIEW",
                "files_count": 1,
                "latest_stage2_at": ts,
            },
        ],
        "zero_success": [
            {
                "report_type": "eco_driving_driver",
                "implementation_status": "todo",
                "cleaner_module": "jobs.reports.stage2.types.eco_driving_driver",
            },
        ],
        "recent_attention": [
            {
                "raw_file_id": "00000000-0000-0000-0000-000000000001",
                "report_type": "eco_driving_driver",
                "status": "PENDING_REVIEW",
                "reason": "cleaning_not_implemented",
                "original_filename": "eco.csv",
                "path": "/tmp/eco.csv",
                "latest_ts": ts,
            },
        ],
    }


def _test_render_report() -> None:
    out = io.StringIO()
    with redirect_stdout(out):
        report_status.render_report(_sample_data())
    text = out.getvalue()

    _check("title rendered", "Workflow B Report Status" in text)
    _check("registry section rendered", "Registry Summary" in text)
    _check("status section rendered", "Stage 2 Status By Report Type" in text)
    _check("zero-success section rendered",
           "Registered Report Types With Zero Successful Stage 2 Files" in text)
    _check("recent attention section rendered", "Recent Pending/Failed Stage 2 Files" in text)
    _check("implemented row rendered", "d104_1" in text and "implemented" in text)
    _check("todo row rendered", "eco_driving_driver" in text and "todo" in text)
    _check("timestamp rendered deterministically", "2026-05-12T08:30:00+00:00" in text)
    _check("None is not printed for missing values", "None" not in text)


def _test_limit_validation() -> None:
    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = report_status.main(["--limit", "0"])
    _check("invalid limit exits with code 2", code == 2, f"code={code}")
    _check("invalid limit message is clear", "--limit must be >= 1" in err.getvalue())


def main() -> int:
    _test_render_report()
    _test_limit_validation()

    print("")
    if FAILURES:
        print(f"FAIL - {len(FAILURES)} check(s) failed:")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("OK - workflow_b_report_status offline checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
