#!/usr/bin/env python3
"""Manual regressions for Workflow B Stage 2 client_code and record_id finalization.

Run:

    cd /opt/log-platform
    PYTHONPATH="$PWD" python3 ops/tests_manual/test_stage2_client_code_record_id.py
"""
from __future__ import annotations

import sys
import types
from pathlib import Path


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

from jobs.reports.stage2 import job_stage2  # noqa: E402


class FakeSeries:
    def __init__(self, values):
        self.values = values

    def tolist(self):
        return list(self.values)


class FakeDataFrame:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = [dict(row) for row in rows]
        self.columns = list(rows[0].keys()) if rows else []

    def __getitem__(self, column):
        return FakeSeries([row.get(column) for row in self.rows])

    def __setitem__(self, column, values):
        if column not in self.columns:
            self.columns.append(column)
        if isinstance(values, list):
            for row, value in zip(self.rows, values):
                row[column] = value
        else:
            for row in self.rows:
                row[column] = values

    def copy(self):
        return FakeDataFrame(self.rows)

    def drop(self, *, columns):
        out = self.copy()
        for column in columns:
            if column in out.columns:
                out.columns.remove(column)
            for row in out.rows:
                row.pop(column, None)
        return out

    def iterrows(self):
        for idx, row in enumerate(self.rows):
            yield idx, row

    def __len__(self):
        return len(self.rows)


class FakeClient:
    def __init__(self) -> None:
        self.logs = []

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


def _account(code: str) -> job_stage2.Stage2ClientAccount:
    return job_stage2.Stage2ClientAccount(
        client_id=f"id-{code}",
        client_code=code,
        client_name=code,
        client_db_host="127.0.0.1",
        client_db_port=5432,
        client_db_name=f"db_{code}",
        client_db_user="user",
        client_db_password_secret_ref="SECRET_REF",
        client_db_schema="public",
    )


def _with_match_patches(accounts, query_fn):
    class Patch:
        def __enter__(self):
            self.orig_load = job_stage2._load_enabled_client_accounts
            self.orig_query = job_stage2._query_client_trip_matches
            job_stage2._load_enabled_client_accounts = lambda cur: accounts
            job_stage2._query_client_trip_matches = query_fn

        def __exit__(self, exc_type, exc, tb):
            job_stage2._load_enabled_client_accounts = self.orig_load
            job_stage2._query_client_trip_matches = self.orig_query

    return Patch()


def _resolve_with_matches(column_name: str, rows: list[dict], accounts, matches_by_code):
    def query(account, candidate_values):
        return matches_by_code.get(
            account.client_code,
            {"match_count": 0, "columns": set(), "sample_values": []},
        )

    client = FakeClient()
    with _with_match_patches(accounts, query):
        result = job_stage2._resolve_client_code_for_cleaned_report(
            client=client,
            platform_cur=object(),
            run_id="run-1",
            report_type="report_207",
            raw_file_id="raw-1",
            cleaned_df=FakeDataFrame(rows),
            id_sync_column_name=column_name,
        )
    return result, client.logs


def test_migration_adds_registry_columns() -> None:
    sql = (REPO_ROOT / "db/migrations/028_workflow_b_stage2_client_code_record_id.sql").read_text()
    assert "id_sync_column_name TEXT NULL" in sql, sql
    assert "record_id_ingredients TEXT NULL" in sql, sql
    assert "workflow_b_control.report_type_registry" in sql, sql
    print("PASS: migration adds report_type_registry id_sync and record_id config columns")


def test_migration_adds_raw_file_client_code() -> None:
    sql = (REPO_ROOT / "db/migrations/028_workflow_b_stage2_client_code_record_id.sql").read_text()
    assert "ALTER TABLE ingest.raw_file" in sql, sql
    assert "ADD COLUMN IF NOT EXISTS client_code TEXT NULL" in sql, sql
    print("PASS: migration adds ingest.raw_file.client_code")


def test_client_detection_skips_empty_id_sync_column() -> None:
    client = FakeClient()
    result = job_stage2._resolve_client_code_for_cleaned_report(
        client=client,
        platform_cur=object(),
        run_id="run-1",
        report_type="report_207",
        raw_file_id="raw-1",
        cleaned_df=FakeDataFrame([{"registration": "ABC"}]),
        id_sync_column_name="  ",
    )
    assert result is None
    assert any(log["context"].get("reason") == "id_sync_column_name_not_configured" for log in client.logs)
    print("PASS: client detection skips when id_sync_column_name is empty")


def test_client_detection_fails_for_missing_report_column() -> None:
    try:
        job_stage2._resolve_client_code_for_cleaned_report(
            client=FakeClient(),
            platform_cur=object(),
            run_id="run-1",
            report_type="report_207",
            raw_file_id="raw-1",
            cleaned_df=FakeDataFrame([{"registration": "ABC"}]),
            id_sync_column_name="missing_column",
        )
    except RuntimeError as exc:
        assert "does not exist" in str(exc), exc
    else:
        raise AssertionError("expected missing id_sync report column to fail")
    print("PASS: client detection fails for missing id_sync report column")


def test_client_detection_assigns_by_registration() -> None:
    code, _logs = _resolve_with_matches(
        "Registration",
        [{"Registration": "ABC123"}],
        [_account("DELTA00001")],
        {"DELTA00001": {"match_count": 1, "columns": {"registration"}, "sample_values": ["ABC123"]}},
    )
    assert code == "DELTA00001"
    print("PASS: client_code is assigned from registration match")


def test_client_detection_assigns_by_chassis_number() -> None:
    code, _logs = _resolve_with_matches(
        "VIN",
        [{"VIN": "VIN123"}],
        [_account("DELTA00002")],
        {"DELTA00002": {"match_count": 1, "columns": {"chassis_number"}, "sample_values": ["VIN123"]}},
    )
    assert code == "DELTA00002"
    print("PASS: client_code is assigned from chassis_number match")


def test_client_detection_assigns_by_driver_name() -> None:
    code, _logs = _resolve_with_matches(
        "Driver",
        [{"Driver": "Jan Kowalski"}],
        [_account("DELTA00003")],
        {"DELTA00003": {"match_count": 1, "columns": {"driver_name"}, "sample_values": ["Jan Kowalski"]}},
    )
    assert code == "DELTA00003"
    print("PASS: client_code is assigned from driver_name match")


def test_client_db_connection_uses_keyword_password_argument() -> None:
    source = (REPO_ROOT / "jobs/reports/stage2/job_stage2.py").read_text()
    assert "password=resolve_secret(account.client_db_password_secret_ref)" in source, source
    assert "f\"password={resolve_secret" not in source, source
    print("PASS: client DB connection avoids password interpolation into a DSN string")


def test_client_detection_fails_on_ambiguous_matches() -> None:
    try:
        _resolve_with_matches(
            "Registration",
            [{"Registration": "ABC123"}],
            [_account("DELTA00001"), _account("DELTA00002")],
            {
                "DELTA00001": {"match_count": 1, "columns": {"registration"}, "sample_values": ["ABC123"]},
                "DELTA00002": {"match_count": 1, "columns": {"driver_name"}, "sample_values": ["ABC123"]},
            },
        )
    except RuntimeError as exc:
        assert "ambiguous client_code detection" in str(exc), exc
    else:
        raise AssertionError("expected ambiguous client_code detection to fail")
    print("PASS: ambiguous client_code matches fail")


def test_client_detection_leaves_null_when_no_match() -> None:
    code, logs = _resolve_with_matches(
        "Registration",
        [{"Registration": "NOPE"}],
        [_account("DELTA00001")],
        {},
    )
    assert code is None
    assert any(log["message"] == "Stage2 client_code detection found no matching client" for log in logs), logs
    print("PASS: no client match leaves client_code NULL")


def test_record_id_is_last_column() -> None:
    df = job_stage2._add_record_id_column(
        FakeDataFrame([{"driver_name": "A", "trip_date": "2026-05-14"}]),
        record_id_ingredients="driver_name,trip_date",
        client=FakeClient(),
        run_id="run-1",
        context={},
    )
    assert df.columns[-1] == "record_id", df.columns
    print("PASS: record_id is added as the last column")


def test_record_id_is_deterministic_for_same_values() -> None:
    df = job_stage2._add_record_id_column(
        FakeDataFrame(
            [
                {"driver_name": " A ", "trip_date": "2026-05-14"},
                {"driver_name": "A", "trip_date": "2026-05-14"},
            ]
        ),
        record_id_ingredients="driver_name,trip_date",
        client=FakeClient(),
        run_id="run-1",
        context={},
    )
    assert df.rows[0]["record_id"] == df.rows[1]["record_id"], df.rows
    print("PASS: record_id is deterministic for equivalent ingredient values")


def test_record_id_changes_when_ingredient_changes() -> None:
    df = job_stage2._add_record_id_column(
        FakeDataFrame(
            [
                {"driver_name": "A", "trip_date": "2026-05-14"},
                {"driver_name": "B", "trip_date": "2026-05-14"},
            ]
        ),
        record_id_ingredients="driver_name,trip_date",
        client=FakeClient(),
        run_id="run-1",
        context={},
    )
    assert df.rows[0]["record_id"] != df.rows[1]["record_id"], df.rows
    print("PASS: record_id changes when any ingredient changes")


def test_record_id_empty_when_no_ingredients_configured() -> None:
    df = job_stage2._add_record_id_column(
        FakeDataFrame([{"driver_name": "A"}]),
        record_id_ingredients=" , ",
        client=FakeClient(),
        run_id="run-1",
        context={},
    )
    assert df.rows[0]["record_id"] is None, df.rows
    assert df.columns[-1] == "record_id", df.columns
    print("PASS: record_id is empty when no ingredients are configured")


def test_record_id_fails_for_missing_ingredient_column() -> None:
    try:
        job_stage2._add_record_id_column(
            FakeDataFrame([{"driver_name": "A"}]),
            record_id_ingredients="driver_name,missing_column",
            client=FakeClient(),
            run_id="run-1",
            context={},
        )
    except RuntimeError as exc:
        assert "missing_column" in str(exc), exc
    else:
        raise AssertionError("expected missing record_id ingredient column to fail")
    print("PASS: record_id generation fails for missing ingredient columns")


def main() -> None:
    test_migration_adds_registry_columns()
    test_migration_adds_raw_file_client_code()
    test_client_detection_skips_empty_id_sync_column()
    test_client_detection_fails_for_missing_report_column()
    test_client_detection_assigns_by_registration()
    test_client_detection_assigns_by_chassis_number()
    test_client_detection_assigns_by_driver_name()
    test_client_db_connection_uses_keyword_password_argument()
    test_client_detection_fails_on_ambiguous_matches()
    test_client_detection_leaves_null_when_no_match()
    test_record_id_is_last_column()
    test_record_id_is_deterministic_for_same_values()
    test_record_id_changes_when_ingredient_changes()
    test_record_id_empty_when_no_ingredients_configured()
    test_record_id_fails_for_missing_ingredient_column()


if __name__ == "__main__":
    main()
