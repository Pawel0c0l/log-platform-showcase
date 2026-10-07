#!/usr/bin/env python3
"""Service-free regressions for automated ALPHA Dysponent_ID enrichment."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from jobs.reports.postprocess import job_alpha00001_dysponent_id_enrichment as job

WARSAW = ZoneInfo("Europe/Warsaw")


def config() -> job.ClientDbConfig:
    return job.ClientDbConfig(
        "ALPHA00001", "client-1", "db", 5432, "alpha_main", "user", "secret",
        client_db_environment="local_dev",
        client_db_identity_id="00000000-0000-0000-0000-000000000001",
    )


def source(loaded_at: datetime | None = None) -> dict:
    return {
        "source_rows_inspected": 100,
        "blank_source_assignments": 2,
        "invalid_source_rows": 1,
        "raw_file_count": 1,
        "workflow_run_count": 1,
        "cleaned_artifact_count": 1,
        "source_rows_missing_provenance": 0,
        "raw_file_id": "raw-1",
        "cleaned_artifact_id": "artifact-1",
        "workflow_run_id": "run-1",
        "source_loaded_at": loaded_at or datetime(2026, 7, 23, 20, tzinfo=WARSAW),
        "source_business_date_min": date(2020, 1, 1),
        "source_business_date_max": date(2026, 7, 21),
    }


def metrics(**updates) -> dict:
    values = {
        "target_trips_in_scope": 100,
        "trips_requiring_dysponent_fallback": 60,
        "trips_with_usable_existing_dysponent": 5,
        "trips_that_would_gain_dysponent": 54,
        "ambiguous_source_matches": 1,
        "predicted_trip_coverage_percent": "99.00",
        "predicted_distance_coverage_percent": "99.50",
    }
    values.update(updates)
    return values


def window() -> job.ResolvedWindow:
    return job.ResolvedWindow(
        date(2026, 7, 21), None, date(2026, 7, 21), date(2026, 7, 23),
        date(2026, 7, 23), date(2026, 7, 27), True, True,
    )


def test_parameter_contract_and_client_isolation() -> None:
    parsed = job._parse_params({"date_from": "2026-07-21"})
    assert parsed.dry_run is True and parsed.overwrite_existing is False
    assert parsed.min_coverage_percent == Decimal("95.00")
    assert parsed.max_ambiguous_matches == 25
    for payload in (
        {"date_from": "2026-07-21", "force": True},
        {"date_from": "2026-07-21", "dry_run": "yes"},
        {"date_from": "2026-07-21", "unknown": 1},
    ):
        try:
            job._parse_params(payload)
        except (ValueError, job.EnrichmentPreconditionError):
            pass
        else:
            raise AssertionError(f"unsafe parameters accepted: {payload}")
    try:
        job._validate_client_code_allowed("BRAVO00016")
    except job.EnrichmentPreconditionError as exc:
        assert exc.code == job.UNSUPPORTED_CLIENT
    else:
        raise AssertionError("non-ALPHA client accepted")


def test_inclusive_start_exclusive_end_and_warsaw_sql() -> None:
    params = job._parse_params({"date_from": "2026-03-28", "date_to": "2026-03-31"})
    sql, values = job._scope_ctes(params=params)
    assert ">= (%(start_date)s::date::timestamp AT TIME ZONE %(timezone)s)" in sql
    assert "< (%(end_date)s::date::timestamp AT TIME ZONE %(timezone)s)" in sql
    assert "start_timestamp AT TIME ZONE %(timezone)s" in sql
    assert values["timezone"] == "Europe/Warsaw"
    assert job._optional_date("2024-02-29", "date") == date(2024, 2, 29)
    spring_start = datetime(2026, 3, 29, 0, tzinfo=WARSAW)
    spring_end = datetime(2026, 3, 30, 0, tzinfo=WARSAW)
    autumn_start = datetime(2026, 10, 25, 0, tzinfo=WARSAW)
    autumn_end = datetime(2026, 10, 26, 0, tzinfo=WARSAW)
    assert spring_end.astimezone(timezone.utc) - spring_start.astimezone(timezone.utc) == timedelta(hours=23)
    assert autumn_end.astimezone(timezone.utc) - autumn_start.astimezone(timezone.utc) == timedelta(hours=25)


def test_injectable_clock_and_readiness_categories() -> None:
    instant = datetime(2026, 7, 23, 20, 0, 1, tzinfo=WARSAW)
    assert job._aware_now(lambda: instant) is instant
    try:
        job._aware_now(lambda: datetime(2026, 7, 23, 20))
    except ValueError:
        pass
    else:
        raise AssertionError("naive clock accepted")
    params = job._parse_params({"date_from": "2026-07-21"})
    assert job._readiness_failures(
        params=params, source=source(), window=window(), metrics=metrics(), evaluated_at=instant,
    ) == []
    assert job._readiness_failures(
        params=params, source=source(datetime(2026, 7, 20, tzinfo=WARSAW)),
        window=window(), metrics=metrics(), evaluated_at=instant,
    ) == []
    failures = job._readiness_failures(
        params=job._parse_params({"date_from": "2026-07-21", "require_fresh_source": True}),
        source=source(datetime(2026, 7, 20, tzinfo=WARSAW)),
        window=window(), metrics=metrics(), evaluated_at=instant,
    )
    assert job.SOURCE_REPORT_NOT_READY in {item["code"] for item in failures}
    failures = job._readiness_failures(
        params=job._parse_params({"date_from": "2026-07-21", "max_ambiguous_matches": 0}),
        source=source(), window=window(), metrics=metrics(ambiguous_source_matches=1),
        evaluated_at=instant,
    )
    assert job.AMBIGUOUS_ENRICHMENT_MATCH in {item["code"] for item in failures}
    failures = job._readiness_failures(
        params=params, source=source(), window=window(),
        metrics=metrics(predicted_trip_coverage_percent="94.99"), evaluated_at=instant,
    )
    assert job.COVERAGE_BELOW_THRESHOLD in {item["code"] for item in failures}
    failures = job._readiness_failures(
        params=params, source={**source(), "source_rows_inspected": 0},
        window=window(), metrics=metrics(), evaluated_at=instant,
    )
    assert failures[0]["code"] == job.SOURCE_REPORT_EMPTY


class WindowCursor:
    def execute(self, sql, params=()):
        self.row = {"latest_trip_timestamp": datetime(2026, 7, 26, 23, tzinfo=WARSAW)}
    def fetchone(self):
        return self.row


def test_window_resolution_caps_end_and_rejects_empty() -> None:
    params = job._parse_params({
        "date_from": "2026-07-21", "date_to": "2026-07-30", "require_fresh_source": True,
    })
    resolved = job._resolve_window(WindowCursor(), config=config(), params=params, source=source())
    assert resolved.start == date(2026, 7, 21)
    assert resolved.end_exclusive == date(2026, 7, 23)
    assert resolved.capped_to_source is True
    trips_only = job._resolve_window(
        WindowCursor(), config=config(),
        params=job._parse_params({"date_from": "2026-07-21", "date_to": "2026-07-30"}),
        source=source(),
    )
    assert trips_only.end_exclusive == date(2026, 7, 27) and trips_only.capped_to_trips is True
    try:
        job._resolve_window(
            WindowCursor(), config=config(),
            params=job._parse_params({"date_from": "2026-07-23", "require_fresh_source": True}),
            source=source(),
        )
    except job.EnrichmentPreconditionError as exc:
        assert exc.code == job.INVALID_DATE_RANGE
    else:
        raise AssertionError("empty resolved window accepted")


def test_stale_source_enriches_by_default_and_stays_observable() -> None:
    instant = datetime(2026, 7, 30, 12, tzinfo=WARSAW)
    stale = source(datetime(2026, 7, 23, 20, tzinfo=WARSAW))
    default = job._parse_params({"date_from": "2026-07-21", "date_to": "2026-07-27"})
    strict = job._parse_params({
        "date_from": "2026-07-21", "date_to": "2026-07-27", "require_fresh_source": True,
    })
    assert default.require_fresh_source is False

    extended = job._resolve_window(WindowCursor(), config=config(), params=default, source=stale)
    assert extended.end_exclusive == date(2026, 7, 27) and extended.capped_to_source is False
    assert extended.source_boundary_exclusive == date(2026, 7, 23)
    capped = job._resolve_window(WindowCursor(), config=config(), params=strict, source=stale)
    assert capped.end_exclusive == date(2026, 7, 23) and capped.capped_to_source is True

    assert job._readiness_failures(
        params=default, source=stale, window=extended, metrics=metrics(), evaluated_at=instant,
    ) == []
    blocked = job._readiness_failures(
        params=strict, source=stale, window=extended, metrics=metrics(), evaluated_at=instant,
    )
    assert job.SOURCE_REPORT_NOT_READY in {item["code"] for item in blocked}

    future = job._readiness_failures(
        params=default, source=source(datetime(2026, 7, 31, tzinfo=WARSAW)),
        window=extended, metrics=metrics(), evaluated_at=instant,
    )
    assert job.SOURCE_REPORT_NOT_READY in {item["code"] for item in future}
    ambiguous = job._readiness_failures(
        params=job._parse_params({
            "date_from": "2026-07-21", "date_to": "2026-07-27", "max_ambiguous_matches": 0,
        }),
        source=stale, window=extended, metrics=metrics(ambiguous_source_matches=1),
        evaluated_at=instant,
    )
    assert job.AMBIGUOUS_ENRICHMENT_MATCH in {item["code"] for item in ambiguous}
    summary = job._summary(
        config=config(), params=default, source=stale, window=extended, metrics=metrics(),
        failures=[], evaluated_at=instant, rows_updated=0, batches_committed=0,
    )
    assert summary["require_fresh_source"] is False
    assert summary["enriched_beyond_source_boundary"] is True
    assert job._summary(
        config=config(), params=strict, source=stale, window=capped, metrics=metrics(),
        failures=[], evaluated_at=instant, rows_updated=0, batches_committed=0,
    )["enriched_beyond_source_boundary"] is False


class SqlCursor:
    def __init__(self):
        self.sql = ""
        self.params = None
    def execute(self, sql, params=()):
        self.sql = sql
        self.params = params
    def fetchone(self):
        return {"updated_count": 0}


def test_update_sql_is_idempotent_non_overwriting_and_ambiguity_safe() -> None:
    cur = SqlCursor()
    params = job._parse_params({"date_from": "2026-07-21", "process_all": True})
    job._update_batch(cur, config=config(), params=params, window=window(), batch_size=100)
    assert "ORDER BY sg.assignment_date DESC LIMIT 1" in cur.sql
    assert "ON cardinality(chosen.assignment_ids) = 1" in cur.sql
    assert "SKIP LOCKED" in cur.sql
    assert cur.params["overwrite_existing"] is False
    assert 'SET "Dysponent_ID" = candidates.proposed_dysponent_id' in cur.sql
    params = job._parse_params({
        "date_from": "2026-07-21", "overwrite_existing": True,
    })
    job._update_batch(cur, config=config(), params=params, window=window(), batch_size=100)
    assert cur.params["overwrite_existing"] is True


class FakeCursor:
    def __init__(self, conn): self.conn = conn; self.row = None
    def __enter__(self): return self
    def __exit__(self, *_args): return False
    def execute(self, sql, params=()): self.conn.sql.append(sql)
    def fetchone(self): return self.row
    def fetchall(self): return []


class FakeConn:
    def __init__(self): self.commits = 0; self.rollbacks = 0; self.closed = False; self.sql = []
    def cursor(self): return FakeCursor(self)
    def commit(self): self.commits += 1
    def rollback(self): self.rollbacks += 1
    def close(self): self.closed = True


def test_dry_run_never_reaches_update_batches() -> None:
    conn = FakeConn()
    params = job._parse_params({"date_from": "2026-07-21", "dry_run": True})
    originals = {name: getattr(job, name) for name in (
        "_client_business_pg_conn", "attest_client_identity", "_existing_columns",
        "_load_source_metadata", "_resolve_window", "_analyze_scope", "_execute_batches",
    )}
    try:
        job._client_business_pg_conn = lambda _config: conn
        job.attest_client_identity = lambda *_a, **_k: None
        job._existing_columns = lambda *_a, **_k: (
            set(job.ASSIGNMENT_REQUIRED_COLUMNS)
            if _a[1] == job.ASSIGNMENT_SCHEMA
            else {*job.CLIENT_TRIPS_PK_COLUMNS, job.CLIENT_TRIPS_REGISTRATION_COLUMN,
                  job.CLIENT_TRIPS_START_COLUMN, job.CLIENT_TRIPS_TARGET_COLUMN,
                  "Driver_Restrictions", "driver_tag_description", "trip_distance_meters"}
        )
        job._load_source_metadata = lambda _cur: source()
        job._resolve_window = lambda *_a, **_k: window()
        job._analyze_scope = lambda *_a, **_k: metrics()
        job._execute_batches = lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("DML reached"))
        summary = job._process_client(
            config(), params, runtime=object(),
            now_fn=lambda: datetime(2026, 7, 23, 20, 1, tzinfo=WARSAW),
        )
    finally:
        for name, value in originals.items(): setattr(job, name, value)
    assert summary["dry_run"] is True and summary["rows_updated"] == 0
    assert conn.commits == 0 and conn.rollbacks >= 2 and conn.closed


def test_schema_and_migration_prerequisites_remain_additive() -> None:
    root = Path(__file__).resolve().parents[2]
    sql24 = (root / "db/client_business/024_alpha00001_client_trips_dysponent_id.sql").read_text()
    sql25 = (root / "db/client_business/025_alpha00001_dysponent_id_batch_indexes.sql").read_text()
    assert 'ADD COLUMN IF NOT EXISTS "Dysponent_ID" TEXT NULL' in sql24
    assert "idx_client_trips_dysponent_id_pending_window" in sql25


def main() -> None:
    test_parameter_contract_and_client_isolation()
    test_inclusive_start_exclusive_end_and_warsaw_sql()
    test_injectable_clock_and_readiness_categories()
    test_window_resolution_caps_end_and_rejects_empty()
    test_stale_source_enriches_by_default_and_stays_observable()
    test_update_sql_is_idempotent_non_overwriting_and_ambiguity_safe()
    test_dry_run_never_reaches_update_batches()
    test_schema_and_migration_prerequisites_remain_additive()
    print("OK - ALPHA Dysponent automated enrichment unit regressions passed")


if __name__ == "__main__":
    main()
