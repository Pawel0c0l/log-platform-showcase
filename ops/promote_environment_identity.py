#!/usr/bin/env python3
"""Dry-run-first, journalled environment identity promotion CLI.

Exit codes: 0 success, 2 invalid intent, 3 failed precondition/database error,
4 production-readiness incompatibility, 5 concurrent promotion, 6 partial run.
The command never restarts services and never performs provider/network messaging.
"""
from __future__ import annotations

import argparse
import grp
import json
import os
import pwd
import re
import shlex
import socket
import stat
import sys
from pathlib import Path
from typing import Any
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ops import environment_identity_promotion as promotion
from ops import failed_promotion_recovery as recovery_v2
from ops import promotion_plan_v4 as v4
from ops import promotion_plan_v5 as v5
from ops import resume_plan_v2 as resume_v2
from ops.environment_identity_file import identity_assignments_in_env_file
from ops.git_repository_state import (
    RepositoryState, RepositoryStateError, require_clean_repository,
)
from ops.runtime_identity_readiness import inspect_runtime_convergence


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-environment")
    parser.add_argument("--to-environment")
    parser.add_argument("--platform-uuid")
    parser.add_argument("--client-code", action="append", default=[])
    parser.add_argument("--expected-client-db-uuid", action="append", default=[], metavar="CLIENT_CODE=UUID")
    parser.add_argument("--runtime-environment-file", type=Path)
    parser.add_argument("--backup-reference", type=Path)
    parser.add_argument("--recovery-root", type=Path)
    parser.add_argument("--preserved-recovery-backup", type=Path)
    parser.add_argument("--recovery-evidence", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--attestation")
    parser.add_argument("--promotion-id")
    parser.add_argument("--recovery-plan-sha256")
    parser.add_argument("--resume-plan", action="store_true")
    parser.add_argument("--resume-plan-v1-diagnostic", action="store_true")
    parser.add_argument("--resume-plan-sha256")
    parser.add_argument("--check-production-readiness", action="store_true")
    parser.add_argument("--inspect-promotions", nargs="?", const="all", choices=("all", "planned", "in_progress", "completed", "failed", "rolled_back"))
    parser.add_argument("--rollback-plan", action="store_true")
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def _emit(value: object, *, as_json: bool = True) -> None:
    if as_json:
        print(json.dumps(value, sort_keys=True, indent=2, default=str))
    else:
        print(value)


def _require_arguments(args: argparse.Namespace, names: tuple[str, ...]) -> None:
    missing = [
        name.replace("_", "-")
        for name in names
        if getattr(args, name, None) in (None, [], "")
    ]
    if missing:
        raise promotion.PromotionError(
            "REQUIRED_ARGUMENT_MISSING",
            "missing explicit arguments: " + ", ".join(f"--{name}" for name in missing),
            exit_code=promotion.EXIT_INVALID,
        )


def _repository_state() -> RepositoryState:
    try:
        return require_clean_repository(REPO_ROOT)
    except RepositoryStateError as exc:
        raise promotion.PromotionError(
            exc.classification,
            "repository worktree or operation state is not clean",
            details=exc.details,
        ) from exc


def _require_plan_repository(plan: dict[str, object], state: RepositoryState) -> None:
    planned_head = plan.get("repository_head")
    if not planned_head:
        raise promotion.PromotionError(
            "PROMOTION_PLAN_REPOSITORY_HEAD_MISSING",
            "immutable promotion plan predates repository HEAD binding",
        )
    if planned_head != state.head:
        raise promotion.PromotionError(
            "PROMOTION_PLAN_REPOSITORY_HEAD_MISMATCH",
            "current repository HEAD differs from the immutable promotion plan",
        )


def _runtime(args: argparse.Namespace) -> promotion.RuntimeFileState:
    _require_arguments(args, ("runtime_environment_file",))
    if identity_assignments_in_env_file(REPO_ROOT / ".env"):
        raise promotion.PromotionError(
            "RUNTIME_IDENTITY_NOT_PROVISIONED",
            "repository .env remains an authoritative identity source; run runtime provisioning first",
        )
    load_dotenv(REPO_ROOT / ".env", override=False)
    return promotion.inspect_runtime_file(
        args.runtime_environment_file,
        allowed_paths=promotion.approved_runtime_paths(REPO_ROOT),
    )


def _platform_conn(runtime: promotion.RuntimeFileState, *, read_only: bool, autocommit: bool = False):
    values = os.environ
    required = ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB", "POSTGRES_USER")
    missing = [name for name in required if not str(values.get(name) or "").strip()]
    if missing:
        raise promotion.PromotionError("PLATFORM_CONFIG_MISSING", "runtime file lacks required platform coordinates")
    return promotion.connect_database(
        host=str(values["POSTGRES_HOST"]),
        port=int(values["POSTGRES_PORT"]),
        dbname=str(values["POSTGRES_DB"]),
        user=str(values["POSTGRES_USER"]),
        password=str(values.get("POSTGRES_PASSWORD") or os.environ.get("POSTGRES_PASSWORD") or ""),
        read_only=read_only, autocommit=autocommit,
    )


def _validate_scope(args: argparse.Namespace) -> dict[str, str]:
    _require_arguments(
        args,
        (
            "from_environment",
            "to_environment",
            "platform_uuid",
            "client_code",
            "expected_client_db_uuid",
            "runtime_environment_file",
            "backup_reference",
            "recovery_root",
            "preserved_recovery_backup",
            "recovery_evidence",
        ),
    )
    promotion.validate_environments(args.from_environment, args.to_environment)
    platform_uuid = promotion.canonical_uuid(args.platform_uuid, "platform database UUID")
    expected = promotion.parse_expected_clients(args.expected_client_db_uuid)
    requested_codes = args.client_code
    if len(requested_codes) != len(set(requested_codes)):
        raise promotion.PromotionError("DUPLICATE_CLIENT", "--client-code contains a duplicate", exit_code=promotion.EXIT_INVALID)
    if set(requested_codes) != set(expected):
        raise promotion.PromotionError(
            "CLIENT_SCOPE_MISMATCH",
            "--client-code and --expected-client-db-uuid must name exactly the same clients",
            exit_code=promotion.EXIT_INVALID,
        )
    args.platform_uuid = platform_uuid
    return expected


def _open_client_connection(client: promotion.ClientPlan, *, read_only: bool):
    password = promotion.resolve_secret(client.password_secret_ref, os.environ)
    return promotion.connect_database(
        host=client.database_host, port=client.database_port,
        dbname=client.database_name, user=client.database_user,
        password=password, read_only=read_only,
    )


def _client_connection_factory(client: promotion.ClientPlan):
    return lambda read_only: _open_client_connection(client, read_only=read_only)


def _connect_clients(clients: list[promotion.ClientPlan], runtime: promotion.RuntimeFileState, *, read_only: bool) -> dict[str, Any]:
    connections: dict[str, Any] = {}
    try:
        for client in clients:
            connections[client.client_code] = _open_client_connection(client, read_only=read_only)
        return connections
    except Exception:
        for conn in connections.values():
            conn.close()
        raise


def _check_preconditions(
    *,
    platform_conn,
    client_connections: dict[str, Any],
    clients: list[promotion.ClientPlan],
    platform_uuid: str,
    platform_database: str,
    source: str,
    target: str,
    allow_target: bool,
) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
    platform_marker = promotion.marker_snapshot(
        platform_conn,
        expected_role="platform",
        expected_database=platform_database,
        expected_client_code=None,
    )
    accepted = {source, target} if allow_target else {source}
    if platform_marker["database_uuid"] != platform_uuid:
        raise promotion.PromotionError("PLATFORM_UUID_MISMATCH", "platform marker UUID does not match the explicit operator argument")
    if platform_marker["environment"] not in accepted:
        raise promotion.PromotionError("PLATFORM_SOURCE_MISMATCH", "platform marker environment is not an accepted source/resume value")
    markers: dict[str, dict[str, object]] = {}
    for client in clients:
        marker = promotion.marker_snapshot(
            client_connections[client.client_code],
            expected_role="client_business",
            expected_database=client.database_name,
            expected_client_code=client.client_code,
        )
        if marker["database_uuid"] != client.database_uuid:
            raise promotion.PromotionError("CLIENT_UUID_MISMATCH", f"client marker UUID mismatch: {client.client_code}")
        if marker["environment"] not in accepted:
            raise promotion.PromotionError("CLIENT_SOURCE_MISMATCH", f"client marker environment is not an accepted source/resume value: {client.client_code}")
        markers[client.client_code] = marker
    return platform_marker, markers


def _build_v5_plan(
    args: argparse.Namespace, runtime: promotion.RuntimeFileState, platform_conn,
    clients: list[promotion.ClientPlan], client_connections: dict[str, Any],
    repository_state: RepositoryState, runtime_convergence: dict[str, object],
    platform_marker: dict[str, object], client_markers: dict[str, dict[str, object]],
) -> dict[str, object]:
    checkpoint_binding, recovery_binding = v5.checkpoint_and_recovery_bindings(
        repository_root=REPO_ROOT, checkpoint=args.backup_reference, recovery_root=args.recovery_root,
        backup_path=args.preserved_recovery_backup, evidence_path=args.recovery_evidence)
    skeleton = {"platform_uuid": args.platform_uuid, "source_environment": args.from_environment,
                "clients": [client.public_dict() for client in sorted(clients, key=lambda row: row.client_code)]}
    promotion.validate_backup_reference(args.backup_reference, skeleton)
    database, historical_recovery = v5.database_and_history_bindings(platform_conn=platform_conn, client_connections=client_connections,
        clients=clients, platform_marker=platform_marker, client_markers=client_markers,
        source=args.from_environment, target=args.to_environment)
    return promotion.build_plan(
        source=args.from_environment, target=args.to_environment, platform_uuid=args.platform_uuid,
        platform_database=str(os.environ.get("POSTGRES_DB") or ""), runtime_file=runtime, clients=clients,
        backup_reference=str(args.backup_reference.absolute()), runtime_convergence=runtime_convergence,
        repository_head=repository_state.head,
        operation_identity=v5.operation_binding(repository_root=REPO_ROOT, repository_state=repository_state, source=args.from_environment, target=args.to_environment),
        canonical_identity=v5.canonical_identity_binding(runtime, target=args.to_environment),
        database_bindings=database, runtime_bindings=v5.runtime_bindings(repository_root=REPO_ROOT, runtime_convergence=runtime_convergence),
        checkpoint_binding=checkpoint_binding, recovery_binding=recovery_binding,
        effective_sudo_policy=v5.effective_sudo_policy_binding(),
        implementation_assets=v5.implementation_asset_bindings(REPO_ROOT),
        historical_recovery=historical_recovery, excluded_actions=v5.EXCLUDED_ACTIONS)


def _new_plan(
    args: argparse.Namespace, runtime: promotion.RuntimeFileState, platform_conn,
    expected: dict[str, str], repository_state: RepositoryState,
) -> tuple[dict[str, object], list[promotion.ClientPlan]]:
    if runtime.values.get(promotion.TARGET_ENVIRONMENT_KEY) != args.from_environment:
        raise promotion.PromotionError("RUNTIME_FILE_SOURCE_MISMATCH", "runtime file does not declare the source environment")
    runtime_convergence = inspect_runtime_convergence(REPO_ROOT, canonical_path=runtime.path)
    if not runtime_convergence.get("execute_allowed"):
        raise promotion.PromotionError(str(runtime_convergence.get("classification") or "READINESS_DRIFT"), "canonical runtime identity consumers are not fully converged")
    clients = promotion.load_selected_clients(platform_conn, expected, source=args.from_environment)
    client_connections = _connect_clients(clients, runtime, read_only=True)
    try:
        platform_marker, client_markers = _check_preconditions(
            platform_conn=platform_conn, client_connections=client_connections, clients=clients,
            platform_uuid=args.platform_uuid, platform_database=str(os.environ["POSTGRES_DB"]),
            source=args.from_environment, target=args.to_environment, allow_target=False)
        readiness = promotion.readiness_report(platform_conn, expected, {**os.environ, **runtime.values})
        readiness = _augment_readiness(readiness, clients=clients, client_connections=client_connections, runtime=runtime)
        if not readiness.get("execute_allowed"):
            raise promotion.PromotionError(str(readiness.get("classification") or "READINESS_DRIFT"), "production readiness is not satisfied")
        plan = _build_v5_plan(args, runtime, platform_conn, clients, client_connections, repository_state,
                              dict(readiness["runtime_identity"]), platform_marker, client_markers)
    finally:
        for conn in client_connections.values(): conn.close()
    return plan, clients

def _resume_plan(
    args: argparse.Namespace, platform_conn, expected: dict[str, str],
    repository_state: RepositoryState,
) -> tuple[dict[str, object], list[promotion.ClientPlan], dict[str, object]]:
    rows = promotion.inspect_journals(platform_conn, promotion_id=args.promotion_id)
    if len(rows) != 1:
        raise promotion.PromotionError("PROMOTION_NOT_FOUND", "resume promotion journal was not found")
    journal = rows[0]
    plan = dict(journal["immutable_plan_json"])
    if journal.get("state") == "rolled_back":
        raise promotion.PromotionError("PROMOTION_TERMINAL", "rolled-back promotion cannot be resumed")
    promotion.require_v5_forward_plan(plan)
    if promotion.plan_hash(plan) != journal.get("plan_sha256"):
        raise promotion.PromotionError("PROMOTION_PLAN_MISMATCH", "journal plan hash does not match its immutable payload")
    if (
        plan.get("source_environment") != args.from_environment
        or plan.get("target_environment") != args.to_environment
        or plan.get("platform_uuid") != args.platform_uuid
        or plan.get("runtime_environment_file") != str(args.runtime_environment_file.absolute())
        or plan.get("backup_reference") != str(args.backup_reference.absolute())
        or plan.get("recovery_binding", {}).get("root", {}).get("path") != str(args.recovery_root.absolute())
        or plan.get("recovery_binding", {}).get("preserved_backup", {}).get("path") != str(args.preserved_recovery_backup.absolute())
        or plan.get("recovery_binding", {}).get("evidence", {}).get("path") != str(args.recovery_evidence.absolute())
    ):
        raise promotion.PromotionError("PROMOTION_PLAN_MISMATCH", "resume arguments do not match the immutable journal plan")
    planned_expected = {row["client_code"]: row["database_uuid"] for row in plan["clients"]}
    if planned_expected != expected:
        raise promotion.PromotionError("PROMOTION_PLAN_MISMATCH", "resume client scope does not match the immutable journal plan")
    clients = promotion.load_selected_clients(
        platform_conn,
        expected,
        source=args.from_environment,
        target=args.to_environment,
    )
    return plan, clients, journal


def _query_control_environments(platform_conn, codes: list[str]) -> dict[str, str]:
    with platform_conn.cursor() as cur:
        cur.execute(
            "SELECT client_code, client_db_environment FROM workflow_a_control.client_account WHERE client_code=ANY(%s)",
            (codes,),
        )
        return {str(row["client_code"]): str(row["client_db_environment"]) for row in cur.fetchall()}



def _augment_readiness(
    report: dict[str, object], *, clients: list[promotion.ClientPlan],
    client_connections: dict[str, Any], runtime: promotion.RuntimeFileState,
) -> dict[str, object]:
    capabilities = {
        client.client_code: promotion.client_promotion_capability(
            client_connections[client.client_code], expected_user=client.database_user
        )
        for client in clients
    }
    runtime_report = inspect_runtime_convergence(REPO_ROOT, canonical_path=runtime.path)
    unsafe = [code for code, row in capabilities.items() if not row.get("least_privilege_safe")]
    missing = [code for code, row in capabilities.items() if not row.get("available")]
    if not runtime_report.get("execute_allowed"):
        classification = runtime_report.get("classification")
    elif missing:
        classification = "CLIENT_PROMOTION_PRIMITIVE_MISSING"
    elif unsafe:
        classification = "CLIENT_PROMOTION_PRIVILEGE_UNSAFE"
    else:
        classification = "PRODUCTION_PROMOTION_READY"
    report["runtime_identity"] = runtime_report
    report["client_promotion_primitives"] = capabilities
    report["classification"] = classification
    report["execute_allowed"] = bool(report.get("execute_allowed")) and classification == "PRODUCTION_PROMOTION_READY"
    return report


def _verify_planned_helper(plan: dict[str, object]) -> None:
    helper = dict(plan.get("privileged_helper") or {})
    installed = Path(str(helper.get("path") or ""))
    dependency = Path(str(helper.get("dependency_path") or ""))
    if installed.is_symlink() or not installed.is_file() or promotion.sha256_bytes(installed.read_bytes()) != helper.get("sha256"):
        raise promotion.PromotionError("PRIVILEGED_HELPER_CHANGED", "installed helper no longer matches the immutable plan")
    if dependency.is_symlink() or not dependency.is_file() or promotion.sha256_bytes(dependency.read_bytes()) != helper.get("dependency_sha256"):
        raise promotion.PromotionError("PRIVILEGED_HELPER_CHANGED", "helper parser dependency no longer matches the immutable plan")

def _dry_run(args: argparse.Namespace) -> int:
    repository_state = _repository_state()
    expected = _validate_scope(args)
    runtime = _runtime(args)
    with _platform_conn(runtime, read_only=True) as platform_conn:
        plan, clients = _new_plan(args, runtime, platform_conn, expected, repository_state)
        client_connections = _connect_clients(clients, runtime, read_only=True)
        try:
            platform_marker, client_markers = _check_preconditions(
                platform_conn=platform_conn,
                client_connections=client_connections,
                clients=clients,
                platform_uuid=args.platform_uuid,
                platform_database=str(os.environ["POSTGRES_DB"]),
                source=args.from_environment,
                target=args.to_environment,
                allow_target=False,
            )
            backup = promotion.validate_backup_reference(args.backup_reference, plan)
            readiness = promotion.readiness_report(platform_conn, expected, {**os.environ, **runtime.values})
            readiness = _augment_readiness(readiness, clients=clients, client_connections=client_connections, runtime=runtime)
        finally:
            for conn in client_connections.values():
                conn.close()
    result = {
        "mode": "dry_run",
        "writes_performed": False,
        "plan": plan,
        "canonical_plan_json": promotion.canonical_json(plan),
        "plan_sha256": promotion.plan_hash(plan),
        "required_attestation": promotion.promotion_attestation(plan),
        "backup_evidence": backup,
        "platform_marker": {key: platform_marker[key] for key in ("environment", "database_uuid", "database_name")},
        "client_markers": {code: {key: row[key] for key in ("environment", "database_uuid", "database_name", "client_code")} for code, row in client_markers.items()},
        "readiness": readiness,
        "affected_rows": ["ops_control.environment_identity@platform"] + [f"workflow_a_control.client_account:{code}" for code in sorted(expected)] + [f"ops_control.environment_identity@{code}" for code in sorted(expected)],
        "affected_files": [str(runtime.path)],
        "service_restart_performed": False,
    }
    _emit(result)
    return promotion.EXIT_OK


def _validate_v5_static_bindings(
    plan: dict[str, object], *, platform_conn, repository_state: RepositoryState,
    resuming: bool,
) -> None:
    promotion.require_v5_forward_plan(plan)
    _require_plan_repository(plan, repository_state)
    if v5.effective_sudo_policy_binding() != plan.get("effective_sudo_policy"):
        raise promotion.PromotionError("SUDO_POLICY_DRIFT", "effective sudo policy differs from immutable v5 plan")
    if v5.implementation_asset_bindings(REPO_ROOT) != plan.get("implementation_assets"):
        raise promotion.PromotionError("PROMOTION_IMPLEMENTATION_ASSET_DRIFT", "promotion implementation differs from immutable v5 plan")
    if list(v5.EXCLUDED_ACTIONS) != plan.get("excluded_actions"):
        raise promotion.PromotionError("PROMOTION_EXCLUSION_CONTRACT_DRIFT", "excluded actions differ from immutable v5 plan")
    current = v5.historical_recovery_bindings(platform_conn)
    planned = dict(plan.get("historical_recovery") or {})
    stable_keys = (
        "historical_row_count", "incomplete_recovery_row_count",
        "rolled_back_rows_excluded_from_active_blocking",
        "rolled_back_rows_cryptographically_bound_as_history", "entries",
    )
    if any(current.get(key) != planned.get(key) for key in stable_keys):
        raise promotion.PromotionError("PROMOTION_HISTORY_DRIFT", "historical recovery differs from immutable v5 plan")
    expected_active = 1 if resuming else 0
    expected_total = int(planned.get("total_journal_row_count") or 0) + expected_active
    if (current.get("active_promotion_row_count") != expected_active
            or current.get("total_journal_row_count") != expected_total):
        raise promotion.PromotionError("PROMOTION_HISTORY_DRIFT", "promotion journal baseline differs from immutable v5 plan")


class _ForwardV5State:
    """Local execution truth and ordered secondary cleanup evidence."""

    def __init__(self) -> None:
        self.promotion_id: str | None = None
        self.current_step: str | None = None
        self.writes_started = False
        self.cleanup_failures: list[dict[str, object]] = []

    def record_cleanup_failure(self, operation: str, exc: BaseException) -> None:
        self.cleanup_failures.append(_cleanup_failure_record(operation, exc))


def _forward_v5_close(handle, operation: str, state: _ForwardV5State) -> None:
    if handle is None:
        return
    try:
        handle.close()
    except Exception as exc:
        state.record_cleanup_failure(operation, exc)


class _ForwardV5Session:
    """Own all platform execution connections and advisory-lock state."""

    def __init__(self) -> None:
        self.lock_conn = None
        self.read_conn = None
        self.write_conn = None
        self.lock_acquired = False

    def open(self, runtime: promotion.RuntimeFileState, state: _ForwardV5State) -> None:
        """Open all handles or close every earlier handle before re-raising."""
        try:
            self.lock_conn = _platform_conn(runtime, read_only=False, autocommit=True)
            self.read_conn = _platform_conn(runtime, read_only=True)
            self.write_conn = _platform_conn(runtime, read_only=False)
        except Exception:
            self.release(state)
            raise

    def release(self, state: _ForwardV5State) -> None:
        """Isolate every cleanup phase so none can mask execution truth."""
        _forward_v5_close(self.write_conn, "write_connection_close", state)
        self.write_conn = None
        _forward_v5_close(self.read_conn, "read_connection_close", state)
        self.read_conn = None
        if self.lock_acquired:
            try:
                promotion.release_promotion_lock(self.lock_conn)
            except Exception as exc:
                state.record_cleanup_failure("advisory_lock_release", exc)
            self.lock_acquired = False
        _forward_v5_close(self.lock_conn, "lock_connection_close", state)
        self.lock_conn = None


def _forward_v5_schema_gate(
    runtime: promotion.RuntimeFileState, state: _ForwardV5State,
) -> None:
    """Probe journal capability before any write-capable connection or lock."""
    probe_conn = None
    try:
        probe_conn = _platform_conn(runtime, read_only=True)
        promotion.require_consistent_journal_schema(probe_conn)
    finally:
        _forward_v5_close(probe_conn, "schema_probe_connection_close", state)
    if state.cleanup_failures:
        raise promotion.PromotionError(
            "FORWARD_V5_CLEANUP_FAILED",
            "the read-only schema probe succeeded but its connection cleanup failed",
            details={
                "writes_performed": False,
                "reconciliation_required": False,
                "execution_phase": "journal_schema_capability_gate",
                "cleanup_failures": list(state.cleanup_failures),
            },
        )


def _forward_v5_attach_cleanup(
    exc: promotion.PromotionError, state: _ForwardV5State,
) -> promotion.PromotionError:
    details = dict(exc.details or {})
    details["writes_performed"] = state.writes_started
    details["reconciliation_required"] = state.writes_started
    details.setdefault("promotion_id", state.promotion_id)
    details.setdefault(
        "execution_phase", state.current_step or "before_first_journal_write",
    )
    if state.cleanup_failures:
        details["cleanup_failures"] = list(state.cleanup_failures)
    exc.details = details
    if state.writes_started:
        exc.exit_code = promotion.EXIT_PARTIAL
    return exc


def _forward_v5_cleanup_only_error(
    state: _ForwardV5State, *, journal_state: str,
    completed_steps: list[str], completed_may_have_succeeded: bool,
) -> promotion.PromotionError:
    message = (
        "forward execution reached its durable result but resource cleanup failed; "
        "inspect the promotion journal read-only with --inspect-promotions all "
        "before taking any further action"
    )
    return promotion.PromotionError(
        "FORWARD_V5_CLEANUP_FAILED",
        message,
        exit_code=promotion.EXIT_PARTIAL if state.writes_started else promotion.EXIT_PRECONDITION,
        details={
            "writes_performed": state.writes_started,
            "reconciliation_required": state.writes_started,
            "promotion_id": state.promotion_id,
            "execution_phase": "resource_cleanup",
            "journal_state": journal_state,
            "completed_steps": list(completed_steps),
            "completion_may_have_succeeded": completed_may_have_succeeded,
            "operator_action": (
                "inspect the journal read-only with --inspect-promotions all "
                f"--promotion-id '{state.promotion_id}'"
            ),
            "cleanup_failures": list(state.cleanup_failures),
        },
    )


def _execute(args: argparse.Namespace) -> int:
    repository_state = _repository_state()
    if args.promotion_id:
        raise promotion.PromotionError(
            "RESUME_V1_NON_EXECUTABLE",
            "the retired generic resume path is non-executable; CLI routing must use state-aware resume-v2 or fail closed",
            exit_code=promotion.EXIT_INVALID,
            details={"writes_performed": False},
        )
    expected = _validate_scope(args)
    if not args.attestation:
        raise promotion.PromotionError("ATTESTATION_MISSING", "--execute requires the exact generated attestation", exit_code=promotion.EXIT_INVALID)
    if "contract=v3" in args.attestation or "contract=v4" in args.attestation:
        raise promotion.PromotionError(
            "PROMOTION_PLAN_CONTRACT_SUPERSEDED",
            "v3 and v4 forward attestations are superseded and non-executable",
            exit_code=promotion.EXIT_INVALID,
        )
    runtime = _runtime(args)
    state = _ForwardV5State()
    session = _ForwardV5Session()
    try:
        _forward_v5_schema_gate(runtime, state)
        session.open(runtime, state)
        lock_conn = session.lock_conn
        read_conn = session.read_conn
        write_conn = session.write_conn
        if not promotion.try_promotion_lock(lock_conn):
            raise promotion.PromotionError("PROMOTION_BUSY", "another environment promotion holds the advisory lock", exit_code=promotion.EXIT_BUSY)
        session.lock_acquired = True
        plan, clients = _new_plan(args, runtime, read_conn, expected, repository_state)
        _require_plan_repository(plan, repository_state)
        promotion.validate_backup_reference(args.backup_reference, plan)
        checkpoint_now, recovery_now = v5.checkpoint_and_recovery_bindings(
            repository_root=REPO_ROOT, checkpoint=args.backup_reference, recovery_root=args.recovery_root,
            backup_path=args.preserved_recovery_backup, evidence_path=args.recovery_evidence)
        if checkpoint_now != plan.get("checkpoint_binding"):
            raise promotion.PromotionError("CHECKPOINT_DRIFT", "checkpoint differs from immutable v5 plan")
        if recovery_now != plan.get("recovery_binding"):
            raise promotion.PromotionError("RECOVERY_DRIFT", "recovery evidence differs from immutable v5 plan")
        v5.validate_installed_assets(plan)
        _verify_planned_helper(plan)
        _validate_v5_static_bindings(
            plan, platform_conn=read_conn, repository_state=repository_state,
            resuming=False,
        )
        client_reads = _connect_clients(clients, runtime, read_only=True)
        try:
            readiness = promotion.readiness_report(read_conn, expected, {**os.environ, **runtime.values})
            readiness = _augment_readiness(readiness, clients=clients, client_connections=client_reads, runtime=runtime)
            if not readiness["execute_allowed"]:
                raise promotion.PromotionError(str(readiness.get("classification") or "PRODUCTION_READINESS_INCOMPATIBLE"), "production readiness is not satisfied", exit_code=promotion.EXIT_INCOMPATIBLE)
            platform_marker, client_markers = _check_preconditions(
                platform_conn=read_conn, client_connections=client_reads, clients=clients,
                platform_uuid=args.platform_uuid, platform_database=str(os.environ["POSTGRES_DB"]),
                source=args.from_environment, target=args.to_environment,
                allow_target=False,
            )
            prewrite_plan = _build_v5_plan(
                args, runtime, read_conn, clients, client_reads, repository_state,
                dict(readiness["runtime_identity"]), platform_marker, client_markers,
            )
            if promotion.canonical_json(prewrite_plan) != promotion.canonical_json(plan):
                raise promotion.PromotionError(
                    v5.binding_drift_code(plan, prewrite_plan),
                    "immutable v5 inputs changed immediately before journal creation",
                    details={
                        "planned_sha256": promotion.plan_hash(plan),
                        "current_sha256": promotion.plan_hash(prewrite_plan),
                    },
                )
            required_attestation = promotion.promotion_attestation(plan)
        finally:
            for conn in client_reads.values():
                conn.close()
        if args.attestation != required_attestation:
            raise promotion.PromotionError("ATTESTATION_MISMATCH", "provided attestation does not exactly match the current immutable execution plan", exit_code=promotion.EXIT_INVALID)
        promotion_id, journal = promotion.create_or_resume_journal(
            write_conn, promotion_id=None, plan=plan,
            attestation=promotion.promotion_attestation(plan),
            backup_reference=str(args.backup_reference.absolute()),
        )
        state.promotion_id = promotion_id
        state.writes_started = True
        mutation_attestation_hash = promotion.sha256_bytes(args.attestation.encode("utf-8"))
        for client in clients:
            current_step = f"{promotion.STEP_CLIENT_PREFIX}{client.client_code}"
            state.current_step = current_step
            promotion.journal_step(write_conn, promotion_id, current_step=current_step)
            promotion.mutate_client_marker_durably(
                open_connection=_client_connection_factory(client),
                expected_database=client.database_name, expected_uuid=client.database_uuid,
                expected_user=client.database_user, expected_client_code=client.client_code,
                source=args.from_environment, target=args.to_environment,
                promotion_id=promotion_id, attestation_hash=mutation_attestation_hash,
            )
            promotion.journal_step(write_conn, promotion_id, current_step=None, completed_step=current_step)
        current_step = promotion.STEP_CONTROL_PLANE
        state.current_step = current_step
        promotion.journal_step(write_conn, promotion_id, current_step=current_step)
        promotion.update_control_plane(write_conn, clients, source=args.from_environment, target=args.to_environment)
        promotion.journal_step(write_conn, promotion_id, current_step=None, completed_step=current_step)
        current_step = promotion.STEP_PLATFORM_MARKER
        state.current_step = current_step
        promotion.journal_step(write_conn, promotion_id, current_step=current_step)
        promotion.update_marker_environment(
            write_conn, expected_database=str(os.environ["POSTGRES_DB"]),
            expected_uuid=args.platform_uuid, expected_role="platform",
            expected_client_code=None, source=args.from_environment, target=args.to_environment,
        )
        promotion.journal_step(write_conn, promotion_id, current_step=None, completed_step=current_step)
        current_step = promotion.STEP_RUNTIME_FILE
        state.current_step = current_step
        promotion.journal_step(write_conn, promotion_id, current_step=current_step)
        _verify_planned_helper(plan)
        runtime_now = promotion.inspect_runtime_file(runtime.path, allowed_paths=(runtime.path,))
        if runtime_now.values.get(promotion.TARGET_ENVIRONMENT_KEY) == args.to_environment:
            if runtime_now.checksum != plan["runtime_file_after_sha256"]:
                raise promotion.PromotionError("RUNTIME_FILE_RESUME_EVIDENCE", "target canonical file checksum mismatches the immutable plan")
            journal_now = promotion.inspect_journals(read_conn, promotion_id=promotion_id)[0]
            if not journal_now.get("runtime_file_backup_path"):
                raise promotion.PromotionError("RUNTIME_FILE_RESUME_EVIDENCE", "journal lacks the checksum-bound helper backup")
            file_result = {"backup_path": str(journal_now["runtime_file_backup_path"]), "before_sha256": str(plan["runtime_file_before_sha256"]), "after_sha256": runtime_now.checksum}
        else:
            file_result = promotion.invoke_identity_helper(runtime_now, source=args.from_environment, target=args.to_environment)
        promotion.journal_step(write_conn, promotion_id, current_step=None, completed_step=current_step, file_result=file_result)
        current_step = promotion.STEP_RUNTIME_RELOAD
        state.current_step = current_step
        promotion.journal_step(write_conn, promotion_id, current_step=current_step)
        convergence = inspect_runtime_convergence(REPO_ROOT, canonical_path=runtime.path)
        if convergence.get("classification") == "RUNTIME_RELOAD_REQUIRED":
            result = {
                "mode": "execute_paused", "promotion_id": promotion_id,
                "state": "in_progress", "current_step": current_step,
                "completed_steps": promotion.inspect_journals(read_conn, promotion_id=promotion_id)[0]["completed_steps"],
                "required_service_actions": plan["required_service_actions"],
                "resume_phases": [
                    {
                        "phase": "generate_read_only_resume_v2_plan",
                        "command": _resume_plan_command(args, promotion_id),
                        "writes_performed": False,
                    },
                    {
                        "phase": "separately_approve_and_execute_exact_generated_plan",
                        "command_source": "future_command from the exact --resume-plan output",
                        "required_arguments": ["--resume-plan-sha256", "--attestation"],
                    },
                ],
                "retired_resume_v1_command": {"executable": False, "reason": "resume-v1 is retired"},
                "message": "runtime identity file changed; approved reload/recreate actions are required before resume",
            }
            session.release(state)
            if state.cleanup_failures:
                raise _forward_v5_cleanup_only_error(
                    state, journal_state="in_progress",
                    completed_steps=list(result["completed_steps"]),
                    completed_may_have_succeeded=False,
                )
            _emit(result)
            return promotion.EXIT_PARTIAL
        if not convergence.get("execute_allowed"):
            raise promotion.PromotionError(str(convergence.get("classification")), "runtime convergence verification failed")
        promotion.journal_step(write_conn, promotion_id, current_step=None, completed_step=current_step)
        current_step = promotion.STEP_RUNTIME_PROCESSES
        state.current_step = current_step
        promotion.journal_step(write_conn, promotion_id, current_step=current_step)
        promotion.journal_step(write_conn, promotion_id, current_step=None, completed_step=current_step)
        current_step = promotion.STEP_FINAL_VERIFY
        state.current_step = current_step
        promotion.journal_step(write_conn, promotion_id, current_step=current_step)
        verify_platform = _platform_conn(runtime, read_only=True)
        verify_clients = _connect_clients(clients, runtime, read_only=True)
        try:
            runtime_final = promotion.inspect_runtime_file(runtime.path, allowed_paths=(runtime.path,))
            final_platform, final_clients = _check_preconditions(
                platform_conn=verify_platform, client_connections=verify_clients, clients=clients,
                platform_uuid=args.platform_uuid, platform_database=str(os.environ["POSTGRES_DB"]),
                source=args.to_environment, target=args.to_environment, allow_target=False,
            )
            final_control = _query_control_environments(verify_platform, [client.client_code for client in clients])
            mixed = promotion.mixed_state_report(
                plan=plan, runtime_environment=str(runtime_final.values.get(promotion.TARGET_ENVIRONMENT_KEY)),
                platform_marker=final_platform, client_markers=final_clients,
                control_plane_environments=final_control,
            )
            if not mixed["all_at_target"]:
                raise promotion.PromotionError("FINAL_VERIFICATION_FAILED", "fresh connections do not observe all promotion surfaces at target")
        finally:
            for conn in verify_clients.values(): conn.close()
            verify_platform.close()
        promotion.journal_finalize_forward_v5(
            write_conn, promotion_id,
            expected_completed_steps=[str(step) for step in plan["steps"][:-1]],
        )
        final_journal_conn = _platform_conn(runtime, read_only=True)
        try:
            final_journal = promotion.inspect_journals(final_journal_conn, promotion_id=promotion_id)[0]
            if final_journal["state"] != "completed":
                raise promotion.PromotionError("JOURNAL_POST_COMMIT_VERIFY", "fresh connection did not observe completed journal state")
        finally:
            final_journal_conn.close()
        result = {"mode": "execute", "promotion_id": promotion_id, "state": "completed", "completed_steps": plan["steps"], "uuid_policy": "preserved", "service_restart_performed": False}
        session.release(state)
        if state.cleanup_failures:
            raise _forward_v5_cleanup_only_error(
                state, journal_state="completed",
                completed_steps=[str(step) for step in plan["steps"]],
                completed_may_have_succeeded=True,
            )
        _emit(result)
        return promotion.EXIT_OK
    except promotion.PromotionError as exc:
        if (state.promotion_id and state.writes_started
                and exc.code != "FORWARD_V5_CLEANUP_FAILED"):
            try:
                promotion.journal_fail(
                    session.write_conn, state.promotion_id,
                    current_step=state.current_step, error=str(exc),
                )
            except Exception as journal_exc:
                details = dict(exc.details or {})
                details["journal_failure_transition_failure"] = {
                    "original_exception_class": type(journal_exc).__name__,
                    "sanitized_message": _sanitized_exception_detail(journal_exc),
                }
                exc.details = details
        session.release(state)
        raise _forward_v5_attach_cleanup(exc, state)
    except Exception as exc:
        primary = promotion.PromotionError(
            "FORWARD_V5_EXECUTION_INTERRUPTED",
            f"{type(exc).__name__}: {_sanitized_exception_detail(exc)}",
            exit_code=promotion.EXIT_PARTIAL if state.writes_started else promotion.EXIT_PRECONDITION,
            details=_forward_interruption_details(
                exc, promotion_id=state.promotion_id,
                current_step=state.current_step, writes_started=state.writes_started,
            ),
        )
        session.release(state)
        raise _forward_v5_attach_cleanup(primary, state) from exc
    finally:
        session.release(state)

def _resume_v2_journal_counts(platform_conn) -> dict[str, int]:
    with platform_conn.cursor() as cur:
        cur.execute(
            f"""SELECT count(*) AS total,
                       count(*) FILTER (WHERE state IN ('planned','in_progress')) AS active,
                       count(*) FILTER (WHERE state='failed') AS incomplete_recovery
                  FROM {promotion.JOURNAL_TABLE}"""
        )
        row = dict(cur.fetchone())
    return {key: int(row[key]) for key in ("total", "active", "incomplete_recovery")}


def _collect_resume_v2_plan(
    args: argparse.Namespace, platform_conn, expected: dict[str, str],
    repository_state: RepositoryState, runtime: promotion.RuntimeFileState,
) -> tuple[dict[str, object], list[promotion.ClientPlan], dict[str, object]]:
    original_plan, clients, journal = _resume_plan(
        args, platform_conn, expected, repository_state,
    )
    original_head = str(dict(original_plan["operation_identity"])["repository_head"])
    relationship = resume_v2.head_relationship(
        REPO_ROOT, original_head, repository_state.head,
    )
    original_steps = [str(step) for step in original_plan["steps"]]
    resume_v2.require_resume_v2_finalization_route(
        journal=journal,
        original_steps=original_steps,
    )
    completed = [str(step) for step in journal.get("completed_steps") or []]
    remaining = original_steps[len(completed):]

    client_connections = _connect_clients(clients, runtime, read_only=True)
    try:
        platform_marker, client_markers = _check_preconditions(
            platform_conn=platform_conn, client_connections=client_connections,
            clients=clients, platform_uuid=args.platform_uuid,
            platform_database=str(original_plan["platform_database"]),
            source=args.from_environment, target=args.to_environment, allow_target=True,
        )
        controls = _query_control_environments(
            platform_conn, [client.client_code for client in clients],
        )
        capabilities = {
            client.client_code: promotion.client_promotion_capability(
                client_connections[client.client_code],
                expected_user=client.database_user,
            )
            for client in clients
        }
    finally:
        for connection in client_connections.values():
            connection.close()

    target = str(original_plan["target_environment"])
    if (
        runtime.values[promotion.TARGET_ENVIRONMENT_KEY] != target
        or platform_marker["environment"] != target
        or any(row["environment"] != target for row in client_markers.values())
        or any(value != target for value in controls.values())
    ):
        raise promotion.PromotionError(
            "RESUME_PERSISTENT_STATE_DRIFT",
            "every persistent identity surface must already be observed at target",
            details={"writes_performed": False},
        )

    convergence = inspect_runtime_convergence(REPO_ROOT, canonical_path=runtime.path)
    runtime_binding = {
        "readiness_classification": convergence.get("classification"),
        "reload_required_consumers": sorted(str(value) for value in convergence.get("reload_required", [])),
        "unconverted_consumers": sorted(str(value) for value in convergence.get("unconverted_consumers", [])),
        "conflicting_declarations": sorted(
            [dict(row) for row in convergence.get("conflicting_declarations", [])],
            key=lambda row: str(row.get("path")),
        ),
        "per_invocation_backup_readiness": convergence.get("per_invocation_consumers", {}).get("backup"),
        "per_invocation_prune_readiness": convergence.get("per_invocation_consumers", {}).get("prune"),
    }
    if (
        runtime_binding["readiness_classification"] != "PRODUCTION_PROMOTION_READY"
        or runtime_binding["reload_required_consumers"]
        or runtime_binding["unconverted_consumers"]
        or runtime_binding["conflicting_declarations"]
    ):
        raise promotion.PromotionError(
            "RESUME_IMMUTABLE_BINDING_DRIFT",
            "runtime convergence is not approval-ready",
            details={"writes_performed": False},
        )

    systemd = resume_v2.systemd_runtime_binding(expected_environment=target)
    docker = resume_v2.docker_runtime_binding(
        repository_root=REPO_ROOT, expected_environment=target,
    )
    canonical_stat = runtime.path.stat()
    canonical_identity = {
        "path": str(runtime.path.resolve()), "environment": target,
        "sha256": runtime.checksum,
        "owner": pwd.getpwuid(runtime.uid).pw_name,
        "group": grp.getgrgid(runtime.gid).gr_name,
        "uid": runtime.uid, "gid": runtime.gid,
        "mode": f"{runtime.mode:04o}", "regular_file": stat.S_ISREG(canonical_stat.st_mode),
        "symlink": runtime.path.is_symlink(), "assignment_cardinality": 1,
    }
    persistent_clients = []
    for client in sorted(clients, key=lambda row: row.client_code):
        marker = client_markers[client.client_code]
        capability = capabilities[client.client_code]
        persistent_clients.append({
            "client_code": client.client_code,
            "client_id": promotion.canonical_uuid(client.client_id, f"{client.client_code} client ID"),
            "database_name": client.database_name, "database_user": client.database_user,
            "database_host": client.database_host, "database_port": client.database_port,
            "marker_identity": marker["environment"],
            "control_plane_identity": controls[client.client_code],
            "database_uuid": promotion.canonical_uuid(marker["database_uuid"], f"{client.client_code} database UUID"),
            "control_plane_uuid": promotion.canonical_uuid(client.database_uuid, f"{client.client_code} control-plane UUID"),
            "enabled": True,
            "guarded_primitive_identity": str(capability.get("signature") or ""),
            "direct_marker_update_revoked": capability.get("table_update") is False and capability.get("column_update") is False,
            "least_privilege_result": capability.get("least_privilege_safe") is True,
        })
        if not persistent_clients[-1]["guarded_primitive_identity"] or not persistent_clients[-1]["direct_marker_update_revoked"] or not persistent_clients[-1]["least_privilege_result"]:
            raise promotion.PromotionError(
                "RESUME_PERSISTENT_STATE_DRIFT",
                f"client least-privilege binding is incomplete: {client.client_code}",
                details={"writes_performed": False},
            )

    counts = _resume_v2_journal_counts(platform_conn)
    reconciliation = []
    for step in original_steps:
        reconciliation.append({
            "step": step,
            "journal_reported_complete": step in completed,
            "observed_at_target": True,
            "classification": "completed_verified" if step in completed else "remaining_verified_ready",
        })
    journal_binding = {
        "promotion_id": promotion.canonical_uuid(journal["promotion_id"], "promotion ID"),
        "state": journal["state"], "current_step": journal["current_step"],
        "completed_steps": completed, "expected_remaining_steps": remaining,
        "active_promotion_count": counts["active"],
        "incomplete_recovery_count": counts["incomplete_recovery"],
        "total_journal_row_count": counts["total"],
        "reconciliation_classification": "JOURNAL_AND_REALITY_CONVERGED",
        "actual_state_authoritative": True,
        "all_completed_surfaces_observed_at_target": True,
        "reconciliation_records": reconciliation,
    }

    operation = dict(original_plan["operation_identity"])
    approval = {
        "host": socket.gethostname(),
        "repository_path": str(REPO_ROOT.resolve()),
        "repository_branch": repository_state.branch,
        "original_execution_head": original_head,
        "resume_implementation_head": repository_state.head,
        "head_relationship": relationship,
        "promotion_id": promotion.canonical_uuid(journal["promotion_id"], "promotion ID"),
        "original_v5_plan_sha256": str(journal["plan_sha256"]),
        "original_plan_contract_version": int(original_plan["contract_version"]),
        "source_environment": original_plan["source_environment"],
        "target_environment": original_plan["target_environment"],
        "platform_uuid": promotion.canonical_uuid(args.platform_uuid, "platform UUID"),
        "collection_locale": "C",
    }
    if approval["host"] != operation["host"]:
        raise promotion.PromotionError(
            "RESUME_APPROVAL_IDENTITY_DRIFT", "resume host differs from the original operation host",
            details={"writes_performed": False},
        )
    remote_binding = resume_v2.remote_repository_binding(
        REPO_ROOT,
        local_head=repository_state.head,
        local_branch=repository_state.branch,
    )

    persistent = {
        "canonical_identity": canonical_identity,
        "platform": {
            "database_name": str(original_plan["platform_database"]),
            "database_role": str(platform_marker["database_role"]),
            "marker_identity": str(platform_marker["environment"]),
            "database_uuid": promotion.canonical_uuid(platform_marker["database_uuid"], "platform database UUID"),
            "marker_cardinality": 1,
        },
        "clients": persistent_clients,
        "uuid_consistency": {
            "platform_unchanged": platform_marker["database_uuid"] == args.platform_uuid,
            "all_clients_unchanged": all(row["database_uuid"] == row["control_plane_uuid"] for row in persistent_clients),
            "all_canonical_lowercase": True,
        },
    }
    if not all(persistent["uuid_consistency"].values()):
        raise promotion.PromotionError(
            "RESUME_PERSISTENT_STATE_DRIFT", "UUID consistency is not proven",
            details={"writes_performed": False},
        )
    schema_contract = resume_v2.migration_054_schema_contract(
        platform_conn,
        expected_database_name=str(original_plan["platform_database"]),
        expected_database_uuid=str(args.platform_uuid),
        target_promotion_id=str(args.promotion_id),
    )

    checkpoint_now, recovery_now = v5.checkpoint_and_recovery_bindings(
        repository_root=REPO_ROOT, checkpoint=args.backup_reference,
        recovery_root=args.recovery_root, backup_path=args.preserved_recovery_backup,
        evidence_path=args.recovery_evidence,
    )
    helper_source = REPO_ROOT / "ops/systemd/proposed/log-platform-environment-identity-helper"
    parser_source = REPO_ROOT / "ops/environment_identity_file.py"
    helper_installed = Path(str(dict(original_plan["privileged_helper"])["path"]))
    parser_installed = Path(str(dict(original_plan["privileged_helper"])["dependency_path"]))
    helper = resume_v2._file_metadata(helper_installed)
    helper.update({
        "version": str(dict(original_plan["privileged_helper"])["version"]),
        "source_path": str(helper_source.resolve()),
        "source_sha256": promotion.sha256_bytes(helper_source.read_bytes()),
        "owner": pwd.getpwuid(int(helper["uid"])).pw_name,
        "group": grp.getgrgid(int(helper["gid"])).gr_name,
    })
    parser = resume_v2._file_metadata(parser_installed)
    parser.update({
        "source_path": str(parser_source.resolve()),
        "source_sha256": promotion.sha256_bytes(parser_source.read_bytes()),
        "owner": pwd.getpwuid(int(parser["uid"])).pw_name,
        "group": grp.getgrgid(int(parser["gid"])).gr_name,
    })
    if helper["source_sha256"] != helper["sha256"] or parser["source_sha256"] != parser["sha256"]:
        raise promotion.PromotionError(
            "RESUME_SECURITY_BINDING_DRIFT", "installed helper/parser differ from reviewed sources",
            details={"writes_performed": False},
        )
    history = v5.historical_recovery_bindings(platform_conn)
    security = {
        "privileged_helper": helper, "parser": parser,
        "effective_sudo_policy": resume_v2.effective_sudo_policy_binding(),
        "original_v5_effective_sudo_policy": dict(original_plan["effective_sudo_policy"]),
        "original_implementation_assets": list(original_plan["implementation_assets"]),
        "resume_implementation_assets": resume_v2.implementation_asset_bindings(REPO_ROOT),
        "provisioning_checkpoint": checkpoint_now,
        "provisioning_recovery": recovery_now,
        "historical_promotions": resume_v2.normalize_absence(sorted(history["entries"], key=lambda row: str(row["promotion_id"]))),
        "historical_promotion_counts": {
            "historical": history["historical_row_count"],
            "active": history["active_promotion_row_count"],
            "incomplete_recovery": history["incomplete_recovery_row_count"],
            "total": history["total_journal_row_count"],
        },
        "retired_contracts": {
            "resume_v1": {"contract": "resume-v1", "sha256": resume_v2.RETIRED_RESUME_V1_HASH, "executable": False},
            "recovery_v1": {"contract": "recovery-v1", "sha256": resume_v2.RETIRED_RECOVERY_V1_HASH, "executable": False},
        },
    }
    plan = resume_v2.build_plan(
        approval_identity=approval,
        remote_repository_binding=remote_binding,
        migration_054_schema_contract=schema_contract,
        journal_state=journal_binding,
        persistent_state=persistent, runtime_convergence=runtime_binding,
        systemd_runtime=systemd, docker_runtime=docker,
        security_and_recovery=security, promotion_id=str(args.promotion_id),
    )
    return plan, clients, journal


def _resume_plan_command(args: argparse.Namespace, promotion_id: str) -> str:
    command = [
        str(REPO_ROOT / ".venv/bin/python"), "ops/promote_environment_identity.py",
        "--from-environment", args.from_environment, "--to-environment", args.to_environment,
        "--platform-uuid", args.platform_uuid,
    ]
    for code in args.client_code:
        command += ["--client-code", code]
    for item in args.expected_client_db_uuid:
        command += ["--expected-client-db-uuid", item]
    command += [
        "--runtime-environment-file", str(args.runtime_environment_file),
        "--backup-reference", str(args.backup_reference),
        "--recovery-root", str(args.recovery_root),
        "--preserved-recovery-backup", str(args.preserved_recovery_backup),
        "--recovery-evidence", str(args.recovery_evidence),
        "--promotion-id", promotion_id,
        "--resume-plan",
    ]
    return f"PYTHONDONTWRITEBYTECODE=1 PYTHONPATH={shlex.quote(str(REPO_ROOT))} " + shlex.join(command)


def _resume_v2_command(
    args: argparse.Namespace, promotion_id: str, plan_sha256: str, attestation: str,
) -> str:
    command = [
        str(REPO_ROOT / ".venv/bin/python"), "ops/promote_environment_identity.py",
        "--from-environment", args.from_environment, "--to-environment", args.to_environment,
        "--platform-uuid", args.platform_uuid,
    ]
    for code in args.client_code:
        command += ["--client-code", code]
    for item in args.expected_client_db_uuid:
        command += ["--expected-client-db-uuid", item]
    command += [
        "--runtime-environment-file", str(args.runtime_environment_file),
        "--backup-reference", str(args.backup_reference),
        "--recovery-root", str(args.recovery_root),
        "--preserved-recovery-backup", str(args.preserved_recovery_backup),
        "--recovery-evidence", str(args.recovery_evidence),
        "--promotion-id", promotion_id, "--resume-plan-sha256", plan_sha256,
        "--execute", "--attestation", attestation,
    ]
    return f"PYTHONDONTWRITEBYTECODE=1 PYTHONPATH={shlex.quote(str(REPO_ROOT))} " + shlex.join(command)




def _resume_plan_dry_run(args: argparse.Namespace) -> int:
    repository_state = _repository_state()
    expected = _validate_scope(args)
    _require_arguments(args, ("promotion_id",))
    runtime = _runtime(args)
    with _platform_conn(runtime, read_only=True) as conn:
        promotion.require_resume_audit_schema(conn)
        resume_plan, _clients, _journal = _collect_resume_v2_plan(
            args, conn, expected, repository_state, runtime,
        )
    digest = promotion.plan_hash(resume_plan)
    required = resume_v2.attestation(resume_plan)
    _emit({
        "mode": "resume_plan_v2", "writes_performed": False,
        "resume_plan": resume_plan,
        "canonical_resume_plan_json": promotion.canonical_json(resume_plan),
        "resume_plan_sha256": digest,
        "required_attestation": required,
        "future_command": _resume_v2_command(
            args, str(args.promotion_id), digest, required,
        ),
    })
    return promotion.EXIT_OK


def _rollback_command(args: argparse.Namespace, promotion_id: str, plan_sha256: str, attestation: str) -> str:
    command = [
        str(REPO_ROOT / ".venv/bin/python"), "ops/promote_environment_identity.py",
        "--runtime-environment-file", str(args.runtime_environment_file),
        "--backup-reference", str(args.backup_reference),
        "--recovery-root", str(args.recovery_root),
        "--preserved-recovery-backup", str(args.preserved_recovery_backup),
        "--recovery-evidence", str(args.recovery_evidence),
        "--promotion-id", promotion_id, "--rollback", "--execute",
        "--recovery-plan-sha256", plan_sha256, "--attestation", attestation,
    ]
    return f"PYTHONDONTWRITEBYTECODE=1 PYTHONPATH={shlex.quote(str(REPO_ROOT))} " + shlex.join(command)


class _RollbackState:
    """Local rollback truth and ordered secondary cleanup evidence."""

    def __init__(self, promotion_id: str | None) -> None:
        self.promotion_id = promotion_id
        self.phase = "rollback_schema_capability_gate"
        self.mutation_started = False
        self.completed_actions: list[str] = []
        self.action_in_flight: str | None = None
        self.journal_state: str | None = None
        self.journal_transition_may_have_committed = False
        self.recovery_evidence_may_have_committed = False
        self.recovery_evidence: dict[str, object] | None = None
        self.cleanup_failures: list[dict[str, object]] = []

    def begin_action(
        self, action: str, *, mutation: bool = False,
        journal_commit_ambiguous: bool = False,
        evidence_commit_ambiguous: bool = False,
    ) -> None:
        self.phase = action
        self.action_in_flight = action
        if mutation:
            self.mutation_started = True
        if journal_commit_ambiguous:
            self.journal_transition_may_have_committed = True
        if evidence_commit_ambiguous:
            self.recovery_evidence_may_have_committed = True

    def complete_action(self, *actions: str) -> None:
        self.completed_actions.extend(actions)
        self.action_in_flight = None

    def record_cleanup_failure(self, operation: str, exc: BaseException) -> None:
        self.cleanup_failures.append(_cleanup_failure_record(operation, exc))


def _rollback_close(handle, operation: str, state: _RollbackState) -> None:
    if handle is None:
        return
    try:
        handle.close()
    except Exception as exc:
        state.record_cleanup_failure(operation, exc)


class _RollbackSession:
    """Own rollback write/lock connections and release each handle once."""

    def __init__(self) -> None:
        self.lock_conn = None
        self.write_conn = None
        self.lock_acquired = False

    def open(
        self, runtime: promotion.RuntimeFileState, state: _RollbackState,
    ) -> None:
        """Open all required handles before locking or close partial state."""
        try:
            self.lock_conn = _platform_conn(
                runtime, read_only=True, autocommit=True,
            )
            self.write_conn = _platform_conn(runtime, read_only=False)
        except Exception:
            self.release(state)
            raise

    def release(self, state: _RollbackState) -> None:
        """Attempt every cleanup operation in deterministic order."""
        _rollback_close(
            self.write_conn, "rollback_write_connection_close", state,
        )
        self.write_conn = None
        if self.lock_acquired:
            try:
                promotion.release_promotion_lock(self.lock_conn)
            except Exception as exc:
                state.record_cleanup_failure("advisory_lock_release", exc)
            self.lock_acquired = False
        _rollback_close(
            self.lock_conn, "advisory_lock_connection_close", state,
        )
        self.lock_conn = None


def _rollback_details(
    state: _RollbackState, details: dict[str, object],
) -> dict[str, object]:
    details["writes_performed"] = state.mutation_started
    details["reconciliation_required"] = state.mutation_started
    details.setdefault("promotion_id", state.promotion_id)
    details["execution_phase"] = state.phase
    details["completed_actions"] = list(state.completed_actions)
    details["action_in_flight"] = state.action_in_flight
    details["journal_state"] = state.journal_state
    details["journal_transition_may_have_committed"] = (
        state.journal_transition_may_have_committed
    )
    details["recovery_evidence_may_have_committed"] = (
        state.recovery_evidence_may_have_committed
    )
    if state.recovery_evidence is not None:
        details["recovery_evidence"] = dict(state.recovery_evidence)
    if state.cleanup_failures:
        details["cleanup_failures"] = list(state.cleanup_failures)
    if state.mutation_started:
        details["operator_action"] = (
            "inspect the journal and rollback evidence read-only with "
            f"--inspect-promotions all --promotion-id '{state.promotion_id}' "
            "before any retry"
        )
    return details


def _rollback_attach_cleanup(
    exc: promotion.PromotionError, state: _RollbackState,
) -> promotion.PromotionError:
    exc.details = _rollback_details(state, dict(exc.details or {}))
    if state.mutation_started:
        exc.exit_code = promotion.EXIT_PARTIAL
    return exc


def _rollback_interruption_error(
    exc: BaseException, state: _RollbackState,
) -> promotion.PromotionError:
    code = (
        "FAILED_PROMOTION_RECOVERY_PARTIAL_STATE"
        if state.mutation_started
        else "RECOVERY_V2_EXECUTION_INTERRUPTED"
    )
    message = (
        "recovery stopped after a rollback mutation started; reconcile durable "
        "state read-only before any retry"
        if state.mutation_started
        else "recovery execution was interrupted before any rollback mutation"
    )
    return promotion.PromotionError(
        code,
        message,
        exit_code=(
            promotion.EXIT_PARTIAL
            if state.mutation_started else promotion.EXIT_PRECONDITION
        ),
        details=_rollback_details(state, {
            "original_exception_class": type(exc).__name__,
            "sanitized_message": _sanitized_exception_detail(exc),
        }),
    )


def _rollback_cleanup_only_error(
    state: _RollbackState,
) -> promotion.PromotionError:
    message = (
        "rollback reached its durable result but resource cleanup failed; "
        "reconcile the journal and rollback evidence read-only before any retry"
        if state.mutation_started
        else "rollback preflight succeeded but resource cleanup failed before "
             "any mutation"
    )
    return promotion.PromotionError(
        "RECOVERY_V2_CLEANUP_FAILED",
        message,
        exit_code=(
            promotion.EXIT_PARTIAL
            if state.mutation_started else promotion.EXIT_PRECONDITION
        ),
        details=_rollback_details(state, {
            "operator_action": (
                "inspect the journal and rollback evidence read-only with "
                f"--inspect-promotions all --promotion-id '{state.promotion_id}'"
            ),
        }),
    )


def _rollback_schema_gate(
    runtime: promotion.RuntimeFileState, state: _RollbackState,
) -> None:
    """Probe schema before write/lock opens without discarding close evidence."""
    probe_conn = None
    primary = None
    try:
        probe_conn = _platform_conn(runtime, read_only=True)
        promotion.require_consistent_journal_schema(probe_conn)
    except Exception as exc:
        primary = exc
    finally:
        _rollback_close(
            probe_conn, "rollback_schema_probe_connection_close", state,
        )
    if primary is not None:
        if isinstance(primary, promotion.PromotionError):
            raise _rollback_attach_cleanup(primary, state)
        raise _rollback_interruption_error(primary, state) from primary
    if state.cleanup_failures:
        raise _rollback_cleanup_only_error(state)


def _inspect_row_surfaces(platform_conn, runtime: promotion.RuntimeFileState, journal: dict[str, object]) -> dict[str, object]:
    plan = dict(journal["immutable_plan_json"])
    expected = {row["client_code"]: row["database_uuid"] for row in plan["clients"]}
    clients = promotion.load_selected_clients(
        platform_conn,
        expected,
        source=str(plan["source_environment"]),
        target=str(plan["target_environment"]),
    )
    client_connections = _connect_clients(clients, runtime, read_only=True)
    try:
        platform_marker = promotion.marker_snapshot(
            platform_conn,
            expected_role="platform",
            expected_database=str(plan["platform_database"]),
            expected_client_code=None,
        )
        client_markers = {
            client.client_code: promotion.marker_snapshot(
                client_connections[client.client_code],
                expected_role="client_business",
                expected_database=client.database_name,
                expected_client_code=client.client_code,
            )
            for client in clients
        }
        control = _query_control_environments(platform_conn, list(expected))
        mixed = promotion.mixed_state_report(
            plan=plan,
            runtime_environment=str(runtime.values.get(promotion.TARGET_ENVIRONMENT_KEY)),
            platform_marker=platform_marker,
            client_markers=client_markers,
            control_plane_environments=control,
        )
        uuid_consistent = (
            platform_marker.get("database_uuid") == plan["platform_uuid"]
            and all(client_markers[code].get("database_uuid") == database_uuid for code, database_uuid in expected.items())
        )
        return {
            **mixed,
            "uuid_consistent": uuid_consistent,
            "platform_uuid": platform_marker.get("database_uuid"),
            "client_uuids": {code: marker.get("database_uuid") for code, marker in client_markers.items()},
        }
    finally:
        for client_conn in client_connections.values():
            client_conn.close()


def _inspection(args: argparse.Namespace) -> int:
    runtime = _runtime(args)
    with _platform_conn(runtime, read_only=True) as conn:
        schema = promotion.journal_schema_capability(conn)
        state = None if args.inspect_promotions == "all" else args.inspect_promotions
        rows = promotion.inspect_journals(conn, state=state, promotion_id=args.promotion_id)
        for row in rows:
            try:
                row["surface_state"] = _inspect_row_surfaces(conn, runtime, row)
            except Exception as exc:
                row["surface_state"] = {
                    "inspection_error": f"{type(exc).__name__}: {exc}",
                    "uuid_consistent": False,
                    "mixed": True,
                    "all_at_target": False,
                }
    _emit({
        "mode": "inspection", "count": len(rows), "promotions": rows,
        "resume_audit_schema": schema, "writes_performed": False,
    })
    return promotion.EXIT_OK


def _readiness(args: argparse.Namespace) -> int:
    repository_state = _repository_state()
    expected = _validate_scope(args)
    runtime = _runtime(args)
    with _platform_conn(runtime, read_only=True) as conn:
        plan, clients = _new_plan(args, runtime, conn, expected, repository_state)
        client_connections = _connect_clients(clients, runtime, read_only=True)
        try:
            platform_marker, client_markers = _check_preconditions(
                platform_conn=conn,
                client_connections=client_connections,
                clients=clients,
                platform_uuid=args.platform_uuid,
                platform_database=str(os.environ["POSTGRES_DB"]),
                source=args.from_environment,
                target=args.to_environment,
                allow_target=False,
            )
            report = promotion.readiness_report(conn, expected, {**os.environ, **runtime.values})
            report = _augment_readiness(report, clients=clients, client_connections=client_connections, runtime=runtime)
            control = _query_control_environments(conn, list(expected))
            mixed = promotion.mixed_state_report(
                plan=plan,
                runtime_environment=str(runtime.values.get(promotion.TARGET_ENVIRONMENT_KEY)),
                platform_marker=platform_marker,
                client_markers=client_markers,
                control_plane_environments=control,
            )
        finally:
            for client_conn in client_connections.values():
                client_conn.close()
    _emit({"mode": "production_readiness", "readiness": report, "surface_state": mixed, "writes_performed": False})
    return promotion.EXIT_OK if report["execute_allowed"] else promotion.EXIT_INCOMPATIBLE


def _build_recovery_v2_plan(
    args: argparse.Namespace, *, runtime: promotion.RuntimeFileState,
    conn, repository_state: RepositoryState, journal: dict[str, object],
) -> tuple[dict[str, object], list[promotion.ClientPlan]]:
    original_plan = dict(journal["immutable_plan_json"])
    promotion.require_historical_v4_plan(original_plan)
    if promotion.plan_hash(original_plan) != journal.get("plan_sha256"):
        raise promotion.PromotionError("RECOVERY_JOURNAL_DRIFT", "journal immutable plan hash does not match its payload")
    if journal.get("state") != "failed":
        raise promotion.PromotionError("RECOVERY_JOURNAL_DRIFT", "recovery-v2 requires the failed journal state")
    if str(args.backup_reference.absolute()) != journal.get("backup_reference"):
        raise promotion.PromotionError("RECOVERY_JOURNAL_DRIFT", "recovery checkpoint differs from the failed journal")
    if journal.get("runtime_file_backup_path") is not None or journal.get("runtime_file_before_sha256") is not None:
        raise promotion.PromotionError("RECOVERY_JOURNAL_DRIFT", "this failed-state contract requires an explicit null promotion backup")
    expected = {row["client_code"]: row["database_uuid"] for row in original_plan["clients"]}
    clients = promotion.load_selected_clients(
        conn, expected, source=str(original_plan["target_environment"]),
        target=str(original_plan["source_environment"]),
    )
    client_connections = _connect_clients(clients, runtime, read_only=True)
    try:
        platform_marker = promotion.marker_snapshot(
            conn, expected_role="platform", expected_database=str(original_plan["platform_database"]),
            expected_client_code=None,
        )
        markers = {
            client.client_code: promotion.marker_snapshot(
                client_connections[client.client_code], expected_role="client_business",
                expected_database=client.database_name, expected_client_code=client.client_code,
            ) for client in clients
        }
        controls = _query_control_environments(conn, [client.client_code for client in clients])
        capabilities = {
            client.client_code: promotion.client_promotion_capability(
                client_connections[client.client_code], expected_user=client.database_user,
            ) for client in clients
        }
    finally:
        for client_conn in client_connections.values(): client_conn.close()
    active_rows = int(conn.execute(
        f"SELECT count(*) AS n FROM {promotion.JOURNAL_TABLE} WHERE state IN ('planned','in_progress')"
    ).fetchone()["n"])
    canonical = v4.canonical_identity_binding(runtime, target=str(original_plan["source_environment"]))
    canonical.update({
        "already_equals_rollback_target": runtime.values[promotion.TARGET_ENVIRONMENT_KEY] == original_plan["source_environment"],
        "promotion_specific_backup_exists": False,
        "promotion_specific_recovery_evidence_exists": False,
        "helper_required": False,
    })
    database = {
        "platform": {
            "marker": platform_marker["environment"], "database_uuid": platform_marker["database_uuid"],
            "database_name": platform_marker["database_name"],
        },
        "clients": [
            {
                "client_code": client.client_code, "marker": markers[client.client_code]["environment"],
                "control_plane_environment": controls[client.client_code],
                "database_uuid": markers[client.client_code]["database_uuid"],
                "migration_045_capability": capabilities[client.client_code],
                "guarded_primitive": promotion.CLIENT_PROMOTION_SIGNATURE,
                "uuid_retained": True,
            } for client in clients
        ],
    }
    surface_binding = {
        "surfaces": {
            **{f"{promotion.STEP_CLIENT_PREFIX}{code}": marker["environment"] for code, marker in markers.items()},
            "platform_marker": platform_marker["environment"],
            "runtime_file": runtime.values[promotion.TARGET_ENVIRONMENT_KEY],
            "control_plane": controls,
        }
    }
    reconciliation = recovery_v2.reconcile_promotion_steps(
        journal=journal, plan=original_plan, surfaces=surface_binding,
    )
    convergence = inspect_runtime_convergence(
        REPO_ROOT, canonical_path=runtime.path, conflict_sources=(),
        probe_processes=True, enforce_canonical_metadata=False,
    )
    runtime_binding = v4.runtime_bindings(repository_root=REPO_ROOT, runtime_convergence=convergence)
    if (runtime_binding["systemd_api"]["semantic_identity"] != original_plan["source_environment"]
            or runtime_binding["docker_api"]["semantic_identity"] != original_plan["source_environment"]
            or runtime_binding["systemd_api"]["health"]["passed"] is not True
            or runtime_binding["docker_api"]["health"]["passed"] is not True):
        raise promotion.PromotionError("RECOVERY_RUNTIME_DRIFT", "running APIs are not healthy source-identity processes")
    checkpoint, provisioning_recovery = v4.checkpoint_and_recovery_bindings(
        repository_root=REPO_ROOT, checkpoint=args.backup_reference, recovery_root=args.recovery_root,
        backup_path=args.preserved_recovery_backup, evidence_path=args.recovery_evidence,
    )
    if checkpoint != original_plan.get("checkpoint_binding") or provisioning_recovery != original_plan.get("recovery_binding"):
        raise promotion.PromotionError("RECOVERY_PRIVILEGE_ASSET_DRIFT", "checkpoint or provisioning recovery binding changed")
    _verify_planned_helper(original_plan)
    privilege = {
        "effective_helper_policy": recovery_v2.effective_helper_policy_binding(),
        "helper": recovery_v2._metadata(Path(str(original_plan["privileged_helper"]["path"]))),
        "parser": recovery_v2._metadata(Path(str(original_plan["privileged_helper"]["dependency_path"]))),
    }
    plan = recovery_v2.build_plan(
        repository_root=REPO_ROOT, repository_state=repository_state, journal=journal,
        original_plan=original_plan, canonical_binding=canonical, database_binding=database,
        runtime_binding=runtime_binding, privilege_binding=privilege, checkpoint_binding=checkpoint,
        provisioning_recovery_binding=provisioning_recovery, recovery_root=args.recovery_root,
        reconciliation=reconciliation, active_promotion_rows=active_rows,
    )
    return plan, clients


def _verify_recovery_final_state(
    *, runtime: promotion.RuntimeFileState, plan: dict[str, object],
    clients: list[promotion.ClientPlan], cleanup_state: _RollbackState,
) -> dict[str, object]:
    original_source = str(plan["operation"]["original_source_environment"])
    expected = {row["client_code"]: row["database_uuid"] for row in plan["clients"]}
    platform_conn = _platform_conn(runtime, read_only=True)
    client_connections: dict[str, Any] = {}
    try:
        client_connections = _connect_clients(clients, runtime, read_only=True)
        platform_marker, client_markers = _check_preconditions(
            platform_conn=platform_conn, client_connections=client_connections, clients=clients,
            platform_uuid=str(plan["current_durable_database_state"]["platform"]["database_uuid"]),
            platform_database=str(plan["current_durable_database_state"]["platform"]["database_name"]),
            source=original_source, target=original_source, allow_target=False,
        )
        controls = _query_control_environments(platform_conn, list(expected))
        if any(value != original_source for value in controls.values()):
            raise promotion.PromotionError("RECOVERY_DATABASE_STATE_DRIFT", "fresh control-plane verification did not observe rollback target")
        runtime_now = promotion.inspect_runtime_file(runtime.path, allowed_paths=(runtime.path,))
        if runtime_now.checksum != plan["canonical_identity"]["current_sha256"] or runtime_now.values[promotion.TARGET_ENVIRONMENT_KEY] != original_source:
            raise promotion.PromotionError("RECOVERY_CANONICAL_DRIFT", "canonical identity changed during database-only rollback")
        readiness = promotion.readiness_report(platform_conn, expected, {**os.environ, **runtime_now.values})
        readiness = _augment_readiness(readiness, clients=clients, client_connections=client_connections, runtime=runtime_now)
        if readiness.get("classification") != "PRODUCTION_PROMOTION_READY" or not readiness.get("execute_allowed"):
            raise promotion.PromotionError("RECOVERY_READINESS_FAILED", "restored surfaces are not production-promotion ready")
        runtime_binding = v4.runtime_bindings(
            repository_root=REPO_ROOT, runtime_convergence=dict(readiness["runtime_identity"]),
        )
        planned_runtime = plan["runtime"]
        if (runtime_binding["systemd_api"]["pid"] != planned_runtime["systemd_api"]["pid"]
                or runtime_binding["systemd_api"]["invocation_id"] != planned_runtime["systemd_api"]["invocation_id"]
                or runtime_binding["docker_api"]["container_id"] != planned_runtime["docker_api"]["container_id"]
                or runtime_binding["docker_api"]["compose_configuration_fingerprint"] != planned_runtime["docker_api"]["compose_configuration_fingerprint"]):
            raise promotion.PromotionError("RECOVERY_RUNTIME_DRIFT", "runtime identity changed during rollback")
        state = {
            "canonical": original_source, "platform_marker": platform_marker["environment"],
            "client_markers": {code: row["environment"] for code, row in sorted(client_markers.items())},
            "control_plane": dict(sorted(controls.items())),
            "uuids": {"platform": platform_marker["database_uuid"], **{code: row["database_uuid"] for code, row in sorted(client_markers.items())}},
            "readiness": readiness["classification"],
        }
        return {"state": state, "fingerprint": promotion.plan_hash(state)}
    finally:
        for code, client_conn in client_connections.items():
            _rollback_close(
                client_conn,
                f"fresh_client_verification_connection_close:{code}",
                cleanup_state,
            )
        _rollback_close(
            platform_conn, "fresh_platform_verification_connection_close",
            cleanup_state,
        )


def _rollback(args: argparse.Namespace) -> int:
    state = _RollbackState(None)
    try:
        repository_state = _repository_state()
        _require_arguments(args, ("promotion_id", "runtime_environment_file", "backup_reference", "recovery_root", "preserved_recovery_backup", "recovery_evidence"))
        runtime = _runtime(args)
        state.promotion_id = str(args.promotion_id)
    except promotion.PromotionError as exc:
        raise _rollback_attach_cleanup(exc, state)
    except Exception as exc:
        raise _rollback_interruption_error(exc, state) from exc
    _rollback_schema_gate(runtime, state)
    read_conn = None
    planning_error = None
    try:
        read_conn = _platform_conn(runtime, read_only=True)
        state.phase = "recovery_plan_collection"
        rows = promotion.inspect_journals(read_conn, promotion_id=args.promotion_id)
        if len(rows) != 1:
            raise promotion.PromotionError("PROMOTION_NOT_FOUND", "promotion journal was not found")
        journal = rows[0]
        state.journal_state = str(journal.get("state") or "") or None
        plan, clients = _build_recovery_v2_plan(
            args, runtime=runtime, conn=read_conn,
            repository_state=repository_state, journal=journal,
        )
    except Exception as exc:
        planning_error = exc
    finally:
        _rollback_close(
            read_conn, "recovery_plan_connection_close", state,
        )
    if planning_error is not None:
        if isinstance(planning_error, promotion.PromotionError):
            raise _rollback_attach_cleanup(planning_error, state)
        raise _rollback_interruption_error(
            planning_error, state,
        ) from planning_error
    if state.cleanup_failures:
        raise _rollback_cleanup_only_error(state)
    digest = promotion.plan_hash(plan)
    required = recovery_v2.attestation(plan)
    command = _rollback_command(args, str(args.promotion_id), digest, required)
    report = {
        "mode": "recovery_v2_execute" if args.execute else "recovery_v2_plan",
        "writes_performed": False,
        "recovery_plan": plan,
        "recovery_plan_sha256": digest,
        "required_attestation": required,
        "future_command": command,
        "old_recovery_v1_sha256": recovery_v2.OLD_AUDIT_PLAN_SHA256,
        "old_recovery_v1_executable": False,
    }
    if not args.execute:
        _emit(report)
        return promotion.EXIT_OK
    recovery_v2.validate_execution_approval(
        plan, provided_plan_sha256=args.recovery_plan_sha256,
        provided_attestation=args.attestation,
    )
    session = _RollbackSession()
    try:
        state.phase = "rollback_execution_connection_open"
        session.open(runtime, state)
        if not promotion.try_promotion_lock(session.lock_conn):
            raise promotion.PromotionError("PROMOTION_BUSY", "another promotion or recovery holds the advisory lock", exit_code=promotion.EXIT_BUSY)
        session.lock_acquired = True
        state.phase = "prewrite_binding_reverification"
        prewrite_state = _repository_state()
        prewrite_conn = _platform_conn(runtime, read_only=True)
        try:
            prewrite_rows = promotion.inspect_journals(prewrite_conn, promotion_id=args.promotion_id)
            if len(prewrite_rows) != 1:
                raise promotion.PromotionError("RECOVERY_JOURNAL_DRIFT", "journal disappeared before rollback")
            prewrite_plan, prewrite_clients = _build_recovery_v2_plan(
                args, runtime=runtime, conn=prewrite_conn,
                repository_state=prewrite_state, journal=prewrite_rows[0],
            )
        finally:
            _rollback_close(
                prewrite_conn, "prewrite_verification_connection_close", state,
            )
        if promotion.canonical_json(prewrite_plan) != promotion.canonical_json(plan):
            raise promotion.PromotionError("RECOVERY_IMMUTABLE_PLAN_MISMATCH", "complete recovery-v2 plan changed immediately before first write")
        if [client.public_dict() for client in prewrite_clients] != [client.public_dict() for client in clients]:
            raise promotion.PromotionError("RECOVERY_DATABASE_STATE_DRIFT", "selected client bindings changed immediately before rollback")
        attestation_hash = promotion.sha256_bytes(args.attestation.encode("utf-8"))
        for client in clients:
            action = f"rollback_client_marker:{client.client_code}"
            state.begin_action(action, mutation=True)
            promotion.mutate_client_marker_durably(
                open_connection=_client_connection_factory(client),
                expected_database=client.database_name, expected_uuid=client.database_uuid,
                expected_user=client.database_user, expected_client_code=client.client_code,
                source=str(plan["operation"]["original_target_environment"]),
                target=str(plan["operation"]["original_source_environment"]),
                promotion_id=str(args.promotion_id), attestation_hash=attestation_hash,
            )
            state.complete_action(
                action,
                f"fresh_verify_client_marker:{client.client_code}",
            )
        state.begin_action("commit_platform_transaction", mutation=True)
        promotion.rollback_platform_surfaces(
            session.write_conn, clients=clients,
            expected_database=str(plan["current_durable_database_state"]["platform"]["database_name"]),
            expected_uuid=str(plan["current_durable_database_state"]["platform"]["database_uuid"]),
            source=str(plan["operation"]["original_target_environment"]),
            target=str(plan["operation"]["original_source_environment"]),
        )
        state.complete_action("commit_platform_transaction")
        state.begin_action("fresh_verify_all_surfaces_and_runtime")
        verified = _verify_recovery_final_state(
            runtime=runtime, plan=plan, clients=clients, cleanup_state=state,
        )
        state.complete_action("fresh_verify_all_surfaces_and_runtime")
        state.begin_action(
            "commit_journal_transition:rolled_back", mutation=True,
            journal_commit_ambiguous=True,
        )
        promotion.journal_mark_rolled_back(
            session.write_conn, str(args.promotion_id),
        )
        state.journal_state = "rolled_back"
        state.complete_action("commit_journal_transition:rolled_back")
        state.begin_action("fresh_verify_journal_transition")
        journal_verify_conn = _platform_conn(runtime, read_only=True)
        try:
            final_rows = promotion.inspect_journals(journal_verify_conn, promotion_id=args.promotion_id)
            if len(final_rows) != 1 or final_rows[0]["state"] != "rolled_back":
                raise promotion.PromotionError("JOURNAL_FINALIZATION_VERIFY_FAILED", "fresh connection did not observe rolled_back journal state")
        finally:
            _rollback_close(
                journal_verify_conn,
                "journal_transition_verification_connection_close", state,
            )
        state.complete_action("fresh_verify_journal_transition")
        state.begin_action(
            "atomically_create_checksum_bound_rollback_evidence",
            mutation=True, evidence_commit_ambiguous=True,
        )
        evidence = recovery_v2.write_evidence(
            repository_root=REPO_ROOT, plan=plan,
            payload={
                "pre_recovery_state_fingerprint": promotion.plan_hash(plan["current_durable_database_state"]),
                "post_recovery_state_fingerprint": verified["fingerprint"],
                "uuids": verified["state"]["uuids"],
                "completed_recovery_actions": list(state.completed_actions),
                "commit_and_verification_outcomes": "all_committed_and_verified",
                "journal_final_state": "rolled_back",
                "runtime_ids": {
                    "systemd_pid": plan["runtime"]["systemd_api"]["pid"],
                    "systemd_invocation_id": plan["runtime"]["systemd_api"]["invocation_id"],
                    "docker_container_id": plan["runtime"]["docker_api"]["container_id"],
                },
            },
        )
        state.recovery_evidence = {
            "path": evidence["path"], "sha256": evidence["sha256"],
        }
        state.complete_action(
            "atomically_create_checksum_bound_rollback_evidence",
        )
        state.begin_action(
            "commit_rollback_evidence_reference", mutation=True,
            journal_commit_ambiguous=True,
        )
        promotion.journal_record_rollback_evidence(
            session.write_conn, str(args.promotion_id),
            evidence_path=evidence["path"],
            evidence_sha256=evidence["sha256"],
        )
        state.complete_action("commit_rollback_evidence_reference")
        state.begin_action("fresh_verify_rollback_evidence_reference")
        final_verify_conn = _platform_conn(runtime, read_only=True)
        try:
            final_journal = promotion.inspect_journals(final_verify_conn, promotion_id=args.promotion_id)[0]
            expected_reference = f"ROLLBACK_EVIDENCE path={evidence['path']} sha256={evidence['sha256']}"
            if final_journal["state"] != "rolled_back" or final_journal["error"] != expected_reference:
                raise promotion.PromotionError("JOURNAL_EVIDENCE_VERIFY_FAILED", "fresh connection did not observe rollback evidence reference")
        finally:
            _rollback_close(
                final_verify_conn,
                "rollback_evidence_verification_connection_close", state,
            )
        state.complete_action("fresh_verify_rollback_evidence_reference")
        state.begin_action("final_fresh_verify_all_surfaces_and_runtime")
        _verify_recovery_final_state(
            runtime=runtime, plan=plan, clients=clients, cleanup_state=state,
        )
        state.complete_action("final_fresh_verify_all_surfaces_and_runtime")
        report.update({
            "writes_performed": True, "state": "rolled_back",
            "completed_actions": list(state.completed_actions),
            "rollback_evidence_path": evidence["path"],
            "rollback_evidence_sha256": evidence["sha256"],
            "service_restart_performed": False,
            "docker_recreation_performed": False,
        })
        state.phase = "resource_cleanup"
        session.release(state)
        if state.cleanup_failures:
            raise _rollback_cleanup_only_error(state)
        _emit(report)
        return promotion.EXIT_OK
    except promotion.PromotionError as exc:
        session.release(state)
        raise _rollback_attach_cleanup(exc, state)
    except Exception as exc:
        session.release(state)
        raise _rollback_interruption_error(exc, state) from exc
    finally:
        session.release(state)


def _require_resume_nonjournal_match(
    approved: dict[str, object], current: dict[str, object],
) -> None:
    comparable = (
        "approval_identity", "remote_repository_binding",
        "migration_054_schema_contract", "persistent_state", "runtime_convergence",
        "systemd_runtime", "docker_runtime", "security_and_recovery",
        "remaining_execution_contract",
    )
    for key in comparable:
        if key == "remaining_execution_contract":
            old = dict(approved[key])
            new = dict(current[key])
            if old != new:
                code = "RESUME_EXECUTION_CONTRACT_DRIFT"
            else:
                continue
        elif approved[key] != current[key]:
            code = resume_v2.drift_classification(approved, current)
        else:
            continue
        raise promotion.PromotionError(
            code, f"resume-v2 binding changed during journal-only execution: {key}",
            details={"writes_performed": False},
        )


_CREDENTIAL_URI_PATTERN = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/@]*@\S*")
_DATABASE_URI_PATTERN = re.compile(
    r"(?i)\b(?:postgres|postgresql|mysql|mongodb|redis|amqp)(?:\+[a-z0-9]+)?://\S+"
)
_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(?:password|passfile|pgpassword|sslpassword|dsn|conninfo|secret|token)\s*[=:]\s*\S+"
)

# Journal helpers raise these only after an explicit rollback, so a write that
# was in flight and classified this way provably did not commit.
_RESUME_V2_DEFINITIVE_ROLLBACK_CODES = frozenset({
    "JOURNAL_COMPLETE_COUNT",
    "JOURNAL_TRANSACTION_NOT_IDLE",
    "JOURNAL_UPDATE_COUNT",
    "RESUME_APPROVAL_ARGUMENT_MISSING",
    "RESUME_EXECUTION_CONTRACT_DRIFT",
    "RESUME_JOURNAL_STATE_DRIFT",
})


def _sanitized_exception_detail(exc: BaseException, *, limit: int = 400) -> str:
    text = " ".join(str(exc).split())
    text = _CREDENTIAL_URI_PATTERN.sub("<redacted-uri>", text)
    text = _DATABASE_URI_PATTERN.sub("<redacted-dsn>", text)
    text = _SECRET_ASSIGNMENT_PATTERN.sub("<redacted>", text)
    return text[:limit]


def _cleanup_failure_record(
    operation: str, exc: BaseException,
) -> dict[str, object]:
    """Return one executor-independent sanitized cleanup evidence record."""
    return {
        "operation": operation,
        "exception_class": type(exc).__name__,
        "detail": _sanitized_exception_detail(exc),
    }


def _forward_interruption_details(
    exc: BaseException, *, promotion_id: str | None, current_step: str | None,
    writes_started: bool,
) -> dict[str, object]:
    """Describe an unexpected forward-v5 interruption without hiding durable writes.

    An unexpected non-`PromotionError` after the first committed journal write
    must never be reported as a plain precondition failure, because persistent
    promotion surfaces may already have changed.
    """
    return {
        "writes_performed": writes_started,
        "reconciliation_required": writes_started,
        "promotion_id": promotion_id,
        "execution_phase": current_step or "before_first_journal_write",
        "original_exception_class": type(exc).__name__,
        "sanitized_message": _sanitized_exception_detail(exc),
    }


class _ResumeV2State:
    """Local durable-write truth for resume-v2 reporting.

    This state, not an inner exception detail, decides what the operator is
    told.  A write that was still in flight when the failure happened counts as
    possibly committed unless the journal helper reported an explicit rollback.
    """

    def __init__(self) -> None:
        self.phase = "route_classification"
        self.committed_writes: list[str] = []
        self.pending_write: str | None = None
        self.finalization_attempted = False
        self.cleanup_failures: list[dict[str, object]] = []

    def begin_write(self, step: str) -> None:
        self.phase = f"journal_write:{step}"
        self.pending_write = step

    def complete_write(self, step: str) -> None:
        self.pending_write = None
        self.committed_writes.append(step)

    def writes_performed(self, *, definitively_rolled_back: bool) -> bool:
        if self.committed_writes:
            return True
        return self.pending_write is not None and not definitively_rolled_back

    def record_cleanup_failure(self, operation: str, exc: BaseException) -> None:
        self.cleanup_failures.append({
            "operation": operation,
            "exception_class": type(exc).__name__,
            "detail": _sanitized_exception_detail(exc),
        })


def _resume_v2_close(handle, operation: str, state: _ResumeV2State) -> None:
    if handle is None:
        return
    try:
        handle.close()
    except Exception as exc:
        state.record_cleanup_failure(operation, exc)


class _ResumeV2Session:
    """Owns the advisory-lock and journal-write connections for one execution."""

    def __init__(self, lock_conn) -> None:
        self.lock_conn = lock_conn
        self.lock_acquired = False
        self.write_conn = None

    def close_write_connection(self, state: _ResumeV2State) -> None:
        _resume_v2_close(self.write_conn, "journal_write_connection_close", state)
        self.write_conn = None

    def release(self, state: _ResumeV2State) -> None:
        """Release every resource without letting cleanup mask the real outcome."""
        self.close_write_connection(state)
        if self.lock_acquired:
            try:
                promotion.release_promotion_lock(self.lock_conn)
            except Exception as exc:
                state.record_cleanup_failure("advisory_lock_release", exc)
            self.lock_acquired = False
        _resume_v2_close(self.lock_conn, "advisory_lock_connection_close", state)


def _resume_v2_details(
    state: _ResumeV2State, details: dict[str, object], writes_performed: bool,
) -> dict[str, object]:
    details["writes_performed"] = writes_performed
    details["execution_phase"] = state.phase
    details["committed_journal_writes"] = list(state.committed_writes)
    details["finalization_may_have_committed"] = state.finalization_attempted
    details["reconciliation_required"] = writes_performed
    if state.cleanup_failures:
        details["cleanup_failures"] = list(state.cleanup_failures)
    return details


def _execute_resume_v2(args: argparse.Namespace) -> int:
    state = _ResumeV2State()
    session: _ResumeV2Session | None = None
    try:
        repository_state = _repository_state()
        expected = _validate_scope(args)
        _require_arguments(args, ("promotion_id",))
        runtime = _runtime(args)
        with _platform_conn(runtime, read_only=True) as route_conn:
            state.phase = "resume_audit_schema_gate"
            promotion.require_resume_audit_schema(route_conn)
            state.phase = "route_classification"
            original_plan, _route_clients, route_journal = _resume_plan(
                args, route_conn, expected, repository_state,
            )
            resume_v2.require_resume_v2_finalization_route(
                journal=route_journal,
                original_steps=[str(step) for step in original_plan["steps"]],
            )

        state.phase = "approval_argument_validation"
        resume_v2.reject_retired_execution(
            attestation_value=args.attestation,
            resume_plan_sha256=args.resume_plan_sha256,
        )
        if not args.attestation:
            raise promotion.PromotionError(
                "RESUME_APPROVAL_ARGUMENT_MISSING",
                "--execute requires the exact resume-v2 attestation",
                exit_code=promotion.EXIT_INVALID,
            )

        state.phase = "approved_plan_collection"
        with _platform_conn(runtime, read_only=True) as initial_conn:
            approved_plan, _clients, _journal = _collect_resume_v2_plan(
                args, initial_conn, expected, repository_state, runtime,
            )
        approved_digest = promotion.plan_hash(approved_plan)
        if args.resume_plan_sha256 != approved_digest:
            raise promotion.PromotionError(
                "RESUME_IMMUTABLE_BINDING_DRIFT",
                "requested resume plan SHA-256 does not match fresh canonical bytes",
            )
        required_attestation = resume_v2.attestation(approved_plan)
        if args.attestation != required_attestation:
            raise promotion.PromotionError(
                "RESUME_IMMUTABLE_BINDING_DRIFT",
                "provided attestation is not the exact resume-v2 attestation",
                exit_code=promotion.EXIT_INVALID,
            )

        state.phase = "advisory_lock_acquisition"
        session = _ResumeV2Session(_platform_conn(runtime, read_only=True, autocommit=True))
        if not promotion.try_promotion_lock(session.lock_conn):
            raise promotion.PromotionError(
                "PROMOTION_BUSY", "another environment promotion holds the advisory lock",
                exit_code=promotion.EXIT_BUSY,
            )
        session.lock_acquired = True

        state.phase = "prewrite_binding_reverification"
        with _platform_conn(runtime, read_only=True) as prewrite_conn:
            prewrite_plan, _clients, _journal = _collect_resume_v2_plan(
                args, prewrite_conn, expected, repository_state, runtime,
            )
        if promotion.canonical_json(prewrite_plan) != promotion.canonical_json(approved_plan):
            raise promotion.PromotionError(
                resume_v2.drift_classification(approved_plan, prewrite_plan),
                "resume-v2 canonical bytes changed immediately before journal writes",
            )
        if resume_v2.attestation(prewrite_plan) != args.attestation:
            raise promotion.PromotionError(
                "RESUME_IMMUTABLE_BINDING_DRIFT",
                "resume-v2 attestation changed immediately before journal writes",
            )

        state.phase = "journal_write_connection_open"
        session.write_conn = _platform_conn(runtime, read_only=False)
        remaining = list(dict(approved_plan["journal_state"])["expected_remaining_steps"])
        promotion_id = str(args.promotion_id)

        if remaining and remaining[0] == promotion.STEP_RUNTIME_RELOAD:
            state.begin_write(promotion.STEP_RUNTIME_RELOAD)
            promotion.journal_resume_progress_v2(
                session.write_conn, promotion_id,
                completed_step=promotion.STEP_RUNTIME_RELOAD,
                current_step=promotion.STEP_RUNTIME_PROCESSES,
            )
            state.complete_write(promotion.STEP_RUNTIME_RELOAD)
            remaining.pop(0)

        if remaining and remaining[0] == promotion.STEP_RUNTIME_PROCESSES:
            state.phase = "runtime_process_reverification"
            with _platform_conn(runtime, read_only=True) as verify_process_conn:
                process_plan, _clients, _journal = _collect_resume_v2_plan(
                    args, verify_process_conn, expected, repository_state, runtime,
                )
            _require_resume_nonjournal_match(approved_plan, process_plan)
            state.begin_write(promotion.STEP_RUNTIME_PROCESSES)
            promotion.journal_resume_progress_v2(
                session.write_conn, promotion_id,
                completed_step=promotion.STEP_RUNTIME_PROCESSES,
                current_step=promotion.STEP_FINAL_VERIFY,
            )
            state.complete_write(promotion.STEP_RUNTIME_PROCESSES)
            remaining.pop(0)

        expected_all_steps = [
            str(record["step"])
            for record in dict(approved_plan["journal_state"])["reconciliation_records"]
        ]
        if remaining and remaining[0] == promotion.STEP_FINAL_VERIFY:
            state.phase = "final_cross_surface_verification"
            with _platform_conn(runtime, read_only=True) as final_verify_conn:
                final_plan, _clients, _journal = _collect_resume_v2_plan(
                    args, final_verify_conn, expected, repository_state, runtime,
                )
            _require_resume_nonjournal_match(approved_plan, final_plan)
            state.begin_write("atomic_finalization")
            state.finalization_attempted = True
            promotion.journal_finalize_resume_v2(
                session.write_conn,
                promotion_id,
                resume_plan_sha256=approved_digest,
                expected_completed_steps=expected_all_steps[:-1],
            )
            state.complete_write("atomic_finalization")
            remaining.pop(0)

        if remaining:
            raise promotion.PromotionError(
                "RESUME_EXECUTION_CONTRACT_DRIFT",
                "journal remaining suffix is outside the finalization allowlist",
            )
        state.phase = "journal_write_connection_close"
        session.close_write_connection(state)

        state.phase = "fresh_journal_verification"
        fresh_conn = _platform_conn(runtime, read_only=True)
        try:
            rows = promotion.inspect_journals(fresh_conn, promotion_id=promotion_id)
        finally:
            _resume_v2_close(fresh_conn, "fresh_verification_connection_close", state)
        if (len(rows) != 1
                or rows[0]["state"] != "completed"
                or rows[0]["current_step"] is not None
                or rows[0]["completed_at"] is None
                or rows[0]["resume_contract"] != "resume-v2"
                or rows[0]["resume_plan_sha256"] != approved_digest
                or rows[0]["completed_steps"] != expected_all_steps):
            raise promotion.PromotionError(
                "JOURNAL_POST_COMMIT_VERIFY",
                "fresh connection did not observe the exact completed journal row",
            )
        result = {
            "mode": "resume_v2_execute", "writes_performed": True,
            "promotion_id": promotion_id, "resume_plan_sha256": approved_digest,
            "journal_state": "completed", "persistent_surface_writes": False,
            "runtime_actions_performed": False,
        }
        state.phase = "resource_release"
        session.release(state)
        session = None
        if state.cleanup_failures:
            raise promotion.PromotionError(
                "RESUME_V2_CLEANUP_FAILED",
                "the journal reached completed but releasing execution resources failed",
                details={"journal_state": "completed"},
            )
    except promotion.PromotionError as exc:
        if session is not None:
            session.release(state)
        writes = state.writes_performed(
            definitively_rolled_back=exc.code in _RESUME_V2_DEFINITIVE_ROLLBACK_CODES,
        )
        exc.details = _resume_v2_details(state, dict(exc.details or {}), writes)
        if writes:
            exc.exit_code = promotion.EXIT_PARTIAL
        raise
    except Exception as exc:
        if session is not None:
            session.release(state)
        writes = state.writes_performed(definitively_rolled_back=False)
        raise promotion.PromotionError(
            "RESUME_V2_EXECUTION_INTERRUPTED",
            f"{type(exc).__name__}: {_sanitized_exception_detail(exc)}",
            exit_code=promotion.EXIT_PARTIAL if writes else promotion.EXIT_PRECONDITION,
            details=_resume_v2_details(state, {
                "original_exception_class": type(exc).__name__,
                "sanitized_message": _sanitized_exception_detail(exc),
            }, writes),
        ) from exc
    _emit(result)
    return promotion.EXIT_OK


def cli(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.rollback and args.rollback_plan:
        raise promotion.PromotionError(
            "MODE_CONFLICT",
            "--rollback and --rollback-plan are mutually exclusive",
            exit_code=promotion.EXIT_INVALID,
        )
    if args.execute and (args.inspect_promotions or args.rollback_plan or args.resume_plan or args.resume_plan_v1_diagnostic or args.check_production_readiness):
        raise promotion.PromotionError(
            "READ_ONLY_MODE_WITH_EXECUTE",
            "inspection, readiness, and rollback-plan modes never accept --execute",
            exit_code=promotion.EXIT_INVALID,
        )
    if args.inspect_promotions:
        return _inspection(args)
    if args.resume_plan_v1_diagnostic:
        _emit({"mode": "resume_plan_v1_diagnostic", "writes_performed": False, **resume_v2.diagnostic_v1()})
        return promotion.EXIT_OK
    if args.resume_plan:
        return _resume_plan_dry_run(args)
    if args.rollback or args.rollback_plan:
        return _rollback(args)
    if args.check_production_readiness:
        return _readiness(args)
    if args.execute:
        if args.promotion_id:
            return _execute_resume_v2(args)
        return _execute(args)
    return _dry_run(args)


def main() -> int:
    try:
        return cli()
    except promotion.PromotionError as exc:
        if exc.details is not None:
            print(json.dumps({
                "classification": exc.code,
                "writes_performed": False,
                **exc.details,
            }, sort_keys=True, indent=2))
            return exc.exit_code
        print(f"ERROR: {exc}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("ERROR: interrupted; inspect the promotion journal before resuming", file=sys.stderr)
        return promotion.EXIT_PARTIAL
    except Exception as exc:
        print(f"ERROR: OPERATION_FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return promotion.EXIT_PRECONDITION


if __name__ == "__main__":
    raise SystemExit(main())
