#!/usr/bin/env python3
"""Least-privilege retention access for client business databases.

Run:
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" \
        .venv/bin/python ops/tests_manual/test_client_retention_privileges_postgres.py

Uses a disposable PostgreSQL 16 this suite creates and removes. No DSN comes
from the environment; production is unreachable from here.

WHAT THIS PROVES

The read-only production rehearsal found 27 (client, table) pairs where the
retention sweep answered `permission denied`. A policy a role cannot execute is
not implemented, so `db/client_business/052_retention_runtime_privileges.sql`
closes the gap — and this suite proves it closes it WITHOUT over-granting:

  * before the migration the runtime role cannot even COUNT eligible rows, and
    the sweep reports `INSUFFICIENT_PRIVILEGE` rather than pretending to govern;
  * after it, the same role can plan and delete, and the real sweep succeeds;
  * the role gains SELECT and DELETE and nothing else — INSERT, UPDATE and
    TRUNCATE all remain refused;
  * no ownership, no schema ownership, no `ALL PRIVILEGES`, and no
    `GRANT … ON ALL TABLES`, which would silently widen to every future table;
  * the migration is idempotent and skips relations a client does not have;
  * the granted set is exactly the set the sweep touches — no ungoverned grant,
    no ungranted sweep.
"""
from __future__ import annotations

import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ops import hard_retention as hr  # noqa: E402
from ops import retention_registry as rr  # noqa: E402
from ops.tests_manual.disposable_postgres import (  # noqa: E402
    DisposablePostgresUnavailable,
    disposable_postgres,
)

MIGRATION = REPO_ROOT / "db" / "client_business" / "052_retention_runtime_privileges.sql"
# The grants for the two 2026-06-19 write-test snapshots in alpha_main. They
# live in a second file because 052 is applied to all five clients and applied
# migrations are immutable; `GRANT` is additive, so a strict extension is the
# only correct shape. Both files are applied wherever this suite grants, and the
# swept-set invariant below reads their UNION — a relation the sweep touches must
# be granted by SOME applied migration, not necessarily by the first one.
MIGRATION_WRITE_TEST = (
    REPO_ROOT / "db" / "client_business"
    / "053_write_test_snapshot_retention_privileges.sql"
)
MIGRATIONS = (MIGRATION, MIGRATION_WRITE_TEST)

#: Per-file expectations for the blanket-grant check. Each file grants exactly
#: one `SELECT, DELETE` statement, applied per relation in its own array, and
#: takes USAGE only on the schemas its relations live in.
EXPECTED_GRANT_SHAPE = {
    MIGRATION.name: {"grant_pairs": 1, "usage": 3},
    MIGRATION_WRITE_TEST.name: {"grant_pairs": 1, "usage": 1},
}


def apply_privilege_migrations(conn) -> None:
    """Apply every privilege migration, in filename order, as the admin role."""
    for path in MIGRATIONS:
        with conn.cursor() as cur:
            cur.execute(path.read_text(encoding="utf-8"))
    conn.commit()


RUNTIME_ROLE = "retention_runtime_probe"
RUNTIME_PASSWORD = "disposable"

NOW = datetime(2026, 8, 29, 5, 0, tzinfo=timezone.utc)
OLD = datetime(2024, 1, 1, tzinfo=timezone.utc)
YOUNG = datetime(2026, 8, 1, tzinfo=timezone.utc)

PASSED: list[str] = []

# A representative slice of the real fleet's shape: a Workflow A table, two
# tables the registry added, a Stage 3 report table in the mixed-case schema,
# and a relation this "client" deliberately does not have.
FIXTURE_SQL = """
CREATE SCHEMA IF NOT EXISTS telematics_reports;
CREATE SCHEMA IF NOT EXISTS telematics_reports;

CREATE TABLE public.client_trips (
    client_id uuid, start_timestamp timestamptz
);
CREATE TABLE public.eco_driver_weekly_stats (
    client_id uuid, period_start_date date
);
CREATE TABLE public.eco_driving_weekly_email_send_log (
    send_log_id uuid PRIMARY KEY, recipient_email text, attempted_at timestamptz
);
CREATE TABLE public.eco_drivers_id_chart (
    client_id uuid, driver_id text, updated_at timestamptz
);
CREATE TABLE telematics_reports.report_207 (
    record_id text, _loaded_at timestamptz
);
-- Present, and DELIBERATELY NOT GRANTED: the GPS assignment log is the
-- owner-approved exemption from age-based retention, so the retention role has
-- no supported operation against it and must receive no privilege on it.
CREATE TABLE telematics_reports."Alpha_GPS_Baza_LOG" (
    source_id text, assignment_date date
);
CREATE TABLE telematics_reports.alpha_gps_baza_log_import_runs (
    import_run_id uuid, started_at timestamptz
);
-- Deliberately absent: client_trips_legacy_backup_020, the V2 staging tables,
-- report_d105_2_ecodriving. The fleet's schemas genuinely differ and the
-- migration must skip what a client does not have.
"""


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail}" if detail else label)


def connect(dsn: str, **kwargs):
    import psycopg

    return psycopg.connect(dsn, autocommit=False, **kwargs)


def runtime_dsn(port: int, database: str) -> str:
    return f"postgresql://{RUNTIME_ROLE}:{RUNTIME_PASSWORD}@127.0.0.1:{port}/{database}"


def refused(conn, statement: str, params=None) -> bool:
    """Did PostgreSQL refuse this for lack of privilege?"""
    import psycopg

    try:
        with conn.cursor() as cur:
            cur.execute(statement, params)
    except psycopg.errors.InsufficientPrivilege:
        conn.rollback()
        return True
    except Exception:
        conn.rollback()
        return False
    conn.rollback()
    return False


def test_before_the_migration_the_sweep_cannot_govern(runtime_conn) -> None:
    """The production symptom, reproduced: fail-closed, never a false pass."""
    sweep = next(item for item in hr.workflow_a_table_sweeps()
                 if item.table == "eco_driver_weekly_stats")
    outcome = hr.sweep_table(
        runtime_conn, sweep, cutoff=NOW - timedelta(days=400),
        scope="PROBE", dry_run=True,
    )
    check("the sweep fails rather than reporting zero eligible rows",
          outcome.classification == "RETENTION_FAILED", outcome.classification)
    check("and it names the cause precisely",
          outcome.defect_code == "INSUFFICIENT_PRIVILEGE", str(outcome.defect_code))
    check("nothing is deleted", outcome.deleted == 0)
    PASSED.append("before_the_migration_the_sweep_cannot_govern")


def test_the_migration_grants_select_and_delete(admin_conn, runtime_conn) -> None:
    apply_privilege_migrations(admin_conn)

    granted = {
        "public.client_trips": "start_timestamp",
        "public.eco_driver_weekly_stats": "period_start_date",
        "public.eco_driving_weekly_email_send_log": "attempted_at",
        "public.eco_drivers_id_chart": "updated_at",
        "telematics_reports.report_207": "_loaded_at",
        "telematics_reports.alpha_gps_baza_log_import_runs": "started_at",
    }
    for relation, column in granted.items():
        with runtime_conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {relation} WHERE {column} < %s", (OLD,))
            cur.fetchone()
        runtime_conn.rollback()
    check("the runtime role can now plan against every granted relation", True)

    with runtime_conn.cursor() as cur:
        cur.execute("DELETE FROM public.eco_driver_weekly_stats WHERE false")
    runtime_conn.rollback()
    check("and it can delete", True)

    # LEAST PRIVILEGE, THE OTHER WAY ROUND. The GPS assignment log is exempt
    # from age-based retention, so the retention role has no supported operation
    # against it — and therefore holds no privilege on it. A grant "just in
    # case" would be destructive authority over a store the owner has placed out
    # of the sweep's reach.
    check("the exempt assignment log is not readable by the retention role",
          refused(runtime_conn,
                  'SELECT count(*) FROM telematics_reports."Alpha_GPS_Baza_LOG"'))
    check("and certainly not deletable",
          refused(runtime_conn,
                  'DELETE FROM telematics_reports."Alpha_GPS_Baza_LOG" WHERE false'))
    PASSED.append("the_migration_grants_select_and_delete")


def test_it_grants_nothing_more(runtime_conn) -> None:
    """Least privilege, checked as refusals rather than as a grant listing."""
    check("INSERT is still refused",
          refused(runtime_conn,
                  "INSERT INTO public.eco_driver_weekly_stats (client_id) VALUES (NULL)"))
    check("UPDATE is still refused",
          refused(runtime_conn,
                  "UPDATE public.eco_driver_weekly_stats SET client_id = NULL"))
    check("TRUNCATE is still refused",
          refused(runtime_conn, "TRUNCATE public.eco_driver_weekly_stats"),
          "a bounded batched DELETE is the supported cleanup; TRUNCATE is not")
    check("creating a table in the schema is still refused",
          refused(runtime_conn, "CREATE TABLE public.retention_probe_should_fail (x int)"))
    check("dropping a governed table is still refused",
          refused(runtime_conn, "DROP TABLE public.eco_drivers_id_chart"))

    with runtime_conn.cursor() as cur:
        # Aggregated in Python rather than with `array_agg`: the
        # `information_schema` privilege columns are domain types, and the
        # array a driver hands back for them is not worth reasoning about when
        # a two-column projection is unambiguous.
        cur.execute(
            """
            SELECT table_schema || '.' || table_name AS relation, privilege_type
              FROM information_schema.role_table_grants
             WHERE grantee = %s
             ORDER BY 1, 2
            """,
            (RUNTIME_ROLE,),
        )
        by_relation: dict[str, set[str]] = {}
        for relation, privilege in cur.fetchall():
            by_relation.setdefault(str(relation), set()).add(str(privilege))
    runtime_conn.rollback()

    # `client_trips` already carried the Workflow A runtime write grants before
    # this migration existed. Preserving them is required — the sweep must not
    # narrow what the ingest jobs need — so it is asserted, not excused.
    check("the pre-existing client_trips write grants are preserved",
          {"SELECT", "INSERT", "UPDATE"} <= by_relation.get("public.client_trips", set()),
          str(sorted(by_relation.get("public.client_trips", set()))))
    check("and it gained DELETE for retention",
          "DELETE" in by_relation.get("public.client_trips", set()))

    for relation, privileges in by_relation.items():
        if relation == "public.client_trips":
            continue
        check(f"{relation} holds only SELECT and DELETE",
              privileges == {"SELECT", "DELETE"}, str(sorted(privileges)))
    check("every relation the sweep governs in this fixture was granted",
          len(by_relation) == 6, str(sorted(by_relation)))
    check("and the exempt assignment log was granted nothing at all",
          not [name for name in by_relation if "Alpha_GPS_Baza_LOG" in name],
          str(sorted(by_relation)))
    PASSED.append("it_grants_nothing_more")


def test_the_sweep_works_end_to_end_as_the_runtime_role(runtime_conn, admin_conn) -> None:
    with admin_conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO public.eco_driving_weekly_email_send_log "
            "(send_log_id, recipient_email, attempted_at) VALUES (%s, %s, %s)",
            [(uuid.uuid4(), "driver@example.invalid", OLD),
             (uuid.uuid4(), "driver@example.invalid", OLD),
             (uuid.uuid4(), "driver@example.invalid", YOUNG)],
        )
    admin_conn.commit()

    sweep = next(item for item in hr.CLIENT_EXTRA_SWEEPS
                 if item.table == "eco_driving_weekly_email_send_log")
    cutoff = hr.cutoff_for(sweep.policy_id, NOW)
    outcome = hr.sweep_table(runtime_conn, sweep, cutoff=cutoff, scope="PROBE",
                             dry_run=False)
    check("the sweep succeeds under the runtime role",
          outcome.classification == "RETENTION_EXECUTION_SUCCEEDED",
          f"{outcome.classification} {outcome.error}")
    check("it removed exactly the over-age rows", outcome.deleted == 2,
          str(outcome.deleted))

    with admin_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM public.eco_driving_weekly_email_send_log")
        remaining = int(cur.fetchone()[0])
    admin_conn.rollback()
    check("and left the young one", remaining == 1, str(remaining))
    PASSED.append("the_sweep_works_end_to_end_as_the_runtime_role")


def test_the_migration_is_idempotent_and_skips_absent_relations(admin_conn) -> None:
    for _ in range(2):
        apply_privilege_migrations(admin_conn)
    check("re-applying it is a no-op", True)

    with admin_conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.client_trips_legacy_backup_020')")
        check("a relation this client does not have stays absent",
              cur.fetchone()[0] is None)
        cur.execute(
            "SELECT count(*) FROM information_schema.role_table_grants "
            " WHERE grantee = %s AND table_name = 'client_trips_legacy_backup_020'",
            (RUNTIME_ROLE,),
        )
        check("and no grant was invented for it", int(cur.fetchone()[0]) == 0)
    admin_conn.rollback()
    PASSED.append("the_migration_is_idempotent_and_skips_absent_relations")


def test_the_migration_never_uses_a_blanket_grant() -> None:
    for path in MIGRATIONS:
        code = re.sub(r"--.*$", " ", path.read_text(encoding="utf-8"), flags=re.M)
        upper = code.upper()
        for banned in ("ALL PRIVILEGES", "ON ALL TABLES", "ON ALL SEQUENCES",
                       "GRANT INSERT", "GRANT UPDATE", "GRANT TRUNCATE",
                       "ALTER DEFAULT PRIVILEGES", "OWNER TO", "SUPERUSER",
                       "CREATEDB", "CREATEROLE"):
            check(f"{path.name} never uses {banned}", banned not in upper, banned)
        shape = EXPECTED_GRANT_SHAPE[path.name]
        check(f"{path.name} grants exactly SELECT, DELETE",
              upper.count("GRANT SELECT, DELETE ON TABLE") == shape["grant_pairs"],
              "one statement, one privilege pair, applied per relation")
        check(f"{path.name} takes USAGE only on the schemas it needs",
              upper.count("GRANT USAGE ON SCHEMA") == shape["usage"]
              and "GRANT CREATE ON SCHEMA" not in upper)
        # A pattern grant would confer destructive rights on relations nobody
        # decided to govern; every entry must be a literal relation name.
        check(f"{path.name} names no relation by pattern",
              "%" not in re.sub(r"%I|%s", "", code),
              "a LIKE/wildcard grant is not least privilege")
    PASSED.append("the_migration_never_uses_a_blanket_grant")


def test_the_granted_set_matches_the_swept_set() -> None:
    """A grant nothing sweeps is over-privilege; a sweep with no grant is a gap."""
    listed = {
        item.replace('"', "")
        for path in MIGRATIONS
        for item in re.findall(r"^\s*'([^']+)',?\s*$",
                               path.read_text(encoding="utf-8"), re.M)
    }
    swept = {
        f"{item.schema}.{item.table}"
        for item in hr.workflow_a_table_sweeps() + hr.CLIENT_EXTRA_SWEEPS
    }
    check("every relation the sweep touches is granted",
          not (swept - listed), str(sorted(swept - listed)))
    check("and every granted relation is governed by the registry",
          not (listed - set(rr.GOVERNED_CLIENT_RELATIONS)),
          str(sorted(listed - set(rr.GOVERNED_CLIENT_RELATIONS))))

    # And nothing is granted for a store no sweep may touch. `listed` is the
    # migration's own array, so this is the file itself being held to least
    # privilege rather than a restatement of it.
    exempt_relations = {
        relation for relation, policy_id in rr.GOVERNED_CLIENT_RELATIONS.items()
        if rr.get(policy_id).is_owner_exempt
    }
    check("the fixture really does cover the exemption", exempt_relations)
    granted_exempt = {
        relation for relation in exempt_relations
        if relation in listed or relation.replace(".", '."') + '"' in listed
    }
    check("no owner-exempt relation is granted anything",
          not granted_exempt, str(sorted(granted_exempt)))
    check("and no exempt relation is swept either",
          not (swept & exempt_relations), str(sorted(swept & exempt_relations)))
    PASSED.append("the_granted_set_matches_the_swept_set")


def main() -> int:
    try:
        context = disposable_postgres(label="privret")
    except DisposablePostgresUnavailable as exc:
        print(f"NOT AVAILABLE - {exc}")
        return 0
    try:
        with context as (dsn, info):
            print(f"disposable PostgreSQL {info['server_version']} on port {info['port']}")
            database = dsn.rsplit("/", 1)[-1]
            admin_conn = connect(dsn)
            with admin_conn.cursor() as cur:
                cur.execute(FIXTURE_SQL)
                # The real fleet's shape: a runtime role holding the Workflow A
                # write grants on `client_trips` and nothing else. Migration 052
                # discovers it exactly this way.
                cur.execute(
                    f"CREATE ROLE {RUNTIME_ROLE} LOGIN PASSWORD '{RUNTIME_PASSWORD}'")
                cur.execute(f"GRANT CONNECT ON DATABASE \"{database}\" TO {RUNTIME_ROLE}")
                cur.execute(f"GRANT USAGE ON SCHEMA public TO {RUNTIME_ROLE}")
                cur.execute(
                    f"GRANT SELECT, INSERT, UPDATE ON public.client_trips TO {RUNTIME_ROLE}")
            admin_conn.commit()

            runtime_conn = connect(runtime_dsn(info["port"], database))
            try:
                test_before_the_migration_the_sweep_cannot_govern(runtime_conn)
                test_the_migration_grants_select_and_delete(admin_conn, runtime_conn)
                test_it_grants_nothing_more(runtime_conn)
                test_the_sweep_works_end_to_end_as_the_runtime_role(
                    runtime_conn, admin_conn)
                test_the_migration_is_idempotent_and_skips_absent_relations(admin_conn)
                test_the_migration_never_uses_a_blanket_grant()
                test_the_granted_set_matches_the_swept_set()
            finally:
                runtime_conn.close()
                admin_conn.close()
    except DisposablePostgresUnavailable as exc:
        print(f"NOT AVAILABLE - {exc}")
        return 0

    for name in PASSED:
        print(f"PASS {name}")
    print(f"\n{len(PASSED)} checks passed — client retention privileges")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
