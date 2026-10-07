#!/usr/bin/env python3
"""Deterministic regressions for retrospective `public.runs` reconciliation.

The capability exists because an abandoned run stays `RUNNING` forever and the
only existing writer, `PATCH /runs/{run_id}`, stamps `ended_at = now()`. Using
it months later would record an execution that never happened, so the ledger
would stop telling the truth in a *new* way while appearing to be repaired.

The properties these tests falsify are therefore all about truthfulness:

  * a reconciliation must be indistinguishable from nothing at all until an
    operator passes `--execute`;
  * the stored end time must be the supplied historical one, never `now()`;
  * a naturally finalized run and a reconciled run must remain distinguishable
    afterwards, forever;
  * a row that reached a terminal state — including one another writer settled
    between the dry run and the execution — must lose the CAS untouched;
  * nothing may be inferred: not the terminal status, not the timestamp, not
    the justification.

Pure checks always run. Set PUBLIC_RUNS_RECONCILIATION_TEST_DSN only to a
*disposable* PostgreSQL 16 database — never logdb — for the schema, CAS,
transaction and provenance checks:

  docker run -d --rm --name recon-pg -e POSTGRES_PASSWORD=recon \\
      -e POSTGRES_USER=loguser -e POSTGRES_DB=recon_test \\
      -p 55764:5432 postgres:16
  PUBLIC_RUNS_RECONCILIATION_TEST_DSN='postgresql://loguser:recon@127.0.0.1:55764/recon_test' \\
      .venv/bin/python ops/tests_manual/test_public_runs_historical_reconciliation.py

The DSN is asserted not to be production before a single statement is issued.
"""
from __future__ import annotations

import ast
import os
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ops.tests_manual.postgres_dsn_safety import (  # noqa: E402
    require_loopback_dsn_or_exit,
)
import ops.reconcile_historical_run as recon  # noqa: E402

MIGRATION_NAME = "064_public_runs_historical_reconciliation.sql"
MIGRATION_PATH = REPO_ROOT / "db" / "migrations" / MIGRATION_NAME

FAILURES: list[str] = []
RUN_ID = "612faf1d-917a-4f04-8a01-6f27d5057b90"
OTHER_RUN_ID = "89706b8e-9f58-478a-88b3-06bd587f60d6"
HEAD40 = "d69fc5e94e78d6cc9417d8497de2801a398c709e"

STARTED = datetime(2026, 7, 10, 1, 21, 21, 119867, tzinfo=timezone.utc)
ENDED = datetime(2026, 7, 10, 1, 21, 21, 260753, tzinfo=timezone.utc)
NOW = datetime(2026, 8, 18, 10, 0, 0, tzinfo=timezone.utc)


def check(label: str, condition: bool) -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        FAILURES.append(label)


def raises(label: str, classification: str, fn) -> None:
    try:
        fn()
    except recon.ReconciliationError as exc:
        check(f"{label} -> {classification}", exc.classification == classification)
    except Exception as exc:  # noqa: BLE001
        check(f"{label} -> {classification}", False)
        print(f"       unexpected {type(exc).__name__}: {exc}")
    else:
        check(f"{label} -> {classification}", False)


# ---------------------------------------------------------------------------
# Migration text contract
# ---------------------------------------------------------------------------

def test_migration_file_contract() -> None:
    print("\n=== migration contract ===")
    check("064 migration exists", MIGRATION_PATH.is_file())
    sql = MIGRATION_PATH.read_text(encoding="utf-8")

    check("creates ops_control.run_reconciliation",
          "CREATE TABLE IF NOT EXISTS ops_control.run_reconciliation" in sql)
    check("run_id is the primary key, so duplicate reconciliation is a DB invariant",
          re.search(r"run_id\s+UUID\s+PRIMARY KEY", sql) is not None)
    check("terminal vocabulary is exactly FAILED/CANCELED",
          "CHECK (reconciled_status IN ('FAILED', 'CANCELED'))" in sql)
    check("SUCCESS is never an admissible reconciliation status",
          "'SUCCESS'" not in sql.split("ck_run_reconciliation_status")[1].split(";")[0])
    check("historical_ended_at is bounded below by the run's own start",
          "historical_ended_at >= run_started_at" in sql)
    check("historical_ended_at is bounded above by the reconciliation clock",
          "historical_ended_at <= reconciled_at" in sql)
    check("evidence survives deletion of the run it describes",
          "ON DELETE RESTRICT" in sql)
    check("repository_head must be a real 40-char SHA",
          "repository_head ~ '^[0-9a-f]{40}$'" in sql)
    check("actor/reason/approval_ref are non-empty and bounded",
          "btrim(actor) <> ''" in sql
          and "btrim(reason) <> ''" in sql
          and "btrim(approval_ref) <> ''" in sql)
    check("migration seeds no reconciliation row",
          "INSERT INTO ops_control.run_reconciliation" not in sql)
    check("migration installs no trigger or function that could mutate runs",
          "CREATE TRIGGER" not in sql.upper())
    check("migration never updates public.runs",
          not re.search(r"UPDATE\s+public\.runs", sql, re.IGNORECASE))


# ---------------------------------------------------------------------------
# Pure validation — nothing here may reach a database
# ---------------------------------------------------------------------------

def test_identity_is_explicit_and_singular() -> None:
    print("\n=== explicit identity ===")
    check("a valid uuid is normalized", recon.parse_run_id(f"  {RUN_ID}  ") == RUN_ID)
    raises("a non-uuid target", "INVALID_RUN_ID", lambda: recon.parse_run_id("not-a-uuid"))
    raises("a wildcard target", "INVALID_RUN_ID", lambda: recon.parse_run_id("*"))
    raises("the word 'all'", "INVALID_RUN_ID", lambda: recon.parse_run_id("all"))
    raises("a comma-separated list", "INVALID_RUN_ID",
           lambda: recon.parse_run_id(f"{RUN_ID},{OTHER_RUN_ID}"))
    raises("an empty target", "INVALID_RUN_ID", lambda: recon.parse_run_id(""))

    parser = recon.build_parser()
    args = parser.parse_args([
        "reconcile", "--run-id", RUN_ID, "--status", "FAILED",
        "--ended-at", ENDED.isoformat(), "--actor", "a", "--reason", "r",
        "--approval-ref", "ref",
    ])
    check("--run-id binds a single scalar, never a list", isinstance(args.run_id, str))
    check("no bulk/all/stale sweep flag exists on the CLI",
          not any(opt in parser.format_help() for opt in ("--all", "--all-stale", "--sweep")))


def test_terminal_status_is_explicit() -> None:
    print("\n=== explicit terminal status ===")
    check("FAILED is accepted", recon.parse_status("failed") == "FAILED")
    check("CANCELED is accepted", recon.parse_status("CANCELED") == "CANCELED")
    raises("SUCCESS is refused — reconciliation never fabricates an outcome",
           "UNSUPPORTED_STATUS", lambda: recon.parse_status("SUCCESS"))
    raises("RUNNING is refused", "UNSUPPORTED_STATUS", lambda: recon.parse_status("RUNNING"))
    raises("an invented status is refused", "UNSUPPORTED_STATUS",
           lambda: recon.parse_status("ABANDONED"))
    raises("an empty status is refused, never defaulted", "UNSUPPORTED_STATUS",
           lambda: recon.parse_status(""))

    check("the tool's vocabulary is narrower than the API constant",
          set(recon.RECONCILABLE_STATUSES) == {"FAILED", "CANCELED"})

    # The CLI must not carry a default that could silently pick for the operator.
    action = next(a for a in recon.build_parser()._subparsers._group_actions[0]
                  .choices["reconcile"]._actions if a.dest == "status")
    check("--status has no default", action.default is None and action.required)


def test_ended_at_must_be_truthful() -> None:
    print("\n=== truthful historical ended_at ===")
    parsed = recon.parse_ended_at(ENDED.isoformat())
    check("an offset-aware timestamp parses", parsed == ENDED)
    raises("a naive timestamp is refused rather than guessed", "INVALID_ENDED_AT",
           lambda: recon.parse_ended_at("2026-07-10T01:21:21"))
    raises("garbage is refused", "INVALID_ENDED_AT",
           lambda: recon.parse_ended_at("yesterday"))
    raises("an empty value is refused", "INVALID_ENDED_AT",
           lambda: recon.parse_ended_at(""))

    recon.validate_timestamps(started_at=STARTED, ended_at=ENDED, now=NOW)
    check("a plausible historical instant is accepted", True)
    recon.validate_timestamps(started_at=STARTED, ended_at=STARTED, now=NOW)
    check("ended_at == started_at is accepted (a run may die immediately)", True)

    raises("ended_at before started_at", "ENDED_AT_BEFORE_STARTED_AT",
           lambda: recon.validate_timestamps(
               started_at=STARTED, ended_at=STARTED - timedelta(seconds=1), now=NOW))
    raises("ended_at in the future", "ENDED_AT_IN_FUTURE",
           lambda: recon.validate_timestamps(
               started_at=STARTED, ended_at=NOW + timedelta(seconds=1), now=NOW))

    src = Path(recon.__file__).read_text(encoding="utf-8")
    update = src.split("UPDATE public.runs")[1].split("RETURNING")[0]
    check("the UPDATE never stamps now() into ended_at",
          "now()" not in update.lower() and "ended_at = %s" in update)


def test_provenance_is_mandatory() -> None:
    print("\n=== mandatory provenance ===")
    check("a stated reason is kept",
          recon.require_text(" why ", field="--reason", maximum=500) == "why")
    raises("a missing actor", "MISSING_PROVENANCE",
           lambda: recon.require_text(None, field="--actor", maximum=200))
    raises("a blank reason", "MISSING_PROVENANCE",
           lambda: recon.require_text("   ", field="--reason", maximum=500))
    raises("a missing approval reference", "MISSING_PROVENANCE",
           lambda: recon.require_text("", field="--approval-ref", maximum=200))
    raises("an over-long reason", "INVALID_PROVENANCE",
           lambda: recon.require_text("x" * 501, field="--reason", maximum=500))
    check("evidence_ref stays optional",
          recon.require_text(None, field="--evidence-ref", maximum=500, required=False) is None)


def test_precondition_guard_is_exact() -> None:
    print("\n=== reconcilable precondition ===")
    running = {"run_id": RUN_ID, "status": "RUNNING", "ended_at": None}
    recon.assert_reconcilable(running, None)
    check("a RUNNING row with no ended_at is reconcilable", True)

    raises("an already-terminal SUCCESS row", "RUN_NOT_RECONCILABLE",
           lambda: recon.assert_reconcilable(
               {"run_id": RUN_ID, "status": "SUCCESS", "ended_at": ENDED}, None))
    raises("an already-terminal FAILED row", "RUN_NOT_RECONCILABLE",
           lambda: recon.assert_reconcilable(
               {"run_id": RUN_ID, "status": "FAILED", "ended_at": ENDED}, None))
    raises("a RUNNING row that already carries an ended_at", "RUN_NOT_RECONCILABLE",
           lambda: recon.assert_reconcilable(
               {"run_id": RUN_ID, "status": "RUNNING", "ended_at": ENDED}, None))
    raises("a run reconciled once already", "ALREADY_RECONCILED",
           lambda: recon.assert_reconcilable(running, {
               "reconciled_at": NOW, "reconciled_status": "FAILED"}))

    check("the precondition demands both status and a null ended_at",
          "status = 'RUNNING'" in recon.RECONCILABLE_PRECONDITION
          and "ended_at IS NULL" in recon.RECONCILABLE_PRECONDITION)


def test_p1i_live_finalization_is_untouched() -> None:
    print("\n=== P1-I live contract is not weakened ===")
    api_src = (REPO_ROOT / "api" / "main.py").read_text(encoding="utf-8")
    check("the live endpoint keeps its one-way terminal guard",
          "TERMINAL_RUN_STATUSES" in api_src
          and "NOT IN ('SUCCESS','FAILED','CANCELED')" in api_src)
    check("the live endpoint still stamps its own ended_at",
          "UPDATE runs SET status=%s, ended_at=%s" in api_src)

    client_src = (REPO_ROOT / "api" / "client.py").read_text(encoding="utf-8")
    check("run_context still finalizes SUCCESS outside its try (P1-I)",
          "RunFinalizationError" in client_src
          and "RUN_FINALIZATION_FAILED" in client_src)

    recon_src = Path(recon.__file__).read_text(encoding="utf-8")
    tree = ast.parse(recon_src)

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    check("the reconciliation tool imports no part of the live API",
          not any(m == "api" or m.startswith("api.") for m in imported))

    # Only statements actually handed to a cursor count. The dry run's human
    # readable `mutation` string necessarily *describes* the same UPDATE, and a
    # raw text scan cannot tell a description from an execution.
    def sql_text(node) -> str | None:
        """Literal SQL of an execute() argument, f-strings included.

        The reconciliation UPDATE interpolates `RECONCILABLE_PRECONDITION`, so
        it is a JoinedStr. Reading only ast.Constant would silently collect
        nothing and let every assertion below pass vacuously.
        """
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            parts = [v.value for v in node.values
                     if isinstance(v, ast.Constant) and isinstance(v.value, str)]
            # Substitute the module-level constants the f-string splices in, so
            # the guard clause is visible to the assertions.
            return "".join(parts) + " " + recon.RECONCILABLE_PRECONDITION
        return None

    executed = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "execute"
                and node.args):
            text = sql_text(node.args[0])
            if text is not None:
                executed.append(text)
    check("the AST scan actually found the tool's SQL", len(executed) >= 4)

    runs_updates = [s for s in executed if re.search(r"UPDATE\s+public\.runs", s, re.I)]
    check("exactly one executed statement updates public.runs", len(runs_updates) == 1)
    check("no executed statement deletes from public.runs",
          not any(re.search(r"DELETE\s+FROM\s+public\.runs", s, re.I) for s in executed))
    check("no executed statement updates runs without the CAS guard",
          all("status = 'RUNNING' AND ended_at IS NULL" in s for s in runs_updates))
    check("the reconciliation UPDATE is strictly narrower than the API's guard",
          "status = 'RUNNING' AND ended_at IS NULL" in recon_src)


# ---------------------------------------------------------------------------
# Disposable PostgreSQL
# ---------------------------------------------------------------------------

def _bootstrap(conn) -> None:
    """Minimal parent schema plus the migration under test."""
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS ops_control.run_reconciliation CASCADE")
        cur.execute("DROP TABLE IF EXISTS public.logs CASCADE")
        cur.execute("DROP TABLE IF EXISTS public.runs CASCADE")
        cur.execute("""
            CREATE TABLE public.runs (
              run_id uuid PRIMARY KEY,
              started_at timestamptz,
              ended_at timestamptz,
              status text,
              trigger text,
              source text,
              actor text,
              params jsonb
            )
        """)
        cur.execute("""
            CREATE TABLE public.logs (
              id bigserial PRIMARY KEY, ts timestamptz, run_id uuid, message text
            )
        """)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute(MIGRATION_PATH.read_text(encoding="utf-8"))
    conn.commit()


def _seed_running(conn, run_id: str = RUN_ID) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO public.runs (run_id, started_at, ended_at, status, trigger, source) "
            "VALUES (%s, %s, NULL, 'RUNNING', 'MANUAL', 'jobs.reports.stage3.job_stage3')",
            (run_id, STARTED),
        )
        cur.execute(
            "INSERT INTO public.logs (ts, run_id, message) VALUES (%s, %s, 'last event')",
            (ENDED, run_id),
        )
    conn.commit()


def _plan(run_id: str = RUN_ID, *, status: str = "FAILED", ended_at=ENDED,
          started_at=STARTED) -> dict:
    return {
        "run_id": run_id,
        "run_source": "jobs.reports.stage3.job_stage3",
        "run_started_at": started_at,
        "status": status,
        "ended_at": ended_at,
        "actor": "owner",
        "reason": "process interrupted before finalization; superseded by a later success",
        "approval_ref": "OPS-2026-08-18-run-ledger",
        "repository_head": HEAD40,
        "evidence_ref": "docs/07_operations.md#58",
    }


def test_schema_shape(conn) -> None:
    print("\n=== pg: schema shape ===")
    with conn.cursor() as cur:
        cur.execute("""
            SELECT column_name, is_nullable FROM information_schema.columns
             WHERE table_schema='ops_control' AND table_name='run_reconciliation'
        """)
        cols = {r["column_name"]: r["is_nullable"] for r in cur.fetchall()}
    for required in ("run_id", "run_source", "run_started_at", "reconciled_status",
                     "historical_ended_at", "reconciled_at", "actor", "reason",
                     "approval_ref", "repository_head"):
        check(f"{required} exists and is NOT NULL",
              cols.get(required) == "NO")
    check("evidence_ref is optional", cols.get("evidence_ref") == "YES")
    conn.rollback()


def test_dry_run_writes_nothing(conn) -> None:
    print("\n=== pg: dry run mutates nothing ===")
    _seed_running(conn)
    run = recon.load_run(conn, RUN_ID)
    recon.assert_reconcilable(run, recon.load_existing_reconciliation(conn, RUN_ID))
    recon.validate_timestamps(started_at=run["started_at"], ended_at=ENDED, now=NOW)
    # A dry run performs exactly the reads above and then stops.
    with conn.cursor() as cur:
        cur.execute("SELECT status, ended_at FROM public.runs WHERE run_id=%s", (RUN_ID,))
        after = cur.fetchone()
        cur.execute("SELECT count(*) AS n FROM ops_control.run_reconciliation")
        evidence_rows = cur.fetchone()["n"]
    check("the run is still RUNNING after a dry run", after["status"] == "RUNNING")
    check("the run still has no ended_at after a dry run", after["ended_at"] is None)
    check("no provenance row was created by a dry run", evidence_rows == 0)


def test_execute_uses_the_supplied_historical_time(conn) -> None:
    print("\n=== pg: execute stores the historical time, not now() ===")
    result = recon.execute_reconciliation(conn, plan=_plan())
    with conn.cursor() as cur:
        cur.execute("SELECT status, ended_at FROM public.runs WHERE run_id=%s", (RUN_ID,))
        row = cur.fetchone()
    check("the run reached the explicitly chosen terminal status", row["status"] == "FAILED")
    check("the stored ended_at is the supplied historical instant", row["ended_at"] == ENDED)
    check("the stored ended_at is emphatically not the reconciliation time",
          abs((datetime.now(timezone.utc) - row["ended_at"]).days) > 30)
    check("the tool reported what it wrote", result["run"]["status"] == "FAILED")


def test_provenance_is_durable_and_queryable(conn) -> None:
    print("\n=== pg: provenance is durable and queryable ===")
    with conn.cursor() as cur:
        cur.execute("""
            SELECT r.status, r.ended_at, rc.reconciled_status, rc.historical_ended_at,
                   rc.reconciled_at, rc.actor, rc.reason, rc.approval_ref,
                   rc.repository_head, rc.evidence_ref
              FROM public.runs r
              JOIN ops_control.run_reconciliation rc USING (run_id)
             WHERE r.run_id = %s
        """, (RUN_ID,))
        row = cur.fetchone()
    check("a provenance row exists for the reconciled run", row is not None)
    check("it records the chosen status", row["reconciled_status"] == "FAILED")
    check("it records the supplied historical end", row["historical_ended_at"] == ENDED)
    check("it records the actor", row["actor"] == "owner")
    check("it records the reason", "interrupted" in row["reason"])
    check("it records the approval reference", row["approval_ref"].startswith("OPS-"))
    check("it records the repository head", row["repository_head"] == HEAD40)
    check("the reconciliation clock is distinct from the historical end",
          row["reconciled_at"] > row["historical_ended_at"])


def test_reconciled_and_natural_stay_distinguishable(conn) -> None:
    print("\n=== pg: an administrative outcome never looks observed ===")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO public.runs (run_id, started_at, ended_at, status, trigger, source) "
            "VALUES (%s, %s, %s, 'SUCCESS', 'MANUAL', 'jobs.reports.workflow_b.orchestrator')",
            (OTHER_RUN_ID, STARTED, ENDED),
        )
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT r.run_id, (rc.run_id IS NOT NULL) AS reconciled
              FROM public.runs r
              LEFT JOIN ops_control.run_reconciliation rc USING (run_id)
             ORDER BY r.started_at
        """)
        rows = {str(r["run_id"]): r["reconciled"] for r in cur.fetchall()}
    check("the reconciled run is flagged", rows[RUN_ID] is True)
    check("the naturally finalized run is not flagged", rows[OTHER_RUN_ID] is False)
    check("both are terminal, so status alone cannot distinguish them", True)


def test_already_terminal_row_is_rejected(conn) -> None:
    print("\n=== pg: an already-terminal row is refused ===")
    try:
        recon.execute_reconciliation(conn, plan=_plan(OTHER_RUN_ID))
    except recon.ReconciliationError as exc:
        check("a settled SUCCESS row loses the CAS", exc.classification == "CAS_LOST")
    else:
        check("a settled SUCCESS row loses the CAS", False)
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM public.runs WHERE run_id=%s", (OTHER_RUN_ID,))
        check("the settled row was not overwritten", cur.fetchone()["status"] == "SUCCESS")
        cur.execute("SELECT count(*) AS n FROM ops_control.run_reconciliation")
        check("no orphan provenance row was left behind", cur.fetchone()["n"] == 1)


def test_concurrent_change_loses_the_cas(conn) -> None:
    print("\n=== pg: a concurrently settled row is not overwritten ===")
    race_id = str(uuid.uuid4())
    _seed_running(conn, race_id)
    # Simulate the real race: the operator read a RUNNING row, and a live
    # finalizer settled it before --execute reached the database.
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE public.runs SET status='SUCCESS', ended_at=%s WHERE run_id=%s",
            (ENDED, race_id),
        )
    conn.commit()
    try:
        recon.execute_reconciliation(conn, plan=_plan(race_id))
    except recon.ReconciliationError as exc:
        check("the reconciliation loses the CAS", exc.classification == "CAS_LOST")
    else:
        check("the reconciliation loses the CAS", False)
    with conn.cursor() as cur:
        cur.execute("SELECT status, ended_at FROM public.runs WHERE run_id=%s", (race_id,))
        row = cur.fetchone()
        check("the winner's status survives", row["status"] == "SUCCESS")
        check("the winner's ended_at survives", row["ended_at"] == ENDED)
        cur.execute(
            "SELECT count(*) AS n FROM ops_control.run_reconciliation WHERE run_id=%s",
            (race_id,),
        )
        check("the losing attempt wrote no provenance", cur.fetchone()["n"] == 0)


def test_duplicate_reconciliation_is_refused(conn) -> None:
    print("\n=== pg: duplicate reconciliation is a database invariant ===")
    dup_id = str(uuid.uuid4())
    _seed_running(conn, dup_id)
    recon.execute_reconciliation(conn, plan=_plan(dup_id))
    existing = recon.load_existing_reconciliation(conn, dup_id)
    check("the first reconciliation is recorded", existing is not None)
    raises("a second reconciliation of the same run", "ALREADY_RECONCILED",
           lambda: recon.assert_reconcilable(
               recon.load_run(conn, dup_id), existing))
    # Even bypassing the preflight, the row is now terminal so the CAS refuses.
    try:
        recon.execute_reconciliation(conn, plan=_plan(dup_id))
    except recon.ReconciliationError as exc:
        check("bypassing the preflight still fails closed",
              exc.classification == "CAS_LOST")
    else:
        check("bypassing the preflight still fails closed", False)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM ops_control.run_reconciliation WHERE run_id=%s",
            (dup_id,),
        )
        check("exactly one provenance row exists", cur.fetchone()["n"] == 1)


def test_database_enforces_the_invariants_independently(conn) -> None:
    print("\n=== pg: the database keeps its own copy of every invariant ===")
    guard_id = str(uuid.uuid4())
    _seed_running(conn, guard_id)

    def insert(**over):
        base = dict(
            run_id=guard_id, run_source="s", run_started_at=STARTED,
            reconciled_status="FAILED", historical_ended_at=ENDED,
            actor="a", reason="r", approval_ref="ref", repository_head=HEAD40,
        )
        base.update(over)
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO ops_control.run_reconciliation
                    (run_id, run_source, run_started_at, reconciled_status,
                     historical_ended_at, actor, reason, approval_ref, repository_head)
                VALUES (%(run_id)s, %(run_source)s, %(run_started_at)s,
                        %(reconciled_status)s, %(historical_ended_at)s, %(actor)s,
                        %(reason)s, %(approval_ref)s, %(repository_head)s)
            """, base)

    def refuses(label: str, **over) -> None:
        try:
            with conn.transaction():
                insert(**over)
        except Exception:  # noqa: BLE001 - any integrity error is a pass
            check(label, True)
        else:
            check(label, False)

    refuses("SUCCESS is rejected by the status constraint", reconciled_status="SUCCESS")
    refuses("ended_at before started_at is rejected",
            historical_ended_at=STARTED - timedelta(seconds=1))
    refuses("a future ended_at is rejected",
            historical_ended_at=datetime.now(timezone.utc) + timedelta(days=1))
    refuses("a blank reason is rejected", reason="   ")
    refuses("a blank actor is rejected", actor="")
    refuses("a blank approval_ref is rejected", approval_ref=" ")
    refuses("a non-SHA repository_head is rejected", repository_head="HEAD")
    refuses("an unknown run_id is rejected by the foreign key",
            run_id=str(uuid.uuid4()))

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM ops_control.run_reconciliation WHERE run_id=%s",
                    (guard_id,))
        check("no refused insert left a row", cur.fetchone()["n"] == 0)
    conn.commit()


def test_evidence_survives_run_deletion(conn) -> None:
    print("\n=== pg: evidence is not erased with its subject ===")
    with conn.cursor() as cur:
        try:
            with conn.transaction():
                cur.execute("DELETE FROM public.runs WHERE run_id=%s", (RUN_ID,))
        except Exception:  # noqa: BLE001
            check("deleting a reconciled run is restricted", True)
        else:
            check("deleting a reconciled run is restricted", False)


# ---------------------------------------------------------------------------
# Cross-contract: platform prune must retain a reconciled run
#
# The FK in migration 064 is ON DELETE RESTRICT, and `_delete_objects` runs
# BEFORE the database transaction. A reconciled run reaching `plan.run_ids`
# would therefore delete MinIO objects and then roll back every DB deletion in
# the batch. These tests exercise the real `build_prune_plan` against a real
# schema so the exclusion is proven where it actually has to happen.
# ---------------------------------------------------------------------------

#: The planner refuses to plan against a catalog it has not reviewed, and it
#: compares the WHOLE foreign key — referencing and referenced endpoints, the
#: delete action, and the referencing column's nullability. So this fixture has
#: to carry production's actual contract, not merely tables of the right names:
#: a `NO ACTION` stand-in for a `CASCADE` reference is exactly the drift the
#: guard exists to stop, and it would stop this suite too.
#:
#: These tables are never written to here. They exist so the reference contract
#: is satisfiable and so the planner's `EXISTS` subqueries resolve; the subject
#: of this section is `ops_control.run_reconciliation`, not artifact retention.
PRUNE_SCHEMA = """
CREATE SCHEMA IF NOT EXISTS ingest;
CREATE TABLE public.artifacts (
  artifact_id uuid PRIMARY KEY, run_id uuid REFERENCES public.runs(run_id),
  storage_key text, storage_backend text DEFAULT 'S3', raw_file_id uuid,
  workflow_name text DEFAULT 'platform', created_at timestamptz NOT NULL
);
-- Curation. ON DELETE CASCADE over NOT NULL columns (migrations 024, 026).
CREATE TABLE public.artifact_metadata_overrides (
  artifact_id uuid NOT NULL
    REFERENCES public.artifacts(artifact_id) ON DELETE CASCADE);
CREATE TABLE public.artifact_tags (
  artifact_id uuid NOT NULL
    REFERENCES public.artifacts(artifact_id) ON DELETE CASCADE);
CREATE TABLE public.artifact_virtual_folder_items (
  artifact_id uuid NOT NULL
    REFERENCES public.artifacts(artifact_id) ON DELETE CASCADE);
-- Separate retention and Workflow B. ON DELETE SET NULL over nullable columns
-- (migrations 043, 048).
CREATE TABLE public.database_export_jobs (
  artifact_id uuid REFERENCES public.artifacts(artifact_id) ON DELETE SET NULL);
CREATE TABLE ingest.raw_file (
  id uuid PRIMARY KEY,
  stage2_cleaned_artifact_id uuid
    REFERENCES public.artifacts(artifact_id) ON DELETE SET NULL);
-- Portal V1 generated reports (migration 068). SET NULL over a nullable column
-- so the member outlives its bytes; `is_available` is projected by the planner,
-- so the column has to be here even though no member is ever seeded.
CREATE TABLE public.portal_generated_report_files (
  member_id uuid PRIMARY KEY,
  artifact_id uuid REFERENCES public.artifacts(artifact_id) ON DELETE SET NULL,
  is_available boolean NOT NULL DEFAULT TRUE);
"""

CUTOFF = datetime(2026, 8, 1, tzinfo=timezone.utc)
OLD = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _prune_bootstrap(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS ingest.raw_file CASCADE")
        for t in ("portal_generated_report_files", "database_export_jobs",
                  "artifact_virtual_folder_items", "artifact_tags",
                  "artifact_metadata_overrides", "artifacts"):
            cur.execute(f"DROP TABLE IF EXISTS public.{t} CASCADE")
        cur.execute(PRUNE_SCHEMA)
    conn.commit()


def _seed_prune_run(conn, run_id: str, *, status: str = "FAILED") -> None:
    """Seed one aged run. RUNNING rows are seeded non-terminal, as they really are."""
    ended = None if status == "RUNNING" else OLD
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO public.runs (run_id, started_at, ended_at, status, trigger, source) "
            "VALUES (%s, %s, %s, %s, 'MANUAL', 'jobs.reports.stage3.job_stage3')",
            (run_id, OLD, ended, status),
        )
    conn.commit()


def _plan_for(conn):
    import api.platform_prune as prune
    with conn.cursor() as cur:
        return prune.build_prune_plan(
            cur, cutoff=CUTOFF, retention_days=60, lock_rows=False,
            now_utc=datetime(2026, 9, 30, tzinfo=timezone.utc),
        )


def test_prune_retains_a_reconciled_run(conn) -> None:
    print("\n=== pg: platform prune retains a reconciled run ===")
    _prune_bootstrap(conn)

    eligible = str(uuid.uuid4())
    reconciled = str(uuid.uuid4())
    _seed_prune_run(conn, eligible)                        # already terminal
    _seed_prune_run(conn, reconciled, status="RUNNING")    # abandoned, aged

    plan = _plan_for(conn)
    check("an ordinary aged terminal run is prune-eligible", eligible in plan.run_ids)
    check("the abandoned run is excluded as active before reconciliation",
          reconciled not in plan.run_ids)

    recon.execute_reconciliation(
        conn, plan=_plan(reconciled, ended_at=OLD, started_at=OLD))

    # It is now terminal and older than the cutoff, so ONLY the reconciliation
    # reference can keep it out of the plan.
    with conn.cursor() as cur:
        cur.execute("SELECT status, ended_at FROM public.runs WHERE run_id=%s", (reconciled,))
        row = cur.fetchone()
    check("the reconciled run is now terminal and aged",
          row["status"] == "FAILED" and row["ended_at"] == OLD)

    plan = _plan_for(conn)
    check("the reconciled run is excluded from the plan",
          reconciled not in plan.run_ids)
    check("exclusion is reported as a retained reference",
          plan.excluded.get("run_retained_reference", 0) >= 1)
    check("a mixed batch still prunes the independently eligible run",
          eligible in plan.run_ids)
    check("no destructive object work is implied for the reconciled run",
          plan.artifacts == [])

    # The FK is what makes the exclusion necessary; prove it is still armed.
    with conn.cursor() as cur:
        try:
            with conn.transaction():
                cur.execute("DELETE FROM public.runs WHERE run_id=%s", (reconciled,))
        except Exception:  # noqa: BLE001
            check("ON DELETE RESTRICT still refuses to delete the reconciled run", True)
        else:
            check("ON DELETE RESTRICT still refuses to delete the reconciled run", False)
    # ...and that the plan the tool produced really is executable end to end:
    # the exact DELETE `_delete_database_rows` issues must succeed for every
    # planned row. This is the assertion that would fail if a reconciled run
    # ever leaked into the plan.
    with conn.cursor() as cur:
        with conn.transaction():
            cur.execute("DELETE FROM public.runs WHERE run_id = ANY(%s::uuid[])",
                        (plan.run_ids,))
            check("every planned run row deletes cleanly without hitting the FK",
                  cur.rowcount == len(plan.run_ids))
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM public.runs WHERE run_id=%s", (reconciled,))
        check("the reconciled run survived the prune", cur.fetchone() is not None)
        cur.execute("SELECT count(*) AS n FROM ops_control.run_reconciliation WHERE run_id=%s",
                    (reconciled,))
        check("its reconciliation evidence survived too", cur.fetchone()["n"] == 1)


def test_prune_survives_missing_migration_064(conn) -> None:
    print("\n=== pg: prune works on a schema without migration 064 ===")
    _prune_bootstrap(conn)
    older = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS ops_control.run_reconciliation CASCADE")
    conn.commit()
    _seed_prune_run(conn, older)

    plan = _plan_for(conn)
    check("an unmigrated schema still produces a plan", older in plan.run_ids)
    check("absence is reported rather than silently assumed",
          plan.excluded.get("run_reconciliation_absent") == 1)

    # Restore for any later test in the same connection.
    with conn.cursor() as cur:
        cur.execute(MIGRATION_PATH.read_text(encoding="utf-8"))
    conn.commit()


def test_prune_batch_stays_consistent_for_a_reconciled_run(conn) -> None:
    """The partial-prune scenario from the review, end to end.

    `_delete_objects` runs BEFORE the database transaction. If a reconciled run
    reached `plan.run_ids`, the FK would abort the DB transaction *after* the
    objects were gone, rolling back every row deletion in the batch and leaving
    storage and ledger disagreeing. The fix is that the run never enters the
    plan, so object deletion and row deletion always agree.

    Note what this deliberately does NOT claim: a reconciled run's *artifacts*
    still follow ordinary artifact retention and may be pruned. That is safe —
    the reconciliation row is self-contained evidence (status, historical end,
    actor, reason, approval_ref, evidence_ref) and survives independently. This
    test asserts the consistency invariant that matters, not a broader
    retention policy nobody has decided.
    """
    print("\n=== pg: the prune batch stays consistent for a reconciled run ===")
    _prune_bootstrap(conn)
    reconciled = str(uuid.uuid4())
    _seed_prune_run(conn, reconciled, status="RUNNING")
    artifact_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO public.artifacts (artifact_id, run_id, storage_key, created_at) "
            "VALUES (%s, %s, %s, %s)",
            (artifact_id, reconciled, "reconciled-run-object", OLD),
        )
    conn.commit()
    recon.execute_reconciliation(
        conn, plan=_plan(reconciled, ended_at=OLD, started_at=OLD))

    plan = _plan_for(conn)
    check("the reconciled run is not planned for deletion", reconciled not in plan.run_ids)

    deleted_keys = []

    class FakeS3:
        def head_bucket(self, **_):
            pass

        def delete_object(self, **kwargs):
            deleted_keys.append(kwargs.get("Key"))

    import api.platform_prune as prune
    prune._delete_objects(FakeS3(), "bucket", plan.artifacts)

    # Whatever objects the plan contained, the row deletion that follows must
    # succeed — that is the property whose violation caused the partial prune.
    with conn.cursor() as cur:
        with conn.transaction():
            cur.execute("DELETE FROM public.artifacts WHERE artifact_id = ANY(%s::uuid[])",
                        ([c.artifact_id for c in plan.artifacts],))
            cur.execute("DELETE FROM public.runs WHERE run_id = ANY(%s::uuid[])",
                        (plan.run_ids,))
            check("the DB deletions matching the deleted objects all succeed",
                  cur.rowcount == len(plan.run_ids))
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM public.runs WHERE run_id=%s", (reconciled,))
        check("the reconciled run is still present after the batch",
              cur.fetchone()["n"] == 1)
        cur.execute("SELECT count(*) AS n FROM ops_control.run_reconciliation WHERE run_id=%s",
                    (reconciled,))
        check("its self-contained provenance is still present",
              cur.fetchone()["n"] == 1)
    check("object deletion and row deletion agreed; no rollback was needed", True)


def test_on_disposable_postgres(dsn: str) -> None:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        _bootstrap(conn)
        test_schema_shape(conn)
        test_dry_run_writes_nothing(conn)
        test_execute_uses_the_supplied_historical_time(conn)
        test_provenance_is_durable_and_queryable(conn)
        test_reconciled_and_natural_stay_distinguishable(conn)
        test_already_terminal_row_is_rejected(conn)
        test_concurrent_change_loses_the_cas(conn)
        test_duplicate_reconciliation_is_refused(conn)
        test_database_enforces_the_invariants_independently(conn)
        test_evidence_survives_run_deletion(conn)
        test_prune_retains_a_reconciled_run(conn)
        test_prune_survives_missing_migration_064(conn)
        test_prune_batch_stays_consistent_for_a_reconciled_run(conn)
        conn.rollback()


def main() -> int:
    test_migration_file_contract()
    test_identity_is_explicit_and_singular()
    test_terminal_status_is_explicit()
    test_ended_at_must_be_truthful()
    test_provenance_is_mandatory()
    test_precondition_guard_is_exact()
    test_p1i_live_finalization_is_untouched()

    dsn = os.getenv("PUBLIC_RUNS_RECONCILIATION_TEST_DSN")
    if dsn:
        require_loopback_dsn_or_exit(dsn, label="PUBLIC_RUNS_RECONCILIATION_TEST_DSN")
        test_on_disposable_postgres(dsn)
    else:
        print("\nSKIP: set PUBLIC_RUNS_RECONCILIATION_TEST_DSN for PostgreSQL checks")

    print()
    if FAILURES:
        print(f"FAIL - {len(FAILURES)} check(s) failed:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("OK - public.runs historical reconciliation checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
