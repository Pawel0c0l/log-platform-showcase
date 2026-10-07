#!/usr/bin/env python3
"""Transactional DB checks for migration 043 and physical-person behavior."""

from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path
import sys

from dotenv import load_dotenv
import psycopg
from psycopg.rows import dict_row

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
load_dotenv(REPO_ROOT / ".env", override=False)

from jobs.ecodriving_person.email_idempotency import reserve_send  # noqa: E402
from jobs.ecodriving_person.job_eco_driving_person_aggregate import (  # noqa: E402
    RankingPeriod,
    _apply_rankings,
    _assignment_upsert_batch,
    _fetch_aggregate_rows,
    _stats_row_from_aggregate,
    _upsert_monthly_stats,
    _upsert_weekly_stats,
)
from jobs.ecodriving_person.job_eco_driving_person_monthly_email_notifications import (  # noqa: E402
    _fetch_candidates as fetch_monthly_candidates,
)
from jobs.ecodriving_person.job_eco_driving_person_weekly_email_notifications import (  # noqa: E402
    CandidateDecision,
    _fetch_candidates as fetch_weekly_candidates,
)
from jobs.ecodriving_person.normalization import normalize_person_source_identity  # noqa: E402

CLIENT_ID = "6018be20-5faa-41b6-89c9-fe2b54a8283e"


def _connect():
    return psycopg.connect(
        host=os.environ["POSTGRES_HOST"],
        port=os.environ["POSTGRES_PORT"],
        dbname="telematics_main",
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        row_factory=dict_row,
    )


def _person(cur, source: str, name: str, *, active: bool = True) -> None:
    cur.execute(
        """
        INSERT INTO public.eco_person_people (
          client_id, person_id, person_id_match_key, person_name,
          person_name_group_key, email, ranking_included, is_active
        ) VALUES (%s, %s, 'triggered', %s, 'triggered', 'shared@example.test', true, %s)
        """,
        (CLIENT_ID, source, name, active),
    )


def main() -> None:
    migration = (
        REPO_ROOT
        / "db"
        / "client_business"
        / "043_eco_person_physical_person_identity.sql"
    ).read_text(encoding="utf-8")
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(migration)

            samples = [
                "Driver 1-business",
                " driver_1 business ",
                "DRIVER.1/BUSINESS",
                "Driver\t1\nbusiness",
                "Lukasz",
                "Łukasz",
                "Zolty",
                "Żółty",
            ]
            for sample in samples:
                cur.execute(
                    "SELECT public.eco_person_normalize_source_identity(%s) AS key",
                    (sample,),
                )
                assert cur.fetchone()["key"] == normalize_person_source_identity(sample)

            _person(cur, "Alias A", "Jan Kowalski")
            _person(cur, "Alias B", "JAN   KOWALSKI")
            _person(cur, "Alias C", "Anna Nowak")
            _person(cur, "Łukasz", "Łukasz Test")
            _person(cur, "Inactive", "Inactive Person", active=False)

            cur.execute("SAVEPOINT duplicate_source")
            try:
                _person(cur, "Alias-A", "Other Person")
                raise AssertionError("normalized duplicate source identity was accepted")
            except psycopg.errors.UniqueViolation:
                cur.execute("ROLLBACK TO SAVEPOINT duplicate_source")

            cur.execute("SAVEPOINT group_conflict")
            try:
                cur.execute(
                    """
                    INSERT INTO public.eco_person_people (
                      client_id, person_id, person_id_match_key, person_name,
                      person_name_group_key, email, ranking_included, is_active
                    ) VALUES (%s, 'Alias D', 'triggered', 'Jan Kowalski',
                              'triggered', 'different@example.test', true, true)
                    """,
                    (CLIENT_ID,),
                )
                cur.execute("SET CONSTRAINTS trg_eco_person_people_group_consistency IMMEDIATE")
                raise AssertionError("conflicting physical-person email was accepted")
            except psycopg.errors.RaiseException:
                cur.execute("ROLLBACK TO SAVEPOINT group_conflict")
                cur.execute("SET CONSTRAINTS trg_eco_person_people_group_consistency DEFERRED")

            cur.execute(
                """
                CREATE TEMP TABLE eco_person_test_trips (
                  client_id UUID, client_code TEXT, provider_trip_id INTEGER,
                  record_id UUID, driver_name TEXT, driver_tag_description TEXT,
                  trip_mode TEXT, start_timestamp TIMESTAMPTZ, end_timestamp TIMESTAMPTZ,
                  trip_distance_meters BIGINT, overrev_events_count BIGINT,
                  harsh_braking_events BIGINT, harsh_acceleration_events BIGINT,
                  harsh_turning_events BIGINT, idle_events BIGINT,
                  speeding_140_160_count BIGINT, speeding_160_170_count BIGINT,
                  speeding_170_plus_count BIGINT
                )
                """
            )
            trips = [
                (-10, " alias_a ", 60_000, 1),
                (-9, "ALIAS.B", 60_000, 3),
                (-8, "Alias C", 100_000, 2),
                (-7, "Lukasz", 10_000, 0),
                (-6, "ŁUKASZ", 10_000, 0),
                (-5, "", 10_000, 0),
                (-4, "Unknown", 10_000, 0),
                (-3, "Inactive", 10_000, 0),
            ]
            cur.executemany(
                """
                INSERT INTO eco_person_test_trips
                VALUES (%s, 'BRAVO00016', %s, NULL, %s, NULL, 'business',
                        '2026-06-02 08:00:00+02', '2026-06-02 09:00:00+02',
                        %s, %s, 0, 0, 0, 0, 0, 0, 0)
                """,
                [(CLIENT_ID, trip_id, driver, meters, overrev) for trip_id, driver, meters, overrev in trips],
            )
            counts = _assignment_upsert_batch(
                cur,
                trips_table="pg_temp.eco_person_test_trips",
                people_table="public.eco_person_people",
                assignments_table="public.eco_person_trip_assignments",
                client_id=CLIENT_ID,
                start_ts=datetime.fromisoformat("2026-06-01T00:00:00+02:00"),
                end_ts=datetime.fromisoformat("2026-07-01T00:00:00+02:00"),
                person_name_group_key=None,
                batch_size=100,
                last_provider_trip_id=-100,
            )
            assert counts["assigned_trips_count"] == 5
            assert counts["skipped_no_driver_name_count"] == 1
            assert counts["unmapped_driver_name_count"] == 2
            assert counts["invalid_ambiguous_mapping_count"] == 0

            period = RankingPeriod(
                date(2026, 6, 1),
                date(2026, 7, 1),
                date(2026, 6, 1),
                1,
                "2026-06-W1",
                True,
            )
            grouped = _fetch_aggregate_rows(
                cur,
                assignments_table="public.eco_person_trip_assignments",
                driver_chart_table="public.eco_person_people_email_view",
                client_id=CLIENT_ID,
                start_ts=period.start_ts,
                end_ts=period.end_ts,
                person_name_group_key=None,
                monthly=False,
            )
            assert len(grouped) == 4
            jan = next(row for row in grouped if row["person_name_group_key"] == "jan kowalski")
            assert jan["trips_count"] == 2
            assert jan["total_distance_meters"] == 120_000
            assert jan["overrev_events_count"] == 4

            weekly = [_stats_row_from_aggregate(row, period, monthly=False) for row in grouped]
            _apply_rankings(weekly, monthly=False)
            assert len(weekly) == 4
            assert _upsert_weekly_stats(
                cur, weekly_table="public.eco_person_weekly_stats", rows=weekly
            ) == 4

            monthly_grouped = _fetch_aggregate_rows(
                cur,
                assignments_table="public.eco_person_trip_assignments",
                driver_chart_table="public.eco_person_people_email_view",
                client_id=CLIENT_ID,
                start_ts=period.start_ts,
                end_ts=period.end_ts,
                person_name_group_key=None,
                monthly=True,
                month_start=period.period_start_date,
                month_end=period.period_end_date,
            )
            monthly = [
                _stats_row_from_aggregate(row, None, monthly=True)
                for row in monthly_grouped
            ]
            _apply_rankings(monthly, monthly=True)
            assert _upsert_monthly_stats(
                cur, monthly_table="public.eco_person_monthly_stats", rows=monthly
            ) == 4

            weekly_candidates = fetch_weekly_candidates(
                cur,
                weekly_table="public.eco_person_weekly_stats",
                driver_chart_table="public.eco_person_people_email_view",
                client_id=CLIENT_ID,
                period_start_date=period.period_start_date,
                period_end_date=period.period_end_date,
                email_column="email",
                driver_columns=["person_name"],
                person_name_group_key=None,
                limit=None,
            )
            assert len(weekly_candidates) == 3
            assert len({row["person_name_group_key"] for row in weekly_candidates}) == 3

            monthly_candidates = fetch_monthly_candidates(
                cur,
                monthly_table="public.eco_person_monthly_stats",
                driver_chart_table="public.eco_person_people_email_view",
                client_id=CLIENT_ID,
                period_start_date=period.period_start_date,
                period_end_date=period.period_end_date,
                email_column="email",
                driver_columns=["person_name"],
                person_name_group_key=None,
                limit=None,
            )
            assert len(monthly_candidates) == 3

            jan_candidate = next(
                row for row in weekly_candidates
                if row["person_name_group_key"] == "jan kowalski"
            )
            decision = CandidateDecision(
                status=None,
                template_type="bezpieczny",
                template_filename="test.html",
                template_variant="ranked",
            )
            first = reserve_send(
                cur,
                send_log_table="public.eco_person_weekly_email_send_log",
                report_type="weekly",
                run_id="test",
                row=jan_candidate,
                decision=decision,
                recipient_email=jan_candidate["recipient_email"],
                original_recipient_email=jan_candidate["recipient_email"],
                subject="test",
                send_scope="normal",
                pending_stale_after_minutes=120,
                metadata_json={},
            )
            second = reserve_send(
                cur,
                send_log_table="public.eco_person_weekly_email_send_log",
                report_type="weekly",
                run_id="test",
                row=jan_candidate,
                decision=decision,
                recipient_email=jan_candidate["recipient_email"],
                original_recipient_email=jan_candidate["recipient_email"],
                subject="test",
                send_scope="normal",
                pending_stale_after_minutes=120,
                metadata_json={},
            )
            assert first.reserved and not second.reserved
    finally:
        conn.rollback()
        conn.close()
    print("OK - migration 043 DB assignment/aggregation/email checks passed (rolled back)")


if __name__ == "__main__":
    main()
