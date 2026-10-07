#!/usr/bin/env python3
"""Disposable-PostgreSQL integration tests for resume-v2 journal semantics.

Requires an explicitly supplied RESUME_V2_TEST_DSN.  It refuses known
production database names and never loads the repository .env.
"""
from __future__ import annotations

import os
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

from ops import environment_identity_promotion as promotion
from ops import promote_environment_identity as promotion_cli
from ops import resume_plan_v2 as resume_v2
from ops.tests_manual.test_resume_plan_v2 import fixture_plan


ROOT = Path(__file__).resolve().parents[2]
DSN_ENV = "RESUME_V2_TEST_DSN"
FORBIDDEN_DATABASES = {"logdb", "telematics_main", "alpha_main"}
PLATFORM_UUID = "52517750-7438-4558-8490-2736ae4cc629"


def connect(dsn: str, *, autocommit: bool = False):
    return psycopg.connect(
        dsn, autocommit=autocommit, row_factory=dict_row,
        options="-c application_name=resume_v2_disposable_test",
    )


def production_connect(dsn: str, *, read_only: bool, autocommit: bool = False):
    values = conninfo_to_dict(dsn)
    return promotion.connect_database(
        host=values.get("host", "127.0.0.1"),
        port=int(values.get("port", 5432)),
        dbname=values["dbname"],
        user=values["user"],
        password=values.get("password", ""),
        read_only=read_only,
        autocommit=autocommit,
    )


def execute_sql_file(conn, relative: str) -> None:
    sql = (ROOT / relative).read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


def original_plan(test_nonce: str = "fixture"):
    return {
        "contract_version": 5, "promotion_plan_contract_version": 5,
        "source_environment": "local_dev", "target_environment": "production",
        "platform_uuid": PLATFORM_UUID, "test_nonce": test_nonce, "clients": [{"client_code": "TEST00001"}],
        "steps": [
            "client_marker:TEST00001", "platform_control_plane", "platform_marker",
            "runtime_environment_file", "runtime_reload_required",
            "runtime_processes_verified", "final_verification",
        ],
    }


def seed(conn, promotion_id: str, *, state: str = "in_progress",
         completed_steps=None, current_step="runtime_reload_required"):
    plan = original_plan(promotion_id)
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO ops_control.environment_identity_promotion (
                   promotion_id,source_environment,target_environment,platform_identity_id,
                   selected_clients,immutable_plan_json,plan_sha256,state,started_at,
                   current_step,completed_steps,operator_attestation_hash,backup_reference,
                   runtime_file_backup_path,runtime_file_before_sha256,runtime_file_after_sha256)
               VALUES (%s::uuid,'local_dev','production',%s::uuid,%s::jsonb,%s::jsonb,%s,%s,
                       now(),%s,%s::jsonb,%s,'/disposable/checkpoint',
                       '/disposable/runtime.bak',%s,%s)""",
            (
                promotion_id, PLATFORM_UUID, promotion.canonical_json(plan["clients"]),
                promotion.canonical_json(plan), promotion.plan_hash(plan), state,
                current_step, promotion.canonical_json(completed_steps or plan["steps"][:4]),
                "a" * 64, "b" * 64, "c" * 64,
            ),
        )
    conn.commit()


def reset(conn):
    with conn.cursor() as cur:
        cur.execute("TRUNCATE ops_control.environment_identity_promotion")
    conn.commit()


def expect_code(code, fn):
    try:
        fn()
    except promotion.PromotionError as exc:
        assert exc.code == code, exc
    else:
        raise AssertionError(f"expected {code}")


class FailingCloseConnection:
    """Proxy whose close() commits nothing but fails after releasing the socket."""

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        self._conn.close()
        raise OSError("injected write connection close failure")


# Raw, non-PromotionError failures injected at each execution boundary.
RAW_FAILURE_MODES = {
    "pre_write_raw": (1, psycopg.OperationalError, "injected pre-write database failure"),
    "runtime_reload_required_raw": (3, psycopg.OperationalError, "injected failure after reload commit"),
    "runtime_processes_verified_raw": (4, RuntimeError, "injected failure after process commit"),
}


def exercise_executor(
    dsn: str, admin, *, fail_after: str | None = None,
) -> tuple[str, promotion.PromotionError | None]:
    reset(admin)
    promotion_id = str(uuid4())
    seed(admin, promotion_id)
    unrelated_promotion_id = str(uuid4())
    seed(
        admin, unrelated_promotion_id, state="completed",
        completed_steps=original_plan(unrelated_promotion_id)["steps"],
        current_step=None,
    )
    steps = original_plan(promotion_id)["steps"]
    row = promotion.inspect_journals(admin, promotion_id=promotion_id)[0]
    admin.rollback()

    plan = fixture_plan()
    plan["approval_identity"]["promotion_id"] = promotion_id
    plan["migration_054_schema_contract"][
        "target_promotion_resume_audit_state"
    ]["promotion_id"] = promotion_id
    plan["journal_state"]["promotion_id"] = promotion_id
    plan["journal_state"]["completed_steps"] = steps[:4]
    plan["journal_state"]["current_step"] = promotion.STEP_RUNTIME_RELOAD
    plan["journal_state"]["expected_remaining_steps"] = steps[4:]
    plan["journal_state"]["reconciliation_records"] = [
        {
            "step": step,
            "journal_reported_complete": step in steps[:4],
            "observed_at_target": True,
            "classification": "completed_verified" if step in steps[:4] else "remaining_verified_ready",
        }
        for step in steps
    ]
    plan["remaining_execution_contract"] = resume_v2.remaining_execution_contract(promotion_id)
    resume_v2.validate_plan(plan)
    digest = promotion.plan_hash(plan)
    args = SimpleNamespace(
        promotion_id=promotion_id,
        resume_plan_sha256=digest,
        attestation=resume_v2.attestation(plan),
    )
    immutable = {"steps": steps}
    route_journal = {
        "state": row["state"],
        "completed_steps": list(row["completed_steps"]),
        "current_step": row["current_step"],
    }
    collect_count = 0

    open_count = 0

    def collect(*unused, **unused_kwargs):
        nonlocal collect_count
        collect_count += 1
        if (
            fail_after == "target_audit_before_first_write" and collect_count == 2
        ) or (
            fail_after == "target_audit_after_first_write" and collect_count == 3
        ):
            with admin.cursor() as cur:
                cur.execute(
                    """UPDATE ops_control.environment_identity_promotion
                          SET resume_contract='resume-v2'
                        WHERE promotion_id=%s::uuid""",
                    (promotion_id,),
                )
            admin.commit()
        if fail_after == "unrelated_audit_after_approval" and collect_count == 2:
            with admin.cursor() as cur:
                cur.execute(
                    """UPDATE ops_control.environment_identity_promotion
                          SET resume_contract='resume-v2', resume_plan_sha256=%s
                        WHERE promotion_id=%s::uuid""",
                    ("9" * 64, unrelated_promotion_id),
                )
            admin.commit()
        with admin.cursor() as cur:
            cur.execute(
                """SELECT count(*) AS n
                     FROM ops_control.environment_identity_promotion
                    WHERE promotion_id=%s::uuid
                      AND (resume_contract IS NOT NULL OR resume_plan_sha256 IS NOT NULL)""",
                (promotion_id,),
            )
            target_audit_rows = int(cur.fetchone()["n"])
        admin.rollback()
        if target_audit_rows:
            raise promotion.PromotionError(
                "RESUME_SCHEMA_CONTRACT_DRIFT",
                "target promotion resume audit columns already contain values",
                details={
                    "writes_performed": False,
                    "promotion_id": promotion_id,
                },
            )
        if fail_after == "runtime_reload_required" and collect_count == 3:
            raise promotion.PromotionError(
                "TEST_DRIFT", "after reload commit",
                details={"writes_performed": False},
            )
        if fail_after == "runtime_processes_verified" and collect_count == 4:
            raise promotion.PromotionError(
                "TEST_DRIFT", "after process commit",
                details={"writes_performed": False},
            )
        raw = RAW_FAILURE_MODES.get(str(fail_after))
        if raw and collect_count == raw[0]:
            raise raw[1](raw[2])
        return plan, [], route_journal

    def open_connection(runtime, *, read_only, autocommit=False):
        nonlocal open_count
        open_count += 1
        if fail_after == "fresh_connection_open_raw" and open_count == 8:
            raise psycopg.OperationalError("injected post-finalization connection failure")
        conn = production_connect(dsn, read_only=read_only, autocommit=autocommit)
        if fail_after == "write_connection_close_raw" and not read_only:
            return FailingCloseConnection(conn)
        return conn

    with ExitStack() as stack:
        stack.enter_context(patch.object(
            promotion_cli, "_repository_state",
            return_value=SimpleNamespace(head="4" * 40),
        ))
        stack.enter_context(patch.object(
            promotion_cli, "_validate_scope",
            return_value={"TEST00001": "a41f7fe6-e113-42f7-8789-2dc20b2091d7"},
        ))
        stack.enter_context(patch.object(promotion_cli, "_require_arguments", return_value=None))
        stack.enter_context(patch.object(promotion_cli, "_runtime", return_value=SimpleNamespace()))
        stack.enter_context(patch.object(
            promotion_cli, "_resume_plan",
            return_value=(immutable, [], route_journal),
        ))
        stack.enter_context(patch.object(
            promotion_cli, "_collect_resume_v2_plan",
            side_effect=collect,
        ))
        stack.enter_context(patch.object(
            promotion_cli, "_platform_conn", side_effect=open_connection,
        ))
        stack.enter_context(patch.object(promotion_cli, "_emit", return_value=None))
        if fail_after == "atomic_final_commit":
            stack.enter_context(patch.object(
                promotion, "inspect_journals",
                side_effect=promotion.PromotionError(
                    "TEST_FRESH_READ_FAILED", "after atomic commit",
                    details={"writes_performed": False},
                ),
            ))
        if fail_after == "fresh_read_raw":
            stack.enter_context(patch.object(
                promotion, "inspect_journals",
                side_effect=psycopg.OperationalError("injected fresh verification read failure"),
            ))
        if fail_after == "lock_release_raw":
            real_release = promotion.release_promotion_lock

            def release(conn):
                real_release(conn)
                raise RuntimeError("injected advisory unlock failure")

            stack.enter_context(patch.object(
                promotion, "release_promotion_lock", side_effect=release,
            ))
        try:
            result = promotion_cli._execute_resume_v2(args)
        except promotion.PromotionError as exc:
            assert fail_after is not None
            expected_writes = fail_after not in {
                "pre_write_raw", "target_audit_before_first_write",
            }
            assert exc.details["writes_performed"] is expected_writes, exc.details
            assert exc.details["reconciliation_required"] is expected_writes
            assert exc.exit_code == (
                promotion.EXIT_PRECONDITION if not expected_writes
                else promotion.EXIT_PARTIAL
            ), exc.details
            if fail_after in RAW_FAILURE_MODES or fail_after in {
                "fresh_connection_open_raw", "fresh_read_raw",
            }:
                assert exc.code == "RESUME_V2_EXECUTION_INTERRUPTED", exc.code
                assert "injected" in str(exc)
                assert exc.details["original_exception_class"] in {
                    "OperationalError", "RuntimeError",
                }
            if fail_after in {"lock_release_raw", "write_connection_close_raw"}:
                assert exc.code == "RESUME_V2_CLEANUP_FAILED", exc.code
                assert exc.details["journal_state"] == "completed"
                assert exc.details["cleanup_failures"]
            if fail_after in {
                "target_audit_before_first_write",
                "target_audit_after_first_write",
            }:
                assert exc.code == "RESUME_SCHEMA_CONTRACT_DRIFT"
                assert exc.details["promotion_id"] == promotion_id
            failure = exc
        else:
            assert fail_after in {None, "unrelated_audit_after_approval"}
            assert result == promotion.EXIT_OK
            failure = None

    # Durable journal state is always read back through a separate connection.
    verify = connect(dsn)
    try:
        final = promotion.inspect_journals(verify, promotion_id=promotion_id)[0]
    finally:
        verify.close()
    if fail_after == "pre_write_raw":
        assert final["state"] == "in_progress"
        assert final["current_step"] == promotion.STEP_RUNTIME_RELOAD
        assert final["completed_steps"] == steps[:4]
    elif fail_after in {
        "runtime_reload_required", "runtime_reload_required_raw",
        "target_audit_after_first_write",
    }:
        assert final["state"] == "in_progress"
        assert final["current_step"] == promotion.STEP_RUNTIME_PROCESSES
        assert final["completed_steps"] == steps[:5]
    elif fail_after in {"runtime_processes_verified", "runtime_processes_verified_raw"}:
        assert final["state"] == "in_progress"
        assert final["current_step"] == promotion.STEP_FINAL_VERIFY
        assert final["completed_steps"] == steps[:6]
    elif fail_after == "target_audit_before_first_write":
        assert final["state"] == "in_progress"
        assert final["current_step"] == promotion.STEP_RUNTIME_RELOAD
        assert final["completed_steps"] == steps[:4]
    else:
        assert final["state"] == "completed"
        assert final["current_step"] is None
        assert final["completed_steps"] == steps
        assert final["resume_contract"] == "resume-v2"
        assert final["resume_plan_sha256"] == digest

    # No interruption can leave an all-steps-complete row that is still active.
    assert not (final["state"] == "in_progress" and final["completed_steps"] == steps)

    # Rerun classification: resumable while active, terminal once completed.
    rerun = connect(dsn)
    try:
        rerun_row = promotion.inspect_journals(rerun, promotion_id=promotion_id)[0]
    finally:
        rerun.close()
    route = {
        "state": rerun_row["state"],
        "completed_steps": list(rerun_row["completed_steps"]),
        "current_step": rerun_row["current_step"],
    }
    if final["state"] == "completed":
        expect_code(
            "PROMOTION_TERMINAL",
            lambda: resume_v2.require_resume_v2_finalization_route(
                journal=route, original_steps=steps,
            ),
        )
    else:
        assert resume_v2.require_resume_v2_finalization_route(
            journal=route, original_steps=steps,
        ) == "resume-v2-finalization"
    return promotion_id, failure


def main() -> None:
    dsn = os.environ.get(DSN_ENV)
    if not dsn:
        raise SystemExit(f"{DSN_ENV} is required; no platform DSN fallback is allowed")

    admin = connect(dsn)
    try:
        with admin.cursor() as cur:
            cur.execute("SELECT current_database() AS database, inet_server_addr()::text AS host, inet_server_port() AS port")
            identity = dict(cur.fetchone())
        admin.rollback()
        assert identity["database"] not in FORBIDDEN_DATABASES, identity
        assert identity["database"].startswith("resume_v2_test"), identity

        execute_sql_file(admin, "db/migrations/053_environment_identity_promotion_journal.sql")
        reset(admin)
        preexisting = str(uuid4())
        seed(admin, preexisting)
        # db_migrate.sh feeds psql without BEGIN, then records the filename separately.
        # Simulate interruption after the first committed DDL statement and recover
        # by rerunning the complete idempotent migration.
        partial = connect(dsn, autocommit=True)
        try:
            with partial.cursor() as cur:
                cur.execute(
                    """ALTER TABLE ops_control.environment_identity_promotion
                           ADD COLUMN IF NOT EXISTS resume_contract TEXT NULL,
                           ADD COLUMN IF NOT EXISTS resume_plan_sha256 TEXT NULL"""
                )
        finally:
            partial.close()
        execute_sql_file(admin, "db/migrations/054_environment_identity_resume_contract.sql")
        execute_sql_file(admin, "db/migrations/054_environment_identity_resume_contract.sql")
        with admin.cursor() as cur:
            cur.execute("SELECT promotion_id::text,resume_contract,resume_plan_sha256 FROM ops_control.environment_identity_promotion WHERE promotion_id=%s::uuid", (preexisting,))
            row = dict(cur.fetchone())
        admin.rollback()
        assert row == {"promotion_id": preexisting, "resume_contract": None, "resume_plan_sha256": None}

        # Journal state changed after approval: state predicate refuses before any step append.
        with admin.cursor() as cur:
            cur.execute("UPDATE ops_control.environment_identity_promotion SET state='failed' WHERE promotion_id=%s::uuid", (preexisting,))
        admin.commit()
        expect_code(
            "RESUME_JOURNAL_STATE_DRIFT",
            lambda: promotion.journal_resume_progress_v2(
                admin, preexisting, completed_step="runtime_reload_required",
                current_step="runtime_processes_verified",
            ),
        )
        with admin.cursor() as cur:
            cur.execute("SELECT completed_steps FROM ops_control.environment_identity_promotion WHERE promotion_id=%s::uuid", (preexisting,))
            assert "runtime_reload_required" not in cur.fetchone()["completed_steps"]
        admin.rollback()

        # Interruption after each independently durable progress write reduces the suffix.
        reset(admin)
        interrupted = str(uuid4())
        seed(admin, interrupted)
        promotion.journal_resume_progress_v2(
            admin, interrupted, completed_step="runtime_reload_required",
            current_step="runtime_processes_verified",
        )
        fresh = connect(dsn)
        try:
            rows = promotion.inspect_journals(fresh, promotion_id=interrupted)
            assert rows[0]["completed_steps"][-1] == "runtime_reload_required"
            assert rows[0]["current_step"] == "runtime_processes_verified"
            assert rows[0]["resume_plan_sha256"] is None
        finally:
            fresh.close()
        promotion.journal_resume_progress_v2(
            admin, interrupted, completed_step="runtime_processes_verified",
            current_step="final_verification",
        )
        rows = promotion.inspect_journals(admin, promotion_id=interrupted)
        assert rows[0]["completed_steps"][-2:] == ["runtime_reload_required", "runtime_processes_verified"]
        admin.rollback()

        # Exact row scoping and an unsupported/nonexistent surface never touch another row.
        other = str(uuid4())
        expect_code(
            "RESUME_JOURNAL_STATE_DRIFT",
            lambda: promotion.journal_resume_progress_v2(
                admin, other, completed_step="final_verification",
                current_step="final_verification",
            ),
        )
        admin.rollback()
        assert promotion.inspect_journals(admin, promotion_id=interrupted)[0]["current_step"] == "final_verification"
        admin.rollback()

        # Successful finalization records the final approval once and verifies through a fresh connection.
        digest = "d" * 64
        expected_before_final = original_plan(interrupted)["steps"][:-1]
        promotion.journal_finalize_resume_v2(
            admin, interrupted, resume_plan_sha256=digest,
            expected_completed_steps=expected_before_final,
        )
        fresh = connect(dsn)
        try:
            final = promotion.inspect_journals(fresh, promotion_id=interrupted)[0]
            assert final["state"] == "completed"
            assert final["resume_contract"] == "resume-v2"
            assert final["resume_plan_sha256"] == digest
            assert final["completed_steps"][-1] == "final_verification"
            assert final["current_step"] is None
            assert final["completed_steps"] == original_plan(interrupted)["steps"]
        finally:
            fresh.close()
        expect_code(
            "RESUME_JOURNAL_STATE_DRIFT",
            lambda: promotion.journal_finalize_resume_v2(
                admin, interrupted, resume_plan_sha256=digest,
                expected_completed_steps=expected_before_final,
            ),
        )
        admin.rollback()

        # Interruption immediately before the atomic transaction leaves a resumable final suffix.
        before_atomic = str(uuid4())
        seed(admin, before_atomic)
        promotion.journal_resume_progress_v2(
            admin, before_atomic, completed_step="runtime_reload_required",
            current_step="runtime_processes_verified",
        )
        promotion.journal_resume_progress_v2(
            admin, before_atomic, completed_step="runtime_processes_verified",
            current_step="final_verification",
        )
        before_row = promotion.inspect_journals(admin, promotion_id=before_atomic)[0]
        admin.rollback()
        assert before_row["state"] == "in_progress"
        assert before_row["current_step"] == "final_verification"
        assert before_row["completed_steps"] == original_plan(before_atomic)["steps"][:-1]

        # A committed atomic finalization is safe even if execution stops before any fresh read.
        promotion.journal_finalize_resume_v2(
            admin, before_atomic, resume_plan_sha256="e" * 64,
            expected_completed_steps=original_plan(before_atomic)["steps"][:-1],
        )
        after_commit = connect(dsn)
        try:
            committed = promotion.inspect_journals(after_commit, promotion_id=before_atomic)[0]
        finally:
            after_commit.close()
        assert committed["state"] == "completed"
        assert committed["current_step"] is None
        assert committed["completed_steps"] == original_plan(before_atomic)["steps"]

        # An interruption during the fresh read cannot regress the already committed row.
        try:
            during_read = connect(dsn)
            try:
                observed = promotion.inspect_journals(during_read, promotion_id=before_atomic)[0]
                assert observed["state"] == "completed"
                raise RuntimeError("injected interruption during fresh read")
            finally:
                during_read.close()
        except RuntimeError:
            pass
        reconcile = connect(dsn)
        try:
            reconciled = promotion.inspect_journals(reconcile, promotion_id=before_atomic)[0]
        finally:
            reconcile.close()
        assert reconciled["state"] == "completed"
        assert not (
            reconciled["state"] == "in_progress"
            and reconciled["completed_steps"] == original_plan(before_atomic)["steps"]
        )

        # Full production executor: approval, read-only lock, writes, atomic finalization, fresh read.
        exercise_executor(dsn, admin)
        exercise_executor(dsn, admin, fail_after="runtime_reload_required")
        exercise_executor(dsn, admin, fail_after="runtime_processes_verified")
        exercise_executor(dsn, admin, fail_after="atomic_final_commit")

        # Raw, non-PromotionError interruptions at every boundary stay truthful.
        exercise_executor(dsn, admin, fail_after="pre_write_raw")
        exercise_executor(dsn, admin, fail_after="runtime_reload_required_raw")
        exercise_executor(dsn, admin, fail_after="runtime_processes_verified_raw")
        exercise_executor(dsn, admin, fail_after="fresh_connection_open_raw")
        exercise_executor(dsn, admin, fail_after="fresh_read_raw")
        exercise_executor(dsn, admin, fail_after="lock_release_raw")
        exercise_executor(dsn, admin, fail_after="write_connection_close_raw")
        exercise_executor(dsn, admin, fail_after="target_audit_before_first_write")
        exercise_executor(dsn, admin, fail_after="target_audit_after_first_write")
        exercise_executor(dsn, admin, fail_after="unrelated_audit_after_approval")

        # The advisory lock is never leaked by any interruption above.
        lock_probe = production_connect(dsn, read_only=True, autocommit=True)
        try:
            assert promotion.try_promotion_lock(lock_probe) is True
            promotion.release_promotion_lock(lock_probe)
        finally:
            lock_probe.close()

        drift_id = str(uuid4())
        seed(admin, drift_id)
        before_drift = promotion.inspect_journals(admin, promotion_id=drift_id)[0]
        admin.rollback()
        approved_binding = fixture_plan()
        drift_cases = [
            ("RESUME_SYSTEMD_RUNTIME_DRIFT", ("systemd_runtime", "pid"), 999),
            ("RESUME_SYSTEMD_RUNTIME_DRIFT", ("systemd_runtime", "invocation_id"), "9" * 32),
            ("RESUME_DOCKER_RUNTIME_DRIFT", ("docker_runtime", "container_id"), "8" * 64),
            ("RESUME_SECURITY_BINDING_DRIFT", ("security_and_recovery", "privileged_helper"), {"path": "/drift"}),
        ]
        for expected_code, (section, key), value in drift_cases:
            changed = deepcopy(approved_binding)
            changed[section][key] = value
            expect_code(
                expected_code,
                lambda changed=changed: promotion_cli._require_resume_nonjournal_match(
                    approved_binding, changed,
                ),
            )
        after_drift = promotion.inspect_journals(admin, promotion_id=drift_id)[0]
        admin.rollback()
        assert after_drift["state"] == before_drift["state"] == "in_progress"
        assert after_drift["current_step"] == before_drift["current_step"]
        assert after_drift["completed_steps"] == before_drift["completed_steps"]

        # Synthetic rolled-back history is preserved while a new active row progresses.
        reset(admin)
        historical = str(uuid4())
        seed(admin, historical, state="rolled_back", completed_steps=["historical"], current_step=None)
        active = str(uuid4())
        seed(admin, active)
        promotion.journal_resume_progress_v2(
            admin, active, completed_step="runtime_reload_required",
            current_step="runtime_processes_verified",
        )
        rows = promotion.inspect_journals(admin)
        admin.rollback()
        by_id = {row["promotion_id"]: row for row in rows}
        assert by_id[historical]["state"] == "rolled_back"
        assert by_id[historical]["completed_steps"] == ["historical"]

        # Corrected read-only autocommit sessions can lock but cannot perform ordinary writes.
        lock_one = production_connect(dsn, read_only=True, autocommit=True)
        lock_two = production_connect(dsn, read_only=True, autocommit=True)
        try:
            with lock_one.cursor() as cur:
                cur.execute("SHOW default_transaction_read_only")
                assert cur.fetchone()["default_transaction_read_only"] == "on"
                cur.execute("SHOW transaction_read_only")
                assert cur.fetchone()["transaction_read_only"] == "on"
                try:
                    cur.execute(
                        "UPDATE ops_control.environment_identity_promotion SET error='forbidden' WHERE promotion_id=%s::uuid",
                        (active,),
                    )
                except psycopg.errors.ReadOnlySqlTransaction:
                    pass
                else:
                    raise AssertionError("read-only advisory-lock connection accepted an ordinary write")
            assert promotion.try_promotion_lock(lock_one) is True
            assert promotion.try_promotion_lock(lock_two) is False
            promotion.release_promotion_lock(lock_one)
            assert promotion.try_promotion_lock(lock_two) is True
            promotion.release_promotion_lock(lock_two)
        finally:
            lock_one.close()
            lock_two.close()

        # Migration write-once protections cover null-to-value, same-value, and drift.
        approval_hash = "f" * 64
        with admin.cursor() as cur:
            cur.execute(
                """UPDATE ops_control.environment_identity_promotion
                      SET resume_contract='resume-v2', resume_plan_sha256=%s
                    WHERE promotion_id=%s::uuid""",
                (approval_hash, active),
            )
        admin.commit()
        with admin.cursor() as cur:
            cur.execute(
                """UPDATE ops_control.environment_identity_promotion
                      SET resume_contract='resume-v2', resume_plan_sha256=%s
                    WHERE promotion_id=%s::uuid""",
                (approval_hash, active),
            )
        admin.commit()

        for sql, params in (
            (
                "UPDATE ops_control.environment_identity_promotion SET resume_contract='other' WHERE promotion_id=%s::uuid",
                (active,),
            ),
            (
                "UPDATE ops_control.environment_identity_promotion SET resume_plan_sha256=%s WHERE promotion_id=%s::uuid",
                ("0" * 64, active),
            ),
            (
                "UPDATE ops_control.environment_identity_promotion SET started_at=started_at + interval '1 second' WHERE promotion_id=%s::uuid",
                (active,),
            ),
            (
                "UPDATE ops_control.environment_identity_promotion SET source_environment='staging' WHERE promotion_id=%s::uuid",
                (active,),
            ),
            (
                "UPDATE ops_control.environment_identity_promotion SET runtime_file_backup_path='/different' WHERE promotion_id=%s::uuid",
                (active,),
            ),
            (
                "UPDATE ops_control.environment_identity_promotion SET runtime_file_before_sha256=%s WHERE promotion_id=%s::uuid",
                ("e" * 64, active),
            ),
            (
                "UPDATE ops_control.environment_identity_promotion SET runtime_file_after_sha256=%s WHERE promotion_id=%s::uuid",
                ("e" * 64, active),
            ),
        ):
            with admin.cursor() as cur:
                try:
                    cur.execute(sql, params)
                except psycopg.Error:
                    admin.rollback()
                else:
                    raise AssertionError(f"write-once/immutable trigger accepted drift: {sql}")

        # Same-value rewrites preserve every immutable/runtime evidence field.
        with admin.cursor() as cur:
            cur.execute(
                """UPDATE ops_control.environment_identity_promotion
                      SET started_at=started_at,
                          runtime_file_backup_path=runtime_file_backup_path,
                          runtime_file_before_sha256=runtime_file_before_sha256,
                          runtime_file_after_sha256=runtime_file_after_sha256
                    WHERE promotion_id=%s::uuid""",
                (active,),
            )
        admin.commit()
        preserved = promotion.inspect_journals(admin, promotion_id=active)[0]
        admin.rollback()
        assert preserved["resume_contract"] == "resume-v2"
        assert preserved["resume_plan_sha256"] == approval_hash
        assert preserved["runtime_file_backup_path"] == "/disposable/runtime.bak"
        print("resume plan v2 disposable PostgreSQL tests: OK")
    finally:
        admin.close()


if __name__ == "__main__":
    main()
