#!/usr/bin/env python3
"""Disposable-PostgreSQL proof that promotion survives the 053 -> 054 transition.

Requires an explicitly supplied ``JOURNAL_SCHEMA_TEST_ADMIN_DSN`` pointing at a
throwaway PostgreSQL 16 server.  The suite drives the real forward-v5 executor,
not only the journal helpers: the platform journal operations, the advisory
lock, the schema-capability probe and ``inspect_journals`` all run against the
live database.  Only host observations and external side effects are stubbed --
the privileged canonical helper, client database mutation, control-plane and
marker writes, and the runtime convergence probes.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import psycopg
from psycopg.rows import dict_row

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ops import environment_identity_promotion as promotion
from ops import promote_environment_identity as cli
from ops import promotion_plan_v5 as v5
from ops.git_repository_state import RepositoryState

DSN_ENV = "JOURNAL_SCHEMA_TEST_ADMIN_DSN"
FORBIDDEN_DATABASES = {"logdb", "telematics_main", "alpha_main"}
DATABASE_PREFIX = "journal_schema_test"

PLATFORM_UUID = "7cac378a-5787-4d62-85d1-282bed208c8c"
CLIENT_UUID = "9c9c9261-2cf7-4b6f-8955-b515814be2f7"
CLIENT_ID = "f25c8a6c-7ca5-4899-8a16-2490d9e5e241"
CLIENT_CODE = "TESTC00001"
ATTESTATION = "PROMOTE_ENVIRONMENT_IDENTITY_TEST contract=v5"
HEAD = "d" * 40
SYNTHETIC_PASSWORD = "synthetic-journal-schema-password"

FORWARD_STEPS = [
    f"{promotion.STEP_CLIENT_PREFIX}{CLIENT_CODE}",
    promotion.STEP_CONTROL_PLANE,
    promotion.STEP_PLATFORM_MARKER,
    promotion.STEP_RUNTIME_FILE,
    promotion.STEP_RUNTIME_RELOAD,
    promotion.STEP_RUNTIME_PROCESSES,
    promotion.STEP_FINAL_VERIFY,
]
PRE_RELOAD_STEPS = FORWARD_STEPS[:4]


def execute_sql_file(conn, relative: str) -> None:
    conn.execute((REPO_ROOT / relative).read_text(encoding="utf-8"))


def reset_journal(conn) -> None:
    conn.execute(f"TRUNCATE {promotion.JOURNAL_TABLE}")


MIGRATION_053 = "db/migrations/053_environment_identity_promotion_journal.sql"
MIGRATION_054 = f"db/migrations/{promotion.RESUME_AUDIT_MIGRATION}"


def restore_schema_053(conn) -> None:
    """Return the disposable database to the pre-054 schema.

    Migration 054 replaces the migration-053 immutability trigger function with a
    version that reads the audit columns, so dropping the columns alone would
    leave a trigger referencing fields the row type no longer has.  Re-applying
    migration 053 restores its own `CREATE OR REPLACE` function body, which also
    reconfirms that migration 053 is safely rerunnable.
    """
    for column in promotion.RESUME_AUDIT_COLUMNS:
        conn.execute(f"ALTER TABLE {promotion.JOURNAL_TABLE} DROP COLUMN IF EXISTS {column}")
    execute_sql_file(conn, MIGRATION_053)


def add_audit_column(conn, column: str) -> None:
    assert column in promotion.RESUME_AUDIT_COLUMNS, column
    conn.execute(f"ALTER TABLE {promotion.JOURNAL_TABLE} ADD COLUMN {column} text")


def present_audit_columns(conn) -> list[str]:
    rows = conn.execute(
        """SELECT column_name FROM information_schema.columns
            WHERE table_schema=%s AND table_name=%s AND column_name = ANY(%s)
            ORDER BY column_name""",
        (promotion.JOURNAL_SCHEMA, promotion.JOURNAL_TABLE_NAME, list(promotion.RESUME_AUDIT_COLUMNS)),
    ).fetchall()
    return [str(dict(row)["column_name"]) for row in rows]


def forbidden_rows(conn) -> int:
    row = conn.execute(
        f"""SELECT count(*) AS total FROM {promotion.JOURNAL_TABLE}
             WHERE state IN ('planned','in_progress')
               AND completed_steps ? %s""",
        (promotion.STEP_FINAL_VERIFY,),
    ).fetchone()
    return int(dict(row)["total"])


def fresh_read(dsn: str, statement: str, params: tuple = ()):  # separate connection on purpose
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        row = conn.execute(statement, params).fetchone()
    return dict(row) if row is not None else None


def advisory_lock_held(dsn: str) -> bool:
    row = fresh_read(
        dsn,
        "SELECT count(*) AS total FROM pg_locks WHERE locktype='advisory'",
    )
    return int(row["total"]) > 0


def build_plan() -> dict[str, object]:
    return {
        "promotion_plan_contract_version": 5,
        "contract_version": 5,
        "source_environment": "local_dev",
        "target_environment": "production",
        "platform_uuid": PLATFORM_UUID,
        "repository_head": HEAD,
        "steps": list(FORWARD_STEPS),
        "clients": [{"client_code": CLIENT_CODE, "client_id": CLIENT_ID, "database_uuid": CLIENT_UUID}],
        "runtime_file_before_sha256": "e" * 64,
        "runtime_file_after_sha256": "f" * 64,
        "required_service_actions": ["systemctl restart log-platform-api.service"],
        "checkpoint_binding": {"checkpoint": "synthetic"},
        "recovery_binding": {"recovery": "synthetic"},
        "uuid_policy": "preserve_existing_database_uuids",
    }


def client_plan() -> promotion.ClientPlan:
    return promotion.ClientPlan(
        client_id=CLIENT_ID, client_code=CLIENT_CODE, database_name="synthetic_client",
        database_user="synthetic_client_user", database_host="127.0.0.1", database_port=5432,
        database_uuid=CLIENT_UUID, password_secret_ref="SYNTHETIC_CLIENT_DB_PASSWORD",
    )


def runtime_state(environment: str, checksum: str) -> promotion.RuntimeFileState:
    return promotion.RuntimeFileState(
        path=Path("/etc/log-platform/environment-identity.env"),
        values={promotion.TARGET_ENVIRONMENT_KEY: environment},
        checksum=checksum, uid=0, gid=0, mode=0o600,
    )


def repository_state() -> RepositoryState:
    return RepositoryState(
        root=REPO_ROOT, head=HEAD, branch="main",
        counts={"staged": 0, "unstaged": 0, "untracked": 0, "conflicts": 0, "deleted": 0, "submodules": 0},
        paths=(), operation_state=(),
    )


def cli_arguments() -> SimpleNamespace:
    return SimpleNamespace(
        from_environment="local_dev", to_environment="production", platform_uuid=PLATFORM_UUID,
        client_code=[CLIENT_CODE], expected_client_db_uuid=[f"{CLIENT_CODE}={CLIENT_UUID}"],
        runtime_environment_file=Path("/etc/log-platform/environment-identity.env"),
        backup_reference=Path("/tmp/synthetic-checkpoint"), recovery_root=Path("/tmp/synthetic-recovery"),
        preserved_recovery_backup=Path("/tmp/synthetic-recovery/backup"),
        recovery_evidence=Path("/tmp/synthetic-recovery/evidence"),
        execute=True, attestation=ATTESTATION, promotion_id=None, resume_plan_sha256=None,
        recovery_plan_sha256=None, resume_plan=False, resume_plan_v1_diagnostic=False,
        check_production_readiness=False, inspect_promotions=None, rollback=False,
        rollback_plan=False, json=True,
    )


class ForwardExecutorHarness:
    """Run the real forward-v5 executor with only external effects stubbed."""

    def __init__(self, dsn: str, *, convergence: str = "RUNTIME_RELOAD_REQUIRED",
                 canonical_environment: str = "local_dev", raw_failure_step: str | None = None,
                 cleanup_failures: tuple[str, ...] = (),
                 execution_open_failure: int | None = None,
                 outage_callback=None):
        self.dsn = dsn
        self.convergence = convergence
        self.canonical_environment = canonical_environment
        self.raw_failure_step = raw_failure_step
        self.cleanup_failures = set(cleanup_failures)
        self.execution_open_failure = execution_open_failure
        self.outage_callback = outage_callback
        self.plan = build_plan()
        self.clients = [client_plan()]
        self.helper_invocations = 0
        self.client_mutations: list[str] = []
        self.control_plane_updates = 0
        self.platform_marker_updates = 0
        self.emitted: list[dict[str, object]] = []
        self.connection_open_events: list[tuple[bool, bool]] = []
        self.connection_close_events: list[str] = []
        self.lock_attempts = 0
        self._execution_open_count = 0

    class _Connection:
        def __init__(self, inner, harness, phase: str):
            self.inner = inner
            self.harness = harness
            self.phase = phase

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def close(self):
            self.harness.connection_close_events.append(self.phase)
            self.inner.close()
            if self.phase in self.harness.cleanup_failures:
                raise psycopg.errors.OperationalError(
                    f"synthetic {self.phase} failure password={SYNTHETIC_PASSWORD}"
                )

    def _platform_connection(self, runtime, *, read_only: bool, autocommit: bool = False):
        self.connection_open_events.append((read_only, autocommit))
        # The first call is the isolated schema probe. Subsequent calls 1..3
        # are the lock, execution-read and execution-write session handles.
        if len(self.connection_open_events) > 1:
            self._execution_open_count += 1
            if self.execution_open_failure == self._execution_open_count:
                raise psycopg.errors.OperationalError(
                    f"synthetic connection open failure password={SYNTHETIC_PASSWORD}"
                )
        inner = cli.promotion.connect_database(
            host=os.environ["POSTGRES_HOST"], port=int(os.environ["POSTGRES_PORT"]),
            dbname=os.environ["POSTGRES_DB"], user=os.environ["POSTGRES_USER"],
            password=os.environ["POSTGRES_PASSWORD"], read_only=read_only,
            autocommit=autocommit,
        )
        if len(self.connection_open_events) == 1:
            phase = "schema_probe_connection_close"
        elif self._execution_open_count == 1:
            phase = "lock_connection_close"
        elif self._execution_open_count == 2:
            phase = "read_connection_close"
        elif self._execution_open_count == 3:
            phase = "write_connection_close"
        else:
            phase = "auxiliary_connection_close"
        return self._Connection(inner, self, phase)

    def _markers(self):
        platform = {"environment": "production", "database_uuid": PLATFORM_UUID, "database_name": "synthetic_platform"}
        clients = {CLIENT_CODE: {
            "environment": "production", "database_uuid": CLIENT_UUID,
            "database_name": "synthetic_client", "client_code": CLIENT_CODE,
        }}
        return platform, clients

    def _readiness(self, *args, **kwargs):
        return {"execute_allowed": True, "classification": "PRODUCTION_READY", "runtime_identity": {"source": "synthetic"}}

    def _inspect_runtime_file(self, path, *, allowed_paths=None):
        if self.raw_failure_step == "canonical_inspection":
            raise psycopg.errors.OperationalError("synthetic canonical inspection failure")
        checksum = self.plan["runtime_file_after_sha256"] if self.canonical_environment == "production" \
            else self.plan["runtime_file_before_sha256"]
        return runtime_state(self.canonical_environment, str(checksum))

    def _invoke_helper(self, runtime_now, *, source, target):
        self.helper_invocations += 1
        self.canonical_environment = "production"
        return {
            "backup_path": "/var/backups/log-platform/synthetic.env",
            "before_sha256": str(self.plan["runtime_file_before_sha256"]),
            "after_sha256": str(self.plan["runtime_file_after_sha256"]),
        }

    def _mutate_client(self, **kwargs):
        self.client_mutations.append(str(kwargs.get("expected_client_code")))

    def _update_control_plane(self, conn, clients, *, source, target):
        self.control_plane_updates += 1

    def _update_marker(self, conn, **kwargs):
        self.platform_marker_updates += 1

    def _convergence(self, repository_root, *, canonical_path):
        if self.raw_failure_step in {"convergence_probe", "database_outage_convergence"}:
            if self.raw_failure_step == "database_outage_convergence":
                assert self.outage_callback is not None
                self.outage_callback()
            raise psycopg.errors.OperationalError(
                f"synthetic convergence failure dsn={self.dsn} password={SYNTHETIC_PASSWORD}"
            )
        if self.convergence == "RUNTIME_RELOAD_REQUIRED":
            return {"classification": "RUNTIME_RELOAD_REQUIRED", "execute_allowed": False}
        return {"classification": "RUNTIME_CONVERGED", "execute_allowed": True}

    def run(self) -> tuple[int, dict[str, object] | None, promotion.PromotionError | None]:
        args = cli_arguments()
        original_try_lock = promotion.try_promotion_lock
        original_release = promotion.release_promotion_lock

        def try_lock(conn):
            self.lock_attempts += 1
            return original_try_lock(conn)

        def release_lock(conn):
            if self.raw_failure_step == "database_outage_cleanup":
                assert self.outage_callback is not None
                self.outage_callback()
            original_release(conn)
            if "advisory_lock_release" in self.cleanup_failures:
                raise psycopg.errors.OperationalError(
                    f"synthetic advisory unlock failure password={SYNTHETIC_PASSWORD}"
                )

        patches = [
            patch.object(cli, "_repository_state", return_value=repository_state()),
            patch.object(cli, "_runtime", return_value=runtime_state(self.canonical_environment, str(self.plan["runtime_file_before_sha256"]))),
            patch.object(cli, "_new_plan", return_value=(self.plan, self.clients)),
            patch.object(cli, "_build_v5_plan", return_value=self.plan),
            patch.object(cli, "_require_plan_repository", return_value=None),
            patch.object(cli, "_verify_planned_helper", return_value=None),
            patch.object(cli, "_validate_v5_static_bindings", return_value=None),
            patch.object(cli, "_connect_clients", return_value={}),
            patch.object(cli, "_augment_readiness", side_effect=lambda report, **kwargs: report),
            patch.object(cli, "_check_preconditions", return_value=self._markers()),
            patch.object(cli, "_query_control_environments", return_value={CLIENT_CODE: "production"}),
            patch.object(cli, "_resume_plan_command", return_value="synthetic --resume-plan command"),
            patch.object(cli, "inspect_runtime_convergence", side_effect=self._convergence),
            patch.object(v5, "checkpoint_and_recovery_bindings", return_value=(self.plan["checkpoint_binding"], self.plan["recovery_binding"])),
            patch.object(v5, "validate_installed_assets", return_value=None),
            patch.object(promotion, "validate_backup_reference", return_value=None),
            patch.object(promotion, "readiness_report", side_effect=self._readiness),
            patch.object(promotion, "promotion_attestation", return_value=ATTESTATION),
            patch.object(promotion, "inspect_runtime_file", side_effect=self._inspect_runtime_file),
            patch.object(promotion, "invoke_identity_helper", side_effect=self._invoke_helper),
            patch.object(promotion, "mutate_client_marker_durably", side_effect=self._mutate_client),
            patch.object(promotion, "update_control_plane", side_effect=self._update_control_plane),
            patch.object(promotion, "update_marker_environment", side_effect=self._update_marker),
            patch.object(promotion, "mixed_state_report", return_value={"all_at_target": True, "mixed": False}),
            patch.object(cli, "_platform_conn", side_effect=self._platform_connection),
            patch.object(promotion, "try_promotion_lock", side_effect=try_lock),
            patch.object(promotion, "release_promotion_lock", side_effect=release_lock),
            patch.object(cli, "_emit", side_effect=self.emitted.append),
        ]
        with contextlib.ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            try:
                code = cli._execute(args)
            except promotion.PromotionError as exc:
                return exc.exit_code, dict(exc.details or {}), exc
        return code, (self.emitted[-1] if self.emitted else None), None


def structured_cli_output(harness_error: promotion.PromotionError) -> dict[str, object]:
    """Reproduce exactly what main() prints for a failed run."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        with patch.object(cli, "cli", side_effect=harness_error):
            exit_code = cli.main()
    return {"exit_code": exit_code, "payload": json.loads(buffer.getvalue())}


def test_forward_pause_on_schema_053(dsn: str, admin) -> None:
    reset_journal(admin)
    restore_schema_053(admin)
    assert present_audit_columns(admin) == []
    harness = ForwardExecutorHarness(dsn)
    code, result, error = harness.run()
    assert error is None, error
    assert code == promotion.EXIT_PARTIAL, code
    assert result["mode"] == "execute_paused"
    assert result["state"] == "in_progress"
    assert result["current_step"] == promotion.STEP_RUNTIME_RELOAD
    assert result["completed_steps"] == PRE_RELOAD_STEPS
    assert harness.helper_invocations == 1
    assert harness.client_mutations == [CLIENT_CODE]
    promotion_id = str(result["promotion_id"])
    durable = fresh_read(
        dsn,
        f"SELECT state,current_step,completed_steps,runtime_file_backup_path FROM {promotion.JOURNAL_TABLE} WHERE promotion_id=%s::uuid",
        (promotion_id,),
    )
    assert durable["state"] == "in_progress"
    assert durable["current_step"] == promotion.STEP_RUNTIME_RELOAD
    assert durable["completed_steps"] == PRE_RELOAD_STEPS
    assert durable["runtime_file_backup_path"] == "/var/backups/log-platform/synthetic.env"
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        rows = promotion.inspect_journals(conn, promotion_id=promotion_id)
        assert len(rows) == 1
        assert rows[0]["resume_contract"] is None
        assert rows[0]["resume_plan_sha256"] is None
        assert promotion.journal_schema_capability(conn)["resume_audit_schema_ready"] is False
        assert forbidden_rows(conn) == 0
    assert advisory_lock_held(dsn) is False
    return promotion_id


def test_forward_completion_on_schema_053(dsn: str, admin) -> None:
    reset_journal(admin)
    restore_schema_053(admin)
    harness = ForwardExecutorHarness(dsn, convergence="RUNTIME_CONVERGED")
    code, result, error = harness.run()
    assert error is None, error
    assert code == promotion.EXIT_OK, (code, result)
    assert result["mode"] == "execute"
    assert result["state"] == "completed"
    assert result["completed_steps"] == FORWARD_STEPS
    promotion_id = str(result["promotion_id"])
    durable = fresh_read(
        dsn,
        f"SELECT state,current_step,completed_steps,error,failed_at,completed_at FROM {promotion.JOURNAL_TABLE} WHERE promotion_id=%s::uuid",
        (promotion_id,),
    )
    assert durable["state"] == "completed"
    assert durable["current_step"] is None
    assert durable["completed_steps"] == FORWARD_STEPS
    assert durable["error"] is None and durable["failed_at"] is None
    assert durable["completed_at"] is not None
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        assert forbidden_rows(conn) == 0
        rows = promotion.inspect_journals(conn, state="completed")
        assert len(rows) == 1 and rows[0]["resume_contract"] is None
    assert advisory_lock_held(dsn) is False


def test_forward_paths_on_schema_054(dsn: str, admin) -> None:
    reset_journal(admin)
    restore_schema_053(admin)
    execute_sql_file(admin, MIGRATION_054)
    assert present_audit_columns(admin) == sorted(promotion.RESUME_AUDIT_COLUMNS)
    paused = ForwardExecutorHarness(dsn)
    code, result, error = paused.run()
    assert error is None, error
    assert code == promotion.EXIT_PARTIAL
    assert result["completed_steps"] == PRE_RELOAD_STEPS
    paused_id = str(result["promotion_id"])
    audit = fresh_read(
        dsn,
        f"SELECT resume_contract,resume_plan_sha256 FROM {promotion.JOURNAL_TABLE} WHERE promotion_id=%s::uuid",
        (paused_id,),
    )
    assert audit == {"resume_contract": None, "resume_plan_sha256": None}

    reset_journal(admin)
    completed = ForwardExecutorHarness(dsn, convergence="RUNTIME_CONVERGED")
    code, result, error = completed.run()
    assert error is None, error
    assert code == promotion.EXIT_OK
    assert result["state"] == "completed"
    completed_id = str(result["promotion_id"])
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        row = promotion.inspect_journals(conn, promotion_id=completed_id)[0]
        assert row["state"] == "completed"
        assert row["completed_steps"] == FORWARD_STEPS
        # The forward path never writes the resume audit columns, even when present.
        assert row["resume_contract"] is None
        assert row["resume_plan_sha256"] is None
        assert promotion.journal_schema_capability(conn)["resume_audit_schema_ready"] is True
        assert forbidden_rows(conn) == 0
    assert advisory_lock_held(dsn) is False


def test_partial_schema_blocks_before_any_mutation(dsn: str, admin) -> None:
    for column in promotion.RESUME_AUDIT_COLUMNS:
        reset_journal(admin)
        restore_schema_053(admin)
        add_audit_column(admin, column)
        assert present_audit_columns(admin) == [column]
        harness = ForwardExecutorHarness(dsn)
        code, details, error = harness.run()
        assert error is not None
        assert error.code == "ENVIRONMENT_IDENTITY_RESUME_AUDIT_SCHEMA_INCOMPLETE", error.code
        assert code == promotion.EXIT_PRECONDITION
        assert details["writes_performed"] is False
        assert details["expected_migration"] == promotion.RESUME_AUDIT_MIGRATION
        assert details["missing_columns"] == [
            item for item in promotion.RESUME_AUDIT_COLUMNS if item != column
        ]
        # Nothing may have been written or invoked before the refusal.
        assert harness.helper_invocations == 0
        assert harness.client_mutations == []
        assert harness.control_plane_updates == 0
        assert harness.platform_marker_updates == 0
        assert harness.connection_open_events == [(True, False)]
        assert harness.lock_attempts == 0
        row = fresh_read(dsn, f"SELECT count(*) AS total FROM {promotion.JOURNAL_TABLE}")
        assert int(row["total"]) == 0
        assert advisory_lock_held(dsn) is False
        structured = structured_cli_output(error)
        assert structured["exit_code"] == promotion.EXIT_PRECONDITION
        assert structured["payload"]["classification"] == "ENVIRONMENT_IDENTITY_RESUME_AUDIT_SCHEMA_INCOMPLETE"
        assert structured["payload"]["writes_performed"] is False

        rollback_opens: list[tuple[bool, bool]] = []
        original_conn = cli._platform_conn

        def rollback_conn(runtime, *, read_only: bool, autocommit: bool = False):
            rollback_opens.append((read_only, autocommit))
            return original_conn(runtime, read_only=read_only, autocommit=autocommit)

        rollback_args = cli_arguments()
        rollback_args.promotion_id = str(uuid.uuid4())
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(cli, "_repository_state", return_value=repository_state()))
            stack.enter_context(patch.object(
                cli, "_runtime",
                return_value=runtime_state("production", "f" * 64),
            ))
            stack.enter_context(patch.object(cli, "_platform_conn", side_effect=rollback_conn))
            build_recovery = stack.enter_context(patch.object(cli, "_build_recovery_v2_plan"))
            try:
                cli._rollback(rollback_args)
            except promotion.PromotionError as rollback_error:
                assert rollback_error.code == "ENVIRONMENT_IDENTITY_RESUME_AUDIT_SCHEMA_INCOMPLETE"
            else:
                raise AssertionError("partial schema was accepted by rollback")
        assert rollback_opens == [(True, False)]
        assert build_recovery.call_count == 0
        assert advisory_lock_held(dsn) is False
    restore_schema_053(admin)


def test_raw_post_write_exception_reports_partial_state(dsn: str, admin) -> None:
    reset_journal(admin)
    restore_schema_053(admin)
    harness = ForwardExecutorHarness(dsn, raw_failure_step="convergence_probe")
    code, details, error = harness.run()
    assert error is not None
    assert error.code == "FORWARD_V5_EXECUTION_INTERRUPTED", error.code
    assert code == promotion.EXIT_PARTIAL, code
    assert details["writes_performed"] is True
    assert details["reconciliation_required"] is True
    assert details["original_exception_class"] == "OperationalError"
    assert details["execution_phase"] == promotion.STEP_RUNTIME_RELOAD
    message = str(details["sanitized_message"])
    for secret in (SYNTHETIC_PASSWORD, "postgresql://", "password="):
        assert secret not in message, message
    promotion_id = str(details["promotion_id"])
    durable = fresh_read(
        dsn,
        f"SELECT state,current_step,completed_steps FROM {promotion.JOURNAL_TABLE} WHERE promotion_id=%s::uuid",
        (promotion_id,),
    )
    assert durable["state"] == "in_progress"
    assert durable["completed_steps"] == PRE_RELOAD_STEPS
    assert durable["current_step"] == promotion.STEP_RUNTIME_RELOAD
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        assert forbidden_rows(conn) == 0
    assert advisory_lock_held(dsn) is False
    structured = structured_cli_output(error)
    assert structured["exit_code"] == promotion.EXIT_PARTIAL
    assert structured["payload"]["classification"] == "FORWARD_V5_EXECUTION_INTERRUPTED"
    assert structured["payload"]["writes_performed"] is True
    assert structured["payload"]["reconciliation_required"] is True
    assert SYNTHETIC_PASSWORD not in json.dumps(structured["payload"])


def test_primary_failure_keeps_precedence_over_cleanup_failures(dsn: str, admin) -> None:
    reset_journal(admin)
    restore_schema_053(admin)
    harness = ForwardExecutorHarness(
        dsn,
        raw_failure_step="convergence_probe",
        cleanup_failures=(
            "write_connection_close", "read_connection_close",
            "advisory_lock_release", "lock_connection_close",
        ),
    )
    code, details, error = harness.run()
    assert error is not None
    assert error.code == "FORWARD_V5_EXECUTION_INTERRUPTED", error.code
    assert code == promotion.EXIT_PARTIAL
    assert details["writes_performed"] is True
    assert details["reconciliation_required"] is True
    assert details["original_exception_class"] == "OperationalError"
    failures = details["cleanup_failures"]
    assert [row["operation"] for row in failures] == [
        "write_connection_close", "read_connection_close",
        "advisory_lock_release", "lock_connection_close",
    ]
    assert all(row["exception_class"] == "OperationalError" for row in failures)
    assert all(set(row) == {"operation", "exception_class", "detail"} for row in failures)
    assert SYNTHETIC_PASSWORD not in json.dumps(details)
    structured = structured_cli_output(error)
    assert structured["exit_code"] == promotion.EXIT_PARTIAL
    assert structured["payload"]["classification"] == "FORWARD_V5_EXECUTION_INTERRUPTED"
    assert structured["payload"]["writes_performed"] is True
    assert SYNTHETIC_PASSWORD not in json.dumps(structured["payload"])
    assert advisory_lock_held(dsn) is False


def test_pause_cleanup_failure_is_truthful_partial_result(dsn: str, admin) -> None:
    reset_journal(admin)
    restore_schema_053(admin)
    harness = ForwardExecutorHarness(
        dsn, cleanup_failures=("advisory_lock_release",),
    )
    code, details, error = harness.run()
    assert error is not None
    assert error.code == "FORWARD_V5_CLEANUP_FAILED", error.code
    assert code == promotion.EXIT_PARTIAL
    assert details["writes_performed"] is True
    assert details["reconciliation_required"] is True
    assert details["journal_state"] == "in_progress"
    assert details["completed_steps"] == PRE_RELOAD_STEPS
    assert "--inspect-promotions all" in details["operator_action"]
    durable = fresh_read(
        dsn,
        f"SELECT state,current_step,completed_steps FROM {promotion.JOURNAL_TABLE} "
        "WHERE promotion_id=%s::uuid",
        (details["promotion_id"],),
    )
    assert durable == {
        "state": "in_progress",
        "current_step": promotion.STEP_RUNTIME_RELOAD,
        "completed_steps": PRE_RELOAD_STEPS,
    }
    structured = structured_cli_output(error)
    assert structured["payload"]["classification"] == "FORWARD_V5_CLEANUP_FAILED"
    assert structured["payload"]["writes_performed"] is True
    assert advisory_lock_held(dsn) is False


def test_completed_cleanup_failure_preserves_completed_journal(dsn: str, admin) -> None:
    reset_journal(admin)
    restore_schema_053(admin)
    harness = ForwardExecutorHarness(
        dsn, convergence="RUNTIME_CONVERGED",
        cleanup_failures=("write_connection_close",),
    )
    code, details, error = harness.run()
    assert error is not None
    assert error.code == "FORWARD_V5_CLEANUP_FAILED", error.code
    assert code == promotion.EXIT_PARTIAL
    assert details["writes_performed"] is True
    assert details["reconciliation_required"] is True
    assert details["journal_state"] == "completed"
    assert details["completion_may_have_succeeded"] is True
    durable = fresh_read(
        dsn,
        f"SELECT state,current_step,completed_steps,error FROM {promotion.JOURNAL_TABLE} "
        "WHERE promotion_id=%s::uuid",
        (details["promotion_id"],),
    )
    assert durable == {
        "state": "completed", "current_step": None,
        "completed_steps": FORWARD_STEPS, "error": None,
    }
    structured = structured_cli_output(error)
    assert structured["payload"]["classification"] == "FORWARD_V5_CLEANUP_FAILED"
    assert structured["payload"]["journal_state"] == "completed"
    assert advisory_lock_held(dsn) is False


def test_connection_open_failures_close_partial_session(dsn: str, admin) -> None:
    reset_journal(admin)
    restore_schema_053(admin)
    for failed_open, expected_closes in (
        (2, ["schema_probe_connection_close", "lock_connection_close"]),
        (3, [
            "schema_probe_connection_close", "read_connection_close",
            "lock_connection_close",
        ]),
    ):
        harness = ForwardExecutorHarness(dsn, execution_open_failure=failed_open)
        code, details, error = harness.run()
        assert error is not None
        assert error.code == "FORWARD_V5_EXECUTION_INTERRUPTED"
        assert code == promotion.EXIT_PRECONDITION
        assert details["writes_performed"] is False
        assert harness.connection_close_events == expected_closes
        assert harness.lock_attempts == 0
        assert advisory_lock_held(dsn) is False


def test_real_database_outages(dsn: str, container_name: str) -> None:
    """Stop and restart the disposable server at three post-write boundaries."""

    def stop_database() -> None:
        subprocess.run(
            ["docker", "stop", "--time", "0", container_name],
            check=True, capture_output=True, text=True,
        )

    def start_database() -> None:
        subprocess.run(
            ["docker", "start", container_name],
            check=True, capture_output=True, text=True,
        )
        deadline = time.monotonic() + 30
        while True:
            try:
                with psycopg.connect(dsn):
                    return
            except psycopg.OperationalError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)

    scenarios = (
        ("post_write_convergence", "RUNTIME_RELOAD_REQUIRED",
         "database_outage_convergence", "FORWARD_V5_EXECUTION_INTERRUPTED",
         "in_progress"),
        ("pause_cleanup", "RUNTIME_RELOAD_REQUIRED",
         "database_outage_cleanup", "FORWARD_V5_CLEANUP_FAILED",
         "in_progress"),
        ("success_cleanup", "RUNTIME_CONVERGED",
         "database_outage_cleanup", "FORWARD_V5_CLEANUP_FAILED",
         "completed"),
    )
    for label, convergence, failure_mode, classification, expected_state in scenarios:
        with psycopg.connect(dsn, row_factory=dict_row, autocommit=True) as setup:
            reset_journal(setup)
            restore_schema_053(setup)
        harness = ForwardExecutorHarness(
            dsn, convergence=convergence, raw_failure_step=failure_mode,
            outage_callback=stop_database,
        )
        try:
            code, details, error = harness.run()
        finally:
            start_database()
        assert error is not None, label
        assert error.code == classification, (label, error.code)
        assert code == promotion.EXIT_PARTIAL, (label, code, error.code, details)
        assert details["writes_performed"] is True, label
        assert details["reconciliation_required"] is True, label
        assert details["cleanup_failures"], label
        structured = structured_cli_output(error)
        assert structured["payload"]["classification"] == classification, label
        assert structured["payload"]["writes_performed"] is True, label
        assert SYNTHETIC_PASSWORD not in json.dumps(structured["payload"]), label
        durable = fresh_read(
            dsn,
            f"SELECT state,current_step,completed_steps FROM {promotion.JOURNAL_TABLE} "
            "WHERE promotion_id=%s::uuid",
            (details["promotion_id"],),
        )
        assert durable["state"] == expected_state, (label, durable)
        if expected_state == "completed":
            assert durable["current_step"] is None
            assert durable["completed_steps"] == FORWARD_STEPS
        else:
            assert durable["current_step"] == promotion.STEP_RUNTIME_RELOAD
            assert durable["completed_steps"] == PRE_RELOAD_STEPS
        assert advisory_lock_held(dsn) is False, label


def test_raw_pre_write_exception_reports_no_writes(dsn: str, admin) -> None:
    reset_journal(admin)
    restore_schema_053(admin)
    harness = ForwardExecutorHarness(dsn, raw_failure_step="canonical_inspection",
                                     canonical_environment="local_dev")
    code, details, error = harness.run()
    assert error is not None
    assert error.code == "FORWARD_V5_EXECUTION_INTERRUPTED", error.code
    # The canonical inspection happens after the journal exists, so writes are truthful.
    assert details["writes_performed"] is True
    assert code == promotion.EXIT_PARTIAL
    assert details["execution_phase"] == promotion.STEP_RUNTIME_FILE
    assert harness.helper_invocations == 0
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        assert forbidden_rows(conn) == 0
    assert advisory_lock_held(dsn) is False


def test_resume_v2_requires_migration_054(dsn: str, admin) -> None:
    reset_journal(admin)
    restore_schema_053(admin)
    opened: list[bool] = []
    original_conn = cli._platform_conn

    def counting_conn(runtime, *, read_only: bool, autocommit: bool = False):
        opened.append(read_only)
        return original_conn(runtime, read_only=read_only, autocommit=autocommit)

    args = cli_arguments()
    args.promotion_id = str(uuid.uuid4())
    args.resume_plan_sha256 = "1" * 64
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(cli, "_repository_state", return_value=repository_state()))
        stack.enter_context(patch.object(cli, "_runtime", return_value=runtime_state("production", "f" * 64)))
        stack.enter_context(patch.object(cli, "_platform_conn", side_effect=counting_conn))
        collect = stack.enter_context(patch.object(cli, "_collect_resume_v2_plan"))
        resume = stack.enter_context(patch.object(cli, "_resume_plan"))
        try:
            cli._execute_resume_v2(args)
        except promotion.PromotionError as exc:
            error = exc
        else:
            raise AssertionError("expected the resume audit schema gate to refuse")
    assert error.code == "RESUME_V2_AUDIT_SCHEMA_REQUIRED", error.code
    assert error.exit_code == promotion.EXIT_PRECONDITION
    assert error.details["writes_performed"] is False
    assert error.details["reconciliation_required"] is False
    assert error.details["committed_journal_writes"] == []
    assert error.details["execution_phase"] == "resume_audit_schema_gate"
    assert error.details["missing_columns"] == list(promotion.RESUME_AUDIT_COLUMNS)
    assert error.details["expected_migration"] == promotion.RESUME_AUDIT_MIGRATION
    # The gate must precede journal reconciliation, plan collection and every write.
    assert collect.call_count == 0
    assert resume.call_count == 0
    assert opened == [True], opened
    row = fresh_read(dsn, f"SELECT count(*) AS total FROM {promotion.JOURNAL_TABLE}")
    assert int(row["total"]) == 0
    assert advisory_lock_held(dsn) is False
    structured = structured_cli_output(error)
    assert structured["payload"]["classification"] == "RESUME_V2_AUDIT_SCHEMA_REQUIRED"
    assert structured["payload"]["writes_performed"] is False


def test_resume_v2_passes_the_gate_on_schema_054(dsn: str, admin) -> None:
    reset_journal(admin)
    restore_schema_053(admin)
    execute_sql_file(admin, MIGRATION_054)
    args = cli_arguments()
    args.promotion_id = str(uuid.uuid4())
    args.resume_plan_sha256 = "1" * 64
    sentinel = promotion.PromotionError(
        "RESUME_JOURNAL_STATE_DRIFT", "synthetic post-gate stop",
        details={"writes_performed": False},
    )
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(cli, "_repository_state", return_value=repository_state()))
        stack.enter_context(patch.object(cli, "_runtime", return_value=runtime_state("production", "f" * 64)))
        resume = stack.enter_context(patch.object(cli, "_resume_plan", side_effect=sentinel))
        try:
            cli._execute_resume_v2(args)
        except promotion.PromotionError as exc:
            error = exc
        else:
            raise AssertionError("expected the synthetic post-gate stop")
    # The gate allowed the run to advance to normal journal validation.
    assert error.code == "RESUME_JOURNAL_STATE_DRIFT", error.code
    assert resume.call_count == 1
    assert error.details["writes_performed"] is False
    assert error.details["execution_phase"] == "route_classification"


def test_resume_plan_generation_gate(dsn: str, admin) -> None:
    args = cli_arguments()
    args.promotion_id = str(uuid.uuid4())
    args.resume_plan = True
    args.execute = False
    sentinel = promotion.PromotionError(
        "RESUME_JOURNAL_STATE_DRIFT", "synthetic post-gate stop",
        details={"writes_performed": False},
    )
    for audit_ready in (False, True):
        restore_schema_053(admin)
        if audit_ready:
            execute_sql_file(admin, MIGRATION_054)
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(cli, "_repository_state", return_value=repository_state()))
            stack.enter_context(patch.object(cli, "_validate_scope", return_value={CLIENT_CODE: CLIENT_UUID}))
            stack.enter_context(patch.object(cli, "_runtime", return_value=runtime_state("production", "f" * 64)))
            collect = stack.enter_context(patch.object(cli, "_collect_resume_v2_plan", side_effect=sentinel))
            try:
                cli._resume_plan_dry_run(args)
            except promotion.PromotionError as exc:
                error = exc
            else:
                raise AssertionError("expected a refusal or the synthetic post-gate stop")
        if audit_ready:
            # Plan generation reached normal collection on the same read-only connection.
            assert error.code == "RESUME_JOURNAL_STATE_DRIFT", error.code
            assert collect.call_count == 1
        else:
            assert error.code == "RESUME_V2_AUDIT_SCHEMA_REQUIRED", error.code
            assert error.details["writes_performed"] is False
            assert collect.call_count == 0
        row = fresh_read(dsn, f"SELECT count(*) AS total FROM {promotion.JOURNAL_TABLE}")
        assert int(row["total"]) == 0


def test_migration_054_remains_additive_idempotent_and_write_once(dsn: str, admin) -> None:
    reset_journal(admin)
    restore_schema_053(admin)
    execute_sql_file(admin, MIGRATION_054)
    execute_sql_file(admin, MIGRATION_054)
    execute_sql_file(admin, MIGRATION_054)  # an interrupted application can be rerun safely
    assert present_audit_columns(admin) == sorted(promotion.RESUME_AUDIT_COLUMNS)
    harness = ForwardExecutorHarness(dsn, convergence="RUNTIME_CONVERGED")
    code, result, error = harness.run()
    assert error is None and code == promotion.EXIT_OK
    promotion_id = str(result["promotion_id"])
    started = fresh_read(
        dsn, f"SELECT started_at FROM {promotion.JOURNAL_TABLE} WHERE promotion_id=%s::uuid", (promotion_id,)
    )["started_at"]

    def expect_refusal(statement: str, params: tuple, label: str) -> None:
        try:
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute(statement, params)
        except psycopg.Error:
            return
        raise AssertionError(f"{label} was allowed")

    table = promotion.JOURNAL_TABLE
    expect_refusal(f"UPDATE {table} SET started_at=started_at + interval '1 second' WHERE promotion_id=%s::uuid",
                   (promotion_id,), "started_at mutation")
    expect_refusal(f"UPDATE {table} SET immutable_plan_json='{{}}'::jsonb WHERE promotion_id=%s::uuid",
                   (promotion_id,), "immutable plan mutation")
    expect_refusal(f"UPDATE {table} SET plan_sha256=%s WHERE promotion_id=%s::uuid",
                   ("9" * 64, promotion_id), "plan hash mutation")
    expect_refusal(f"UPDATE {table} SET resume_plan_sha256='NOT-HEX' WHERE promotion_id=%s::uuid",
                   (promotion_id,), "non-hex resume plan hash")
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            f"UPDATE {table} SET resume_contract='resume-v2', resume_plan_sha256=%s WHERE promotion_id=%s::uuid",
            ("a" * 64, promotion_id),
        )
    expect_refusal(f"UPDATE {table} SET resume_contract='other' WHERE promotion_id=%s::uuid",
                   (promotion_id,), "second resume contract write")
    expect_refusal(f"UPDATE {table} SET resume_plan_sha256=%s WHERE promotion_id=%s::uuid",
                   ("b" * 64, promotion_id), "second resume plan hash write")
    expect_refusal(f"UPDATE {table} SET resume_contract=NULL WHERE promotion_id=%s::uuid",
                   (promotion_id,), "resume contract reset")
    assert fresh_read(
        dsn, f"SELECT started_at FROM {table} WHERE promotion_id=%s::uuid", (promotion_id,)
    )["started_at"] == started


def main() -> None:
    dsn = os.environ.get(DSN_ENV)
    if not dsn:
        raise SystemExit(f"{DSN_ENV} is required; no platform DSN fallback is allowed")
    with psycopg.connect(dsn, row_factory=dict_row) as probe:
        identity = dict(probe.execute("SELECT current_database() AS database").fetchone())
    assert identity["database"] not in FORBIDDEN_DATABASES, identity
    assert identity["database"].startswith(DATABASE_PREFIX), identity

    environment = {
        "POSTGRES_HOST": "", "POSTGRES_PORT": "", "POSTGRES_DB": "",
        "POSTGRES_USER": "", "POSTGRES_PASSWORD": "",
    }
    parsed = psycopg.conninfo.conninfo_to_dict(dsn)
    environment.update({
        "POSTGRES_HOST": str(parsed.get("host") or "127.0.0.1"),
        "POSTGRES_PORT": str(parsed.get("port") or "5432"),
        "POSTGRES_DB": str(parsed["dbname"]),
        "POSTGRES_USER": str(parsed["user"]),
        "POSTGRES_PASSWORD": str(parsed.get("password") or ""),
    })
    with patch.dict(os.environ, environment):
        with psycopg.connect(dsn, row_factory=dict_row, autocommit=True) as admin:
            execute_sql_file(admin, MIGRATION_053)
            restore_schema_053(admin)
            test_forward_pause_on_schema_053(dsn, admin)
            test_forward_completion_on_schema_053(dsn, admin)
            test_forward_paths_on_schema_054(dsn, admin)
            test_partial_schema_blocks_before_any_mutation(dsn, admin)
            test_raw_post_write_exception_reports_partial_state(dsn, admin)
            test_primary_failure_keeps_precedence_over_cleanup_failures(dsn, admin)
            test_pause_cleanup_failure_is_truthful_partial_result(dsn, admin)
            test_completed_cleanup_failure_preserves_completed_journal(dsn, admin)
            test_connection_open_failures_close_partial_session(dsn, admin)
            test_raw_pre_write_exception_reports_no_writes(dsn, admin)
            test_resume_v2_requires_migration_054(dsn, admin)
            test_resume_v2_passes_the_gate_on_schema_054(dsn, admin)
            test_resume_plan_generation_gate(dsn, admin)
            test_migration_054_remains_additive_idempotent_and_write_once(dsn, admin)
            reset_journal(admin)
        container_name = os.environ.get("JOURNAL_SCHEMA_TEST_CONTAINER")
        if container_name:
            test_real_database_outages(dsn, container_name)
    print("promotion journal schema compatibility PostgreSQL tests: OK")


if __name__ == "__main__":
    main()
