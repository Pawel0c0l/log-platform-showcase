#!/usr/bin/env python3
"""Disposable-Postgres tests for ALPHA_DYSPONENT_ASSIGNMENT_CONFLICT reporting.

Fixtures cover an unambiguous group, an ambiguity group affecting many trips, two
registrations, a repeated run and a changed conflict set. Matching behaviour must
stay exactly as before: ambiguous trips are skipped and never updated.

  SUSPECTED_BUG_TEST_DSN='postgresql://loguser:...@127.0.0.1:5432/suspected_bug_test' \
      .venv/bin/python ops/tests_manual/test_alpha_dysponent_suspected_bug.py

No SMTP is configured or contacted: the outbox is inspected, never delivered.
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import psycopg
from psycopg.rows import dict_row

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

MIGRATION = REPO_ROOT / "db" / "migrations" / "052_suspected_bug_incidents_and_email_outbox.sql"
PLATFORM_BOOTSTRAP = """
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE TABLE IF NOT EXISTS runs (
  run_id UUID PRIMARY KEY, started_at TIMESTAMPTZ NOT NULL, ended_at TIMESTAMPTZ,
  status TEXT NOT NULL, trigger TEXT NOT NULL, source TEXT NOT NULL, actor TEXT,
  params JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE TABLE IF NOT EXISTS logs (
  id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ NOT NULL, level TEXT NOT NULL, type TEXT NOT NULL,
  source TEXT NOT NULL, run_id UUID REFERENCES runs(run_id) ON DELETE SET NULL,
  message TEXT NOT NULL, context JSONB NOT NULL DEFAULT '{}'::jsonb, error TEXT
);
"""

CLIENT = "9536f715-2fd0-4ffd-86ed-ba06f5490c5e"
RAW = "33333333-3333-3333-3333-333333333333"
WORKFLOW_RUN = "55555555-5555-5555-5555-555555555555"
CLEANED = "66666666-6666-6666-6666-666666666666"
RUN_ID = "44444444-4444-4444-4444-444444444444"
NOW = datetime(2026, 3, 31, 12, 0, tzinfo=timezone.utc)

AMBIGUOUS_TRIP_COUNT = 37


def _configure_platform_env(dsn: str) -> None:
    parsed = urlparse(dsn)
    os.environ.update({
        "POSTGRES_HOST": parsed.hostname or "127.0.0.1",
        "POSTGRES_PORT": str(parsed.port or 5432),
        "POSTGRES_DB": (parsed.path or "/").lstrip("/"),
        "POSTGRES_USER": parsed.username or "",
        "POSTGRES_PASSWORD": parsed.password or "",
        "LOG_PLATFORM_TARGET_ENVIRONMENT": "local_dev",
        "SUSPECTED_BUG_ALERT_TO": "platform-alerts@example.com",
        "SUSPECTED_BUG_ALERT_COOLDOWN_MINUTES": "120",
        "SUSPECTED_BUG_ALERT_REMINDER_HOURS": "24",
        # No SMTP host is configured: nothing can be delivered from this test.
        "AUTOMATION_SMTP_HOST": "",
    })


import api.suspected_bug as sb  # noqa: E402
from jobs.reports.postprocess import job_alpha00001_dysponent_id_enrichment as job  # noqa: E402


def setup(conn, *, conflicting_ids_for_bb2=("447352", "551392")) -> None:
    with conn.cursor() as cur:
        cur.execute(PLATFORM_BOOTSTRAP)
        cur.execute(MIGRATION.read_text(encoding="utf-8"))
        cur.execute("TRUNCATE suspected_bug_email_outbox, suspected_bug_occurrences, "
                    "suspected_bug_incidents RESTART IDENTITY CASCADE")
        cur.execute("DELETE FROM logs")
        cur.execute("DELETE FROM runs")
        cur.execute("INSERT INTO runs(run_id, started_at, status, trigger, source) "
                    "VALUES (%s, now(), 'RUNNING', 'MANUAL', 'fixture')", (RUN_ID,))

        cur.execute("DROP SCHEMA IF EXISTS telematics_reports CASCADE")
        cur.execute("DROP TABLE IF EXISTS public.client_trips CASCADE")
        cur.execute("DROP TABLE IF EXISTS public.eco_drivers_id_chart CASCADE")
        cur.execute("CREATE SCHEMA telematics_reports")
        cur.execute('''CREATE TABLE telematics_reports."Alpha_GPS_Baza_LOG" (
          source_id text, registration text, assignment_date date, csv_filename text,
          imported_at timestamptz, workflow_run_id uuid, raw_file_id uuid,
          source_artifact_id uuid, normalized_artifact_id uuid, cleaned_artifact_id uuid,
          source_row_number integer)''')
        cur.execute('''CREATE TABLE public.client_trips (
          client_id uuid, provider_trip_id bigint, registration text, start_timestamp timestamptz,
          trip_distance_meters integer, "Driver_Restrictions" text, "Dysponent_ID" text,
          driver_tag_description text, PRIMARY KEY(client_id, provider_trip_id))''')
        cur.execute('''CREATE TABLE public.eco_drivers_id_chart (
          client_id uuid, driver_id text, is_active boolean, PRIMARY KEY(client_id, driver_id))''')

        source_rows = [
            # Unambiguous: one id for one registration and date.
            ("100001", "AA1", "2025-02-10", "gps_baza_2025_02.csv", 11),
            # Ambiguity group 1: WZ457JN-like, two ids on the latest applicable date.
            (conflicting_ids_for_bb2[0], "BB 2", "2025-02-13", "gps_baza_2025_02.csv", 12),
            (conflicting_ids_for_bb2[1], "bb2", "2025-02-13", "gps_baza_2025_02_fix.csv", 13),
            ("300001", "BB2", "2025-01-01", "gps_baza_2025_01.csv", 14),  # older, not applicable
            # Ambiguity group 2: a different registration.
            ("400001", "CC3", "2025-02-05", "gps_baza_2025_02.csv", 15),
            ("400002", "CC3", "2025-02-05", "gps_baza_2025_02.csv", 16),
        ]
        for source_id, registration, assigned, filename, row_number in source_rows:
            cur.execute('''INSERT INTO telematics_reports."Alpha_GPS_Baza_LOG"
              VALUES (%s,%s,%s,%s,'2026-03-31 08:00+02',%s,%s,NULL,NULL,%s,%s)''',
                        (source_id, registration, assigned, filename, WORKFLOW_RUN, RAW,
                         CLEANED, row_number))

        trips: list[tuple] = [
            (CLIENT, 1, "AA1", "2026-03-20 10:00+01", 1000, None, None, None),
            (CLIENT, 2, "AA1", "2026-03-21 10:00+01", 2000, None, None, None),
        ]
        for index in range(AMBIGUOUS_TRIP_COUNT):
            trips.append((CLIENT, 100 + index, "BB2",
                          f"2026-03-{2 + index % 20:02d} 09:00+01", 3000, None, None, None))
        for index in range(3):
            trips.append((CLIENT, 200 + index, "CC3",
                          f"2026-03-{5 + index:02d} 11:00+01", 4000, None, None, None))
        cur.executemany("INSERT INTO public.client_trips VALUES (%s,%s,%s,%s,%s,%s,%s,%s)", trips)
        cur.executemany("INSERT INTO public.eco_drivers_id_chart VALUES (%s,%s,true)",
                        [(CLIENT, "100001")])
    conn.commit()


def params(**overrides) -> job.JobParams:
    values = {"date_from": "2026-03-01", "date_to": "2026-04-01", "process_all": True,
              "batch_size": 50, "max_source_age_hours": 100000}
    values.update(overrides)
    return job._parse_params(values)


def window() -> job.ResolvedWindow:
    return job.ResolvedWindow(date(2026, 3, 1), date(2026, 4, 1), date(2026, 3, 1), date(2026, 4, 1),
                              date(2026, 4, 1), date(2026, 4, 2), False, False)


def config() -> job.ClientDbConfig:
    return job.ClientDbConfig("ALPHA00001", CLIENT, "127.0.0.1", 5432, "alpha_main", "fixture", "ref")


def source_metadata() -> dict:
    return {"raw_file_id": RAW, "workflow_run_id": WORKFLOW_RUN, "cleaned_artifact_id": CLEANED,
            "source_loaded_at": datetime(2026, 3, 31, 6, 0, tzinfo=timezone.utc)}


def detect(conn, job_params: job.JobParams | None = None) -> list[dict]:
    job_params = job_params or params()
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('TimeZone', %s, true)", (job.WARSAW_TZ_NAME,))
        source_columns = job._existing_columns(cur, job.ASSIGNMENT_SCHEMA, job.ASSIGNMENT_TABLE)
        groups = job._load_ambiguity_groups(cur, config=config(), params=job_params,
                                            window=window(), source_columns=source_columns)
    conn.rollback()
    return groups


def report(conn, groups: list[dict], *, at: datetime, job_params: job.JobParams | None = None) -> list[dict]:
    return job._report_ambiguity_groups(
        groups=groups, config=config(), params=job_params or params(), window=window(),
        source=source_metadata(), runtime=None, client=None, run_id=RUN_ID, evaluated_at=at,
    )


def one(conn, sql: str, params_=None) -> dict:
    with conn.cursor() as cur:
        cur.execute(sql, params_) if params_ is not None else cur.execute(sql)
        row = cur.fetchone()
    conn.rollback()
    return dict(row or {})


def rows(conn, sql: str, params_=None) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, params_) if params_ is not None else cur.execute(sql)
        found = [dict(row) for row in cur.fetchall()]
    conn.rollback()
    return found


def test_detects_one_group_per_ambiguity_not_per_trip(conn) -> None:
    setup(conn)
    groups = {str(group["registration_norm"]): group for group in detect(conn)}

    assert set(groups) == {"BB2", "CC3"}, "one group per ambiguous registration, none for AA1"
    bb2 = groups["BB2"]
    assert bb2["affected_trip_count"] == AMBIGUOUS_TRIP_COUNT
    assert sorted(bb2["assignment_ids"]) == ["447352", "551392"]
    assert str(bb2["selected_assignment_date"]) == "2025-02-13", "the latest applicable date wins"
    assert len(bb2["sample_trip_ids"]) == job.MAX_EVIDENCE_ITEMS, "trip ids stay bounded evidence"
    assert bb2["source_row_numbers"] == [12, 13]
    assert bb2["source_csv_filenames"] == ["gps_baza_2025_02.csv", "gps_baza_2025_02_fix.csv"]
    assert bb2["source_raw_file_id"] == RAW and bb2["cleaned_artifact_id"] == CLEANED
    assert bb2["source_workflow_run_id"] == WORKFLOW_RUN
    assert groups["CC3"]["affected_trip_count"] == 3
    print("PASS: one incident group per ambiguity, unambiguous registrations excluded")


def test_two_registrations_produce_two_incidents(conn) -> None:
    setup(conn)
    reports = report(conn, detect(conn), at=NOW)

    assert len(reports) == 2 and all(item["durable_report"] for item in reports)
    assert {item["registration"] for item in reports} == {"BB2", "CC3"}
    assert len({item["fingerprint"] for item in reports}) == 2
    assert all(item["email_enqueued"] for item in reports), reports
    assert all(item["rows_modified"] == 0 for item in reports)

    incidents = rows(conn, "SELECT * FROM suspected_bug_incidents ORDER BY client_code, title")
    assert len(incidents) == 2
    assert {incident["incident_code"] for incident in incidents} == {"ALPHA_DYSPONENT_ASSIGNMENT_CONFLICT"}
    assert {incident["classification"] for incident in incidents} == {"suspected_bug"}
    assert {incident["client_code"] for incident in incidents} == {"ALPHA00001"}

    assert one(conn, "SELECT count(*)::int AS n FROM suspected_bug_email_outbox")["n"] == 2
    assert one(conn, "SELECT count(*)::int AS n FROM logs WHERE level='ERROR'")["n"] == 2

    bb2 = one(conn, "SELECT * FROM suspected_bug_email_outbox WHERE subject LIKE %s", ("%BB2%",))
    assert bb2["subject"] == (
        "[SUSPECTED_BUG][local_dev][ALPHA00001][ALPHA_DYSPONENT_ASSIGNMENT_CONFLICT] "
        "Conflicting Dysponent_ID assignments for BB2"
    ), bb2["subject"]
    for needle in ("447352", "551392", "2025-02-13", "alpha_main", "telematics_reports",
                   '"public"."client_trips"', str(AMBIGUOUS_TRIP_COUNT), RAW, CLEANED, WORKFLOW_RUN):
        assert needle in bb2["body_text"], f"missing {needle!r} in the alert body"
    assert "Rows modified: 0" in bb2["body_text"]

    payload = one(conn, "SELECT latest_payload FROM suspected_bug_incidents WHERE title LIKE %s",
                  ("%BB2%",))["latest_payload"]
    assert len(payload["evidence"]["sample_affected_trip_ids"]) <= 25, "evidence stays bounded"
    assert payload["rows_modified"] == 0 and payload["details"]["trips_skipped"] is True
    print("PASS: two registrations create two incidents with bounded, provenance-rich payloads")


def test_repeated_run_groups_and_suppresses_duplicate_email(conn) -> None:
    setup(conn)
    first = report(conn, detect(conn), at=NOW)
    second = report(conn, detect(conn), at=NOW + timedelta(minutes=20))

    assert {item["fingerprint"] for item in first} == {item["fingerprint"] for item in second}
    assert all(item["occurrence_count"] == 2 for item in second), second
    assert not any(item["email_enqueued"] for item in second)
    assert {item["email_suppression_reason"] for item in second} == {"cooldown"}

    assert one(conn, "SELECT count(*)::int AS n FROM suspected_bug_incidents")["n"] == 2
    assert one(conn, "SELECT count(*)::int AS n FROM suspected_bug_occurrences")["n"] == 4
    assert one(conn, "SELECT count(*)::int AS n FROM suspected_bug_email_outbox")["n"] == 2
    assert one(conn, "SELECT count(*)::int AS n FROM logs WHERE level='ERROR'")["n"] == 4
    print("PASS: a second run increments occurrences while the cooldown suppresses the email")


def test_changed_conflicting_ids_create_a_new_incident(conn) -> None:
    setup(conn)
    original = report(conn, detect(conn), at=NOW)
    original_bb2 = next(item for item in original if item["registration"] == "BB2")

    setup(conn, conflicting_ids_for_bb2=("447352", "999999"))
    changed = report(conn, detect(conn), at=NOW + timedelta(minutes=30))
    changed_bb2 = next(item for item in changed if item["registration"] == "BB2")

    assert changed_bb2["fingerprint"] != original_bb2["fingerprint"], \
        "a different conflict set is a different logical cause"
    assert changed_bb2["email_enqueued"] is True
    print("PASS: a changed conflict set produces a new fingerprint and a new alert")


def test_ambiguous_rows_are_never_updated_and_matching_is_unchanged(conn) -> None:
    setup(conn)
    with conn.cursor() as cur:
        cur.execute("SET TRANSACTION READ ONLY")
        cur.execute("SELECT set_config('TimeZone', %s, true)", (job.WARSAW_TZ_NAME,))
        metrics = job._analyze_scope(cur, config=config(), params=params(), window=window())
    conn.rollback()
    assert metrics["ambiguous_source_matches"] == AMBIGUOUS_TRIP_COUNT + 3
    assert metrics["deterministic_matches"] == 2

    report(conn, detect(conn), at=NOW)

    updated, batches = job._execute_batches(conn, config=config(), params=params(), window=window(),
                                            expected_source_raw_file_id=RAW, client=None, run_id=RUN_ID)
    assert updated == 2, "only the unambiguous registration is enriched"
    ambiguous = rows(conn, 'SELECT provider_trip_id, "Dysponent_ID" FROM public.client_trips '
                           "WHERE registration IN ('BB2','CC3') ORDER BY provider_trip_id")
    assert len(ambiguous) == AMBIGUOUS_TRIP_COUNT + 3
    assert all(row["Dysponent_ID"] is None for row in ambiguous), "ambiguous trips must stay untouched"
    deterministic = rows(conn, 'SELECT "Dysponent_ID" FROM public.client_trips '
                               "WHERE registration = 'AA1'")
    assert {row["Dysponent_ID"] for row in deterministic} == {"100001"}

    assert job.DEFAULT_MAX_AMBIGUOUS_MATCHES == 25, "the default ambiguity allowance must not change"
    failures = job._readiness_failures(params=params(), source={"source_rows_inspected": 6,
                                                               "raw_file_count": 1, "workflow_run_count": 1,
                                                               "cleaned_artifact_count": 1,
                                                               "source_rows_missing_provenance": 0,
                                                               "source_loaded_at": NOW - timedelta(hours=1)},
                                       window=window(), metrics=metrics, evaluated_at=NOW)
    assert any(failure["code"] == job.AMBIGUOUS_ENRICHMENT_MATCH for failure in failures), \
        "readiness still fails closed above the ambiguity allowance"
    print("PASS: reporting changes no matching decision; ambiguous trips stay untouched")


def test_durable_reporting_can_be_disabled_for_cli_inspection(conn) -> None:
    setup(conn)
    cli_params = params(report_suspected_bugs=False)
    reports = report(conn, detect(conn, cli_params), at=NOW, job_params=cli_params)

    assert len(reports) == 2 and not any(item["durable_report"] for item in reports)
    assert all(item["registration"] in {"BB2", "CC3"} for item in reports)
    assert one(conn, "SELECT count(*)::int AS n FROM suspected_bug_incidents")["n"] == 0
    assert one(conn, "SELECT count(*)::int AS n FROM suspected_bug_email_outbox")["n"] == 0
    print("PASS: --no-suspected-bug-report prints the structured incident without persisting it")


def main() -> None:
    dsn = os.environ.get("SUSPECTED_BUG_TEST_DSN")
    if not dsn:
        raise SystemExit("SUSPECTED_BUG_TEST_DSN must point to a disposable database")
    if "logdb" in dsn:
        raise SystemExit("refusing to run against logdb; use a disposable database")
    _configure_platform_env(dsn)

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        test_detects_one_group_per_ambiguity_not_per_trip(conn)
        test_two_registrations_produce_two_incidents(conn)
        test_repeated_run_groups_and_suppresses_duplicate_email(conn)
        test_changed_conflicting_ids_create_a_new_incident(conn)
        test_ambiguous_rows_are_never_updated_and_matching_is_unchanged(conn)
        test_durable_reporting_can_be_disabled_for_cli_inspection(conn)
    print("OK - ALPHA Dysponent suspected_bug integration tests passed")


if __name__ == "__main__":
    main()
