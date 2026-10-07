#!/usr/bin/env python3
"""Disposable-PostgreSQL tests for atomic original forward-v5 finalization.

Requires an explicitly supplied FORWARD_V5_TEST_DSN pointing at a throwaway
database.  It refuses known production database names, never loads the
repository .env, and never touches a real promotion identity.

The suite proves that forward-v5 finalization is one transaction, that it works
against a schema-053 journal without the migration-054 resume audit columns, and
that no interruption can leave an all-steps-complete row in `in_progress`.
"""
from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from ops import environment_identity_promotion as promotion
from ops import resume_plan_v2 as resume_v2


ROOT = Path(__file__).resolve().parents[2]
DSN_ENV = "FORWARD_V5_TEST_DSN"
FORBIDDEN_DATABASES = {"logdb", "telematics_main", "alpha_main"}
PLATFORM_UUID = "666ff6cc-aa5b-4c07-8eaa-3a95d3a4bd2c"
PRODUCTION_PROMOTION_ID = "751f42e4-51b0-406d-8ac4-3ee345a6ce86"

STEPS = [
    "client_marker:TEST00001",
    promotion.STEP_CONTROL_PLANE,
    promotion.STEP_PLATFORM_MARKER,
    promotion.STEP_RUNTIME_FILE,
    promotion.STEP_RUNTIME_RELOAD,
    promotion.STEP_RUNTIME_PROCESSES,
    promotion.STEP_FINAL_VERIFY,
]

JOURNAL_COLUMNS = (
    "promotion_id::text AS promotion_id, state, current_step, completed_steps,"
    " completed_at, failed_at, error, plan_sha256, started_at"
)


def connect(dsn: str, *, autocommit: bool = False):
    return psycopg.connect(
        dsn, autocommit=autocommit, row_factory=dict_row,
        options="-c application_name=forward_v5_disposable_test",
    )


def execute_sql_file(conn, relative: str) -> None:
    with conn.cursor() as cur:
        cur.execute((ROOT / relative).read_text(encoding="utf-8"))
    conn.commit()


def synthetic_plan(nonce: str) -> dict[str, object]:
    return {
        "contract_version": 5, "promotion_plan_contract_version": 5,
        "source_environment": "local_dev", "target_environment": "production",
        "platform_uuid": PLATFORM_UUID, "test_nonce": nonce,
        "clients": [{"client_code": "TEST00001"}], "steps": list(STEPS),
    }


def seed(conn, promotion_id: str, *, state: str = "in_progress",
         completed_steps=None, current_step=promotion.STEP_FINAL_VERIFY) -> None:
    """Insert one synthetic schema-053 forward-v5 journal row."""
    plan = synthetic_plan(promotion_id)
    steps = STEPS[:-1] if completed_steps is None else completed_steps
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO ops_control.environment_identity_promotion (
                   promotion_id,source_environment,target_environment,platform_identity_id,
                   selected_clients,immutable_plan_json,plan_sha256,state,started_at,
                   current_step,completed_steps,operator_attestation_hash,backup_reference)
               VALUES (%s::uuid,'local_dev','production',%s::uuid,%s::jsonb,%s::jsonb,%s,%s,
                       now(),%s,%s::jsonb,%s,'/disposable/checkpoint')""",
            (
                promotion_id, PLATFORM_UUID,
                promotion.canonical_json(plan["clients"]),
                promotion.canonical_json(plan), promotion.plan_hash(plan), state,
                current_step, promotion.canonical_json(steps), "a" * 64,
            ),
        )
    conn.commit()


def read_row(conn, promotion_id: str) -> dict[str, object]:
    """Read journal state with schema-053 columns only."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {JOURNAL_COLUMNS} FROM ops_control.environment_identity_promotion"
            " WHERE promotion_id=%s::uuid",
            (promotion_id,),
        )
        row = dict(cur.fetchone())
    conn.rollback()
    return row


def read_all(conn) -> dict[str, dict[str, object]]:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {JOURNAL_COLUMNS} FROM ops_control.environment_identity_promotion"
            " ORDER BY promotion_id"
        )
        rows = [dict(row) for row in cur.fetchall()]
    conn.rollback()
    return {str(row["promotion_id"]): row for row in rows}


def reset(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("TRUNCATE ops_control.environment_identity_promotion")
    conn.commit()


def expect_code(code, fn) -> None:
    try:
        fn()
    except promotion.PromotionError as exc:
        assert exc.code == code, exc
    else:
        raise AssertionError(f"expected {code}")


def assert_no_dead_end(conn) -> None:
    """No row may report every frozen step complete while still active."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT count(*) AS n FROM ops_control.environment_identity_promotion
                WHERE state IN ('planned','in_progress')
                  AND completed_steps @> %s::jsonb""",
            (promotion.canonical_json([promotion.STEP_FINAL_VERIFY]),),
        )
        dead_ends = int(dict(cur.fetchone())["n"])
    conn.rollback()
    assert dead_ends == 0


def assert_rerun_classification(conn, promotion_id: str, *, terminal: bool) -> None:
    row = read_row(conn, promotion_id)
    journal = {
        "state": row["state"],
        "completed_steps": list(row["completed_steps"]),
        "current_step": row["current_step"],
    }
    if terminal:
        expect_code(
            "PROMOTION_TERMINAL",
            lambda: resume_v2.require_resume_v2_finalization_route(
                journal=journal, original_steps=STEPS,
            ),
        )
    else:
        assert resume_v2.require_resume_v2_finalization_route(
            journal=journal, original_steps=STEPS,
        ) == "resume-v2-finalization"


class _FailingCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        self._cursor.__enter__()
        return self

    def __exit__(self, *args):
        return self._cursor.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self._cursor, name)

    def execute(self, *args, **kwargs):
        self._cursor.execute(*args, **kwargs)
        raise psycopg.OperationalError("injected failure inside the atomic transaction")


class FailingCursorConnection:
    """Raises a raw database error after the atomic UPDATE but before commit."""

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def cursor(self, *args, **kwargs):
        return _FailingCursor(self._conn.cursor(*args, **kwargs))


def test_atomic_finalization_on_schema_053(dsn: str, admin) -> None:
    reset(admin)
    promotion_id = str(uuid4())
    seed(admin, promotion_id)
    before = read_row(admin, promotion_id)
    assert before["state"] == "in_progress"
    assert before["current_step"] == promotion.STEP_FINAL_VERIFY
    assert before["completed_steps"] == STEPS[:-1]

    write = connect(dsn)
    try:
        promotion.journal_finalize_forward_v5(
            write, promotion_id, expected_completed_steps=STEPS[:-1],
        )
    finally:
        write.close()

    fresh = connect(dsn)
    try:
        row = read_row(fresh, promotion_id)
        assert_no_dead_end(fresh)
        assert_rerun_classification(fresh, promotion_id, terminal=True)
    finally:
        fresh.close()
    assert row["state"] == "completed"
    assert row["current_step"] is None
    assert row["completed_at"] is not None
    assert row["failed_at"] is None
    assert row["error"] is None
    assert row["completed_steps"] == STEPS


def test_interruption_before_the_atomic_transaction(dsn: str, admin) -> None:
    reset(admin)
    promotion_id = str(uuid4())
    seed(admin, promotion_id)
    # Execution stops before the finalization call is ever issued.
    fresh = connect(dsn)
    try:
        row = read_row(fresh, promotion_id)
        assert_no_dead_end(fresh)
        assert_rerun_classification(fresh, promotion_id, terminal=False)
    finally:
        fresh.close()
    assert row["state"] == "in_progress"
    assert row["current_step"] == promotion.STEP_FINAL_VERIFY
    assert row["completed_steps"] == STEPS[:-1]


def test_exception_inside_the_atomic_transaction(dsn: str, admin) -> None:
    reset(admin)
    promotion_id = str(uuid4())
    seed(admin, promotion_id)
    write = connect(dsn)
    try:
        expect_code(
            "JOURNAL_COMPLETION_FAILED",
            lambda: promotion.journal_finalize_forward_v5(
                FailingCursorConnection(write), promotion_id,
                expected_completed_steps=STEPS[:-1],
            ),
        )
    finally:
        write.close()

    fresh = connect(dsn)
    try:
        row = read_row(fresh, promotion_id)
        assert_no_dead_end(fresh)
        assert_rerun_classification(fresh, promotion_id, terminal=False)
    finally:
        fresh.close()
    # The whole transaction rolled back: no step append without the transition.
    assert row["state"] == "in_progress"
    assert row["current_step"] == promotion.STEP_FINAL_VERIFY
    assert row["completed_steps"] == STEPS[:-1]


def test_connection_loss_after_commit(dsn: str, admin) -> None:
    reset(admin)
    promotion_id = str(uuid4())
    seed(admin, promotion_id)
    write = connect(dsn)
    with write.cursor() as cur:
        cur.execute("SELECT pg_backend_pid() AS pid")
        backend_pid = int(dict(cur.fetchone())["pid"])
    write.rollback()
    promotion.journal_finalize_forward_v5(
        write, promotion_id, expected_completed_steps=STEPS[:-1],
    )
    # The commit is durable; the session then disappears before any fresh read.
    with admin.cursor() as cur:
        cur.execute("SELECT pg_terminate_backend(%s)", (backend_pid,))
    admin.commit()
    try:
        write.close()
    except Exception:
        pass

    fresh = connect(dsn)
    try:
        row = read_row(fresh, promotion_id)
        assert_no_dead_end(fresh)
        assert_rerun_classification(fresh, promotion_id, terminal=True)
    finally:
        fresh.close()
    assert row["state"] == "completed"
    assert row["completed_steps"] == STEPS
    assert row["current_step"] is None


def test_post_commit_failure_cannot_regress_the_completed_row(dsn: str, admin) -> None:
    """A failure after the durable commit must not relabel the journal `failed`.

    The forward executor's error path calls journal_fail, so the completed state
    has to be protected by the predicate rather than by call ordering.
    """
    reset(admin)
    promotion_id = str(uuid4())
    seed(admin, promotion_id)
    promotion.journal_finalize_forward_v5(
        admin, promotion_id, expected_completed_steps=STEPS[:-1],
    )
    expect_code(
        "JOURNAL_FAILURE_COUNT",
        lambda: promotion.journal_fail(
            admin, promotion_id,
            current_step=promotion.STEP_FINAL_VERIFY,
            error="injected post-commit verification failure",
        ),
    )
    admin.rollback()
    fresh = connect(dsn)
    try:
        row = read_row(fresh, promotion_id)
        assert_no_dead_end(fresh)
        assert_rerun_classification(fresh, promotion_id, terminal=True)
    finally:
        fresh.close()
    assert row["state"] == "completed"
    assert row["completed_steps"] == STEPS
    assert row["current_step"] is None
    assert row["error"] is None


def test_fresh_read_failure_then_reconciliation(dsn: str, admin) -> None:
    reset(admin)
    promotion_id = str(uuid4())
    seed(admin, promotion_id)
    write = connect(dsn)
    try:
        promotion.journal_finalize_forward_v5(
            write, promotion_id, expected_completed_steps=STEPS[:-1],
        )
    finally:
        write.close()

    # The verification read fails on the already-closed session.
    try:
        read_row(write, promotion_id)
    except psycopg.Error:
        pass
    else:
        raise AssertionError("expected the closed verification read to fail")

    # Read-only reconciliation observes the durable completed state.
    reconcile = connect(dsn)
    try:
        row = read_row(reconcile, promotion_id)
        assert_no_dead_end(reconcile)
        assert_rerun_classification(reconcile, promotion_id, terminal=True)
    finally:
        reconcile.close()
    assert row["state"] == "completed"
    assert row["completed_steps"] == STEPS


def test_rerun_after_an_ambiguous_post_commit_result(dsn: str, admin) -> None:
    reset(admin)
    promotion_id = str(uuid4())
    seed(admin, promotion_id)
    promotion.journal_finalize_forward_v5(
        admin, promotion_id, expected_completed_steps=STEPS[:-1],
    )
    # A rerun after an ambiguous result never double-appends or reopens the row.
    expect_code(
        "JOURNAL_COMPLETE_COUNT",
        lambda: promotion.journal_finalize_forward_v5(
            admin, promotion_id, expected_completed_steps=STEPS[:-1],
        ),
    )
    admin.rollback()
    fresh = connect(dsn)
    try:
        row = read_row(fresh, promotion_id)
        assert_no_dead_end(fresh)
    finally:
        fresh.close()
    assert row["state"] == "completed"
    assert row["completed_steps"] == STEPS


def test_exact_row_scoping_and_predicate_refusals(dsn: str, admin) -> None:
    reset(admin)
    rolled_back = str(uuid4())
    seed(admin, rolled_back, state="rolled_back",
         completed_steps=["historical"], current_step=None)
    failed = str(uuid4())
    seed(admin, failed, state="failed", completed_steps=STEPS[:3],
         current_step=promotion.STEP_RUNTIME_FILE)
    active = str(uuid4())
    seed(admin, active)
    baseline = read_all(admin)

    # A promotion ID that does not exist affects nothing.
    expect_code(
        "JOURNAL_COMPLETE_COUNT",
        lambda: promotion.journal_finalize_forward_v5(
            admin, str(uuid4()), expected_completed_steps=STEPS[:-1],
        ),
    )
    admin.rollback()
    # A non-active row is never finalized.
    for other in (rolled_back, failed):
        expect_code(
            "JOURNAL_COMPLETE_COUNT",
            lambda other=other: promotion.journal_finalize_forward_v5(
                admin, other, expected_completed_steps=STEPS[:-1],
            ),
        )
        admin.rollback()
    # The wrong completed-step prefix is refused even for the active row.
    expect_code(
        "JOURNAL_COMPLETE_COUNT",
        lambda: promotion.journal_finalize_forward_v5(
            admin, active,
            expected_completed_steps=STEPS[:4] + [promotion.STEP_RUNTIME_PROCESSES],
        ),
    )
    admin.rollback()
    # A prefix outside the finalization contract is refused before any SQL.
    for bad in ([], STEPS, STEPS[:4]):
        expect_code(
            "PROMOTION_EXECUTION_CONTRACT_DRIFT",
            lambda bad=bad: promotion.journal_finalize_forward_v5(
                admin, active, expected_completed_steps=bad,
            ),
        )
        admin.rollback()

    assert read_all(admin) == baseline
    assert_no_dead_end(admin)

    # A wrong current_step is refused too.
    with admin.cursor() as cur:
        cur.execute(
            "UPDATE ops_control.environment_identity_promotion SET current_step=%s"
            " WHERE promotion_id=%s::uuid",
            (promotion.STEP_RUNTIME_PROCESSES, active),
        )
    admin.commit()
    expect_code(
        "JOURNAL_COMPLETE_COUNT",
        lambda: promotion.journal_finalize_forward_v5(
            admin, active, expected_completed_steps=STEPS[:-1],
        ),
    )
    admin.rollback()
    assert read_row(admin, active)["completed_steps"] == STEPS[:-1]
    assert_no_dead_end(admin)


def test_historical_journals_are_preserved(dsn: str, admin) -> None:
    reset(admin)
    historical = str(uuid4())
    seed(admin, historical, state="rolled_back",
         completed_steps=["historical"], current_step=None)
    completed_before = str(uuid4())
    seed(admin, completed_before, state="completed",
         completed_steps=STEPS, current_step=None)
    active = str(uuid4())
    seed(admin, active)
    before = read_all(admin)

    promotion.journal_finalize_forward_v5(
        admin, active, expected_completed_steps=STEPS[:-1],
    )
    after = read_all(admin)
    assert after[historical] == before[historical]
    assert after[completed_before] == before[completed_before]
    assert after[active]["state"] == "completed"
    assert after[active]["completed_steps"] == STEPS
    assert_no_dead_end(admin)


def test_retired_split_pattern_counterexample(dsn: str, admin) -> None:
    """Control: the retired split pattern really does create the dead-end.

    Without this the other cases would be tautological, because they would pass
    against a guard that can never fire.
    """
    reset(admin)
    promotion_id = str(uuid4())
    seed(admin, promotion_id)
    # Retired behavior: append the final step, then be interrupted before the
    # separate state transition that the atomic function now performs together.
    promotion.journal_step(
        admin, promotion_id, current_step=None,
        completed_step=promotion.STEP_FINAL_VERIFY,
    )
    row = read_row(admin, promotion_id)
    assert row["state"] == "in_progress"
    assert row["current_step"] is None
    assert row["completed_steps"] == STEPS

    try:
        assert_no_dead_end(admin)
    except AssertionError:
        pass
    else:
        raise AssertionError("the dead-end guard failed to detect the retired split pattern")

    # That shape is exactly what no executor can resume.
    expect_code(
        "RESUME_JOURNAL_FINALIZATION_DEAD_END",
        lambda: resume_v2.require_resume_v2_finalization_route(
            journal={
                "state": row["state"],
                "completed_steps": list(row["completed_steps"]),
                "current_step": row["current_step"],
            },
            original_steps=STEPS,
        ),
    )
    reset(admin)
    assert_no_dead_end(admin)


def test_forward_finalization_ignores_resume_audit_columns(dsn: str, admin) -> None:
    """After migration 054 the forward path must leave the audit columns NULL."""
    reset(admin)
    promotion_id = str(uuid4())
    seed(admin, promotion_id)
    promotion.journal_finalize_forward_v5(
        admin, promotion_id, expected_completed_steps=STEPS[:-1],
    )
    row = promotion.inspect_journals(admin, promotion_id=promotion_id)[0]
    admin.rollback()
    assert row["state"] == "completed"
    assert row["completed_steps"] == STEPS
    assert row["resume_contract"] is None
    assert row["resume_plan_sha256"] is None
    assert_no_dead_end(admin)


def main() -> None:
    dsn = os.environ.get(DSN_ENV)
    if not dsn:
        raise SystemExit(f"{DSN_ENV} is required; no platform DSN fallback is allowed")

    admin = connect(dsn)
    try:
        with admin.cursor() as cur:
            cur.execute("SELECT current_database() AS database")
            identity = dict(cur.fetchone())
        admin.rollback()
        assert identity["database"] not in FORBIDDEN_DATABASES, identity
        assert identity["database"].startswith("forward_v5_test"), identity

        # Phase 1: schema 053 only; migration-054 columns are intentionally absent.
        # The disposable database is rebuilt so repeated runs start identically.
        with admin.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS ops_control CASCADE")
        admin.commit()
        execute_sql_file(admin, "db/migrations/053_environment_identity_promotion_journal.sql")
        with admin.cursor() as cur:
            cur.execute(
                """SELECT count(*) AS n FROM information_schema.columns
                    WHERE table_schema='ops_control'
                      AND table_name='environment_identity_promotion'
                      AND column_name IN ('resume_contract','resume_plan_sha256')"""
            )
            assert int(dict(cur.fetchone())["n"]) == 0
        admin.rollback()

        schema_053_cases = (
            test_atomic_finalization_on_schema_053,
            test_interruption_before_the_atomic_transaction,
            test_exception_inside_the_atomic_transaction,
            test_connection_loss_after_commit,
            test_post_commit_failure_cannot_regress_the_completed_row,
            test_fresh_read_failure_then_reconciliation,
            test_rerun_after_an_ambiguous_post_commit_result,
            test_exact_row_scoping_and_predicate_refusals,
            test_historical_journals_are_preserved,
            test_retired_split_pattern_counterexample,
        )
        for case in schema_053_cases:
            case(dsn, admin)

        # Phase 2: the same forward path stays correct after additive 054.
        execute_sql_file(admin, "db/migrations/054_environment_identity_resume_contract.sql")
        execute_sql_file(admin, "db/migrations/054_environment_identity_resume_contract.sql")
        for case in schema_053_cases:
            case(dsn, admin)
        test_forward_finalization_ignores_resume_audit_columns(dsn, admin)

        # The real production promotion identity is never written by this suite.
        with admin.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM ops_control.environment_identity_promotion"
                " WHERE promotion_id=%s::uuid",
                (PRODUCTION_PROMOTION_ID,),
            )
            assert int(dict(cur.fetchone())["n"]) == 0
        admin.rollback()
        print("forward v5 atomic finalization disposable PostgreSQL tests: OK")
    finally:
        admin.close()


if __name__ == "__main__":
    main()
