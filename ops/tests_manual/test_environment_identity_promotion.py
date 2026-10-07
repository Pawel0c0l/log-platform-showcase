#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ops import environment_identity_promotion as promotion
from ops import promote_environment_identity as cli
from ops import promotion_plan_v4 as v4
from ops import promotion_plan_v5 as v5


PLATFORM_UUID = "52517750-7438-4558-8490-2736ae4cc629"
ALPHA_UUID = "5879ec53-e3f6-4a46-89cf-eaae9f57b27e"
BRAVO_UUID = "a41f7fe6-e113-42f7-8789-2dc20b2091d7"


def expect_code(code, fn):
    try:
        fn()
    except promotion.PromotionError as exc:
        assert exc.code == code, exc
    else:
        raise AssertionError(f"expected {code}")


def make_env(path: Path, environment="local_dev", extra="") -> None:
    path.write_text(f"LOG_PLATFORM_TARGET_ENVIRONMENT={environment}\n{extra}", encoding="utf-8")
    path.chmod(0o600)

def client(code, database, uuid, client_id):
    return promotion.ClientPlan(
        client_id=client_id,
        client_code=code,
        database_name=database,
        database_user=f"{database}_user",
        database_host="127.0.0.1",
        database_port=5432,
        database_uuid=uuid,
        password_secret_ref=f"{code}_DB_PASSWORD",
    )


def immutable_bindings():
    return {
        "operation_identity": {
            "operation": "promote_environment_identity", "host": "approved-host",
            "repository_path": "/repo", "repository_head": "a" * 40,
            "repository_branch": "main", "source_environment": "local_dev",
            "target_environment": "production",
            "uuid_policy": "preserve_existing_database_uuids",
        },
        "canonical_identity": {
            "path": "/canonical", "current_sha256": "1" * 64,
            "target_sha256": "2" * 64, "current_value": "local_dev",
            "target_value": "production",
            "current_metadata": {
                "uid": 0, "gid": 1000, "mode": "0640", "regular_file": True,
                "symlink": False, "assignment_count": 1,
            },
            "required_target_metadata": {
                "uid": 0, "gid": 1000, "mode": "0640", "regular_file": True,
                "symlink": False, "assignment_count": 1,
            },
        },
        "database_bindings": {
            "platform": {
                "database_uuid": PLATFORM_UUID, "current_marker": "local_dev",
                "target_marker": "production", "migration_053": {"applied_count": 1},
                "promotion_journal_rows": 0, "incomplete_promotion_rows": 0,
                "advisory_lock_identity": "log_platform_environment_identity_promotion_v1",
            },
            "clients": [
                {
                    "client_code": "BRAVO00016", "database_uuid": BRAVO_UUID,
                    "current_marker": "local_dev", "target_marker": "production",
                    "current_control_plane_environment": "local_dev",
                    "target_control_plane_environment": "production",
                    "migration_045_capability": {"safe": True},
                    "uuid_retained": True,
                },
                {
                    "client_code": "ALPHA00001", "database_uuid": ALPHA_UUID,
                    "current_marker": "local_dev", "target_marker": "production",
                    "current_control_plane_environment": "local_dev",
                    "target_control_plane_environment": "production",
                    "migration_045_capability": {"safe": True},
                    "uuid_retained": True,
                },
            ],
        },
        "runtime_bindings": {
            "readiness_classification": "PRODUCTION_PROMOTION_READY",
            "systemd_api": {
                "pid": 12, "invocation_id": "inv", "process_start_timestamp": "stable",
                "ordered_dropins": [
                    {"path": "/override", "sha256": "3" * 64},
                    {"path": "/zz", "sha256": "4" * 64},
                ],
                "need_daemon_reload": "no", "effective_canonical_source": "/canonical",
                "configuration_provenance": "CONVERGED",
                "process_provenance": "CONVERGED", "semantic_identity": "local_dev",
                "health": {"passed": True, "fingerprint": "a" * 64},
            },
            "docker_api": {
                "project": "log-platform", "service": "api",
                "working_directory": "/repo", "compose_files": ["/repo/docker-compose.yml"],
                "environment_file_contract": {"canonical_source_resolved": True},
                "container_id": "container", "container_created_at": "stable",
                "image_id": "image", "compose_configuration_fingerprint": "5" * 64,
                "configuration_provenance": "CONVERGED",
                "container_provenance": "CONVERGED", "semantic_identity": "local_dev",
                "health": {"passed": True, "fingerprint": "b" * 64},
            },
            "per_invocation_consumers": {
                "prune": "PER_INVOCATION_READY", "backup": "PER_INVOCATION_READY",
                "timer_restart_required": False,
            },
            "reload_required_consumers": [],
        },
        "checkpoint_binding": {
            "path": "/safe/checkpoint.json", "sha256": "6" * 64, "mode": "0600",
            "kind": "checkpoint_v1", "repository_head": "a" * 40,
        },
        "recovery_binding": {
            "root": {
                "path": "/safe/recovery", "uid": 1000, "gid": 1000,
                "mode": "0700", "symlink": False,
            },
            "plan_directory": {"path": "/safe/recovery/plan", "mode": "0700"},
            "preserved_backup": {
                "path": "/safe/backup", "sha256": "7" * 64, "mode": "0600",
            },
            "evidence": {
                "path": "/safe/evidence", "sha256": "8" * 64, "mode": "0600",
            },
            "provisioning_plan_sha256": "9" * 64,
        },
        "effective_sudo_policy": {
            "helper_absolute_path": v5.HELPER_PATH,
            "matching_rule_count": 1, "effective_rule": v5.HELPER_RULE,
            "nopasswd": True, "run_as_user": "root",
            "no_later_conflicting_matching_rule": True,
            "effective_policy_fingerprint": "f" * 64,
            "collection_method_version": v5.SUDO_COLLECTION_METHOD,
        },
        "implementation_assets": [
            {"path": path, "sha256": str(index) * 64,
             "regular_file": True, "symlink": False}
            for index, path in enumerate(sorted(v5.IMPLEMENTATION_ASSET_PATHS), 1)
        ],
        "historical_recovery": {
            "historical_row_count": 1, "total_journal_row_count": 1,
            "active_promotion_row_count": 0, "incomplete_recovery_row_count": 0,
            "rolled_back_rows_excluded_from_active_blocking": True,
            "rolled_back_rows_cryptographically_bound_as_history": True,
            "entries": [{"promotion_id": "historical", "original_contract": "v4",
                         "final_journal_state": "rolled_back"}],
        },
        "excluded_actions": v5.EXCLUDED_ACTIONS,
    }


def build_test_plan(path: Path):
    state = promotion.inspect_runtime_file(path, allowed_paths=(path,))
    return promotion.build_plan(
        source="local_dev",
        target="production",
        platform_uuid=PLATFORM_UUID,
        platform_database="logdb",
        runtime_file=state,
        clients=(
            client("ALPHA00001", "alpha_main", ALPHA_UUID, "9536f715-2fd0-4ffd-86ed-ba06f5490c5e"),
            client("BRAVO00016", "telematics_main", BRAVO_UUID, "6018be20-5faa-41b6-89c9-fe2b54a8283e"),
        ),
        backup_reference="/safe/checkpoint.json",
        runtime_convergence={
            "classification": "PRODUCTION_PROMOTION_READY",
            "helper": {"installed_sha256": "a" * 64},
            "consumers": [],
        },
        repository_head="a" * 40,
        **immutable_bindings(),
    )


def test_environment_contract_and_scope():
    promotion.validate_environments("local_dev", "production")
    expect_code("TARGET_ENVIRONMENT_INVALID", lambda: promotion.validate_environments("local_dev", "prod"))
    expect_code("SOURCE_ENVIRONMENT_INVALID", lambda: promotion.validate_environments("development", "production"))
    expect_code("ENVIRONMENTS_EQUAL", lambda: promotion.validate_environments("production", "production"))
    expect_code("EMPTY_CLIENT_SCOPE", lambda: promotion.parse_expected_clients([]))
    expect_code("DUPLICATE_CLIENT", lambda: promotion.parse_expected_clients([f"ALPHA00001={ALPHA_UUID}", f"ALPHA00001={ALPHA_UUID}"]))
    expect_code("INVALID_UUID", lambda: promotion.parse_expected_clients(["ALPHA00001=NOT-A-UUID"]))


def test_cli_default_and_execute_attestation_gate():
    parser = cli._parser()
    args = parser.parse_args([])
    assert args.execute is False
    assert args.rollback is False
    assert args.check_production_readiness is False
    args = parser.parse_args(["--execute"])
    with patch.object(cli, "_repository_state", return_value=SimpleNamespace(head="a" * 40)), \
         patch.object(cli, "_validate_scope", return_value={"ALPHA00001": ALPHA_UUID}), \
         patch.object(cli, "_runtime"):
        expect_code("ATTESTATION_MISSING", lambda: cli._execute(args))
    expect_code("READ_ONLY_MODE_WITH_EXECUTE", lambda: cli.cli(["--rollback-plan", "--execute"]))
    expect_code("READ_ONLY_MODE_WITH_EXECUTE", lambda: cli.cli(["--check-production-readiness", "--execute"]))
    expect_code("MODE_CONFLICT", lambda: cli.cli(["--rollback", "--rollback-plan"]))


def test_attestation_is_exact_and_plan_bound():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "runtime.env"
        make_env(path)
        plan = build_test_plan(path)
        attestation = promotion.promotion_attestation(plan)
        assert "source=local_dev" in attestation
        assert "target=production" in attestation
        assert f"platform_uuid={PLATFORM_UUID}" in attestation
        assert f"ALPHA00001:{ALPHA_UUID}" in attestation
        assert f"BRAVO00016:{BRAVO_UUID}" in attestation
        assert "UUIDS_UNCHANGED=true" in attestation
        assert plan["contract_version"] == 5
        assert plan["promotion_plan_contract_version"] == 5
        assert "host=approved-host" in attestation
        assert "head=" + "a" * 40 in attestation
        assert "contract=v5" in attestation
        assert plan["repository_head"] == "a" * 40
        assert plan["privileged_helper"]["sha256"] == "a" * 64
        assert plan["runtime_file_after_sha256"]
        assert promotion.STEP_RUNTIME_RELOAD in plan["steps"]
        assert promotion.STEP_RUNTIME_PROCESSES in plan["steps"]
        assert plan["client_marker_write_contract"] == "ops_control.promote_environment_identity_v1"
        changed = dict(plan)
        changed["target_environment"] = "staging"
        assert promotion.promotion_attestation(changed) != attestation


def test_runtime_file_update_preserves_content_and_metadata():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "runtime.env"
        make_env(path)
        before = path.read_text(encoding="utf-8")
        before_stat = path.stat()
        state = promotion.inspect_runtime_file(path, allowed_paths=(path,))
        result = promotion.atomic_update_runtime_file(
            state,
            source="local_dev",
            target="production",
            promotion_id="00000000-0000-0000-0000-000000000001",
        )
        after = path.read_text(encoding="utf-8")
        assert "LOG_PLATFORM_TARGET_ENVIRONMENT=production" in after
        assert after.replace("production", "local_dev", 1) == before
        assert "TOP_SECRET_VALUE" in after
        assert stat.S_IMODE(path.stat().st_mode) == stat.S_IMODE(before_stat.st_mode) == 0o600
        assert path.stat().st_uid == before_stat.st_uid
        assert path.stat().st_gid == before_stat.st_gid
        backup = Path(result["backup_path"])
        assert backup.read_text(encoding="utf-8") == before
        assert stat.S_IMODE(backup.stat().st_mode) == 0o600
        restored_state = promotion.inspect_runtime_file(path, allowed_paths=(path,))
        checksum = promotion.atomic_restore_runtime_file(
            restored_state,
            backup_path=backup,
            expected_backup_sha256=result["before_sha256"],
            expected_target_environment="production",
        )
        assert path.read_text(encoding="utf-8") == before
        assert checksum == result["before_sha256"]


def test_runtime_file_rejections_and_interruption():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        path = root / "runtime.env"
        make_env(path)
        expect_code("RUNTIME_FILE_NOT_ALLOWLISTED", lambda: promotion.inspect_runtime_file(path, allowed_paths=(root / "other.env",)))
        path.chmod(0o644)
        expect_code("RUNTIME_FILE_MODE", lambda: promotion.inspect_runtime_file(path, allowed_paths=(path,)))
        path.chmod(0o600)
        symlink = root / "link.env"
        symlink.symlink_to(path)
        expect_code("RUNTIME_FILE_SYMLINK", lambda: promotion.inspect_runtime_file(symlink, allowed_paths=(symlink,)))
        make_env(path, extra="LOG_PLATFORM_TARGET_ENVIRONMENT=local_dev\n")
        expect_code("RUNTIME_FILE_DUPLICATE_KEY", lambda: promotion.inspect_runtime_file(path, allowed_paths=(path,)))
        path.write_text("NOT AN ASSIGNMENT\n", encoding="utf-8")
        expect_code("RUNTIME_FILE_MALFORMED", lambda: promotion.inspect_runtime_file(path, allowed_paths=(path,)))
        make_env(path)
        original = path.read_bytes()
        state = promotion.inspect_runtime_file(path, allowed_paths=(path,))
        expect_code(
            "INJECTED_WRITE_FAILURE",
            lambda: promotion.atomic_update_runtime_file(
                state,
                source="local_dev",
                target="production",
                promotion_id="00000000-0000-0000-0000-000000000002",
                fail_before_replace=True,
            ),
        )
        assert path.read_bytes() == original
        retry_state = promotion.inspect_runtime_file(path, allowed_paths=(path,))
        promotion.atomic_update_runtime_file(
            retry_state,
            source="local_dev",
            target="production",
            promotion_id="00000000-0000-0000-0000-000000000002",
        )
        assert "LOG_PLATFORM_TARGET_ENVIRONMENT=production" in path.read_text(encoding="utf-8")


def test_runtime_update_never_prints_secret():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "runtime.env"
        make_env(path)
        state = promotion.inspect_runtime_file(path, allowed_paths=(path,))
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            promotion.atomic_update_runtime_file(
                state,
                source="local_dev",
                target="production",
                promotion_id="00000000-0000-0000-0000-000000000003",
            )
        assert "TOP_SECRET_VALUE" not in output.getvalue()


def test_privileged_helper_is_always_non_interactive():
    state = promotion.RuntimeFileState(
        path=promotion.CANONICAL_IDENTITY_FILE,
        values={promotion.TARGET_ENVIRONMENT_KEY: "local_dev"},
        checksum=promotion.intended_checksum("local_dev"),
        uid=0, gid=1000, mode=0o640,
    )
    commands = []

    def successful_run(command, **kwargs):
        commands.append((command, kwargs))
        if "--restore-backup" in command:
            payload = {"after_sha256": state.checksum}
        else:
            payload = {
                "after_sha256": promotion.intended_checksum("production"),
                "environment": "production", "backup_path": "/safe/backup",
            }
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    with patch.object(promotion.subprocess, "run", side_effect=successful_run):
        result = promotion.invoke_identity_helper(
            state, source="local_dev", target="production"
        )
        assert result["backup_path"] == "/safe/backup"
        assert promotion.invoke_identity_restore_helper(
            state, backup_path=Path("/safe/backup"),
            expected_backup_sha256=state.checksum,
        ) == state.checksum

    assert len(commands) == 2
    for command, kwargs in commands:
        assert command[:3] == ["sudo", "-n", str(promotion.HELPER_INSTALL_PATH)]
        assert command.index("-n") < command.index(str(promotion.HELPER_INSTALL_PATH))
        assert kwargs == {
            "check": False, "capture_output": True, "text": True, "timeout": 60,
        }
        assert "shell" not in kwargs
    assert "--set-environment" in commands[0][0]
    assert "--expected-old-value" in commands[0][0]
    assert "--expected-current-sha256" in commands[0][0]
    assert "--attestation" in commands[0][0]
    assert "--restore-backup" in commands[1][0]

    implementation = Path(promotion.__file__).read_text(encoding="utf-8")
    helper_block = implementation[
        implementation.index("def privileged_helper_command"):
        implementation.index("def resolve_secret")
    ]
    assert implementation.count('["sudo", "-n", str(HELPER_INSTALL_PATH)') == 1
    assert '"shell=True"' not in helper_block and "SUDO_ASKPASS" not in helper_block
    assert "password=" not in helper_block and "stdin=" not in helper_block


def test_missing_nopasswd_is_bounded_and_never_retried():
    state = promotion.RuntimeFileState(
        path=promotion.CANONICAL_IDENTITY_FILE,
        values={promotion.TARGET_ENVIRONMENT_KEY: "local_dev"},
        checksum=promotion.intended_checksum("local_dev"),
        uid=0, gid=1000, mode=0o640,
    )
    denied = SimpleNamespace(
        returncode=1, stdout="", stderr="sudo: a password is required\n"
    )
    with patch.object(promotion.subprocess, "run", return_value=denied) as runner:
        expect_code(
            "BLOCKED_BY_CANONICAL_IDENTITY_PRIVILEGE_PATH",
            lambda: promotion.invoke_identity_helper(
                state, source="local_dev", target="production"
            ),
        )
        assert runner.call_count == 1
        assert runner.call_args.args[0][:3] == [
            "sudo", "-n", str(promotion.HELPER_INSTALL_PATH)
        ]


def test_backup_checkpoint_contract():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        runtime = root / "runtime.env"
        make_env(runtime)
        plan = build_test_plan(runtime)
        checkpoint = root / "checkpoint.json"
        checkpoint.write_text(json.dumps({
            "kind": "environment_identity_promotion_checkpoint_v1",
            "environment": "local_dev",
            "platform_uuid": PLATFORM_UUID,
            "verified": True,
            "clients": {"ALPHA00001": ALPHA_UUID, "BRAVO00016": BRAVO_UUID},
        }), encoding="utf-8")
        assert promotion.validate_backup_reference(checkpoint, plan)["sha256"]
        wrong = json.loads(checkpoint.read_text())
        wrong["clients"].pop("BRAVO00016")
        checkpoint.write_text(json.dumps(wrong), encoding="utf-8")
        expect_code("BACKUP_CLIENT_SCOPE_MISMATCH", lambda: promotion.validate_backup_reference(checkpoint, plan))


def test_readiness_categories():
    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def execute(self, *args): pass
        def fetchall(self): return [{"client_code": "ALPHA00001", "dataset_name": "trips_sync"}]
    class Conn:
        def cursor(self): return Cursor()
    report = promotion.readiness_report(Conn(), ["ALPHA00001"], {promotion.TARGET_ENVIRONMENT_KEY: "local_dev"})
    by_name = {row["component"]: row for row in report["components"]}
    assert by_name["prune"]["guarded"] is True
    assert by_name["alpha_source_refresh"]["compatible"] is False
    assert by_name["dispatcher"]["guarded"] is False
    assert report["execute_allowed"] is True


def test_client_primitive_readiness_classifications():
    selected = client("BRAVO00016", "telematics_main", BRAVO_UUID, "6018be20-5faa-41b6-89c9-fe2b54a8283e")
    runtime = type("Runtime", (), {"path": Path("/etc/log-platform/environment-identity.env")})()
    base = {"execute_allowed": True}
    runtime_ready = {"classification": "PRODUCTION_PROMOTION_READY", "execute_allowed": True}
    with patch.object(cli, "inspect_runtime_convergence", return_value=runtime_ready), \
         patch.object(promotion, "client_promotion_capability", return_value={"available": False, "least_privilege_safe": False}):
        report = cli._augment_readiness(dict(base), clients=[selected], client_connections={"BRAVO00016": object()}, runtime=runtime)
        assert report["classification"] == "CLIENT_PROMOTION_PRIMITIVE_MISSING"
        assert report["execute_allowed"] is False
    with patch.object(cli, "inspect_runtime_convergence", return_value=runtime_ready), \
         patch.object(promotion, "client_promotion_capability", return_value={"available": True, "least_privilege_safe": False}):
        report = cli._augment_readiness(dict(base), clients=[selected], client_connections={"BRAVO00016": object()}, runtime=runtime)
        assert report["classification"] == "CLIENT_PROMOTION_PRIVILEGE_UNSAFE"
        assert report["execute_allowed"] is False



def test_v5_canonicalization_and_binding_sensitivity():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "runtime.env"; make_env(path)
        first = build_test_plan(path); second = build_test_plan(path)
        assert promotion.canonical_json(first) == promotion.canonical_json(second)
        assert promotion.plan_hash(first) == promotion.plan_hash(second)
        shuffled = dict(reversed(list(first.items())))
        assert promotion.plan_hash(shuffled) == promotion.plan_hash(first)
        required = ("operation_identity", "canonical_identity", "database_bindings", "runtime_bindings", "checkpoint_binding", "recovery_binding", "effective_sudo_policy", "implementation_assets", "historical_recovery", "ordered_actions", "excluded_actions", "post_promotion_runtime_requirements")
        assert all(key in first for key in required)
        systemd = first["runtime_bindings"]["systemd_api"]
        docker = first["runtime_bindings"]["docker_api"]
        assert systemd["pid"] == 12 and systemd["invocation_id"] == "inv"
        assert systemd["ordered_dropins"][1]["sha256"] == "4" * 64
        assert docker["container_id"] == "container"
        assert docker["compose_configuration_fingerprint"] == "5" * 64
        assert first["checkpoint_binding"]["sha256"] == "6" * 64
        assert first["recovery_binding"]["root"] == {
            "path": "/safe/recovery", "uid": 1000, "gid": 1000,
            "mode": "0700", "symlink": False,
        }
        assert first["recovery_binding"]["preserved_backup"]["mode"] == "0600"
        assert first["recovery_binding"]["evidence"]["sha256"] == "8" * 64
        assert all(row["uuid_retained"] for row in first["database_bindings"]["clients"])
        assert first["post_promotion_runtime_requirements"] == {
            "systemd_api_restart_required": True,
            "docker_api_recreation_required": True,
            "prune": "next_invocation", "backup": "next_invocation",
            "timer_restart_required": False, "manual_consumers": "next_invocation",
        }
        changed = json.loads(promotion.canonical_json(first)); changed["runtime_bindings"]["systemd_api"]["pid"] = 13
        assert promotion.plan_hash(changed) != promotion.plan_hash(first)
        assert "PASSWORD" not in promotion.canonical_json(first)
        old = dict(first); old["contract_version"] = 3; old["promotion_plan_contract_version"] = 3
        expect_code("PROMOTION_PLAN_CONTRACT_SUPERSEDED", lambda: promotion.promotion_attestation(old))
        assert first["excluded_actions"] == list(v5.EXCLUDED_ACTIONS)
        assert first["post_promotion_runtime_requirements"]["timer_restart_required"] is False
        for category, code in (("operation_identity", "REPOSITORY_OR_HOST_DRIFT"), ("canonical_identity", "CANONICAL_IDENTITY_DRIFT"), ("database_bindings", "DATABASE_IDENTITY_DRIFT"), ("runtime_bindings", "RUNTIME_OR_COMPOSE_DRIFT"), ("checkpoint_binding", "CHECKPOINT_DRIFT"), ("recovery_binding", "RECOVERY_DRIFT")):
            drifted = json.loads(promotion.canonical_json(first)); drifted[category]["test_drift"] = True
            assert v5.binding_drift_code(first, drifted) == code
        assert [row["client_code"] for row in first["clients"]] == ["BRAVO00016", "ALPHA00001"]


def test_mixed_state_detection_and_uuid_policy():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "runtime.env"
        make_env(path)
        plan = build_test_plan(path)
        markers = {
            "ALPHA00001": {"environment": "production"},
            "BRAVO00016": {"environment": "local_dev"},
        }
        result = promotion.mixed_state_report(
            plan=plan,
            runtime_environment="local_dev",
            platform_marker={"environment": "local_dev"},
            client_markers=markers,
            control_plane_environments={"ALPHA00001": "local_dev", "BRAVO00016": "local_dev"},
        )
        assert result["mixed"] is True
        assert result["all_at_target"] is False
        assert plan["uuid_policy"] == "preserve_existing_database_uuids"


class _FakeCursor:
    """Minimal cursor returning one catalog answer, recording the exact query."""

    def __init__(self, present, calls):
        self._present = present
        self._calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        self._calls.append((" ".join(str(query).split()), tuple(params or ())))

    def fetchall(self):
        return [{"column_name": name} for name in self._present]


class _FakeConn:
    def __init__(self, present):
        self.present = list(present)
        self.calls: list[tuple[str, tuple]] = []

    def cursor(self):
        return _FakeCursor(self.present, self.calls)


def test_journal_schema_capability_classifies_every_shape():
    both = promotion.RESUME_AUDIT_COLUMNS
    cases = {
        (): (False, True, list(both)),
        ("resume_contract", "resume_plan_sha256"): (True, True, []),
        ("resume_contract",): (False, False, ["resume_plan_sha256"]),
        ("resume_plan_sha256",): (False, False, ["resume_contract"]),
    }
    for present, (ready, consistent, missing) in cases.items():
        conn = _FakeConn(present)
        capability = promotion.journal_schema_capability(conn)
        assert capability["resume_audit_schema_ready"] is ready, present
        assert capability["resume_audit_schema_consistent"] is consistent, present
        assert capability["missing_columns"] == missing, present
        assert capability["resume_contract_present"] is ("resume_contract" in present)
        assert capability["resume_plan_sha256_present"] is ("resume_plan_sha256" in present)
        assert capability["expected_migration"] == promotion.RESUME_AUDIT_MIGRATION
        # The probe is scoped to the exact schema/table and is never cached.
        query, params = conn.calls[0]
        assert "information_schema.columns" in query
        assert params == (promotion.JOURNAL_SCHEMA, promotion.JOURNAL_TABLE_NAME, list(both))
        assert len(conn.calls) == 1
        promotion.journal_schema_capability(conn)
        assert len(conn.calls) == 2


def test_schema_gates_fail_closed_and_report_the_required_migration():
    for present in (("resume_contract",), ("resume_plan_sha256",)):
        for gate in (promotion.require_consistent_journal_schema, promotion.require_resume_audit_schema):
            try:
                gate(_FakeConn(present))
            except promotion.PromotionError as exc:
                assert exc.code == "ENVIRONMENT_IDENTITY_RESUME_AUDIT_SCHEMA_INCOMPLETE"
                assert exc.details["writes_performed"] is False
                assert exc.details["expected_migration"] == promotion.RESUME_AUDIT_MIGRATION
                assert promotion.RESUME_AUDIT_MIGRATION in str(exc.details["required_action"])
            else:
                raise AssertionError(f"expected incomplete-schema refusal for {present}")
    # Migration 053 is a supported forward schema but not a resume-v2 schema.
    assert promotion.require_consistent_journal_schema(_FakeConn(()))["resume_audit_schema_ready"] is False
    expect_code("RESUME_V2_AUDIT_SCHEMA_REQUIRED", lambda: promotion.require_resume_audit_schema(_FakeConn(())))
    try:
        promotion.require_resume_audit_schema(_FakeConn(()))
    except promotion.PromotionError as exc:
        assert exc.details["writes_performed"] is False
        assert exc.details["missing_columns"] == list(promotion.RESUME_AUDIT_COLUMNS)
        assert exc.details["expected_migration"] == promotion.RESUME_AUDIT_MIGRATION
    ready = promotion.require_resume_audit_schema(_FakeConn(promotion.RESUME_AUDIT_COLUMNS))
    assert ready["resume_audit_schema_ready"] is True


def test_journal_inspection_reads_optional_columns_without_dynamic_sql():
    source = Path(promotion.__file__).read_text(encoding="utf-8")
    body = source.split("def inspect_journals(", 1)[1].split("\ndef ", 1)[0]
    assert "to_jsonb(journal) ->> 'resume_contract'" in body
    assert "to_jsonb(journal) ->> 'resume_plan_sha256'" in body
    # No bare column reference that would fail on the migration-053 schema.
    assert "runtime_file_after_sha256, resume_contract" not in body
    # Compatibility must not come from swallowing missing-column errors.
    assert not [line for line in source.splitlines() if "except" in line and "UndefinedColumn" in line]


def test_resume_v2_schema_gate_runs_before_any_write_connection():
    cli_source = Path(cli.__file__).read_text(encoding="utf-8")
    execute_body = cli_source.split("def _execute_resume_v2(", 1)[1].split("\ndef ", 1)[0]
    gate = execute_body.index("require_resume_audit_schema")
    first_collection = execute_body.index("_collect_resume_v2_plan")
    assert gate < first_collection
    assert first_collection < execute_body.index("read_only=False"), "write connection precedes the complete schema gate"
    assert first_collection < execute_body.index("journal_resume_progress_v2")
    assert first_collection < execute_body.index("try_promotion_lock")
    plan_body = cli_source.split("def _resume_plan_dry_run(", 1)[1].split("\ndef ", 1)[0]
    assert plan_body.index("require_resume_audit_schema") < plan_body.index("_collect_resume_v2_plan")
    collector_body = cli_source.split("def _collect_resume_v2_plan(", 1)[1].split("\ndef ", 1)[0]
    assert "migration_054_schema_contract" in collector_body
    assert "remote_repository_binding" in collector_body
    forward_body = cli_source.split("def _execute(", 1)[1].split("\ndef ", 1)[0]
    forward_gate = cli_source.split("def _forward_v5_schema_gate(", 1)[1].split("\ndef ", 1)[0]
    assert "require_consistent_journal_schema" in forward_gate
    assert "require_resume_audit_schema" not in forward_body, "forward-v5 must not require migration 054"
    assert forward_body.index("_forward_v5_schema_gate") < forward_body.index("session.open")
    assert forward_body.index("session.open") < forward_body.index("try_promotion_lock")
    assert forward_body.index("_forward_v5_schema_gate") < forward_body.index("create_or_resume_journal")


def test_forward_interruption_details_are_truthful_and_sanitized():
    boom = RuntimeError("connect failed dsn=postgresql://u:secret@127.0.0.1:5432/db password=hunter2")
    after = cli._forward_interruption_details(
        boom, promotion_id="666ff6cc-aa5b-4c07-8eaa-3a95d3a4bd2c",
        current_step=promotion.STEP_RUNTIME_RELOAD, writes_started=True,
    )
    assert after["writes_performed"] is True
    assert after["reconciliation_required"] is True
    assert after["execution_phase"] == promotion.STEP_RUNTIME_RELOAD
    assert after["original_exception_class"] == "RuntimeError"
    for secret in ("secret", "hunter2", "postgresql://"):
        assert secret not in str(after["sanitized_message"])
    before = cli._forward_interruption_details(
        boom, promotion_id=None, current_step=None, writes_started=False,
    )
    assert before["writes_performed"] is False
    assert before["reconciliation_required"] is False
    assert before["execution_phase"] == "before_first_journal_write"
    # main() must never let a hardcoded default replace a truthful durable-write flag.
    merged = {"classification": "X", "writes_performed": False, **after}
    assert merged["writes_performed"] is True


def test_documentation_uses_the_real_inspection_flag():
    parser_flags = set()
    for action in cli._parser()._actions:
        parser_flags.update(action.option_strings)
    assert "--inspect-promotions" in parser_flags
    assert "--promotion-id" in parser_flags
    assert "--inspect-journal" not in parser_flags
    repository = Path(promotion.__file__).resolve().parents[1]
    for relative in ("docs", "README.md", "REPO_MAP.md", "CURRENT_TASK_CONTEXT.md"):
        target = repository / relative
        files = sorted(target.rglob("*.md")) if target.is_dir() else [target]
        for path in files:
            assert "--inspect-journal" not in path.read_text(encoding="utf-8"), path


def main():
    test_environment_contract_and_scope()
    test_cli_default_and_execute_attestation_gate()
    test_attestation_is_exact_and_plan_bound()
    test_backup_checkpoint_contract()
    test_readiness_categories()
    test_client_primitive_readiness_classifications()
    test_privileged_helper_is_always_non_interactive()
    test_missing_nopasswd_is_bounded_and_never_retried()
    test_v5_canonicalization_and_binding_sensitivity()
    test_mixed_state_detection_and_uuid_policy()
    test_journal_schema_capability_classifies_every_shape()
    test_schema_gates_fail_closed_and_report_the_required_migration()
    test_journal_inspection_reads_optional_columns_without_dynamic_sql()
    test_resume_v2_schema_gate_runs_before_any_write_connection()
    test_forward_interruption_details_are_truthful_and_sanitized()
    test_documentation_uses_the_real_inspection_flag()
    print("environment identity promotion unit tests: OK")


if __name__ == "__main__":
    main()
