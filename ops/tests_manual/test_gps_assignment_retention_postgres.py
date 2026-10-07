#!/usr/bin/env python3
"""GPS assignment history: the platform's one owner-approved retention exception.

Run:
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" \
        .venv/bin/python ops/tests_manual/test_gps_assignment_retention_postgres.py

Uses a disposable PostgreSQL 16 this suite creates and removes; no DSN comes
from the environment, and no production database is reachable from it.

WHAT THIS SUITE EXISTS FOR

`telematics_reports."Alpha_GPS_Baza_LOG"` is loaded by a FULL REPLACE from an
upstream workbook that still contains assignments from 2017. On 2026-08-29 the
owner decided that this relation has **no age-based retention**: those records
are business history that is kept, not overdue cleanup.

The decision has three observable halves, and all three are proved here against
the REAL Stage 3 writer and the REAL executor rather than against a restatement
of them:

  * the registry represents the exception EXPLICITLY — exempt, attributed,
    centrally visible, and not confusable with a store nobody governs;
  * the writers do not filter by age, so a 2017 assignment imports normally and
    survives any number of repeated full-replace imports;
  * the hard-retention executor plans no age-based DELETE here at all, and says
    so in its output instead of reporting candidates, a blocker or an unknown
    anchor.

Everything OTHER than age must keep working exactly as before, so the
provenance columns, the replace semantics and the fail-closed refusal of an
unparsable date are all still asserted.
"""
from __future__ import annotations

import sys
import uuid
from datetime import date, datetime, timedelta, timezone
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

POLICY_ID = rr.GPS_ASSIGNMENT_POLICY_ID
SCHEMA = "telematics_reports"
TABLE = "Alpha_GPS_Baza_LOG"

#: The oldest real production assignment, from the read-only inventory.
ANCIENT = date(2017, 6, 20)

PASSED: list[str] = []

FIXTURE_SQL = f'''
CREATE SCHEMA IF NOT EXISTS {SCHEMA};
CREATE TABLE {SCHEMA}."{TABLE}" (
    source_id              text,
    registration           text,
    assignment_date        date,
    csv_filename           text,
    imported_at            timestamptz NOT NULL DEFAULT now(),
    workflow_run_id        uuid,
    raw_file_id            uuid,
    source_artifact_id     uuid,
    normalized_artifact_id uuid,
    cleaned_artifact_id    uuid,
    source_sha256          text,
    source_row_number      integer,
    raw_row_json           jsonb
);
'''


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail}" if detail else label)


class Frame:
    """The minimal shape the Stage 3 writer consumes: `.columns` + iterable rows.

    Deliberately NOT a `list` subclass. `_row_count` tries `len(df.index)` first
    for pandas, and a list already has an `.index` METHOD — the fixture would
    then fail on a detail of the double rather than on the behaviour under test.
    """

    def __init__(self, columns, rows):
        self.columns = list(columns)
        self._rows = list(rows)

    def __iter__(self):
        return iter(self._rows)

    def __len__(self):
        return len(self._rows)


def workbook_frame(rows):
    """One workbook, in the exact column vocabulary the writer requires."""
    columns = ["ID", "Nr rejestracyjny", "Data przydziału", "Nazwa Pliku csv"]
    return Frame(columns, [dict(zip(columns, row)) for row in rows])


def connect(dsn: str):
    """`dict_row`, because that is what the Stage 3 writer's own connections use.

    `_inspect_destination` reads `row["exists"]`; a default tuple cursor would
    fail there for a reason that has nothing to do with retention, so the
    fixture must model the real connection rather than a convenient one.
    """
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(dsn, autocommit=False, row_factory=dict_row)


def assignment_dates(conn) -> list[date]:
    with conn.cursor() as cur:
        cur.execute(f'SELECT assignment_date FROM {SCHEMA}."{TABLE}" ORDER BY 1')
        values = [row["assignment_date"] for row in cur.fetchall()]
    conn.rollback()
    return values


def import_workbook(conn, frame, *, filename="baza.xlsm"):
    """Drive the REAL Stage 3 Alpha-GPS writer."""
    from jobs.reports.stage3 import job_stage3

    return job_stage3._load_alpha_gps_replace_all(
        conn,
        raw_file_id=str(uuid.uuid4()),
        run_id=str(uuid.uuid4()),
        client_code="ALPHA00001",
        report_type=job_stage3.ALPHA_GPS_REPORT_TYPE,
        source_artifact_id=str(uuid.uuid4()),
        source_filename=filename,
        df=frame,
        source_sha256="0" * 64,
        raw_artifact_id=str(uuid.uuid4()),
        normalized_artifact_id=str(uuid.uuid4()),
        cleaned_artifact_id=str(uuid.uuid4()),
        destination_schema=SCHEMA,
        destination_table=TABLE,
    )


# --- the tests ---------------------------------------------------------------


def test_the_relation_is_registered_as_owner_exempt() -> None:
    """Exempt, attributed, and still centrally governed — not unmanaged."""
    policy = rr.get(POLICY_ID)
    check("the mode is the explicit exemption",
          policy.mode is rr.Mode.OWNER_EXEMPT, policy.mode.value)
    check("the entry is live, not blocked pending a decision",
          policy.status is rr.Status.ACTIVE, policy.status.value)
    check("no open owner decision remains", rr.open_owner_decisions() == ())
    check("the exemption is attributed to an owner and a date",
          policy.exemption is not None
          and policy.exemption.approved_by.strip()
          and policy.exemption.approved_on.strip()
          and policy.exemption.reason.strip())
    check("it declares no age anchor", policy.age_basis is None)
    check("neither assignment_date nor imported_at is used as one",
          "assignment_date" not in str(policy.age_basis)
          and "imported_at" not in str(policy.age_basis))
    check("it has no cutoff of any kind",
          policy.cutoff(NOW) is None and policy.enforcement_cutoff(NOW) is None)
    check("and it is the only exemption on the platform",
          [p.policy_id for p in rr.owner_exemptions()] == [POLICY_ID])

    # Governed, not ungoverned: the relation still resolves to this policy.
    for name in (f"{SCHEMA}.{TABLE}", f"{SCHEMA}.Alpha_GPS_Baza_LOG"):
        check(f"{name} resolves to the exemption",
              rr.relation_policy(name, client_business=True) is policy)
        check(f"{name} is not reported as an ungoverned store",
              rr.ungoverned([name], client_business=True) == [])
    check("while a genuinely unregistered store still fails coverage",
          rr.ungoverned([f"{SCHEMA}.a_table_nobody_registered"],
                        client_business=True)
          == [f"{SCHEMA}.a_table_nobody_registered"])
    check("the registry as a whole still validates clean", rr.validate() == [])
    PASSED.append("the_relation_is_registered_as_owner_exempt")


def test_a_very_old_assignment_imports_normally(conn) -> None:
    """The rows the inventory counted are importable, not filtered."""
    fresh = date.today() - timedelta(days=5)
    frame = workbook_frame([
        ("1", "WX 1000A", ANCIENT.strftime("%d.%m.%Y"), "a.csv"),
        ("2", "WX 2000B", date(2019, 3, 14).strftime("%d.%m.%Y"), "b.csv"),
        ("3", "WX 3000C", fresh.strftime("%d.%m.%Y"), "c.csv"),
    ])
    result = import_workbook(conn, frame)
    check("every workbook row is stored, however old",
          assignment_dates(conn) == [ANCIENT, date(2019, 3, 14), fresh],
          str(assignment_dates(conn)))
    check("the writer reports them as inserted", result.inserted_rows == 3,
          str(result.inserted_rows))
    check("and skips nothing for retention", result.skipped_rows == 0,
          "an exempt store has nothing to drop at ingestion")
    check("the input row count is the workbook's", result.input_rows == 3)

    with conn.cursor() as cur:
        cur.execute(
            f'SELECT count(*) AS total FROM {SCHEMA}."{TABLE}" '
            "WHERE registration IS NOT NULL AND csv_filename IS NOT NULL "
            "AND raw_row_json IS NOT NULL AND workflow_run_id IS NOT NULL "
            "AND source_artifact_id IS NOT NULL AND source_sha256 IS NOT NULL"
        )
        complete = int(next(iter(cur.fetchone().values())))
    conn.rollback()
    check("with every provenance column populated as before", complete == 3,
          str(complete))
    PASSED.append("a_very_old_assignment_imports_normally")


def test_full_replace_restores_historical_rows_every_time(conn) -> None:
    """The decisive business case: delete the history, re-import, get it back.

    Under the rejected policy this was the one thing that had to be impossible.
    Under the owner's decision it is the required behaviour.
    """
    fresh = date.today() - timedelta(days=5)
    frame = workbook_frame([
        ("1", "WX 1000A", ANCIENT.strftime("%d.%m.%Y"), "a.csv"),
        ("2", "WX 2000B", date(2018, 1, 2).strftime("%d.%m.%Y"), "b.csv"),
        ("3", "WX 3000C", fresh.strftime("%d.%m.%Y"), "c.csv"),
    ])
    import_workbook(conn, frame)

    # Whatever removed them — a manual cleanup, a restore, the previous
    # candidate's sweep — the next ordinary import must restore the workbook in
    # full.
    with conn.cursor() as cur:
        cur.execute(f'DELETE FROM {SCHEMA}."{TABLE}" WHERE assignment_date < %s',
                    (date(2020, 1, 1),))
    conn.commit()
    check("the historical rows are gone before the import",
          assignment_dates(conn) == [fresh], str(assignment_dates(conn)))

    import_workbook(conn, frame)
    check("a single full-replace import restores them",
          assignment_dates(conn) == [ANCIENT, date(2018, 1, 2), fresh],
          str(assignment_dates(conn)))

    for _ in range(3):
        import_workbook(conn, frame)
    check("and repeated imports keep them",
          assignment_dates(conn) == [ANCIENT, date(2018, 1, 2), fresh],
          str(assignment_dates(conn)))
    PASSED.append("full_replace_restores_historical_rows_every_time")


def test_neither_writer_filters_by_age() -> None:
    """No ingestion retention filter survives in either GPS writer."""
    import inspect

    from jobs.alpha import import_gps_baza_log_xlsm as xlsm
    from jobs.reports.stage3 import job_stage3

    stage3 = inspect.getsource(job_stage3._load_alpha_gps_replace_all)
    legacy = inspect.getsource(xlsm._replace_target_table)
    for label, source in (("the Stage 3 writer", stage3),
                          ("the deprecated XLSM importer", legacy)):
        check(f"{label} applies no semantic-date partition",
              "partition_by_semantic_date" not in source)
        check(f"{label} computes no retention cutoff",
              "hard_retention_cutoff" not in source
              and "enforcement_cutoff" not in source)
        check(f"{label} drops nothing for retention",
              "retention_expired_rows_dropped" not in source)
    check("and neither module imports the ingestion filter at all",
          "partition_by_semantic_date" not in inspect.getsource(xlsm)
          and "partition_by_semantic_date" not in inspect.getsource(job_stage3))
    PASSED.append("neither_writer_filters_by_age")


def test_the_executor_plans_no_age_based_delete(conn) -> None:
    """Zero candidates BY POLICY — reported as an exemption, not as a clean sweep."""
    swept = [item for item in hr.CLIENT_EXTRA_SWEEPS
             if item.schema == SCHEMA and "Baza_LOG" in item.table]
    check("no sweep targets the assignment log at all", swept == [],
          str([item.qualified() for item in swept]))
    check("nor does any sweep name the exempt policy",
          [item.qualified() for item in hr.CLIENT_EXTRA_SWEEPS
           if item.policy_id == POLICY_ID] == [])
    check("and no sweep anywhere anchors on assignment_date",
          [item.qualified() for item in
           hr.PLATFORM_SWEEPS + hr.CLIENT_EXTRA_SWEEPS
           if item.anchor == "assignment_date"] == [])

    declared = [item for item in hr.CLIENT_EXEMPT_STORES
                if item.policy_id == POLICY_ID]
    check("the relation is declared exempt to the executor",
          {item.table for item in declared} == {TABLE, "Alpha_GPS_Baza_LOG"},
          str([item.qualified() for item in declared]))

    outcome = hr.exempt_outcome(declared[0], scope="ALPHA00001", dry_run=True)
    payload = outcome.as_dict()
    check("the outcome says exempt, not swept",
          payload["classification"] == hr.EXEMPT_CLASSIFICATION,
          str(payload))
    check("it is not blocked", payload["classification"] != "RETENTION_SKIPPED_BLOCKED")
    check("it is not a failure and needs no operator action",
          payload["failed_count"] == 0 and payload["defect_code"] is None)
    check("it reports zero candidates and zero deletions",
          payload["examined_count"] == 0 and payload["deleted_count"] == 0)
    check("it publishes no cutoff, because the policy defines none",
          payload["cutoff"] is None)
    check("and it says why, in the operator's words",
          "OWNER DECISION" in (payload["note"] or ""), str(payload["note"]))

    # The import history is a DIFFERENT store and stays governed.
    runs = [item for item in hr.CLIENT_EXTRA_SWEEPS
            if item.table == "alpha_gps_baza_log_import_runs"]
    check("the import history is still swept", len(runs) == 1, str(runs))
    check("under its own ceiling policy",
          runs[0].policy_id == "client_db.workflow_b_gps_assignment_import_runs")
    check("on its own start timestamp", runs[0].anchor == "started_at")

    # Belt and braces: even planted rows older than any cutoff are untouchable,
    # because there is no sweep that could reach them. Prove it through the
    # aggregate count, never by reading a customer payload.
    with conn.cursor() as cur:
        cur.execute(
            f'INSERT INTO {SCHEMA}."{TABLE}" '
            "(source_id, registration, assignment_date, csv_filename) "
            "VALUES (%s, %s, %s, %s)",
            ("9", "WX 9000Z", date(2017, 1, 1), "z.csv"),
        )
    conn.commit()
    before = len(assignment_dates(conn))
    result = hr.run(
        now=NOW, skip_clients=True, skip_filesystem=True, skip_artifacts=True,
        skip_cloudflare=True, record=False, platform_dsn=DSN,
        policy_filter=POLICY_ID,
    )
    check("a dry run for this policy plans nothing",
          result["totals"]["deleted"] == 0 and result["totals"]["examined"] == 0,
          str(result["totals"]))
    check("and the planted historical row is still there",
          len(assignment_dates(conn)) == before)
    PASSED.append("the_executor_plans_no_age_based_delete")


CLIENT_INVENTORY_SQL = """
CREATE SCHEMA IF NOT EXISTS workflow_a_control;
CREATE TABLE IF NOT EXISTS workflow_a_control.client_account (
    client_code                  text PRIMARY KEY,
    client_db_host               text,
    client_db_port               integer,
    client_db_name               text,
    client_db_user               text,
    client_db_password_secret_ref text,
    client_db_schema             text,
    enabled                      boolean NOT NULL DEFAULT true
);
INSERT INTO workflow_a_control.client_account
    (client_code, client_db_host, client_db_port, client_db_name,
     client_db_user, client_db_password_secret_ref, client_db_schema, enabled)
VALUES ('ALPHA00001', 'localhost', 5432, 'alpha_main', 'runtime', 'ref', 'public', true)
ON CONFLICT (client_code) DO NOTHING;
"""


def test_a_dry_run_reports_the_exemption_to_the_operator(conn) -> None:
    """The run result must name the exemption, not stay silent about it.

    Driven through the WHOLE client pass — one enabled client, its business
    database pointed back at this disposable server — so the exemption is
    observed where an operator would actually see it, not synthesised.
    """
    with conn.cursor() as cur:
        cur.execute(CLIENT_INVENTORY_SQL)
    conn.commit()

    result = hr.run(
        now=NOW, skip_filesystem=True, skip_artifacts=True, skip_cloudflare=True,
        record=False, platform_dsn=DSN, client_dsn_factory=lambda account: DSN,
    )
    exempt = [item for item in result["sweeps"]
              if item["classification"] == hr.EXEMPT_CLASSIFICATION]
    check("the client pass really ran and reported the exemption",
          len(exempt) == len(hr.CLIENT_EXEMPT_STORES), str(result["totals"]))
    check("scoped to the client whose database holds it",
          {item["scope"] for item in exempt} == {"ALPHA00001"}, str(exempt))
    check("naming both spellings of the relation",
          {item["store"] for item in exempt}
          == {f"{SCHEMA}.{TABLE}", f"{SCHEMA}.Alpha_GPS_Baza_LOG"}, str(exempt))
    check("every exempt outcome names the exempt policy",
          all(item["policy_id"] == POLICY_ID for item in exempt), str(exempt))
    check("none of them reports a candidate row",
          all(item["examined_count"] == 0 and item["deleted_count"] == 0
              for item in exempt))
    check("no sweep outcome at all is anchored on the assignment log",
          not [item for item in result["sweeps"]
               if "Baza_LOG" in item["store"]
               and item["classification"] != hr.EXEMPT_CLASSIFICATION],
          "the only thing said about this relation is that it is exempt")
    check("the totals count exemptions separately",
          result["totals"]["exempt"] == len(exempt), str(result["totals"]))
    check("and no exempt store is counted as blocked or failed",
          result["totals"]["blocked"] == 0
          and all(item["failed_count"] == 0 for item in exempt))
    check("no defect is raised for the exempt relation",
          all(POLICY_ID not in " ".join(stores)
              for stores in result["defects"].values()),
          str(result["defects"]))
    check("the summary lists the exempt stores explicitly",
          "owner_exempt_stores" in result)
    check("and the run is not asking anyone to act on them",
          all(item["defect_code"] is None for item in exempt))
    PASSED.append("a_dry_run_reports_the_exemption_to_the_operator")


def test_an_unparsable_assignment_date_still_fails_closed(conn) -> None:
    """Unchanged non-retention behaviour: a date we cannot read is refused."""
    from jobs.reports.stage3 import job_stage3

    before = assignment_dates(conn)
    frame = workbook_frame([("1", "WX 1000A", "not-a-date", "a.csv")])
    try:
        import_workbook(conn, frame)
    except job_stage3.Stage3WriterValidationError as exc:
        check("the refusal names the parse failure",
              "Data przydziału" in str(exc) or "assignment" in str(exc).lower(),
              str(exc)[:160])
    else:
        raise AssertionError("an unparsable assignment date must be refused")
    check("and nothing was replaced", assignment_dates(conn) == before)
    PASSED.append("an_unparsable_assignment_date_still_fails_closed")


#: Fixed, so nothing here depends on the day the suite runs. The exemption is
#: clock-independent by construction — that is most of the point.
NOW = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)

#: Set once the disposable server is up; the executor is driven against it.
DSN = ""


def main() -> int:
    global DSN
    try:
        context = disposable_postgres(label="gpsret")
    except DisposablePostgresUnavailable as exc:
        print(f"NOT AVAILABLE - {exc}")
        return 0
    try:
        with context as (dsn, info):
            print(f"disposable PostgreSQL {info['server_version']} on port {info['port']}")
            DSN = dsn
            conn = connect(dsn)
            try:
                with conn.cursor() as cur:
                    cur.execute(FIXTURE_SQL)
                conn.commit()
                test_the_relation_is_registered_as_owner_exempt()
                test_a_very_old_assignment_imports_normally(conn)
                test_full_replace_restores_historical_rows_every_time(conn)
                test_neither_writer_filters_by_age()
                test_the_executor_plans_no_age_based_delete(conn)
                test_a_dry_run_reports_the_exemption_to_the_operator(conn)
                test_an_unparsable_assignment_date_still_fails_closed(conn)
            finally:
                conn.close()
    except DisposablePostgresUnavailable as exc:
        print(f"NOT AVAILABLE - {exc}")
        return 0

    for name in PASSED:
        print(f"PASS {name}")
    print(f"\n{len(PASSED)} checks passed — GPS assignment log exemption")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
