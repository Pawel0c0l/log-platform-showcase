#!/usr/bin/env python3
"""Disposable DB checks for Eco Driving Person runtime privileges.

Run:

    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" \
      python3 ops/tests_manual/test_eco_person_runtime_privileges.py

The test creates a temporary client-business database plus separate migration
owner and runtime roles. It does not touch production/client databases.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import psycopg  # noqa: E402
from psycopg import sql  # noqa: E402

from jobs.ecodriving_person import job_eco_driving_person_aggregate as aggregate_job  # noqa: E402


MIGRATION_039 = REPO_ROOT / "db" / "client_business" / "039_eco_person_driving_schema.sql"
MIGRATION_040 = REPO_ROOT / "db" / "client_business" / "040_eco_person_runtime_privileges.sql"
MIGRATION_043 = REPO_ROOT / "db" / "client_business" / "043_eco_person_physical_person_identity.sql"

CLIENT_ID = "00000000-0000-0000-0000-000000000016"
PERSON_ID = "Jan Kowalski"
PERSON_GROUP_KEY = "jan kowalski"


class FakeClient:
    def __init__(self) -> None:
        self.logs: list[tuple[str, str]] = []

    def log(self, level: str, _type: str, _source: str, message: str, **_kwargs) -> None:
        self.logs.append((level, message))


def _load_dotenv_if_present() -> None:
    dotenv_path = REPO_ROOT / ".env"
    if not dotenv_path.exists():
        return
    for line in dotenv_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _admin_conn(dbname: str | None = None, *, autocommit: bool = False):
    return psycopg.connect(
        host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        dbname=dbname or os.getenv("POSTGRES_DB", "logdb"),
        user=os.getenv("POSTGRES_USER", "loguser"),
        password=os.getenv("POSTGRES_PASSWORD", ""),
        autocommit=autocommit,
    )


def _role_conn(*, dbname: str, role: str, password: str, autocommit: bool = False):
    return psycopg.connect(
        host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        dbname=dbname,
        user=role,
        password=password,
        autocommit=autocommit,
    )


def _execute_file(conn, path: Path) -> None:
    with conn.cursor() as cur:
        cur.execute(path.read_text(encoding="utf-8"))
    conn.commit()


def _expect_denied(conn, statement: str) -> str:
    try:
        with conn.cursor() as cur:
            cur.execute(statement)
    except Exception as exc:
        conn.rollback()
        return str(exc)
    raise AssertionError(f"Expected permission failure for: {statement}")


def _scalar(conn, statement: str, params: tuple = ()):
    with conn.cursor() as cur:
        cur.execute(statement, params)
        return cur.fetchone()[0]


def _bootstrap_database(dbname: str, owner: str, runtime: str, owner_pw: str, runtime_pw: str) -> None:
    with _admin_conn(autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
                (dbname,),
            )
            cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(dbname)))
            cur.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(runtime)))
            cur.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(owner)))
            cur.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE").format(
                    sql.Identifier(owner),
                    sql.Literal(owner_pw),
                )
            )
            cur.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE").format(
                    sql.Identifier(runtime),
                    sql.Literal(runtime_pw),
                )
            )
            cur.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(dbname), sql.Identifier(owner)))
            cur.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(dbname), sql.Identifier(runtime)))


def _cleanup_database(dbname: str, owner: str, runtime: str) -> None:
    with _admin_conn(autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
                (dbname,),
            )
            cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(dbname)))
            cur.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(runtime)))
            cur.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(owner)))


def _create_prerequisites(conn, runtime: str) -> None:
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        cur.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        cur.execute(
            """
            CREATE TABLE public.client_trips (
              client_id UUID NOT NULL,
              client_code TEXT NULL,
              provider_trip_id INTEGER NOT NULL,
              record_id UUID NULL,
              driver_name TEXT NULL,
              driver_tag_description TEXT NULL,
              trip_mode TEXT NULL,
              start_timestamp TIMESTAMPTZ NOT NULL,
              end_timestamp TIMESTAMPTZ NULL,
              trip_distance_meters BIGINT NULL,
              overrev_events_count BIGINT NULL,
              harsh_braking_events BIGINT NULL,
              harsh_acceleration_events BIGINT NULL,
              harsh_turning_events BIGINT NULL,
              idle_events BIGINT NULL,
              speeding_140_160_count BIGINT NULL,
              speeding_160_170_count BIGINT NULL,
              speeding_170_plus_count BIGINT NULL,
              PRIMARY KEY (client_id, provider_trip_id)
            )
            """
        )
        cur.execute("CREATE TABLE public.eco_drivers_id_chart (driver_id TEXT PRIMARY KEY)")
        cur.execute("CREATE TABLE public.unrelated_runtime_guard (id INTEGER PRIMARY KEY)")
        cur.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(runtime)))
        cur.execute(
            sql.SQL("GRANT SELECT, INSERT, UPDATE ON TABLE public.client_trips TO {}").format(
                sql.Identifier(runtime)
            )
        )
    conn.commit()


def _insert_source_trip(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.client_trips (
              client_id, client_code, provider_trip_id, driver_name,
              start_timestamp, end_timestamp, trip_distance_meters,
              overrev_events_count, harsh_braking_events, harsh_acceleration_events,
              harsh_turning_events, idle_events,
              speeding_140_160_count, speeding_160_170_count, speeding_170_plus_count
            )
            VALUES (
              %s, 'TEST00016', 1001, 'Jan Kowalski',
              '2026-05-10 08:00:00+00', '2026-05-10 09:00:00+00', 150000,
              1, 2, 3, 4, 5, 6, 7, 8
            )
            """,
            (CLIENT_ID,),
        )
    conn.commit()


def _insert_person_and_mapping(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.eco_person_people (
              client_id, person_id, person_id_match_key, person_name,
              person_name_group_key, email, ranking_included, is_active
            )
            VALUES (%s, %s, 'triggered', 'Jan Kowalski', 'triggered',
                    'jan@example.test', true, true)
            ON CONFLICT (client_id, person_id) DO UPDATE
              SET person_name = EXCLUDED.person_name,
                  email = EXCLUDED.email,
                  ranking_included = EXCLUDED.ranking_included,
                  is_active = EXCLUDED.is_active
            """,
            (CLIENT_ID, PERSON_ID),
        )
    conn.commit()


def _insert_stats_and_email_logs(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.eco_person_weekly_stats (
              client_id, client_code, person_name_group_key, person_name,
              week_start_date, week_end_date,
              period_start_date, period_end_date, month_start_date,
              period_sequence_in_month, period_label, trips_count, source_trips_count,
              total_distance_meters, total_kilometers, qualification_status,
              calculation_status, ranking_group, ranking_included,
              ecodriving_rating_type, eco_driving_score_total
            )
            VALUES (
              %s, 'TEST00016', %s, 'Jan Kowalski', '2026-05-01', '2026-05-08',
              '2026-05-01', '2026-05-08', '2026-05-01',
              1, '2026-05-W1', 1, 1, 150000, 150.000, 'QUALIFIED',
              'OK', 'INCLUDED', true, 'bezpieczny', 95
            )
            """,
            (CLIENT_ID, PERSON_GROUP_KEY),
        )
        cur.execute(
            """
            INSERT INTO public.eco_person_monthly_stats (
              client_id, client_code, person_name_group_key, person_name,
              month_start_date, month_end_date,
              trips_count, source_trips_count, total_distance_meters, total_kilometers,
              qualification_status, calculation_status, ranking_group, ranking_included,
              ecodriving_rating_type, eco_driving_score_total
            )
            VALUES (
              %s, 'TEST00016', %s, 'Jan Kowalski', '2026-05-01', '2026-06-01',
              1, 1, 150000, 150.000, 'QUALIFIED', 'OK', 'INCLUDED', true,
              'bezpieczny', 95
            )
            """,
            (CLIENT_ID, PERSON_GROUP_KEY),
        )
        for table_name, report_type, start, end in (
            ("eco_person_weekly_email_send_log", "weekly", "2026-05-01", "2026-05-08"),
            ("eco_person_monthly_email_send_log", "monthly", "2026-05-01", "2026-06-01"),
        ):
            cur.execute(
                sql.SQL(
                    """
                    INSERT INTO public.{} (
                      client_id, person_name_group_key, person_name,
                      recipient_email, report_type,
                      send_scope, idempotency_key, template_type, template_filename,
                      period_start_date, period_end_date, ecodriving_rating_type,
                      email_subject, status
                    )
                    VALUES (%s, %s, 'Jan Kowalski', 'jan@example.test', %s, 'normal', %s,
                            'bezpieczny', 'template.html', %s, %s,
                            'bezpieczny', 'subject', 'pending')
                    RETURNING send_log_id
                    """
                ).format(sql.Identifier(table_name)),
                (CLIENT_ID, PERSON_GROUP_KEY, report_type, f"test|{report_type}", start, end),
            )
            send_log_id = cur.fetchone()[0]
            cur.execute(
                sql.SQL("UPDATE public.{} SET status='sent', sent_at=now() WHERE send_log_id=%s").format(
                    sql.Identifier(table_name)
                ),
                (send_log_id,),
            )
            cur.execute(
                sql.SQL("UPDATE public.{} SET status='failed', sent_at=NULL WHERE send_log_id=%s").format(
                    sql.Identifier(table_name)
                ),
                (send_log_id,),
            )
        cur.execute(
            """
            INSERT INTO public.eco_person_weekly_stats (
              client_id, client_code, person_name_group_key, person_name,
              week_start_date, week_end_date,
              period_start_date, period_end_date, month_start_date,
              period_sequence_in_month, period_label, trips_count, source_trips_count,
              total_distance_meters, total_kilometers, qualification_status,
              calculation_status, ranking_group
            )
            VALUES (
              %s, 'TEST00016', %s, 'Jan Kowalski', '2026-05-08', '2026-05-15',
              '2026-05-08', '2026-05-15', '2026-05-01',
              2, '2026-05-W2', 0, 0, 0, 0, 'NO_DISTANCE', 'NO_DISTANCE', 'EXCLUDED'
            )
            """,
            (CLIENT_ID, PERSON_GROUP_KEY),
        )
        cur.execute(
            """
            INSERT INTO public.eco_person_monthly_stats (
              client_id, client_code, person_name_group_key, person_name,
              month_start_date, month_end_date,
              qualification_status, calculation_status, ranking_group
            )
            VALUES (%s, 'TEST00016', %s, 'Jan Kowalski', '2026-06-01', '2026-07-01', 'NO_DISTANCE', 'NO_DISTANCE', 'EXCLUDED')
            """,
            (CLIENT_ID, PERSON_GROUP_KEY),
        )
        cur.execute("DELETE FROM public.eco_person_weekly_stats WHERE period_start_date = '2026-05-08'")
        cur.execute("DELETE FROM public.eco_person_monthly_stats WHERE month_start_date = '2026-06-01'")
    conn.commit()


def _run_aggregation_dry_run(dbname: str, runtime: str, runtime_pw: str) -> dict:
    os.environ["TEST_ECO_PERSON_RUNTIME_DB_PASSWORD"] = runtime_pw
    cfg = SimpleNamespace(
        client_db_host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
        client_db_port=int(os.getenv("POSTGRES_PORT", "5432")),
        client_db_name=dbname,
        client_db_user=runtime,
        client_db_password_secret_ref="TEST_ECO_PERSON_RUNTIME_DB_PASSWORD",
        client_db_schema="public",
    )
    original_loader = aggregate_job._load_client_account_config
    aggregate_job._load_client_account_config = lambda *, client_id: cfg
    try:
        return aggregate_job.run(
            client=FakeClient(),
            run_id="test-run",
            params={
                "client_id": CLIENT_ID,
                "month": "2026-05",
                "include_weekly": False,
                "include_monthly": True,
                "dry_run": True,
                "recalculate": False,
                "batch_size": 100,
            },
        )
    finally:
        aggregate_job._load_client_account_config = original_loader


def _assert_privilege_catalog(conn, runtime: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT rolname, rolsuper, rolcreaterole, rolcreatedb
            FROM pg_roles
            WHERE rolname = %s
            """,
            (runtime,),
        )
        role = cur.fetchone()
        assert role == (runtime, False, False, False)
        for table_name in (
            "eco_person_people",
            "eco_person_driver_mappings",
            "eco_person_trip_assignments",
            "eco_person_weekly_stats",
            "eco_person_monthly_stats",
            "eco_person_weekly_email_send_log",
            "eco_person_monthly_email_send_log",
            "eco_person_people_email_view",
        ):
            cur.execute("SELECT tableowner FROM pg_tables WHERE schemaname='public' AND tablename=%s", (table_name,))
            row = cur.fetchone()
            if row is not None:
                assert row[0] != runtime
        assert _scalar(conn, "SELECT has_schema_privilege(%s, 'public', 'CREATE')", (runtime,)) is False
        assert _scalar(conn, "SELECT has_table_privilege(%s, 'public.eco_person_weekly_trends_view', 'INSERT')", (runtime,)) is False
        assert _scalar(conn, "SELECT has_table_privilege(%s, 'public.eco_person_monthly_trends_view', 'UPDATE')", (runtime,)) is False
        assert _scalar(conn, "SELECT has_table_privilege(%s, 'public.eco_drivers_id_chart', 'SELECT')", (runtime,)) is False
        assert _scalar(conn, "SELECT has_table_privilege(%s, 'public.unrelated_runtime_guard', 'INSERT')", (runtime,)) is False
        assert _scalar(conn, "SELECT COUNT(*) FROM information_schema.sequences WHERE sequence_schema='public'") == 0


def main() -> None:
    _load_dotenv_if_present()
    suffix = str(os.getpid())
    dbname = f"eco_person_priv_test_{suffix}"
    owner = f"eco_person_priv_owner_{suffix}"
    runtime = f"eco_person_priv_runtime_{suffix}"
    owner_pw = f"owner_pw_{suffix}"
    runtime_pw = f"runtime_pw_{suffix}"

    _bootstrap_database(dbname, owner, runtime, owner_pw, runtime_pw)
    try:
        with _role_conn(dbname=dbname, role=owner, password=owner_pw) as owner_conn:
            _create_prerequisites(owner_conn, runtime)
            _execute_file(owner_conn, MIGRATION_039)

        with _role_conn(dbname=dbname, role=runtime, password=runtime_pw) as runtime_conn:
            assert _scalar(runtime_conn, "SELECT COUNT(*) FROM public.eco_person_people") == 0
            denied = _expect_denied(runtime_conn, "SELECT COUNT(*) FROM public.eco_person_people_email_view")
            assert "permission denied" in denied.lower()
        print("PASS: migration 039 reproduces missing direct SELECT on eco_person_people_email_view")

        with _role_conn(dbname=dbname, role=owner, password=owner_pw) as owner_conn:
            _execute_file(owner_conn, MIGRATION_040)
            _execute_file(owner_conn, MIGRATION_043)
            _insert_source_trip(owner_conn)

        with _role_conn(dbname=dbname, role=runtime, password=runtime_pw) as runtime_conn:
            _insert_person_and_mapping(runtime_conn)
            assert _scalar(runtime_conn, "SELECT COUNT(*) FROM public.eco_person_people_email_view") == 1
            assert _scalar(runtime_conn, "SELECT COUNT(*) FROM public.eco_person_driver_mappings_view") == 1
            assert _scalar(runtime_conn, "SELECT COUNT(*) FROM public.eco_person_weekly_trends_view") == 0
            assert _scalar(runtime_conn, "SELECT COUNT(*) FROM public.eco_person_monthly_trends_view") == 0
            _expect_denied(runtime_conn, "INSERT INTO public.eco_person_people_email_view (client_id, person_name_group_key, person_name) VALUES ('00000000-0000-0000-0000-000000000016', 'forbidden', 'Forbidden')")
            _expect_denied(runtime_conn, "DROP TABLE public.eco_person_people")
        print("PASS: runtime role can read person views but cannot write views or own/drop tables")

        result = _run_aggregation_dry_run(dbname, runtime, runtime_pw)
        assert result["dry_run"] is True
        assert result["source_trips_seen"] == 1
        assert result["assignments_upserted"] == 1
        assert result["monthly_rows_upserted"] == 1
        with _role_conn(dbname=dbname, role=runtime, password=runtime_pw) as runtime_conn:
            assert _scalar(runtime_conn, "SELECT COUNT(*) FROM public.eco_person_trip_assignments") == 0
            assert _scalar(runtime_conn, "SELECT COUNT(*) FROM public.eco_person_monthly_stats") == 0
        print("PASS: aggregation dry-run reaches eco_person_people_email_view and rolls back writes")

        with _role_conn(dbname=dbname, role=runtime, password=runtime_pw) as runtime_conn:
            _insert_stats_and_email_logs(runtime_conn)
            _assert_privilege_catalog(runtime_conn, runtime)
        print("PASS: runtime DML and least-privilege catalog checks passed")

    finally:
        _cleanup_database(dbname, owner, runtime)

    print("OK - Eco Driving Person runtime privilege checks passed")


if __name__ == "__main__":
    main()
