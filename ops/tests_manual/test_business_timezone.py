#!/usr/bin/env python3
"""Manual regressions for the Europe/Warsaw business timezone policy.

Run:

    cd /opt/log-platform
    PYTHONPATH="$PWD" python3 ops/tests_manual/test_business_timezone.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api import client as api_client  # noqa: E402
from api.timezone_utils import (  # noqa: E402
    DEFAULT_BUSINESS_TIMEZONE,
    business_time_log_context,
    format_business_timestamp,
    parse_report_local_timestamp,
    set_pg_session_timezone,
)
from jobs.reports.postprocess import job_report_207_speeding_migration as report_207  # noqa: E402


def test_report_local_timestamp_dst_conversion() -> None:
    summer = parse_report_local_timestamp("2026-05-18 10:00")
    winter = parse_report_local_timestamp("2026-01-18 10:00")
    assert summer.astimezone(timezone.utc) == datetime(2026, 5, 18, 8, 0, tzinfo=timezone.utc)
    assert winter.astimezone(timezone.utc) == datetime(2026, 1, 18, 9, 0, tzinfo=timezone.utc)
    print("PASS: FleetWeb local timestamps convert with Europe/Warsaw DST rules")


def test_business_timestamp_formatting_uses_warsaw_offsets() -> None:
    summer_utc = datetime(2026, 5, 18, 8, 0, tzinfo=timezone.utc)
    winter_utc = datetime(2026, 1, 18, 9, 0, tzinfo=timezone.utc)
    assert format_business_timestamp(summer_utc) == "2026-05-18T10:00:00+02:00"
    assert format_business_timestamp(winter_utc) == "2026-01-18T10:00:00+01:00"
    print("PASS: business timestamp formatting exposes the correct Warsaw offset")


def test_log_context_contains_local_timestamp_and_timezone() -> None:
    context = business_time_log_context(datetime(2026, 1, 18, 9, 0, tzinfo=timezone.utc))
    assert context["timestamp_local"] == "2026-01-18T10:00:00+01:00"
    assert context["timezone"] == DEFAULT_BUSINESS_TIMEZONE
    print("PASS: structured log context includes Warsaw timestamp metadata")


def test_client_log_adds_warsaw_timestamp_context() -> None:
    captured = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            pass

        def json(self):
            return {"id": 123}

    def fake_post(*_args, **kwargs):
        captured.update(kwargs)
        return FakeResponse()

    original_post = api_client.requests.post
    api_client.requests.post = fake_post
    try:
        client = api_client.LogPlatformClient(
            base_url="http://log-platform.invalid",
            read_token="read-token",
            write_token="write-token",
        )
        assert client.log("INFO", "SCRIPT", "test", "message", context={"kept": True}) == 123
    finally:
        api_client.requests.post = original_post

    log_context = json.loads(captured["data"]["context_json"])
    assert log_context["kept"] is True
    assert log_context["timezone"] == DEFAULT_BUSINESS_TIMEZONE
    assert "T" in log_context["timestamp_local"]
    assert log_context["timestamp_local"].endswith(("+01:00", "+02:00"))
    print("PASS: LogPlatformClient.log enriches log context with Warsaw local time")


def test_pg_session_timezone_hook_sets_iana_timezone() -> None:
    executed = []

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params=None):
            executed.append((sql, params))

    class FakeConn:
        def cursor(self):
            return FakeCursor()

    conn = FakeConn()
    assert set_pg_session_timezone(conn) is conn
    assert executed == [("SELECT set_config('TimeZone', %s, false)", (DEFAULT_BUSINESS_TIMEZONE,))]
    print("PASS: DB session timezone hook sets Europe/Warsaw by IANA name")


def test_report_207_join_uses_warsaw_without_fixed_offsets() -> None:
    sql = report_207._build_analysis_sql(
        include_updates=False,
        limit=None,
        force_retry_errors=False,
        has_migrated_column=True,
        has_error_column=True,
    )
    assert "AT TIME ZONE 'Europe/Warsaw'" in sql
    assert "v.event_ts >= t.start_timestamp" in sql
    assert "v.event_ts <= t.end_timestamp" in sql
    assert "INTERVAL '1 hour'" not in sql
    assert "INTERVAL '2 hour'" not in sql
    assert "+ interval" not in sql.lower()
    print("PASS: report_207 matching uses Europe/Warsaw and no fixed offset arithmetic")


def main() -> None:
    test_report_local_timestamp_dst_conversion()
    test_business_timestamp_formatting_uses_warsaw_offsets()
    test_log_context_contains_local_timestamp_and_timezone()
    test_client_log_adds_warsaw_timestamp_context()
    test_pg_session_timezone_hook_sets_iana_timezone()
    test_report_207_join_uses_warsaw_without_fixed_offsets()
    print("OK - business timezone regressions passed")


if __name__ == "__main__":
    main()
