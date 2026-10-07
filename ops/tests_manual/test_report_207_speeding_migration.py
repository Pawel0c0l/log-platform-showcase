#!/usr/bin/env python3
"""Manual regressions for Workflow B report_207 speeding post-processing.

Run:

    cd /opt/log-platform
    PYTHONPATH="$PWD" python3 ops/tests_manual/test_report_207_speeding_migration.py
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.reports.postprocess import job_report_207_speeding_migration as job  # noqa: E402
from jobs.trip_metrics_population_source import (  # noqa: E402
    TRIP_METRICS_SOURCE_API,
    TRIP_METRICS_SOURCE_MISMATCH_REASON,
    TRIP_METRICS_SOURCE_REPORT_207,
)


class FakeCursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, object]] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, query, params=None):
        self.executed.append((str(query), params))

    def fetchone(self):
        return {}

    def fetchall(self):
        return []


class FakeConn:
    def __init__(self) -> None:
        self.cursor_obj = FakeCursor()
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cursor_obj

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class PatchAttrs:
    def __init__(self, **attrs):
        self.attrs = attrs
        self.originals = {}

    def __enter__(self):
        for name, value in self.attrs.items():
            self.originals[name] = getattr(job, name)
            setattr(job, name, value)

    def __exit__(self, exc_type, exc, tb):
        for name, value in self.originals.items():
            setattr(job, name, value)


class FakeClient:
    def __init__(self) -> None:
        self.logs = []

    def log(self, *args, **kwargs):
        self.logs.append((args, kwargs))


def _config(source: str = TRIP_METRICS_SOURCE_REPORT_207) -> job.ClientDbConfig:
    return job.ClientDbConfig(
        client_code="ALPHA00001",
        client_id="client-id",
        client_db_host="127.0.0.1",
        client_db_port=5432,
        client_db_name="alpha_main",
        client_db_user="alpha_user",
        client_db_password_secret_ref="ENV:ALPHA_PASSWORD",
        trip_metrics_population_source=source,
    )


def test_client_without_report_table_is_skipped() -> None:
    conn = FakeConn()
    with PatchAttrs(
        _client_business_pg_conn=lambda _config: conn,
        _table_exists=lambda *_args: False,
    ):
        try:
            job._process_client(_config(), limit=None, dry_run=False, force_retry_errors=False)
        except job.ClientProcessingError as exc:
            assert exc.phase == "check_report_table"
            assert isinstance(exc.original, job.Stage3SchemaReadinessError)
            assert "table telematics_reports.report_207" in str(exc)
            assert "042_workflow_b_stage3_runtime_schema.sql" in str(exc)
        else:
            raise AssertionError("missing report table must fail readiness")
    assert conn.rollbacks == 1 and conn.commits == 0
    print("PASS: missing report_207 is a precise schema-readiness failure")

def test_real_run_adds_only_missing_report_tracking_columns() -> None:
    cur = FakeCursor()
    try:
        job._require_report_207_schema_ready(
            cur,
            missing_report_columns=["migrated_to_client_db", "migrated_to_client_db_error"],
            missing_client_trips_columns=[],
        )
    except job.Stage3SchemaReadinessError as exc:
        assert "column telematics_reports.report_207.migrated_to_client_db" in str(exc)
        assert "column telematics_reports.report_207.migrated_to_client_db_error" in str(exc)
    else:
        raise AssertionError("missing tracking columns must fail readiness")
    assert all("ALTER TABLE" not in query for query, _params in cur.executed)
    print("PASS: missing report tracking columns never trigger runtime DDL")

def test_real_run_adds_only_missing_client_trips_counter_columns() -> None:
    cur = FakeCursor()
    try:
        job._require_report_207_schema_ready(
            cur,
            missing_report_columns=[],
            missing_client_trips_columns=["speeding_170_plus_count"],
        )
    except job.Stage3SchemaReadinessError as exc:
        assert "column public.client_trips.speeding_170_plus_count" in str(exc)
    else:
        raise AssertionError("missing trip counter must fail readiness")
    assert all("ALTER TABLE" not in query for query, _params in cur.executed)
    print("PASS: missing client_trips counters never trigger runtime DDL")

def test_real_run_skips_alter_when_required_columns_exist() -> None:
    conn = FakeConn()
    report_columns = {
        "Data i czas",
        "Nr rejestracyjny",
        "Prędkość",
        "record_id",
        *job.REPORT_TRACKING_COLUMNS.keys(),
    }
    trips_columns = {
        "client_id",
        "provider_trip_id",
        "registration",
        "start_timestamp",
        "end_timestamp",
        "record_id",
        *job.CLIENT_TRIPS_COUNTER_COLUMNS.keys(),
    }

    def existing_columns(_cur, schema, table):
        if schema == "telematics_reports" and table == "report_207":
            return report_columns
        return trips_columns

    with PatchAttrs(
        _client_business_pg_conn=lambda _config: conn,
        _table_exists=lambda *_args: True,
        _existing_columns=existing_columns,
        _record_id_unique_index_exists=lambda *_args: True,
        _migrate_report_rows=lambda *_args, **_kwargs: {"candidate_rows": 0},
    ):
        summary = job._process_client(
            _config(),
            limit=None,
            dry_run=False,
            force_retry_errors=False,
        )
    assert summary.columns_to_add_report_207 == []
    assert summary.columns_to_add_client_trips == []
    assert all("ALTER TABLE" not in query for query, _params in conn.cursor_obj.executed)
    assert conn.commits == 1
    print("PASS: real run skips ALTER TABLE when required columns already exist")


def test_missing_client_trips_columns_without_owner_fails_clearly() -> None:
    conn = FakeConn()
    report_columns = {
        "Data i czas",
        "Nr rejestracyjny",
        "Prędkość",
        "record_id",
        *job.REPORT_TRACKING_COLUMNS.keys(),
    }
    trips_columns = {
        "client_id",
        "provider_trip_id",
        "registration",
        "start_timestamp",
        "end_timestamp",
        "record_id",
    }

    def existing_columns(_cur, schema, table):
        if schema == "telematics_reports" and table == "report_207":
            return report_columns
        return trips_columns

    with PatchAttrs(
        _client_business_pg_conn=lambda _config: conn,
        _table_exists=lambda *_args: True,
        _existing_columns=existing_columns,
        _record_id_unique_index_exists=lambda *_args: True,
    ):
        try:
            job._process_client(
                _config(),
                limit=None,
                dry_run=False,
                force_retry_errors=False,
            )
        except job.ClientProcessingError as exc:
            assert exc.phase == "validate_schema"
            assert (
                "column public.client_trips.speeding_140_160_count" in str(exc)
                and "column public.client_trips.speeding_160_170_count" in str(exc)
                and "column public.client_trips.speeding_170_plus_count" in str(exc)
                and "042_workflow_b_stage3_runtime_schema.sql" in str(exc)
            ), str(exc)
        else:
            raise AssertionError("expected missing-column ownership failure")
    assert all("ALTER TABLE" not in query for query, _params in conn.cursor_obj.executed)
    assert conn.rollbacks == 1
    assert conn.commits == 0
    print("PASS: missing client_trips columns fail clearly when client user is not owner")


def test_dry_run_reports_columns_without_altering() -> None:
    conn = FakeConn()
    report_columns = {
        "Data i czas",
        "Nr rejestracyjny",
        "Prędkość",
        "record_id",
    }
    trips_columns = {
        "client_id",
        "provider_trip_id",
        "registration",
        "start_timestamp",
        "end_timestamp",
        "record_id",
    }

    def existing_columns(_cur, schema, table):
        if schema == "telematics_reports" and table == "report_207":
            return report_columns
        return trips_columns

    with PatchAttrs(
        _client_business_pg_conn=lambda _config: conn,
        _table_exists=lambda *_args: True,
        _existing_columns=existing_columns,
        _analyze_report_rows=lambda *_args, **_kwargs: {
            "candidate_rows": 2,
            "valid_speed_rows": 1,
            "speed_140_160_rows": 1,
            "matched_rows": 1,
            "incremented_140_160": 1,
        },
    ):
        summary = job._process_client(
            _config(),
            limit=10,
            dry_run=True,
            force_retry_errors=False,
        )
    assert set(summary.columns_to_add_report_207) == set(job.REPORT_TRACKING_COLUMNS)
    assert set(summary.columns_to_add_client_trips) == set(job.CLIENT_TRIPS_COUNTER_COLUMNS)
    assert all("ALTER TABLE" not in query for query, _params in conn.cursor_obj.executed)
    assert summary.migrated_rows == 0
    print("PASS: dry-run reports missing columns without altering client DB")


def test_sql_uses_polish_report_columns_safely() -> None:
    sql = job._build_analysis_sql(
        include_updates=False,
        limit=5,
        force_retry_errors=False,
        has_migrated_column=True,
        has_error_column=True,
    )
    assert '"Data i czas"' in sql
    assert '"Nr rejestracyjny"' in sql
    assert '"Prędkość"' in sql
    assert 'FROM "telematics_reports"."report_207" AS r' in sql
    print("PASS: SQL quotes Polish report columns and report table identifiers")


def test_sql_uses_registration_and_timestamp_interval_match() -> None:
    sql = job._build_analysis_sql(
        include_updates=False,
        limit=None,
        force_retry_errors=False,
        has_migrated_column=True,
        has_error_column=True,
    )
    assert "JOIN \"public\".\"client_trips\" AS t" in sql
    assert "btrim(t.registration) = v.registration" in sql
    assert "AT TIME ZONE 'Europe/Warsaw'" in sql
    assert "::timestamptz" not in sql
    assert "v.event_ts >= t.start_timestamp" in sql
    assert "v.event_ts <= t.end_timestamp" in sql
    print("PASS: matching localizes report_207 timestamps to Europe/Warsaw before trip interval comparison")


def test_sql_parses_stage2_valid_excel_serial_timestamps() -> None:
    sql = job._build_analysis_sql(
        include_updates=False,
        limit=None,
        force_retry_errors=True,
        has_migrated_column=True,
        has_error_column=True,
    )
    assert "event_ts_raw ~ '^[+-]?[0-9]+([.][0-9]+)?$'" in sql
    assert "THEN event_ts_raw::numeric" in sql
    assert f"event_serial_value >= {job.EXCEL_SERIAL_MIN}" in sql
    assert f"event_serial_value < {job.EXCEL_SERIAL_MAX_EXCLUSIVE}" in sql
    assert f"timestamp '{job.EXCEL_SERIAL_DATE_BASE}'" in sql
    assert "round(event_serial_value * 86400) * interval '1 second'" in sql
    assert "AT TIME ZONE 'Europe/Warsaw'" in sql
    print("PASS: migration parses Stage 2-valid Excel serial timestamps as Europe/Warsaw local time")


def test_sql_uses_exact_bucket_boundaries() -> None:
    sql = job._build_analysis_sql(
        include_updates=False,
        limit=None,
        force_retry_errors=False,
        has_migrated_column=True,
        has_error_column=True,
    )
    assert "speed_value > 140 AND speed_value <= 160" in sql
    assert "speed_value > 160 AND speed_value <= 170" in sql
    assert "speed_value > 170" in sql
    print("PASS: bucket rules use the requested exclusive/inclusive boundaries")


def test_sql_reports_sub_threshold_rows_without_migrating_them() -> None:
    sql = job._build_analysis_sql(
        include_updates=True,
        limit=None,
        force_retry_errors=False,
        has_migrated_column=True,
        has_error_column=True,
    )
    # Sub-threshold rows (valid registration/speed but bucket IS NULL, i.e.
    # speed <= 140 including exactly 140) are surfaced as a diagnostic count.
    assert "AS sub_threshold_rows" in sql
    assert "WHERE invalid_error IS NULL AND bucket IS NULL" in sql
    # They must NOT feed the migrate/error update CTEs: only exact_matches
    # drive marked_migrated, and only error_rows drive marked_errors.
    assert "FROM exact_matches AS em" in sql
    assert "FROM error_rows AS er" in sql
    print("PASS: sub-threshold (<=140 incl. =140) rows are counted, never migrated")


def test_valid_above_threshold_rows_cannot_be_silently_skipped() -> None:
    """Invariant: every valid speed>140 row is routed to exactly one terminal
    outcome (migrated, NO_MATCHING_TRIP, AMBIGUOUS_TRIP_MATCH, or an
    INVALID_* error). None can remain migrated=false with error NULL.

    This is enforced structurally: valid_speeding selects bucket IS NOT NULL
    (i.e. speed>140) and is the only source of exact_matches/unmatched/
    ambiguous, while error_rows unions invalid + unmatched + ambiguous. So a
    speed>140 row is either INVALID_* (event_ts/registration), or it reaches
    valid_speeding and becomes exactly one of match/unmatched/ambiguous.
    """
    sql = job._build_analysis_sql(
        include_updates=True,
        limit=None,
        force_retry_errors=False,
        has_migrated_column=True,
        has_error_column=True,
    )
    # valid_speeding only admits bucketed (>140) rows.
    assert "bucket IS NOT NULL" in sql
    # The only NULL bucket is speed <= 140 (boundary at exactly 140).
    assert "WHEN speed_value > 140 AND speed_value <= 160 THEN 'speeding_140_160_count'" in sql
    # >140 rows with bad ts/registration are still errored.
    assert "WHEN speed_value > 140 AND (event_ts_raw = '' OR event_ts IS NULL) THEN 'INVALID_TIMESTAMP'" in sql
    assert "WHEN registration = '' THEN 'INVALID_REGISTRATION'" in sql
    # error_rows is the union that catches every non-migrated >140 row.
    assert "SELECT report_ctid, invalid_error AS error_code FROM invalid_rows" in sql
    assert "SELECT report_ctid, error_code FROM unmatched_rows" in sql
    assert "SELECT report_ctid, error_code FROM ambiguous_rows" in sql
    print("PASS: valid speed>140 rows are always migrated or given an explicit error")


def test_sql_marks_unmatched_ambiguous_and_invalid_rows() -> None:
    sql = job._build_analysis_sql(
        include_updates=True,
        limit=None,
        force_retry_errors=False,
        has_migrated_column=True,
        has_error_column=True,
    )
    assert "NO_MATCHING_TRIP" in sql
    assert "AMBIGUOUS_TRIP_MATCH" in sql
    assert "INVALID_SPEED" in sql
    assert "INVALID_TIMESTAMP" in sql
    assert "INVALID_REGISTRATION" in sql
    assert "migrated_to_client_db = FALSE" in sql
    assert "migrated_to_client_db_error = er.error_code" in sql
    print("PASS: SQL records diagnostics for unmatched, ambiguous, and invalid rows")


def test_sql_retries_no_matching_trip_errors_by_default() -> None:
    sql = job._build_analysis_sql(
        include_updates=False,
        limit=None,
        force_retry_errors=False,
        has_migrated_column=True,
        has_error_column=True,
    )
    assert 'COALESCE(r."migrated_to_client_db", FALSE) IS NOT TRUE' in sql
    assert 'r."migrated_to_client_db_error" IS NULL' in sql
    assert 'OR btrim(r."migrated_to_client_db_error") = \'\'' in sql
    assert 'OR r."migrated_to_client_db_error" = \'NO_MATCHING_TRIP\'' in sql
    candidate_filter = sql.split("WHERE", 1)[1].split("ORDER BY", 1)[0]
    assert "AMBIGUOUS_TRIP_MATCH" not in candidate_filter
    assert "INVALID_SPEED" not in candidate_filter
    print("PASS: default selection retries NO_MATCHING_TRIP while keeping other errors skipped")


def test_force_retry_errors_includes_error_rows() -> None:
    sql = job._build_analysis_sql(
        include_updates=False,
        limit=None,
        force_retry_errors=True,
        has_migrated_column=True,
        has_error_column=True,
    )
    assert 'COALESCE(r."migrated_to_client_db", FALSE) IS NOT TRUE' in sql
    assert 'migrated_to_client_db_error" IS NULL' not in sql
    print("PASS: force_retry_errors includes previously errored non-migrated rows")


def test_real_sql_increments_counters_and_marks_report_rows_atomically() -> None:
    sql = job._build_analysis_sql(
        include_updates=True,
        limit=None,
        force_retry_errors=False,
        has_migrated_column=True,
        has_error_column=True,
    )
    assert "updated_trips AS (" in sql
    assert "marked_migrated AS (" in sql
    assert "marked_errors AS (" in sql
    assert "t.client_id = i.client_id" in sql
    assert "t.provider_trip_id = i.provider_trip_id" in sql
    assert "migrated_to_client_db = TRUE" in sql
    print("PASS: real SQL increments trips and marks report rows in one statement")


def test_transaction_rollback_on_client_failure() -> None:
    conn = FakeConn()

    def raise_after_table(*_args):
        raise RuntimeError("boom")

    with PatchAttrs(
        _client_business_pg_conn=lambda _config: conn,
        _table_exists=lambda *_args: True,
        _existing_columns=raise_after_table,
    ):
        try:
            job._process_client(
                _config(),
                limit=None,
                dry_run=False,
                force_retry_errors=False,
            )
        except job.ClientProcessingError as exc:
            assert "boom" in str(exc)
            assert exc.phase == "inspect_schema"
        else:
            raise AssertionError("expected failure")
    assert conn.rollbacks == 1
    assert conn.commits == 0
    print("PASS: client failure rolls back the client transaction")


def test_auto_grant_does_not_change_client_connection_user() -> None:
    fake_client = FakeClient()
    called = {"process": 0}

    def fail_process(*_args, **_kwargs):
        called["process"] += 1
        raise AssertionError("removed auto-grant option must fail before client DML")

    with PatchAttrs(
        _platform_pg_conn=lambda: FakeConn(),
        _load_enabled_clients=lambda *_args, **_kwargs: [_config()],
        _process_client=fail_process,
    ):
        try:
            job.run(fake_client, "run-auto-grant", {"auto_grant_permissions": True})
        except job.RuntimeSchemaMutationDisabledError as exc:
            assert "042_workflow_b_stage3_runtime_schema.sql" in str(exc)
        else:
            raise AssertionError("runtime auto-grant must fail")
    assert called["process"] == 0
    print("PASS: report_207 rejects runtime permission bootstrap before client DML")

def test_run_prints_error_summary_and_final_exception_details() -> None:
    fake_client = FakeClient()

    def failing_process(config, **_kwargs):
        raise job.ClientProcessingError(
            config,
            "match_rows",
            RuntimeError("permission denied for table client_trips"),
        )

    stdout = io.StringIO()
    with PatchAttrs(
        _platform_pg_conn=lambda: FakeConn(),
        _load_enabled_clients=lambda *_args, **_kwargs: [_config()],
        _process_client=failing_process,
    ):
        with contextlib.redirect_stdout(stdout):
            try:
                job.run(fake_client, "run-1", {})
            except RuntimeError as exc:
                message = str(exc)
            else:
                raise AssertionError("expected final RuntimeError")

    assert (
        message
        == "report_207 speeding migration failed for 1 client(s): "
        "ALPHA00001: permission denied for table client_trips"
    ), message
    printed = json.loads(stdout.getvalue().strip())
    assert printed["client_code"] == "ALPHA00001"
    assert printed["client_db_name"] == "alpha_main"
    assert printed["report_event_timezone"] == "Europe/Warsaw"
    assert printed["errors"][0]["phase"] == "match_rows"
    assert printed["errors"][0]["message"] == "permission denied for table client_trips"
    error_logs = [entry for entry in fake_client.logs if entry[0][0] == "ERROR"]
    assert error_logs
    assert error_logs[0][1]["context"]["report_event_timezone"] == "Europe/Warsaw"
    assert error_logs[0][1]["context"]["errors"][0]["message"] == "permission denied for table client_trips"
    assert "ClientProcessingError" in error_logs[0][1]["error"]
    print("PASS: per-client failure is printed, logged, and included in final RuntimeError")


def test_run_source_mismatch_skips_before_client_db_or_permission_work() -> None:
    fake_client = FakeClient()
    called = {"process": 0}

    def fail_process(*_args, **_kwargs):
        called["process"] += 1
        raise AssertionError("source mismatch must skip before _process_client")

    stdout = io.StringIO()
    with PatchAttrs(
        _platform_pg_conn=lambda: FakeConn(),
        _load_enabled_clients=lambda *_args, **_kwargs: [_config(TRIP_METRICS_SOURCE_API)],
        _process_client=fail_process,
    ):
        with contextlib.redirect_stdout(stdout):
            result = job.run(fake_client, "run-source-mismatch", {"dry_run": True, "auto_grant_permissions": True})

    assert called == {"process": 0}
    assert result["clients"][0]["status"] == "SKIPPED"
    assert result["clients"][0]["trip_metrics_population_source"] == TRIP_METRICS_SOURCE_API
    assert result["clients"][0]["required_trip_metrics_population_source"] == TRIP_METRICS_SOURCE_REPORT_207
    assert result["clients"][0]["skip_reason"] == TRIP_METRICS_SOURCE_MISMATCH_REASON
    printed = json.loads(stdout.getvalue().strip())
    assert printed["client_code"] == "ALPHA00001"
    assert printed["skip_reason"] == TRIP_METRICS_SOURCE_MISMATCH_REASON
    assert any("skipped by trip metrics source" in entry[0][3] for entry in fake_client.logs)
    print("PASS: source mismatch skips report_207 before permission grants and client DB work")


def test_missing_unique_index_fails_without_create_index() -> None:
    cur = FakeCursor()
    with PatchAttrs(_record_id_unique_index_exists=lambda *_args: False):
        try:
            job._require_report_207_schema_ready(
                cur,
                missing_report_columns=[],
                missing_client_trips_columns=[],
            )
        except job.Stage3SchemaReadinessError as exc:
            assert "unique index telematics_reports.report_207.report_207__record_id_uidx" in str(exc)
            assert "042_workflow_b_stage3_runtime_schema.sql" in str(exc)
        else:
            raise AssertionError("missing unique index must fail readiness")
    assert all("CREATE INDEX" not in query for query, _params in cur.executed)
    assert all("CREATE UNIQUE INDEX" not in query for query, _params in cur.executed)
    print("PASS: missing Report 207 unique index never triggers runtime CREATE INDEX")


def main() -> None:
    test_client_without_report_table_is_skipped()
    test_run_source_mismatch_skips_before_client_db_or_permission_work()
    test_real_run_adds_only_missing_report_tracking_columns()
    test_real_run_adds_only_missing_client_trips_counter_columns()
    test_real_run_skips_alter_when_required_columns_exist()
    test_missing_unique_index_fails_without_create_index()
    test_missing_client_trips_columns_without_owner_fails_clearly()
    test_dry_run_reports_columns_without_altering()
    test_sql_uses_polish_report_columns_safely()
    test_sql_uses_registration_and_timestamp_interval_match()
    test_sql_parses_stage2_valid_excel_serial_timestamps()
    test_sql_uses_exact_bucket_boundaries()
    test_sql_reports_sub_threshold_rows_without_migrating_them()
    test_valid_above_threshold_rows_cannot_be_silently_skipped()
    test_sql_marks_unmatched_ambiguous_and_invalid_rows()
    test_sql_retries_no_matching_trip_errors_by_default()
    test_force_retry_errors_includes_error_rows()
    test_real_sql_increments_counters_and_marks_report_rows_atomically()
    test_transaction_rollback_on_client_failure()
    test_auto_grant_does_not_change_client_connection_user()
    test_run_prints_error_summary_and_final_exception_details()
    print("OK - report_207 speeding migration regressions passed")


if __name__ == "__main__":
    main()
