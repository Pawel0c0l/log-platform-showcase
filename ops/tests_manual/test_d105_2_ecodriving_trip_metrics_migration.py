#!/usr/bin/env python3
"""Manual regressions for D105.2 EcoDriving trip metrics migration.

Run:

    cd /opt/log-platform
    PYTHONPATH="$PWD" python3 ops/tests_manual/test_d105_2_ecodriving_trip_metrics_migration.py
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

from jobs.reports.postprocess import job_d105_2_ecodriving_trip_metrics_migration as job  # noqa: E402
from jobs.trip_metrics_population_source import (  # noqa: E402
    TRIP_METRICS_SOURCE_API,
    TRIP_METRICS_SOURCE_D105_2_ECODRIVING,
    TRIP_METRICS_SOURCE_MISMATCH_REASON,
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


class PatchTarget:
    def __init__(self, target, **attrs):
        self.target = target
        self.attrs = attrs
        self.originals = {}

    def __enter__(self):
        for name, value in self.attrs.items():
            self.originals[name] = getattr(self.target, name)
            setattr(self.target, name, value)

    def __exit__(self, exc_type, exc, tb):
        for name, value in self.originals.items():
            setattr(self.target, name, value)


class FakeClient:
    def __init__(self) -> None:
        self.logs = []

    def log(self, *args, **kwargs):
        self.logs.append((args, kwargs))


def _config(
    source: str = TRIP_METRICS_SOURCE_D105_2_ECODRIVING,
    environment: str = "local_dev",
) -> job.ClientDbConfig:
    return job.ClientDbConfig(
        client_code="ALPHA00001",
        client_id="client-id",
        client_db_host="127.0.0.1",
        client_db_port=5432,
        client_db_name="alpha_main",
        client_db_user="alpha_user",
        client_db_password_secret_ref="ENV:ALPHA_PASSWORD",
        trip_metrics_population_source=source,
        client_db_environment=environment,
        client_db_identity_id="b454f82c-5857-4bab-8342-b7258e5cf7de",
    )


def _runtime(environment="local_dev"):
    return job.environment_identity.RuntimeIdentity(
        environment=environment,
        platform_identity_id="bd7662a5-eeb4-4614-8720-d477abfcb227",
        postgres_host="127.0.0.1",
        postgres_port=5432,
        postgres_db="logdb",
        postgres_user="loguser",
    )


def _attestation(environment="local_dev"):
    return job.environment_identity.AttestedDatabaseIdentity(
        environment=environment,
        database_identity_id="bd7662a5-eeb4-4614-8720-d477abfcb227",
        database_role="platform",
        database_name="logdb",
        database_user="loguser",
        client_code=None,
    )


def _client_attestation(environment="local_dev"):
    return job.environment_identity.AttestedDatabaseIdentity(
        environment=environment,
        database_identity_id="b454f82c-5857-4bab-8342-b7258e5cf7de",
        database_role="client_business",
        database_name="alpha_main",
        database_user="alpha_user",
        client_code="ALPHA00001",
    )


def _report_columns(with_tracking: bool = True) -> set[str]:
    cols = set(job.REPORT_REQUIRED_COLUMNS) | {"record_id"}
    if with_tracking:
        cols |= set(job.REPORT_TRACKING_COLUMNS)
    return cols


def _trip_columns(with_metrics: bool = True) -> set[str]:
    cols = {"client_id", "provider_trip_id", "registration", "start_timestamp", "end_timestamp", "record_id"}
    if with_metrics:
        cols |= set(job.CLIENT_TRIPS_COUNTER_COLUMNS)
    return cols


def test_run_source_mismatch_skips_before_client_db_or_permission_work() -> None:
    fake_client = FakeClient()
    called = {"process": 0, "grant": 0}

    def fail_process(*_args, **_kwargs):
        called["process"] += 1
        raise AssertionError("source mismatch must skip before _process_client")

    def fail_grant(*_args, **_kwargs):
        called["grant"] += 1
        raise AssertionError("source mismatch must skip before auto-grant")

    stdout = io.StringIO()
    with PatchAttrs(
        _platform_pg_conn=lambda: FakeConn(),
        _load_enabled_clients=lambda *_args, **_kwargs: [_config(TRIP_METRICS_SOURCE_API)],
        _process_client=fail_process,
        _ensure_stage3_permissions_for_client_db=fail_grant,
    ), PatchTarget(
        job.environment_identity,
        load_runtime_identity=lambda: _runtime(),
        require_clean_production_worktree=lambda *_args, **_kwargs: None,
        attest_platform_identity=lambda *_args, **_kwargs: _attestation(),
    ):
        with contextlib.redirect_stdout(stdout):
            result = job.run(fake_client, "run-source-mismatch", {"dry_run": True, "auto_grant_permissions": True})

    assert called == {"process": 0, "grant": 0}
    assert result["clients"][0]["status"] == "SKIPPED"
    assert result["clients"][0]["trip_metrics_population_source"] == TRIP_METRICS_SOURCE_API
    assert result["clients"][0]["required_trip_metrics_population_source"] == TRIP_METRICS_SOURCE_D105_2_ECODRIVING
    assert result["clients"][0]["skip_reason"] == TRIP_METRICS_SOURCE_MISMATCH_REASON
    printed = json.loads(stdout.getvalue().strip())
    assert printed["client_code"] == "ALPHA00001"
    assert printed["skip_reason"] == TRIP_METRICS_SOURCE_MISMATCH_REASON
    print("PASS: source mismatch skips D105.2 migration before permission grants and client DB work")


def test_dry_run_reports_counts_and_does_not_alter() -> None:
    conn = FakeConn()

    def existing_columns(_cur, schema, table):
        if schema == job.REPORT_SCHEMA and table == job.REPORT_TABLE:
            return _report_columns(with_tracking=False)
        return _trip_columns(with_metrics=False)

    with PatchAttrs(
        _client_business_pg_conn=lambda _config: conn,
        _table_exists=lambda *_args: True,
        _existing_columns=existing_columns,
        _analyze_report_rows=lambda *_args, **_kwargs: {
            "candidate_rows": 7,
            "non_zero_metric_rows": 4,
            "zero_metric_rows": 1,
            "exact_match_rows": 1,
            "minute_fallback_match_rows": 1,
            "rounded_fallback_match_rows": 1,
            "matched_rows": 3,
            "unmatched_rows": 1,
            "ambiguous_rows": 1,
            "invalid_registration_rows": 1,
            "invalid_timestamp_rows": 1,
            "invalid_metric_rows": 1,
            "rows_incrementing_140_160": 1,
            "rows_incrementing_160_170": 1,
            "rows_incrementing_170_plus": 1,
            "rows_incrementing_overrev": 1,
            "incremented_140_160": 3,
            "incremented_160_170": 4,
            "incremented_170_plus": 5,
            "incremented_overrev": 2,
        },
    ):
        summary = job._process_client(_config(), limit=10, dry_run=True, force_retry_errors=False)
    assert set(summary.columns_to_add_report_d105_2_ecodriving) == set(job.REPORT_TRACKING_COLUMNS)
    assert set(summary.columns_to_add_client_trips) == set(job.CLIENT_TRIPS_COUNTER_COLUMNS)
    assert all("ALTER TABLE" not in query for query, _params in conn.cursor_obj.executed)
    assert summary.candidate_rows == 7
    assert summary.non_zero_metric_rows == 4
    assert summary.exact_match_rows == 1
    assert summary.minute_fallback_match_rows == 1
    assert summary.rounded_fallback_match_rows == 1
    assert summary.matched_rows == 3
    assert summary.migrated_rows == 0
    assert summary.status == "WARNING"
    print("PASS: D105.2 dry-run reports match categories and performs no DDL/DML")


def test_real_run_adds_only_d105_and_needed_metric_columns() -> None:
    cur = FakeCursor()
    with PatchAttrs(_current_user_owns_table=lambda *_args: True):
        job._ensure_client_trips_counter_columns(cur, ["overrev_events_count"])
    sql = "\n".join(query for query, _params in cur.executed)
    assert 'ADD COLUMN IF NOT EXISTS "overrev_events_count" INTEGER NOT NULL DEFAULT 0' in sql
    assert "high_rpm_events_count" not in sql
    print("PASS: D105.2 migration can add overrev counter without adding high_rpm")


def test_sql_uses_required_report_table_and_columns() -> None:
    sql = job._build_analysis_sql(
        include_updates=False,
        limit=5,
        force_retry_errors=False,
        has_migrated_column=True,
        has_error_column=True,
    )
    assert 'FROM "telematics_reports"."report_d105_2_ecodriving" AS r' in sql
    for column in job.REPORT_REQUIRED_COLUMNS:
        assert f'"{column}"' in sql, column
    print("PASS: D105.2 SQL quotes required report table and columns")


def test_sql_uses_exact_registration_start_end_match() -> None:
    sql = job._build_analysis_sql(
        include_updates=False,
        limit=None,
        force_retry_errors=False,
        has_migrated_column=True,
        has_error_column=True,
    )
    assert 'exact_trip_matches AS (' in sql
    assert 'JOIN "public"."client_trips" AS t' in sql
    assert "btrim(t.registration) = m.registration" in sql
    assert "t.start_timestamp = m.start_ts" in sql
    assert "t.end_timestamp = m.end_ts" in sql
    assert "AT TIME ZONE 'Europe/Warsaw'" in sql
    print("PASS: D105.2 exact registration/start/end matching remains first-class")


def test_sql_uses_minute_fallback_without_broad_tolerance() -> None:
    sql = job._build_analysis_sql(
        include_updates=False,
        limit=None,
        force_retry_errors=False,
        has_migrated_column=True,
        has_error_column=True,
    )
    assert "minute_trip_matches AS (" in sql
    assert "date_trunc('minute', t.start_timestamp) = date_trunc('minute', m.start_ts)" in sql
    assert "date_trunc('minute', t.end_timestamp) = date_trunc('minute', m.end_ts)" in sql
    assert "WHERE COALESCE(emt.match_count, 0) = 0" in sql
    minute_block = sql.split("minute_trip_matches AS (", 1)[1].split("),\nminute_match_totals", 1)[0]
    assert "BETWEEN" not in minute_block
    assert "interval '" not in minute_block
    assert "ORDER BY" not in minute_block
    print("PASS: D105.2 minute fallback uses exact minute equality, not tolerance or nearest-trip matching")


def test_sql_uses_rounded_fallback_with_bounded_60s_window_only() -> None:
    sql = job._build_analysis_sql(
        include_updates=False,
        limit=None,
        force_retry_errors=False,
        has_migrated_column=True,
        has_error_column=True,
    )
    assert "rounded_trip_matches AS (" in sql
    rounded_block = sql.split("rounded_trip_matches AS (", 1)[1].split("),\nrounded_match_totals", 1)[0]
    assert "btrim(t.registration) = m.registration" in rounded_block
    assert "abs(EXTRACT(EPOCH FROM (t.start_timestamp - m.start_ts))) <= 60" in rounded_block
    assert "abs(EXTRACT(EPOCH FROM (t.end_timestamp - m.end_ts))) <= 60" in rounded_block
    assert "<= 300" not in rounded_block
    assert "ORDER BY" not in rounded_block
    assert "LIMIT" not in rounded_block
    assert "date_trunc('day'" not in rounded_block
    assert "::date" not in rounded_block
    print("PASS: D105.2 rounded fallback is bounded to +/-60s on both endpoints")


def test_sql_rounded_fallback_uses_exactly_one_candidate_and_reports_count() -> None:
    sql = job._build_analysis_sql(
        include_updates=False,
        limit=None,
        force_retry_errors=False,
        has_migrated_column=True,
        has_error_column=True,
    )
    assert "rounded_match_totals AS (" in sql
    assert "rounded_fallback_matches AS (" in sql
    assert "JOIN rounded_match_totals AS rmt ON rmt.report_ctid = rtm.report_ctid" in sql
    assert "WHERE rmt.match_count = 1" in sql
    assert "(SELECT COUNT(*) FROM rounded_fallback_matches)::integer AS rounded_fallback_match_rows" in sql
    print("PASS: D105.2 rounded fallback selects only exactly one bounded candidate and reports its counter")


def test_sql_rounded_fallback_rejects_outside_60s_by_construction() -> None:
    sql = job._build_analysis_sql(
        include_updates=False,
        limit=None,
        force_retry_errors=False,
        has_migrated_column=True,
        has_error_column=True,
    )
    rounded_block = sql.split("rounded_trip_matches AS (", 1)[1].split("),\nrounded_match_totals", 1)[0]
    assert "abs(EXTRACT(EPOCH FROM (t.start_timestamp - m.start_ts))) <= 60" in rounded_block
    assert "abs(EXTRACT(EPOCH FROM (t.end_timestamp - m.end_ts))) <= 60" in rounded_block
    assert "OR" not in rounded_block
    print("PASS: D105.2 rounded fallback cannot match outside +/-60s on either endpoint")


def test_sql_rejects_wrong_minute_and_date_only_matching() -> None:
    sql = job._build_analysis_sql(
        include_updates=False,
        limit=None,
        force_retry_errors=False,
        has_migrated_column=True,
        has_error_column=True,
    )
    minute_block = sql.split("minute_trip_matches AS (", 1)[1].split("),\nminute_match_totals", 1)[0]
    assert "date_trunc('minute'" in minute_block
    assert "date_trunc('day'" not in minute_block
    assert "::date" not in minute_block
    assert "date(" not in minute_block.lower()
    print("PASS: D105.2 minute fallback cannot match wrong-minute or date-only candidates")


def test_sql_fallback_ambiguity_does_not_increment() -> None:
    sql = job._build_analysis_sql(
        include_updates=True,
        limit=None,
        force_retry_errors=False,
        has_migrated_column=True,
        has_error_column=True,
    )
    assert "minute_match_totals AS (" in sql
    assert "FROM minute_match_totals AS mt\n    WHERE mt.match_count > 1" in sql
    assert "minute_fallback_matches AS (" in sql
    assert "WHERE mmt.match_count = 1" in sql
    assert "rounded_match_totals AS (" in sql
    assert "FROM rounded_match_totals AS mt\n    WHERE mt.match_count > 1" in sql
    assert "rounded_fallback_matches AS (" in sql
    assert "WHERE rmt.match_count = 1" in sql
    assert "FROM selected_matches" in sql
    assert "marked_errors AS (" in sql
    assert "migrated_to_client_db_error = er.error_code" in sql
    print("PASS: ambiguous minute or rounded fallback rows are errors and cannot increment trip counters")


def test_sql_exact_match_precedence_over_minute_and_rounded_candidates() -> None:
    sql = job._build_analysis_sql(
        include_updates=False,
        limit=None,
        force_retry_errors=False,
        has_migrated_column=True,
        has_error_column=True,
    )
    exact_pos = sql.index("exact_trip_matches AS (")
    minute_pos = sql.index("minute_trip_matches AS (")
    rounded_pos = sql.index("rounded_trip_matches AS (")
    assert exact_pos < minute_pos < rounded_pos
    minute_block = sql.split("minute_trip_matches AS (", 1)[1].split("),\nminute_match_totals", 1)[0]
    assert "LEFT JOIN exact_match_totals AS emt ON emt.report_ctid = m.report_ctid" in minute_block
    assert "WHERE COALESCE(emt.match_count, 0) = 0" in minute_block
    rounded_block = sql.split("rounded_trip_matches AS (", 1)[1].split("),\nrounded_match_totals", 1)[0]
    assert "LEFT JOIN exact_match_totals AS emt ON emt.report_ctid = m.report_ctid" in rounded_block
    assert "LEFT JOIN minute_match_totals AS mmt ON mmt.report_ctid = m.report_ctid" in rounded_block
    assert "WHERE COALESCE(emt.match_count, 0) = 0\n      AND COALESCE(mmt.match_count, 0) = 0" in rounded_block
    assert "SELECT * FROM exact_matches\n    UNION ALL\n    SELECT * FROM minute_fallback_matches\n    UNION ALL\n    SELECT * FROM rounded_fallback_matches" in sql
    print("PASS: exact D105.2 matches take precedence over minute fallback, which precedes rounded fallback")


def test_sql_maps_direct_buckets_and_overrev_only() -> None:
    sql = job._build_analysis_sql(
        include_updates=True,
        limit=None,
        force_retry_errors=False,
        has_migrated_column=True,
        has_error_column=True,
    )
    assert "SUM(speed_140_160_count)::integer AS inc_140_160" in sql
    assert "SUM(speed_160_170_count)::integer AS inc_160_170" in sql
    assert "SUM(speed_170_plus_count)::integer AS inc_170_plus" in sql
    assert "SUM(overrev_count)::integer AS inc_overrev" in sql
    assert "speeding_140_160_count = COALESCE(t.speeding_140_160_count, 0) + i.inc_140_160" in sql
    assert "speeding_160_170_count = COALESCE(t.speeding_160_170_count, 0) + i.inc_160_170" in sql
    assert "speeding_170_plus_count = COALESCE(t.speeding_170_plus_count, 0) + i.inc_170_plus" in sql
    assert "overrev_events_count = COALESCE(t.overrev_events_count, 0) + i.inc_overrev" in sql
    assert "high_rpm_events_count" not in sql
    assert "inc_160_170 -" not in sql and "inc_170_plus -" not in sql
    print("PASS: D105.2 SQL maps direct non-cumulative buckets and overrev only")


def test_sql_marks_all_error_taxonomy() -> None:
    sql = job._build_analysis_sql(
        include_updates=True,
        limit=None,
        force_retry_errors=False,
        has_migrated_column=True,
        has_error_column=True,
    )
    for code in [
        "NO_MATCHING_TRIP",
        "AMBIGUOUS_TRIP_MATCH",
        "INVALID_REGISTRATION",
        "INVALID_TIMESTAMP",
        "INVALID_METRIC_COUNTS",
    ]:
        assert code in sql, code
    assert "migrated_to_client_db_error = er.error_code" in sql
    print("PASS: D105.2 SQL records the required error taxonomy")


def test_sql_retries_no_matching_trip_only_by_default_and_is_idempotent() -> None:
    sql = job._build_analysis_sql(
        include_updates=False,
        limit=None,
        force_retry_errors=False,
        has_migrated_column=True,
        has_error_column=True,
    )
    assert 'COALESCE(r."migrated_to_client_db", FALSE) IS NOT TRUE' in sql
    assert 'OR r."migrated_to_client_db_error" = \'NO_MATCHING_TRIP\'' in sql
    candidate_filter = sql.split("WHERE", 1)[1].split("ORDER BY", 1)[0]
    assert "AMBIGUOUS_TRIP_MATCH" not in candidate_filter
    assert "INVALID_REGISTRATION" not in candidate_filter
    assert "INVALID_TIMESTAMP" not in candidate_filter
    assert "INVALID_METRIC_COUNTS" not in candidate_filter
    print("PASS: default selection is idempotent and retries only NO_MATCHING_TRIP errors")


def test_real_sql_marks_exact_and_zero_rows_migrated() -> None:
    sql = job._build_analysis_sql(
        include_updates=True,
        limit=None,
        force_retry_errors=False,
        has_migrated_column=True,
        has_error_column=True,
    )
    assert "updated_trips AS (" in sql
    assert "marked_migrated AS (" in sql
    assert "marked_zero_metric_rows AS (" in sql
    assert "marked_errors AS (" in sql
    assert "FROM selected_matches AS em" in sql
    assert "FROM zero_metric_rows AS z" in sql
    print("PASS: real SQL increments selected exact/minute/rounded matches and marks zero-metric rows processed")


def test_guarded_local_dry_run_is_accepted() -> None:
    fake_client = FakeClient()
    called = {"process": 0}

    def process(config, **kwargs):
        called["process"] += 1
        return job.ClientSummary(
            client_code=config.client_code,
            client_db_name=config.client_db_name,
            dry_run=True,
            status="OK",
        )

    with PatchAttrs(
        _platform_pg_conn=lambda: FakeConn(),
        _load_enabled_clients=lambda *_args, **_kwargs: [_config()],
        _attest_client_environment=lambda *_args, **_kwargs: _client_attestation(),
        _process_client=process,
    ), PatchTarget(
        job.environment_identity,
        load_runtime_identity=lambda: _runtime(),
        require_clean_production_worktree=lambda *_args, **_kwargs: None,
        attest_platform_identity=lambda *_args, **_kwargs: _attestation(),
    ):
        result = job.run(fake_client, "guarded-local-dry-run", {"client_code": "ALPHA00001", "dry_run": True})
    assert called["process"] == 1
    assert result["clients"][0]["status"] == "OK"


def test_production_dry_run_requires_and_accepts_all_identity_checks() -> None:
    fake_client = FakeClient()
    called = {"process": 0, "attest": 0}

    def attest(*_args, **_kwargs):
        called["attest"] += 1
        return _client_attestation("production")

    def process(config, **kwargs):
        called["process"] += 1
        return job.ClientSummary(
            client_code=config.client_code,
            client_db_name=config.client_db_name,
            dry_run=True,
            status="OK",
        )

    with PatchAttrs(
        _platform_pg_conn=lambda: FakeConn(),
        _load_enabled_clients=lambda *_args, **_kwargs: [_config(environment="production")],
        _attest_client_environment=attest,
        _process_client=process,
    ), PatchTarget(
        job.environment_identity,
        load_runtime_identity=lambda: _runtime("production"),
        require_clean_production_worktree=lambda *_args, **_kwargs: None,
        attest_platform_identity=lambda *_args, **_kwargs: _attestation("production"),
    ):
        result = job.run(fake_client, "guarded-production-dry-run", {"client_code": "ALPHA00001", "dry_run": True})
    assert called == {"process": 1, "attest": 1}
    assert result["clients"][0]["status"] == "OK"


def test_production_write_refuses_missing_confirmation_before_processing() -> None:
    fake_client = FakeClient()
    called = {"process": 0}

    def process(*_args, **_kwargs):
        called["process"] += 1
        raise AssertionError("production write must not process without confirmation")

    with PatchAttrs(
        _platform_pg_conn=lambda: FakeConn(),
        _load_enabled_clients=lambda *_args, **_kwargs: [_config(environment="production")],
        _attest_client_environment=lambda *_args, **_kwargs: _client_attestation("production"),
        _process_client=process,
    ), PatchTarget(
        job.environment_identity,
        load_runtime_identity=lambda: _runtime("production"),
        require_clean_production_worktree=lambda *_args, **_kwargs: None,
        attest_platform_identity=lambda *_args, **_kwargs: _attestation("production"),
    ):
        try:
            job.run(fake_client, "guarded-production-write", {"client_code": "ALPHA00001"})
        except RuntimeError as exc:
            assert "PRODUCTION_WRITE_CONFIRMATION_MISSING" in str(exc)
        else:
            raise AssertionError("missing production confirmation must fail")
    assert called["process"] == 0


def main() -> None:
    test_run_source_mismatch_skips_before_client_db_or_permission_work()
    test_dry_run_reports_counts_and_does_not_alter()
    test_real_run_adds_only_d105_and_needed_metric_columns()
    test_sql_uses_required_report_table_and_columns()
    test_sql_uses_exact_registration_start_end_match()
    test_sql_uses_minute_fallback_without_broad_tolerance()
    test_sql_uses_rounded_fallback_with_bounded_60s_window_only()
    test_sql_rounded_fallback_uses_exactly_one_candidate_and_reports_count()
    test_sql_rounded_fallback_rejects_outside_60s_by_construction()
    test_sql_rejects_wrong_minute_and_date_only_matching()
    test_sql_fallback_ambiguity_does_not_increment()
    test_sql_exact_match_precedence_over_minute_and_rounded_candidates()
    test_sql_maps_direct_buckets_and_overrev_only()
    test_sql_marks_all_error_taxonomy()
    test_sql_retries_no_matching_trip_only_by_default_and_is_idempotent()
    test_real_sql_marks_exact_and_zero_rows_migrated()
    test_guarded_local_dry_run_is_accepted()
    test_production_dry_run_requires_and_accepts_all_identity_checks()
    test_production_write_refuses_missing_confirmation_before_processing()
    print("OK - D105.2 EcoDriving migration regressions passed")


if __name__ == "__main__":
    main()
