#!/usr/bin/env python3
"""Disposable-PostgreSQL regressions for rollback cleanup/error precedence.

The suite drives ``promote_environment_identity._rollback`` with real platform
connections, advisory locking, platform/control-plane rollback transactions,
and journal transitions. Host/runtime and the separate client database are
synthetic so this test can never target production.
"""
from __future__ import annotations

import copy
import contextlib
import io
import json
import os
import socket
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
from ops import failed_promotion_recovery as recovery_v2
from ops import promote_environment_identity as cli
from ops.git_repository_state import RepositoryState

DSN_ENV = "ROLLBACK_CLEANUP_TEST_ADMIN_DSN"
CONTAINER_ENV = "ROLLBACK_CLEANUP_TEST_CONTAINER"
DATABASE_PREFIX = "rollback_cleanup_test"
FORBIDDEN_DATABASES = {"logdb", "telematics_main", "alpha_main"}
PLATFORM_UUID = "bd7662a5-eeb4-4614-8720-d477abfcb227"
CLIENT_UUID = "b454f82c-5857-4bab-8342-b7258e5cf7de"
CLIENT_ID = "f6222a11-06ee-4e4f-8b25-302a9d963cfa"
CLIENT_CODE = "TESTC00001"
SYNTHETIC_SECRET = "synthetic-rollback-secret"


def execute_sql_file(conn, relative: str) -> None:
    conn.execute((REPO_ROOT / relative).read_text(encoding="utf-8"))


def repository_state() -> RepositoryState:
    return RepositoryState(
        root=REPO_ROOT, head="d" * 40, branch="feature/test",
        counts={
            "staged": 0, "unstaged": 0, "untracked": 0,
            "conflicts": 0, "deleted": 0, "submodules": 0,
        },
        paths=(), operation_state=(),
    )


def runtime_state() -> promotion.RuntimeFileState:
    return promotion.RuntimeFileState(
        path=Path("/tmp/synthetic-environment-identity.env"),
        values={promotion.TARGET_ENVIRONMENT_KEY: "local_dev"},
        checksum="a" * 64, uid=os.getuid(), gid=os.getgid(), mode=0o600,
    )


def client_plan() -> promotion.ClientPlan:
    return promotion.ClientPlan(
        client_id=CLIENT_ID, client_code=CLIENT_CODE,
        database_name="synthetic_client", database_user="synthetic_user",
        database_host="127.0.0.1", database_port=5432,
        database_uuid=CLIENT_UUID,
        password_secret_ref="SYNTHETIC_CLIENT_PASSWORD",
    )


def verification_clients() -> list[promotion.ClientPlan]:
    return [
        client_plan(),
        promotion.ClientPlan(
            client_id="44444444-4444-4444-8444-444444444444",
            client_code="TESTC00002",
            database_name="synthetic_client_two",
            database_user="synthetic_user_two",
            database_host="127.0.0.1",
            database_port=5432,
            database_uuid="fbfe405c-a65f-4275-898f-deb81ceb4df2",
            password_secret_ref="SYNTHETIC_CLIENT_TWO_PASSWORD",
        ),
    ]


def recovery_plan(database_name: str, promotion_id: str) -> dict[str, object]:
    return {
        "operation": {
            "host": socket.gethostname(), "head": "d" * 40,
            "promotion_id": promotion_id,
            "original_promotion_plan_sha256": "b" * 64,
            "original_source_environment": "local_dev",
            "original_target_environment": "production",
        },
        "clients": [{"client_code": CLIENT_CODE, "database_uuid": CLIENT_UUID}],
        "current_durable_database_state": {
            "platform": {
                "database_name": database_name, "database_uuid": PLATFORM_UUID,
            },
        },
        "canonical_identity": {"current_sha256": "a" * 64},
        "runtime": {
            "systemd_api": {"pid": 101, "invocation_id": "synthetic"},
            "docker_api": {"container_id": "synthetic-container"},
        },
        "rollback_evidence": {
            "path": f"/tmp/rollback-cleanup-test/{promotion_id}/rollback-evidence.json",
        },
    }


def arguments(plan: dict[str, object]) -> SimpleNamespace:
    return SimpleNamespace(
        promotion_id=plan["operation"]["promotion_id"],
        runtime_environment_file=Path("/tmp/synthetic-environment-identity.env"),
        backup_reference=Path("/tmp/synthetic-checkpoint"),
        recovery_root=Path("/tmp/synthetic-recovery"),
        preserved_recovery_backup=Path("/tmp/synthetic-recovery/backup"),
        recovery_evidence=Path("/tmp/synthetic-recovery/evidence"),
        execute=True, rollback=True, rollback_plan=False,
        recovery_plan_sha256=promotion.plan_hash(plan),
        attestation=recovery_v2.attestation(plan),
    )


def setup_schema(admin) -> None:
    admin.execute("DROP SCHEMA IF EXISTS workflow_a_control CASCADE")
    admin.execute("DROP SCHEMA IF EXISTS ops_control CASCADE")
    execute_sql_file(admin, "db/migrations/053_environment_identity_promotion_journal.sql")
    admin.execute(
        """CREATE TABLE ops_control.environment_identity (
               identity_key text PRIMARY KEY,
               environment text NOT NULL,
               database_identity_id uuid NOT NULL,
               database_role text NOT NULL,
               database_name text NOT NULL,
               client_code text NULL)"""
    )
    admin.execute("CREATE SCHEMA workflow_a_control")
    admin.execute(
        """CREATE TABLE workflow_a_control.client_account (
               client_id uuid PRIMARY KEY,
               client_code text NOT NULL UNIQUE,
               client_db_name text NOT NULL,
               client_db_environment text NOT NULL,
               client_db_identity_id uuid NOT NULL)"""
    )
    admin.execute(
        """CREATE TABLE ops_control.synthetic_client_marker (
               client_code text PRIMARY KEY, environment text NOT NULL)"""
    )


def reset_state(admin, plan: dict[str, object]) -> None:
    promotion_id = str(plan["operation"]["promotion_id"])
    admin.execute(f"TRUNCATE {promotion.JOURNAL_TABLE}")
    admin.execute("TRUNCATE ops_control.environment_identity")
    admin.execute("TRUNCATE workflow_a_control.client_account")
    admin.execute("TRUNCATE ops_control.synthetic_client_marker")
    database_name = str(
        plan["current_durable_database_state"]["platform"]["database_name"]
    )
    admin.execute(
        """INSERT INTO ops_control.environment_identity
             VALUES ('primary','production',%s::uuid,'platform',%s,NULL)""",
        (PLATFORM_UUID, database_name),
    )
    admin.execute(
        """INSERT INTO workflow_a_control.client_account
             VALUES (%s::uuid,%s,%s,'production',%s::uuid)""",
        (CLIENT_ID, CLIENT_CODE, "synthetic_client", CLIENT_UUID),
    )
    admin.execute(
        "INSERT INTO ops_control.synthetic_client_marker VALUES (%s,'production')",
        (CLIENT_CODE,),
    )
    immutable = {
        "promotion_plan_contract_version": 4, "contract_version": 4,
        "source_environment": "local_dev", "target_environment": "production",
        "platform_uuid": PLATFORM_UUID, "platform_database": database_name,
        "clients": [{
            "client_id": CLIENT_ID, "client_code": CLIENT_CODE,
            "database_uuid": CLIENT_UUID,
        }],
        "steps": [f"{promotion.STEP_CLIENT_PREFIX}{CLIENT_CODE}"],
    }
    admin.execute(
        f"""INSERT INTO {promotion.JOURNAL_TABLE} (
               promotion_id,source_environment,target_environment,
               platform_identity_id,selected_clients,immutable_plan_json,
               plan_sha256,state,started_at,failed_at,current_step,
               completed_steps,error,operator_attestation_hash,backup_reference)
             VALUES (
               %s::uuid,'local_dev','production',%s::uuid,%s::jsonb,%s::jsonb,
               %s,'failed',now(),now(),'platform_marker','[]'::jsonb,
               'SYNTHETIC_FAILURE',%s,'/tmp/synthetic-checkpoint')""",
        (
            promotion_id, PLATFORM_UUID,
            promotion.canonical_json(immutable["clients"]),
            promotion.canonical_json(immutable), promotion.plan_hash(immutable),
            "c" * 64,
        ),
    )


class ConnectionWrapper:
    def __init__(self, inner, harness, operation: str):
        self.inner = inner
        self.harness = harness
        self.operation = operation
        self.closed = False

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def close(self):
        assert not self.closed, f"{self.operation} closed more than once"
        self.closed = True
        self.harness.close_events.append(self.operation)
        self.inner.close()
        if self.operation in self.harness.cleanup_failures:
            raise psycopg.OperationalError(
                f"{self.operation} password={SYNTHETIC_SECRET}"
            )


class RollbackHarness:
    def __init__(
        self, dsn: str, plan: dict[str, object], *,
        primary_failure: BaseException | None = None,
        cleanup_failures: tuple[str, ...] = (),
        fail_second_execution_open: bool = False,
        evidence_failure: BaseException | None = None,
        outage_phase: str | None = None,
        container_name: str | None = None,
    ):
        self.dsn = dsn
        self.plan = plan
        self.primary_failure = primary_failure
        self.cleanup_failures = set(cleanup_failures)
        self.fail_second_execution_open = fail_second_execution_open
        self.evidence_failure = evidence_failure
        self.outage_phase = outage_phase
        self.container_name = container_name
        self.close_events: list[str] = []
        self.lock_attempts = 0
        self.lock_read_only_verified = False
        self.lock_write_refused = False
        self.lock_opened = False
        self.write_opened = False
        self.verify_calls = 0
        self.emitted: list[dict[str, object]] = []
        self.real_try_lock = promotion.try_promotion_lock
        self.real_release_lock = promotion.release_promotion_lock

    def stop_database(self) -> None:
        assert self.container_name
        subprocess.run(
            ["docker", "stop", "--time", "0", self.container_name],
            check=True, capture_output=True, text=True,
        )

    def platform_conn(self, runtime, *, read_only: bool, autocommit: bool = False):
        if autocommit:
            self.lock_opened = True
            inner = promotion.connect_database(
                **self._coordinates(), read_only=read_only, autocommit=True,
            )
            return ConnectionWrapper(
                inner, self, "advisory_lock_connection_close",
            )
        if self.lock_opened and not self.write_opened and not read_only:
            if self.fail_second_execution_open:
                raise psycopg.OperationalError(
                    f"second open failed password={SYNTHETIC_SECRET}"
                )
            self.write_opened = True
            inner = promotion.connect_database(
                **self._coordinates(), read_only=False, autocommit=False,
            )
            return ConnectionWrapper(
                inner, self, "rollback_write_connection_close",
            )
        return promotion.connect_database(
            **self._coordinates(), read_only=read_only, autocommit=autocommit,
        )

    def _coordinates(self) -> dict[str, object]:
        parsed = psycopg.conninfo.conninfo_to_dict(self.dsn)
        return {
            "host": str(parsed.get("host") or "127.0.0.1"),
            "port": int(parsed.get("port") or 5432),
            "dbname": str(parsed["dbname"]), "user": str(parsed["user"]),
            "password": str(parsed.get("password") or ""),
        }

    def try_lock(self, conn) -> bool:
        self.lock_attempts += 1
        assert conn.autocommit is True
        with conn.cursor() as cur:
            cur.execute("SHOW default_transaction_read_only")
            self.lock_read_only_verified = (
                cur.fetchone()["default_transaction_read_only"] == "on"
            )
        try:
            conn.execute(
                "UPDATE ops_control.synthetic_client_marker "
                "SET environment='forbidden'"
            )
        except psycopg.errors.ReadOnlySqlTransaction:
            self.lock_write_refused = True
        else:
            raise AssertionError("lock connection accepted an ordinary write")
        return self.real_try_lock(conn)

    def release_lock(self, conn) -> None:
        if self.outage_phase == "advisory_unlock":
            self.stop_database()
        self.real_release_lock(conn)
        if "advisory_lock_release" in self.cleanup_failures:
            raise psycopg.OperationalError(
                f"unlock failed password={SYNTHETIC_SECRET}"
            )

    def mutate_client(self, **kwargs) -> None:
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.execute(
                "UPDATE ops_control.synthetic_client_marker "
                "SET environment='local_dev' WHERE client_code=%s",
                (CLIENT_CODE,),
            )

    def verify(self, **kwargs) -> dict[str, object]:
        self.verify_calls += 1
        if self.verify_calls == 1 and self.outage_phase == "after_writes":
            self.stop_database()
            raise psycopg.OperationalError("database stopped after rollback writes")
        if self.verify_calls == 1 and self.primary_failure is not None:
            raise self.primary_failure
        return {
            "state": {"uuids": {
                "platform": PLATFORM_UUID, CLIENT_CODE: CLIENT_UUID,
            }},
            "fingerprint": "e" * 64,
        }

    def write_evidence(self, **kwargs) -> dict[str, object]:
        if self.outage_phase == "after_rolled_back":
            self.stop_database()
            raise psycopg.OperationalError(
                "database stopped after rolled_back commit"
            )
        if self.evidence_failure is not None:
            raise self.evidence_failure
        return {
            "path": self.plan["rollback_evidence"]["path"],
            "sha256": "f" * 64,
        }

    def run(self):
        args = arguments(self.plan)
        patches = [
            patch.object(cli, "_repository_state", return_value=repository_state()),
            patch.object(cli, "_runtime", return_value=runtime_state()),
            patch.object(
                cli, "_build_recovery_v2_plan",
                return_value=(self.plan, [client_plan()]),
            ),
            patch.object(cli, "_platform_conn", side_effect=self.platform_conn),
            patch.object(promotion, "try_promotion_lock", side_effect=self.try_lock),
            patch.object(
                promotion, "release_promotion_lock",
                side_effect=self.release_lock,
            ),
            patch.object(
                promotion, "mutate_client_marker_durably",
                side_effect=self.mutate_client,
            ),
            patch.object(
                cli, "_verify_recovery_final_state", side_effect=self.verify,
            ),
            patch.object(
                recovery_v2, "write_evidence", side_effect=self.write_evidence,
            ),
            patch.object(cli, "_emit", side_effect=self.emitted.append),
        ]
        with contextlib.ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            try:
                code = cli._rollback(args)
            except promotion.PromotionError as exc:
                return exc.exit_code, dict(exc.details or {}), exc
        return code, self.emitted[-1], None


def fresh_state(dsn: str, promotion_id: str) -> dict[str, object]:
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        journal = dict(conn.execute(
            f"SELECT state,current_step,error FROM {promotion.JOURNAL_TABLE} "
            "WHERE promotion_id=%s::uuid",
            (promotion_id,),
        ).fetchone())
        platform = conn.execute(
            "SELECT environment FROM ops_control.environment_identity "
            "WHERE identity_key='primary'"
        ).fetchone()["environment"]
        control = conn.execute(
            "SELECT client_db_environment FROM workflow_a_control.client_account "
            "WHERE client_code=%s", (CLIENT_CODE,),
        ).fetchone()["client_db_environment"]
        client = conn.execute(
            "SELECT environment FROM ops_control.synthetic_client_marker "
            "WHERE client_code=%s", (CLIENT_CODE,),
        ).fetchone()["environment"]
        locks = conn.execute(
            "SELECT count(*) AS total FROM pg_locks WHERE locktype='advisory'"
        ).fetchone()["total"]
    return {
        "journal": journal, "platform": platform, "control": control,
        "client": client, "advisory_locks": int(locks),
    }


def structured_output(error: promotion.PromotionError) -> dict[str, object]:
    output = io.StringIO()
    with patch.object(cli, "cli", side_effect=error), \
            contextlib.redirect_stdout(output):
        code = cli.main()
    return {"exit_code": code, "payload": json.loads(output.getvalue())}


def assert_cleanup_shape(details: dict[str, object], expected: list[str]) -> None:
    failures = details["cleanup_failures"]
    assert [row["operation"] for row in failures] == expected, failures
    assert all(
        set(row) == {"operation", "exception_class", "detail"}
        for row in failures
    )


def test_real_verification_helper_connection_lifecycle(
    dsn: str, plan: dict[str, object],
) -> None:
    clients = verification_clients()
    verification_plan = copy.deepcopy(plan)
    verification_plan["clients"] = [
        {
            "client_code": client.client_code,
            "database_uuid": client.database_uuid,
        }
        for client in clients
    ]
    verification_plan["runtime"]["docker_api"][
        "compose_configuration_fingerprint"
    ] = "9" * 64
    coordinates = RollbackHarness(dsn, plan)._coordinates()

    def wrapped_connection(operation: str, tracker):
        inner = promotion.connect_database(
            **coordinates, read_only=True, autocommit=False,
        )
        return ConnectionWrapper(inner, tracker, operation)

    expected_client_close_labels = [
        f"fresh_client_verification_connection_close:{client.client_code}"
        for client in clients
    ]
    platform_close_label = "fresh_platform_verification_connection_close"
    platform_marker = {
        "environment": "local_dev",
        "database_uuid": PLATFORM_UUID,
    }
    client_markers = {
        client.client_code: {
            "environment": "local_dev",
            "database_uuid": client.database_uuid,
        }
        for client in clients
    }
    controls = {client.client_code: "local_dev" for client in clients}
    readiness = {
        "classification": "PRODUCTION_PROMOTION_READY",
        "execute_allowed": True,
        "runtime_identity": {"classification": "PRODUCTION_PROMOTION_READY"},
    }
    runtime_binding = {
        "systemd_api": {"pid": 101, "invocation_id": "synthetic"},
        "docker_api": {
            "container_id": "synthetic-container",
            "compose_configuration_fingerprint": "9" * 64,
        },
    }

    success_tracker = SimpleNamespace(
        close_events=[], cleanup_failures=set(),
    )
    success_state = cli._RollbackState(str(plan["operation"]["promotion_id"]))
    with patch.object(
        cli, "_platform_conn",
        side_effect=lambda *_args, **_kwargs: wrapped_connection(
            platform_close_label, success_tracker,
        ),
    ), patch.object(
        cli, "_open_client_connection",
        side_effect=lambda client, **_kwargs: wrapped_connection(
            f"fresh_client_verification_connection_close:{client.client_code}",
            success_tracker,
        ),
    ), patch.object(
        cli, "_check_preconditions",
        return_value=(platform_marker, client_markers),
    ), patch.object(
        cli, "_query_control_environments", return_value=controls,
    ), patch.object(
        promotion, "inspect_runtime_file", return_value=runtime_state(),
    ), patch.object(
        promotion, "readiness_report", return_value=readiness,
    ), patch.object(
        cli, "_augment_readiness", return_value=readiness,
    ), patch.object(
        cli.v4, "runtime_bindings", return_value=runtime_binding,
    ):
        verified = cli._verify_recovery_final_state(
            runtime=runtime_state(), plan=verification_plan,
            clients=clients, cleanup_state=success_state,
        )
    assert verified["state"]["readiness"] == "PRODUCTION_PROMOTION_READY"
    assert success_tracker.close_events == (
        expected_client_close_labels + [platform_close_label]
    )
    assert success_state.cleanup_failures == []

    failure_tracker = SimpleNamespace(
        close_events=[],
        cleanup_failures={
            expected_client_close_labels[0], platform_close_label,
        },
    )
    failure_state = cli._RollbackState(str(plan["operation"]["promotion_id"]))
    primary = promotion.PromotionError(
        "RECOVERY_READINESS_FAILED", "primary verification failure",
    )
    with patch.object(
        cli, "_platform_conn",
        side_effect=lambda *_args, **_kwargs: wrapped_connection(
            platform_close_label, failure_tracker,
        ),
    ), patch.object(
        cli, "_open_client_connection",
        side_effect=lambda client, **_kwargs: wrapped_connection(
            f"fresh_client_verification_connection_close:{client.client_code}",
            failure_tracker,
        ),
    ), patch.object(cli, "_check_preconditions", side_effect=primary):
        try:
            cli._verify_recovery_final_state(
                runtime=runtime_state(), plan=verification_plan,
                clients=clients, cleanup_state=failure_state,
            )
        except promotion.PromotionError as exc:
            assert exc is primary
            assert exc.code == "RECOVERY_READINESS_FAILED"
        else:
            raise AssertionError("primary verification failure was suppressed")
    assert failure_tracker.close_events == (
        expected_client_close_labels + [platform_close_label]
    )
    assert_cleanup_shape(
        {"cleanup_failures": failure_state.cleanup_failures},
        [expected_client_close_labels[0], platform_close_label],
    )
    assert SYNTHETIC_SECRET not in json.dumps(failure_state.cleanup_failures)

    open_failure_tracker = SimpleNamespace(
        close_events=[], cleanup_failures=set(),
    )
    open_failure_state = cli._RollbackState(
        str(plan["operation"]["promotion_id"]),
    )
    open_count = 0

    def open_client_then_fail(client, **_kwargs):
        nonlocal open_count
        open_count += 1
        if open_count == 2:
            raise psycopg.OperationalError(
                f"client verification open failed password={SYNTHETIC_SECRET}"
            )
        return wrapped_connection(
            f"fresh_client_verification_connection_close:{client.client_code}",
            open_failure_tracker,
        )

    with patch.object(
        cli, "_platform_conn",
        side_effect=lambda *_args, **_kwargs: wrapped_connection(
            platform_close_label, open_failure_tracker,
        ),
    ), patch.object(
        cli, "_open_client_connection", side_effect=open_client_then_fail,
    ):
        try:
            cli._verify_recovery_final_state(
                runtime=runtime_state(), plan=verification_plan,
                clients=clients, cleanup_state=open_failure_state,
            )
        except psycopg.OperationalError:
            pass
        else:
            raise AssertionError("client verification open failure was accepted")
    assert open_failure_tracker.close_events == [
        expected_client_close_labels[0], platform_close_label,
    ]


def test_cleanup_only_reconciliation_truth() -> None:
    before = cli._RollbackState("bd7662a5-eeb4-4614-8720-d477abfcb227")
    before.record_cleanup_failure(
        "synthetic_pre_mutation_close", RuntimeError("synthetic"),
    )
    before_error = cli._rollback_cleanup_only_error(before)
    assert before_error.exit_code == promotion.EXIT_PRECONDITION
    assert before_error.details["writes_performed"] is False
    assert before_error.details["reconciliation_required"] is False

    after = cli._RollbackState("bd7662a5-eeb4-4614-8720-d477abfcb227")
    after.begin_action("synthetic_mutation", mutation=True)
    after.record_cleanup_failure(
        "synthetic_post_mutation_close", RuntimeError("synthetic"),
    )
    after_error = cli._rollback_cleanup_only_error(after)
    assert after_error.exit_code == promotion.EXIT_PARTIAL
    assert after_error.details["writes_performed"] is True
    assert after_error.details["reconciliation_required"] is True


def test_primary_and_raw_failures(dsn: str, admin, plan) -> None:
    for primary in (
        promotion.PromotionError(
            "RECOVERY_READINESS_FAILED",
            "path-specific primary recovery classification",
        ),
        psycopg.OperationalError(
            f"raw database failure password={SYNTHETIC_SECRET}"
        ),
        RuntimeError(f"raw runtime failure token={SYNTHETIC_SECRET}"),
    ):
        reset_state(admin, plan)
        harness = RollbackHarness(
            dsn, plan, primary_failure=primary,
            cleanup_failures=(
                "rollback_write_connection_close",
                "advisory_lock_release",
                "advisory_lock_connection_close",
            ),
        )
        code, details, error = harness.run()
        assert error is not None
        expected_code = (
            primary.code
            if isinstance(primary, promotion.PromotionError)
            else "FAILED_PROMOTION_RECOVERY_PARTIAL_STATE"
        )
        assert error.code == expected_code
        assert code == promotion.EXIT_PARTIAL
        assert details["writes_performed"] is True
        assert details["reconciliation_required"] is True
        assert details["completed_actions"][:2] == [
            f"rollback_client_marker:{CLIENT_CODE}",
            f"fresh_verify_client_marker:{CLIENT_CODE}",
        ]
        assert details["action_in_flight"] == "fresh_verify_all_surfaces_and_runtime"
        assert_cleanup_shape(details, [
            "rollback_write_connection_close", "advisory_lock_release",
            "advisory_lock_connection_close",
        ])
        payload = structured_output(error)
        assert payload["exit_code"] == promotion.EXIT_PARTIAL
        assert payload["payload"]["classification"] == error.code
        assert SYNTHETIC_SECRET not in json.dumps(payload)
        assert harness.lock_read_only_verified and harness.lock_write_refused
        assert fresh_state(
            dsn, str(plan["operation"]["promotion_id"])
        )["advisory_locks"] == 0


def test_cleanup_only_preserves_terminal_state(dsn: str, admin, plan) -> None:
    reset_state(admin, plan)
    harness = RollbackHarness(
        dsn, plan, cleanup_failures=(
            "rollback_write_connection_close", "advisory_lock_release",
            "advisory_lock_connection_close",
        ),
    )
    code, details, error = harness.run()
    assert error is not None and error.code == "RECOVERY_V2_CLEANUP_FAILED"
    assert code == promotion.EXIT_PARTIAL
    assert details["writes_performed"] is True
    assert details["journal_state"] == "rolled_back"
    assert details["recovery_evidence"]["sha256"] == "f" * 64
    assert details["reconciliation_required"] is True
    assert "--inspect-promotions all" in details["operator_action"]
    assert_cleanup_shape(details, [
        "rollback_write_connection_close", "advisory_lock_release",
        "advisory_lock_connection_close",
    ])
    durable = fresh_state(dsn, str(plan["operation"]["promotion_id"]))
    assert durable["journal"]["state"] == "rolled_back"
    assert durable["journal"]["error"].startswith("ROLLBACK_EVIDENCE path=")
    assert durable["advisory_locks"] == 0


def test_connection_open_failure(dsn: str, admin, plan) -> None:
    reset_state(admin, plan)
    harness = RollbackHarness(
        dsn, plan, fail_second_execution_open=True,
        cleanup_failures=("advisory_lock_connection_close",),
    )
    code, details, error = harness.run()
    assert error is not None and error.code == "RECOVERY_V2_EXECUTION_INTERRUPTED"
    assert code == promotion.EXIT_PRECONDITION
    assert details["writes_performed"] is False
    assert details["reconciliation_required"] is False
    assert details["original_exception_class"] == "OperationalError"
    assert harness.lock_attempts == 0
    assert harness.close_events == ["advisory_lock_connection_close"]
    assert_cleanup_shape(details, ["advisory_lock_connection_close"])
    assert SYNTHETIC_SECRET not in json.dumps(details)
    assert fresh_state(
        dsn, str(plan["operation"]["promotion_id"])
    )["advisory_locks"] == 0


def test_evidence_failure_is_partial(dsn: str, admin, plan) -> None:
    reset_state(admin, plan)
    harness = RollbackHarness(
        dsn, plan,
        evidence_failure=RuntimeError(
            f"evidence failed password={SYNTHETIC_SECRET}"
        ),
    )
    code, details, error = harness.run()
    assert error is not None
    assert error.code == "FAILED_PROMOTION_RECOVERY_PARTIAL_STATE"
    assert code == promotion.EXIT_PARTIAL
    assert details["recovery_evidence_may_have_committed"] is True
    assert details["action_in_flight"] == (
        "atomically_create_checksum_bound_rollback_evidence"
    )
    assert SYNTHETIC_SECRET not in json.dumps(details)
    durable = fresh_state(dsn, str(plan["operation"]["promotion_id"]))
    assert durable["journal"]["state"] == "rolled_back"
    assert durable["journal"]["error"] is None


def test_schema_054_keeps_normal_rollback_route(dsn: str, admin, plan) -> None:
    execute_sql_file(
        admin, "db/migrations/054_environment_identity_resume_contract.sql",
    )
    reset_state(admin, plan)
    harness = RollbackHarness(dsn, plan)
    code, result, error = harness.run()
    assert error is None, error
    assert code == promotion.EXIT_OK
    assert result["state"] == "rolled_back"
    assert result["writes_performed"] is True
    durable = fresh_state(dsn, str(plan["operation"]["promotion_id"]))
    assert durable["journal"]["state"] == "rolled_back"
    assert durable["advisory_locks"] == 0


def start_database(dsn: str, container_name: str) -> None:
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


def test_database_outages(dsn: str, plan, container_name: str) -> None:
    scenarios = (
        ("after_writes", "FAILED_PROMOTION_RECOVERY_PARTIAL_STATE", "failed"),
        ("advisory_unlock", "RECOVERY_V2_CLEANUP_FAILED", "rolled_back"),
        ("after_rolled_back", "FAILED_PROMOTION_RECOVERY_PARTIAL_STATE", "rolled_back"),
    )
    for phase, classification, journal_state in scenarios:
        with psycopg.connect(
            dsn, row_factory=dict_row, autocommit=True,
        ) as setup:
            reset_state(setup, plan)
        harness = RollbackHarness(
            dsn, plan, outage_phase=phase, container_name=container_name,
        )
        try:
            code, details, error = harness.run()
        finally:
            start_database(dsn, container_name)
        assert error is not None and error.code == classification, phase
        assert code == promotion.EXIT_PARTIAL
        assert details["writes_performed"] is True
        assert details["reconciliation_required"] is True
        durable = fresh_state(dsn, str(plan["operation"]["promotion_id"]))
        assert durable["journal"]["state"] == journal_state, (phase, durable)
        assert durable["advisory_locks"] == 0
        assert durable["platform"] == "local_dev"
        assert durable["control"] == "local_dev"
        assert durable["client"] == "local_dev"
        if phase == "advisory_unlock":
            assert durable["journal"]["error"].startswith(
                "ROLLBACK_EVIDENCE path="
            )
        if phase == "after_rolled_back":
            assert details["recovery_evidence_may_have_committed"] is True
            assert durable["journal"]["error"] is None


def main() -> None:
    dsn = os.environ.get(DSN_ENV)
    if not dsn:
        raise SystemExit(f"{DSN_ENV} is required; no platform DSN fallback is allowed")
    parsed = psycopg.conninfo.conninfo_to_dict(dsn)
    database_name = str(parsed["dbname"])
    assert database_name.startswith(DATABASE_PREFIX), database_name
    assert database_name not in FORBIDDEN_DATABASES, database_name
    plan = recovery_plan(database_name, str(uuid.uuid4()))
    test_cleanup_only_reconciliation_truth()
    with psycopg.connect(dsn, row_factory=dict_row, autocommit=True) as admin:
        setup_schema(admin)
        test_real_verification_helper_connection_lifecycle(dsn, plan)
        test_primary_and_raw_failures(dsn, admin, plan)
        test_cleanup_only_preserves_terminal_state(dsn, admin, plan)
        test_connection_open_failure(dsn, admin, plan)
        test_evidence_failure_is_partial(dsn, admin, plan)
        test_schema_054_keeps_normal_rollback_route(dsn, admin, plan)
    container_name = os.environ.get(CONTAINER_ENV)
    if container_name:
        test_database_outages(dsn, plan, container_name)
    print("rollback recovery cleanup PostgreSQL tests: OK")


if __name__ == "__main__":
    main()
