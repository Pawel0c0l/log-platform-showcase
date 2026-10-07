#!/usr/bin/env python3
"""Behavioral checks for the database-backed Workflow B Stage 2 source contract."""
from __future__ import annotations

import tempfile
from pathlib import Path

from jobs.reports.stage2 import job_stage2


class Cursor:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.sql = ""
        self.params = ()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql, params=()):
        self.sql = sql
        self.params = params

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None


class Connection:
    def __init__(self, rows=None):
        self.cursor_obj = Cursor(rows)

    def cursor(self):
        return self.cursor_obj


def persisted_row(**overrides):
    row = {
        "raw_file_id": "666ff6cc-aa5b-4c07-8eaa-3a95d3a4bd2c",
        "normalized_csv_path": "/synthetic/normalized.csv",
        "original_filename": "synthetic.csv",
        "source_identity": "a" * 64,
        "status": "NORMALIZED",
        "stage2_status": None,
        "stage2_report_type": None,
        "stage2_pending_reason": None,
        "stage2_outcome_category": None,
        "stage2_retryable": None,
        "stage2_cleaned_artifact_id": None,
        "client_code": None,
    }
    row.update(overrides)
    return row


def test_default_selection_is_persisted_and_policy_bounded() -> None:
    conn = Connection([persisted_row()])
    rows = job_stage2._candidate_rows(conn, {})
    assert len(rows) == 1
    sql = conn.cursor_obj.sql
    assert "FROM ingest.raw_file" in sql
    assert "status = 'NORMALIZED'" in sql
    assert "stage2_status IS NULL" in sql
    assert "stage2_retryable = true" in sql
    assert "stage2_exception" in sql
    assert "glob(" not in sql.lower()


def test_completed_review_and_rejection_are_not_default_candidates() -> None:
    conn = Connection([])
    job_stage2._candidate_rows(conn, {})
    sql = conn.cursor_obj.sql
    assert "stage2_status = 'OK'" not in sql
    assert "low_detection_confidence" not in sql
    assert "REJECTED_VALIDATION" not in sql
    assert "stage2_retryable = true" in sql


def test_retry_policy_can_be_disabled() -> None:
    conn = Connection([])
    job_stage2._candidate_rows(conn, {"retry_technical_failures": False})
    assert "stage2_retryable = true" not in conn.cursor_obj.sql
    assert "stage2_status IS NULL" in conn.cursor_obj.sql


def test_manual_path_resolves_to_persisted_identity_and_unresolved_fails() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "synthetic.csv"
        path.write_text("a;b\n1;2\n", encoding="utf-8")
        resolved = Connection([persisted_row(normalized_csv_path=str(path))])
        rows = job_stage2._candidate_rows(resolved, {"input_files": [str(path)]})
        assert rows[0]["raw_file_id"] == persisted_row()["raw_file_id"]
        assert "normalized_csv_path = %s OR sha256 = %s" in resolved.cursor_obj.sql

        try:
            job_stage2._candidate_rows(Connection([]), {"input_files": [str(path)]})
        except ValueError as exc:
            assert "exactly one raw_file_id" in str(exc)
        else:
            raise AssertionError("unresolved path-only input was accepted")


def test_database_registry_access_is_expected() -> None:
    class RegistryCursor(Cursor):
        def execute(self, sql, params=()):
            super().execute(sql, params)
            if "to_regclass" in sql:
                self.rows = [{"rel": "workflow_b_control.report_type_registry"}]
            elif "information_schema.columns" in sql:
                self.rows = [
                    {"column_name": "id_sync_column_name"},
                    {"column_name": "record_id_ingredients"},
                ]
            else:
                self.rows = [{"id_sync_column_name": "id", "record_id_ingredients": "id"}]

    cur = RegistryCursor()
    config = job_stage2._load_stage2_registry_config(cur, report_type="synthetic_report")
    assert config == {"id_sync_column_name": "id", "record_id_ingredients": "id"}
    assert "workflow_b_control.report_type_registry" in cur.sql


def main() -> None:
    test_default_selection_is_persisted_and_policy_bounded()
    test_completed_review_and_rejection_are_not_default_candidates()
    test_retry_policy_can_be_disabled()
    test_manual_path_resolves_to_persisted_identity_and_unresolved_fails()
    test_database_registry_access_is_expected()
    print("OK - Stage 2 uses persisted database candidates and permits registry-backed configuration.")


if __name__ == "__main__":
    main()
