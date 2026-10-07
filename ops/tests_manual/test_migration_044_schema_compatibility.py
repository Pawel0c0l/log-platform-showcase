#!/usr/bin/env python3
"""PostgreSQL 16 fixtures for heterogeneous migration 044 client schemas."""
from __future__ import annotations

import subprocess
import time
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MIGRATION = (ROOT / "db/client_business/044_eco_email_fail_closed_idempotency.sql").read_text()
CONTAINER = f"migration-044-test-{uuid.uuid4().hex[:10]}"
CLIENT_ID = "11111111-1111-1111-1111-111111111111"


def docker(*args: str, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], input=input_text, text=True, capture_output=True, check=check
    )


def psql(database: str, sql: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return docker(
        "exec", "-i", CONTAINER, "psql", "-X", "-At", "-v", "ON_ERROR_STOP=1",
        "-U", "postgres", "-d", database, input_text=sql, check=check,
    )


def create_database(name: str) -> None:
    psql("postgres", f'CREATE DATABASE "{name}";')


def apply_migration(database: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return psql(database, "BEGIN;\n" + MIGRATION + "\nCOMMIT;\n", check=check)


def driver_table(name: str, *, partial: bool = False) -> str:
    extra = """
      ,send_scope text NOT NULL DEFAULT 'normal'
      ,idempotency_key text
      ,parent_send_log_id uuid
      ,force_resend_reason text
      ,force_resend_at timestamptz
    """ if partial else ""
    return f"""
      CREATE TABLE public.{name} (
        send_log_id uuid PRIMARY KEY,
        client_id uuid NOT NULL,
        assigned_id text NOT NULL,
        report_type text NOT NULL,
        period_start_date date NOT NULL,
        period_end_date date NOT NULL,
        template_type text,
        status text NOT NULL,
        attempted_at timestamptz NOT NULL DEFAULT now(),
        metadata_json jsonb NOT NULL DEFAULT '{{}}'::jsonb
        {extra}
      );
      CREATE UNIQUE INDEX uq_{name}_sent_once ON public.{name}
        (client_id,assigned_id,report_type,period_start_date,period_end_date,template_type)
        WHERE status='sent';
    """


def person_table(name: str) -> str:
    return f"""
      CREATE TABLE public.{name} (
        send_log_id uuid PRIMARY KEY,
        client_id uuid NOT NULL,
        person_name_group_key text NOT NULL,
        report_type text NOT NULL,
        period_start_date date NOT NULL,
        period_end_date date NOT NULL,
        template_type text,
        status text NOT NULL,
        send_scope text NOT NULL DEFAULT 'normal',
        idempotency_key text,
        attempted_at timestamptz NOT NULL DEFAULT now()
      );
      CREATE UNIQUE INDEX uq_{name}_normal_identity ON public.{name}
        (client_id,person_name_group_key,report_type,period_start_date,period_end_date,template_type)
        WHERE send_scope='normal' AND status IN ('pending','sent');
      CREATE UNIQUE INDEX uq_{name}_normal_idempotency ON public.{name}(idempotency_key)
        WHERE send_scope='normal' AND status IN ('pending','sent');
    """


def row(table: str, subject_column: str, subject: str, *, scope: str | None = None,
        template: str = "a", status: str = "sent", suffix: int = 1) -> str:
    scope_cols = ",send_scope,idempotency_key" if scope is not None else ""
    scope_vals = f",'{scope}','legacy-{scope}-{suffix}'" if scope is not None else ""
    return f"""
      INSERT INTO public.{table}
        (send_log_id,client_id,{subject_column},report_type,period_start_date,period_end_date,
         template_type,status{scope_cols})
      VALUES ('00000000-0000-0000-0000-{suffix:012d}','{CLIENT_ID}','{subject}',
              'weekly','2026-07-14','2026-07-21','{template}','{status}'{scope_vals});
    """


def scalar(database: str, sql: str) -> str:
    return psql(database, sql).stdout.strip().splitlines()[-1]


def assert_final_indexes(database: str, table: str, subject: str) -> None:
    definitions = psql(database, f"""
      SELECT pg_get_indexdef(indexrelid) FROM pg_index
      WHERE indrelid='public.{table}'::regclass ORDER BY 1;
    """).stdout
    assert f"(client_id, {subject}, report_type, period_start_date, period_end_date)" in definitions
    assert "(idempotency_key)" in definitions
    assert "template_type" not in "\n".join(
        line for line in definitions.splitlines() if "normal_identity" in line
    )
    assert "send_scope = 'normal'" in definitions
    assert "pending" in definitions and "sent" in definitions


def test_alpha_like() -> None:
    db = "alpha_like"; create_database(db)
    psql(db, driver_table("eco_driving_weekly_email_send_log") +
         driver_table("eco_driving_monthly_email_send_log") +
         row("eco_driving_weekly_email_send_log", "assigned_id", "D1"))
    apply_migration(db); apply_migration(db)
    assert_final_indexes(db, "eco_driving_weekly_email_send_log", "assigned_id")
    assert_final_indexes(db, "eco_driving_monthly_email_send_log", "assigned_id")
    assert scalar(db, "SELECT to_regclass('public.eco_person_weekly_email_send_log') IS NULL;") == "t"


def test_bravo_like_and_scope() -> None:
    db = "bravo_like"; create_database(db)
    weekly = "eco_person_weekly_email_send_log"
    psql(db, person_table(weekly) + person_table("eco_person_monthly_email_send_log") +
         row(weekly, "person_name_group_key", "person", scope="normal", suffix=11) +
         row(weekly, "person_name_group_key", "person", scope="test", suffix=12) +
         row(weekly, "person_name_group_key", "person", scope="forced", suffix=13))
    apply_migration(db); apply_migration(db)
    assert_final_indexes(db, weekly, "person_name_group_key")
    assert_final_indexes(db, "eco_person_monthly_email_send_log", "person_name_group_key")
    assert scalar(db, f"SELECT count(*) FROM public.{weekly};") == "3"


def test_both_models() -> None:
    db = "both_models"; create_database(db)
    psql(db, driver_table("eco_driving_weekly_email_send_log") +
         driver_table("eco_driving_monthly_email_send_log") +
         person_table("eco_person_weekly_email_send_log") +
         person_table("eco_person_monthly_email_send_log"))
    apply_migration(db); apply_migration(db)
    for table, subject in (
        ("eco_driving_weekly_email_send_log", "assigned_id"),
        ("eco_driving_monthly_email_send_log", "assigned_id"),
        ("eco_person_weekly_email_send_log", "person_name_group_key"),
        ("eco_person_monthly_email_send_log", "person_name_group_key"),
    ): assert_final_indexes(db, table, subject)


def test_weekly_only_empty_and_partial() -> None:
    weekly = "eco_driving_weekly_email_send_log"
    db = "weekly_only"; create_database(db); psql(db, driver_table(weekly))
    apply_migration(db); assert_final_indexes(db, weekly, "assigned_id")
    assert scalar(db, "SELECT to_regclass('public.eco_driving_monthly_email_send_log') IS NULL;") == "t"

    db = "empty_eco"; create_database(db); apply_migration(db); apply_migration(db)
    assert scalar(db, "SELECT count(*) FROM pg_tables WHERE schemaname='public' AND tablename LIKE 'eco%send_log';") == "0"

    db = "partial_rerun"; create_database(db); psql(db, driver_table(weekly, partial=True))
    apply_migration(db); apply_migration(db); assert_final_indexes(db, weekly, "assigned_id")


def test_conflict(model: str) -> None:
    db = f"conflict_{model}"; create_database(db)
    if model == "driver":
        table, subject = "eco_driving_weekly_email_send_log", "assigned_id"
        ddl = driver_table(table)
        inserts = row(table, subject, "D1", template="a", suffix=21) + row(table, subject, "D1", template="b", suffix=22)
    else:
        table, subject = "eco_person_weekly_email_send_log", "person_name_group_key"
        ddl = person_table(table)
        inserts = row(table, subject, "person", scope="normal", template="a", suffix=31) + row(table, subject, "person", scope="normal", template="b", suffix=32)
    psql(db, ddl + inserts)
    failed = apply_migration(db, check=False)
    message = failed.stdout + failed.stderr
    assert failed.returncode != 0 and "ECO_EMAIL_IDEMPOTENCY_CONFLICT" in message
    for detail in (f"model={model}", f"table={table}", "row_count", "statuses", "scopes", "template_types", "row_ids"):
        assert detail in message, detail
    assert scalar(db, f"SELECT count(*) FROM public.{table};") == "2"
    if model == "driver":
        assert scalar(db, f"SELECT count(*) FROM information_schema.columns WHERE table_schema='public' AND table_name='{table}' AND column_name='send_scope';") == "0"
        assert scalar(db, f"SELECT to_regclass('public.uq_{table}_sent_once') IS NOT NULL;") == "t"
        assert scalar(db, f"SELECT to_regclass('public.uq_{table}_normal_identity') IS NULL;") == "t"
    else:
        definition = psql(db, f"SELECT pg_get_indexdef('public.uq_{table}_normal_identity'::regclass);").stdout
        assert "template_type" in definition


def main() -> None:
    docker("run", "-d", "--rm", "--name", CONTAINER,
           "-e", "POSTGRES_HOST_AUTH_METHOD=trust", "postgres:16")
    try:
        for _ in range(60):
            if docker("exec", CONTAINER, "pg_isready", "-U", "postgres", check=False).returncode == 0:
                break
            time.sleep(0.25)
        else: raise RuntimeError("disposable PostgreSQL 16 fixture did not become ready")
        test_alpha_like(); test_bravo_like_and_scope(); test_both_models()
        test_weekly_only_empty_and_partial(); test_conflict("driver"); test_conflict("person")
    finally:
        docker("rm", "-f", CONTAINER, check=False)
    print("OK - migration 044 PostgreSQL 16 heterogeneous-schema fixtures passed")


if __name__ == "__main__": main()
