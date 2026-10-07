#!/usr/bin/env python3
"""Focused pure tests for deterministic, runtime/security-bound resume-v2."""
from __future__ import annotations

import ast
import inspect
import io
import json
import os
import sys
import tempfile
import types
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

try:
    import psycopg
except ModuleNotFoundError:  # Pure executor tests only need a named injected error.
    class OperationalError(Exception):
        pass

    psycopg = SimpleNamespace(OperationalError=OperationalError)

try:
    import dotenv  # noqa: F401
except ModuleNotFoundError:
    dotenv_stub = types.ModuleType("dotenv")
    dotenv_stub.load_dotenv = lambda *args, **kwargs: False
    sys.modules["dotenv"] = dotenv_stub

from ops import environment_identity_promotion as promotion
from ops import promote_environment_identity as cli
from ops import resume_plan_v2 as v2
from ops import runtime_identity_readiness as readiness


PROMOTION_ID = "751f42e4-51b0-406d-8ac4-3ee345a6ce86"
PLATFORM_UUID = "52517750-7438-4558-8490-2736ae4cc629"
CLIENT_UUID = "a41f7fe6-e113-42f7-8789-2dc20b2091d7"
ORIGINAL_HEAD = "3" * 40
RESUME_HEAD = "4" * 40


def expect_code(code, fn):
    try:
        fn()
    except promotion.PromotionError as exc:
        assert exc.code == code, exc
        assert exc.details is None or exc.details.get("writes_performed") is False
    else:
        raise AssertionError(f"expected {code}")


def fixture_plan():
    approval = {
        "host": "approved-host", "repository_path": "/repo", "repository_branch": "main",
        "original_execution_head": ORIGINAL_HEAD, "resume_implementation_head": RESUME_HEAD,
        "head_relationship": "descendant", "promotion_id": PROMOTION_ID,
        "original_v5_plan_sha256": "a" * 64, "original_plan_contract_version": 5,
        "source_environment": "local_dev", "target_environment": "production",
        "platform_uuid": PLATFORM_UUID, "collection_locale": "C",
    }
    journal = {
        "promotion_id": PROMOTION_ID,
        "state": "in_progress", "current_step": "runtime_reload_required",
        "completed_steps": ["client_marker:BRAVO00016"],
        "expected_remaining_steps": [
            "runtime_reload_required", "runtime_processes_verified", "final_verification",
        ],
        "active_promotion_count": 1, "incomplete_recovery_count": 0,
        "total_journal_row_count": 2,
        "reconciliation_classification": "JOURNAL_AND_REALITY_CONVERGED",
        "actual_state_authoritative": True,
        "all_completed_surfaces_observed_at_target": True,
        "reconciliation_records": [{
            "step": "client_marker:BRAVO00016", "journal_reported_complete": True,
            "observed_at_target": True, "classification": "completed_verified",
        }],
    }
    persistent = {
        "canonical_identity": {
            "path": "/etc/log-platform/environment-identity.env",
            "environment": "production", "sha256": "b" * 64,
            "owner": "root", "group": "service", "uid": 0, "gid": 1000,
            "mode": "0640", "regular_file": True, "symlink": False,
            "assignment_cardinality": 1,
        },
        "platform": {
            "database_name": "test_platform", "database_role": "platform",
            "marker_identity": "production", "database_uuid": PLATFORM_UUID,
            "marker_cardinality": 1,
        },
        "clients": [{
            "client_code": "BRAVO00016", "client_id": "6018be20-5faa-41b6-89c9-fe2b54a8283e",
            "database_name": "test_client", "database_user": "test_user",
            "database_host": "127.0.0.1", "database_port": 55432,
            "marker_identity": "production", "control_plane_identity": "production",
            "database_uuid": CLIENT_UUID, "control_plane_uuid": CLIENT_UUID,
            "enabled": True, "guarded_primitive_identity": "ops_control.promote_environment_identity_v1",
            "direct_marker_update_revoked": True, "least_privilege_result": True,
        }],
        "uuid_consistency": {
            "platform_unchanged": True, "all_clients_unchanged": True,
            "all_canonical_lowercase": True,
        },
    }
    convergence = {
        "readiness_classification": "PRODUCTION_PROMOTION_READY",
        "reload_required_consumers": [], "unconverted_consumers": [],
        "conflicting_declarations": [],
        "per_invocation_backup_readiness": "PER_INVOCATION_READY",
        "per_invocation_prune_readiness": "PER_INVOCATION_READY",
    }
    systemd = {
        "service": "log-platform-api.service", "fragment_path": "/etc/systemd/system/log-platform-api.service",
        "ordered_dropins": [{"path": "/etc/systemd/system/x.conf", "uid": 0, "gid": 0, "mode": "0644", "size": 1, "regular_file": True, "symlink": False, "sha256": "c" * 64}],
        "pid": 123, "invocation_id": "d" * 32, "active_state": "active", "sub_state": "running",
        "control_group": "/system.slice/log-platform-api.service",
        "cgroup_process_members": [123], "main_pid_present_in_cgroup": True,
        "process_uniqueness_rule": "service_cgroup_contains_exactly_MainPID",
        "listener_ownership": {"host": "127.0.0.1", "port": 8001, "owned_by_main_pid": True},
        "need_daemon_reload": False, "identity_source": "/etc/log-platform/environment-identity.env",
        "canonical_source_result": True, "observed_identity": "production",
        "health": {"probe": "http://127.0.0.1:8001/docs", "method": "GET", "accepted_status": "200-399", "status": 200, "passed": True},
        "convergence_components": {"active": True, "unique_main_process": True, "canonical_source": True, "identity_matches": True, "health_passed": True, "manager_metadata_current": True},
        "unique_main_process": True,
    }
    docker = {
        "compose_project": "test-resume-v2", "compose_service": "api",
        "ordered_compose_files": ["/repo/docker-compose.yml"], "compose_working_directory": "/repo",
        "container_id": "e" * 64, "image_reference": "test-api",
        "image_digest": "sha256:" + "f" * 64, "compose_configuration_fingerprint": "1" * 64,
        "identity_source": "/etc/log-platform/environment-identity.env",
        "canonical_source_resolution": True, "observed_identity": "production",
        "health": {"probe": "http://127.0.0.1:8000/docs", "method": "GET", "accepted_status": "200-399", "status": 200, "passed": True},
        "convergence_components": {"running": True, "unique_active_api_container": True, "canonical_source": True, "identity_matches": True, "health_passed": True},
        "active_api_container_count": 1, "duplicate_active_container": False,
    }
    security = {
        "privileged_helper": {"path": "/helper", "sha256": "2" * 64, "uid": 0, "gid": 0, "mode": "0755"},
        "parser": {"path": "/parser", "sha256": "3" * 64, "uid": 0, "gid": 0, "mode": "0644"},
        "effective_sudo_policy": {"collection_method_version": v2.SUDO_COLLECTION_METHOD, "collection_locale": "C", "effective_policy_fingerprint": "4" * 64},
        "original_implementation_assets": [{"path": "ops/original.py", "sha256": "5" * 64}],
        "resume_implementation_assets": [{"path": "ops/resume_plan_v2.py", "sha256": "6" * 64}],
        "provisioning_checkpoint": {"path": "/checkpoint", "sha256": "7" * 64},
        "provisioning_recovery": {"root": {"path": "/recovery"}, "hash": "8" * 64},
        "historical_promotions": [{"promotion_id": "7137fc76-e5e7-49df-8f7a-13570a336f27", "current_step": "not_applicable"}],
        "historical_promotion_counts": {"historical": 1, "active": 1, "incomplete_recovery": 0, "total": 2},
        "retired_contracts": {
            "resume_v1": {"contract": "resume-v1", "sha256": v2.RETIRED_RESUME_V1_HASH, "executable": False},
            "recovery_v1": {"contract": "recovery-v1", "sha256": v2.RETIRED_RECOVERY_V1_HASH, "executable": False},
        },
    }
    remote = {
        "collection_method": "git_ls_remote_refs_v1",
        "remote_name": "origin", "remote_url": "ssh://git@github.com/example/log-platform.git",
        "remote_identity": "ssh://git@github.com/example/log-platform.git",
        "remote_ref": "refs/heads/main", "remote_branch": "main",
        "expected_fetch_refspec": "+refs/heads/*:refs/remotes/origin/*",
        "main_tracking_remote": "origin", "main_tracking_merge_ref": "refs/heads/main",
        "local_branch": "main", "local_head": RESUME_HEAD,
        "remote_head": RESUME_HEAD, "local_remote_head_equal": True,
    }
    schema_contract = {
        "contract": "migration_054_complete_catalog_v1",
        "expected_database": {"name": "test_platform", "identity_uuid": PLATFORM_UUID},
        "current_database": {
            "name": "test_platform", "catalog_name": "test_platform",
            "identity_uuid": PLATFORM_UUID, "marker_name": "test_platform",
            "marker_role": "platform", "marker_identity_key": "primary",
            "owner": "test_owner",
        },
        "schema_migration": {
            "filename": v2.RESUME_SCHEMA_MIGRATION, "filename_count": 1,
            "migration_row_count": 54, "ceiling": v2.RESUME_SCHEMA_MIGRATION,
        },
        "columns": v2._expected_canonical_schema_columns(),
        "constraints": [
            {
                "table_schema": "ops_control",
                "table_name": "environment_identity_promotion",
                "name": "ck_environment_identity_promotion_resume_contract",
                "type": "c", "validated": True,
                "definition": "CHECK (((resume_contract IS NULL) OR (resume_contract = 'resume-v2'::text)))",
            },
            {
                "table_schema": "ops_control",
                "table_name": "environment_identity_promotion",
                "name": "ck_environment_identity_promotion_resume_plan_hash",
                "type": "c", "validated": True,
                "definition": "CHECK (((resume_plan_sha256 IS NULL) OR (resume_plan_sha256 ~ '^[0-9a-f]{64}$'::text)))",
            },
        ],
        "trigger_function": {
            "schema": "ops_control",
            "name": "reject_environment_identity_promotion_plan_update",
            "identity_arguments": v2.CANONICAL_SCHEMA_ABSENCE["identity_arguments"],
            "return_type": "trigger", "kind": "f", "owner": "test_owner",
            "language": "plpgsql", "security_definer": False, "volatility": "v",
            "proconfig": v2.CANONICAL_SCHEMA_ABSENCE["proconfig"],
            "prosrc_sha256": v2.RESUME_TRIGGER_FUNCTION_PROSRC_SHA256,
        },
        "trigger": {
            "name": "trg_environment_identity_promotion_plan_immutable",
            "table_schema": "ops_control",
            "table_name": "environment_identity_promotion",
            "enabled": "O", "timing": "BEFORE", "events": ["UPDATE"],
            "row_level": True,
            "function_schema": "ops_control",
            "function_name": "reject_environment_identity_promotion_plan_update",
            "definition": (
                "CREATE TRIGGER trg_environment_identity_promotion_plan_immutable BEFORE UPDATE "
                "ON ops_control.environment_identity_promotion FOR EACH ROW EXECUTE FUNCTION "
                "ops_control.reject_environment_identity_promotion_plan_update()"
            ),
            "table_owner": "test_owner",
        },
        "comments": [
            {
                "name": "resume_contract",
                "comment": "Write-once executable resume approval contract; only resume-v2 is accepted.",
            },
            {
                "name": "resume_plan_sha256",
                "comment": (
                    "Write-once SHA-256 of the exact canonical resume-v2 plan approved "
                    "for this completion attempt."
                ),
            },
        ],
        "comment_policy": "exact_text_required_block_on_absent_or_changed",
        "target_promotion_resume_audit_state": {
            "promotion_id": PROMOTION_ID,
            "non_null_resume_audit_row_count": 0,
        },
        "volatile_catalog_oids_bound": False,
    }
    return v2.build_plan(
        approval_identity=approval,
        remote_repository_binding=remote,
        migration_054_schema_contract=schema_contract,
        journal_state=journal, persistent_state=persistent,
        runtime_convergence=convergence, systemd_runtime=systemd,
        docker_runtime=docker, security_and_recovery=security,
        promotion_id=PROMOTION_ID,
    )


def test_schema_determinism_normalization_and_attestation():
    first = fixture_plan()
    assert tuple(first) == v2.TOP_LEVEL_SECTIONS
    assert json.loads(promotion.canonical_json(first)) == first
    shuffled = {key: deepcopy(first[key]) for key in reversed(first)}
    canonical_rebuilt = v2.build_plan(
        approval_identity=shuffled["approval_identity"],
        remote_repository_binding=shuffled["remote_repository_binding"],
        migration_054_schema_contract=shuffled["migration_054_schema_contract"],
        journal_state=shuffled["journal_state"],
        persistent_state=shuffled["persistent_state"],
        runtime_convergence=shuffled["runtime_convergence"],
        systemd_runtime=shuffled["systemd_runtime"],
        docker_runtime=shuffled["docker_runtime"],
        security_and_recovery=shuffled["security_and_recovery"],
        promotion_id=PROMOTION_ID,
    )
    assert promotion.canonical_json(first) == promotion.canonical_json(canonical_rebuilt)
    assert "timestamp" not in promotion.canonical_json(first).lower()
    string_value = deepcopy(first)
    string_value["security_and_recovery"]["privileged_helper"]["note"] = "annulling is ordinary text"
    v2.validate_plan(string_value)
    actual_null = deepcopy(first)
    actual_null["persistent_state"]["canonical_identity"]["sha256"] = None
    expect_code("RESUME_IMMUTABLE_BINDING_DRIFT", lambda: v2.validate_plan(actual_null))
    missing_key = deepcopy(first)
    del missing_key["systemd_runtime"]["listener_ownership"]
    expect_code("RESUME_IMMUTABLE_BINDING_DRIFT", lambda: v2.validate_plan(missing_key))
    assert first["persistent_state"]["canonical_identity"]["mode"] == "0640"
    exact = v2.attestation(first)
    assert exact.startswith("RESUME_ENVIRONMENT_IDENTITY_V2 host=approved-host ")
    assert exact.endswith("UUIDS_UNCHANGED=true NO_PERSISTENT_WRITES=true NO_RUNTIME_RESTART=true")
    assert "  " not in exact and exact == exact.strip()
    broken = deepcopy(first)
    broken["persistent_state"]["canonical_identity"]["sha256"] = ""
    expect_code("RESUME_IMMUTABLE_BINDING_DRIFT", lambda: v2.attestation(broken))


def test_locale_stable_sudo_policy():
    c_output = f"""Matching Defaults entries for test:
    env_reset

User test may run the following commands on host:
    (ALL : ALL) ALL
    {v2.HELPER_RULE}
    (root) NOPASSWD: /usr/local/sbin/log-platform-runtime-identity-inspector inspect-runtime-identity-sources
"""
    pl_output = c_output.replace(
        "Matching Defaults entries for test:", "Pasujace wpisy Defaults dla test:"
    ).replace(
        "User test may run the following commands on host:",
        "Uzytkownik test moze uruchamiac nastepujace polecenia na host:",
    )
    captured = {}

    def collect(output):
        def fake_run(command, **kwargs):
            assert command == ["sudo", "-n", "-l"]
            captured.update(kwargs)
            return SimpleNamespace(returncode=0, stdout=output)
        with patch.object(v2.subprocess, "run", side_effect=fake_run):
            return v2.effective_sudo_policy_binding()

    binding = collect(c_output)
    translated = collect(pl_output)
    assert binding["collection_method_version"] == "sudo_n_l_normalized_v2"
    assert binding["matching_rule_count"] == 2
    assert binding["exact_helper_rule_count"] == 1
    assert [
        row["specification"]
        for row in binding["normalized_command_policy_records"][0]["commands"]
    ] == ["ALL"]
    assert binding["normalized_command_policy_records"][0]["commands"][0]["kind"] == "all"
    assert binding["final_effective_authorization"] == "root_nopasswd_allowed"
    assert binding["effective_policy_fingerprint"] == translated["effective_policy_fingerprint"]
    assert captured["env"]["LC_ALL"] == captured["env"]["LANG"] == captured["env"]["LC_MESSAGES"] == "C"
    assert "COLUMNS" not in captured["env"]

    preceding_broad = collect(f"""header
    (root) PASSWD: {v2.HELPER_PATH} *
    {v2.HELPER_RULE}
""")
    assert preceding_broad["final_effective_authorization"] == "root_nopasswd_allowed"

    later_conflict = c_output + "    (ALL : ALL) PASSWD: ALL\n"
    with patch.object(v2, "_run", return_value=later_conflict):
        expect_code("RESUME_SECURITY_BINDING_DRIFT", v2.effective_sudo_policy_binding)

    multiple = collect(f"""header
    {v2.HELPER_RULE}
    {v2.HELPER_RULE}
""")
    assert multiple["exact_helper_rule_count"] == 2

    wrapped = collect(f"""header
    (root) NOPASSWD: {v2.HELPER_PATH}
        *
""")
    assert wrapped["exact_helper_rule_count"] == 1

    with patch.object(v2, "_run", return_value=f"""header
    {v2.HELPER_RULE}
malformed trailing policy text
"""):
        expect_code("RESUME_SECURITY_BINDING_DRIFT", v2.effective_sudo_policy_binding)


def test_sudo_command_list_tokenizer():
    assert v2.tokenize_sudo_command_list("/bin/true, ALL") == ["/bin/true", "ALL"]
    assert v2.tokenize_sudo_command_list("ALL, /bin/true") == ["ALL", "/bin/true"]
    assert v2.tokenize_sudo_command_list("/bin/true,ALL") == ["/bin/true", "ALL"]
    assert v2.tokenize_sudo_command_list(
        "ALL, !/usr/local/sbin/log-platform-environment-identity-helper *"
    ) == ["ALL", "!/usr/local/sbin/log-platform-environment-identity-helper *"]
    # An escaped comma is one argument, not a specification separator.
    assert v2.tokenize_sudo_command_list(r"/bin/echo one\,two, /bin/false") == [
        "/bin/echo one,two", "/bin/false",
    ]
    assert v2.tokenize_sudo_command_list("  /bin/true  ") == ["/bin/true"]
    expect_code(
        "RESUME_SECURITY_BINDING_DRIFT",
        lambda: v2.tokenize_sudo_command_list("/bin/true\\"),
    )
    expect_code(
        "RESUME_SECURITY_BINDING_DRIFT",
        lambda: v2.tokenize_sudo_command_list(r"/bin/echo \q"),
    )
    expect_code(
        "RESUME_SECURITY_BINDING_DRIFT",
        lambda: v2.tokenize_sudo_command_list("/bin/true,,ALL"),
    )
    expect_code(
        "RESUME_SECURITY_BINDING_DRIFT",
        lambda: v2.tokenize_sudo_command_list("/bin/true, "),
    )


def test_sudo_command_classification_is_fail_closed():
    helper = v2.HELPER_PATH
    supported = {
        "ALL": ("all", True),
        f"{helper} *": ("absolute_command", True),
        f"!{helper} *": ("absolute_command", True),
        "/bin/true": ("absolute_command", False),
        f"{helper} inspect": ("absolute_command", True),
    }
    for specification, (kind, matches) in supported.items():
        row = v2.classify_sudo_command(specification)
        assert row["supported"] is True, specification
        assert row["kind"] == kind, specification
        assert row["matches_helper"] is matches, specification
    assert v2.classify_sudo_command(f"!{helper} *")["negated"] is True

    for specification, kind in {
        "LOGPLATFORM_HELPER": "unresolved_command_alias",
        "!LOGPLATFORM_HELPER": "unresolved_command_alias",
        "CMND_ALIAS_2": "unresolved_command_alias",
        "/usr/local/sbin/log-platform-*-helper *": "unsupported_command_path_pattern",
        f"{helper} a*b": "unsupported_argument_pattern",
        f"{helper} [a-z]": "unsupported_argument_pattern",
        "sudoedit /etc/hosts": "unsupported_command_syntax",
        "../relative": "unsupported_command_syntax",
    }.items():
        row = v2.classify_sudo_command(specification)
        assert row["supported"] is False, specification
        assert row["kind"] == kind, specification
        # Unknown syntax is never optimistically classified as non-matching.
        assert row["matches_helper"] is True, specification


def test_sudo_policy_refuses_broad_and_unresolved_helper_rules():
    def refuse(policy_lines):
        output = "header\n" + "".join(f"    {line}\n" for line in policy_lines)
        with patch.object(v2, "_run", return_value=output):
            expect_code("RESUME_SECURITY_BINDING_DRIFT", v2.effective_sudo_policy_binding)

    helper_rule = v2.HELPER_RULE
    # A later broad ALL hidden inside a command list must not be missed.
    refuse([helper_rule, "(root) PASSWD: /bin/true, ALL"])
    refuse([helper_rule, "(root) PASSWD: ALL, /bin/true"])
    refuse([helper_rule, "(root) PASSWD: /bin/true,ALL"])
    refuse([helper_rule, "(ALL : ALL) PASSWD: ALL"])
    # An unresolved command alias can never be proven harmless.
    refuse([helper_rule, "(root) PASSWD: LOGPLATFORM_HELPER"])
    refuse([helper_rule, "(root) NOPASSWD: LOGPLATFORM_HELPER"])
    refuse([helper_rule, "(root) NOPASSWD: !LOGPLATFORM_HELPER"])
    # A trailing negation of the helper removes the authorization.
    refuse([helper_rule, f"(root) NOPASSWD: ALL, !{v2.HELPER_PATH} *"])
    refuse([f"(root) NOPASSWD: !{v2.HELPER_PATH} *"])
    # Unsupported tags on a helper-matching rule fail closed (F-5).
    for tag in ("SETENV", "NOEXEC", "EXEC", "LOG_INPUT", "LOG_OUTPUT", "FOLLOW"):
        refuse([f"(root) NOPASSWD: {tag}: {v2.HELPER_PATH} *"])
        refuse([helper_rule, f"(root) {tag}: NOPASSWD: {v2.HELPER_PATH} *"])
    # Malformed command lists and ambiguous escapes fail closed.
    refuse([helper_rule, "(root) NOPASSWD: /bin/true,,/bin/false"])
    refuse([f"(root) NOPASSWD: {v2.HELPER_PATH} *\\"])
    refuse([f"(root) NOPASSWD: {v2.HELPER_PATH} \\q"])

    # A later broad NOPASSWD: ALL does not weaken the root NOPASSWD conclusion,
    # but it must be visible as a later matching rule and change the fingerprint.
    broad_nopasswd = "header\n" + "".join(f"    {line}\n" for line in (
        helper_rule, "(root) NOPASSWD: ALL",
    ))
    with patch.object(v2, "_run", return_value=broad_nopasswd):
        broad_binding = v2.effective_sudo_policy_binding()
    with patch.object(v2, "_run", return_value="header\n    " + helper_rule + "\n"):
        exact_only = v2.effective_sudo_policy_binding()
    assert broad_binding["final_effective_authorization"] == "root_nopasswd_allowed"
    assert broad_binding["later_matching_rule"] is True
    assert broad_binding["later_rule_changes_password_requirement"] is False
    assert exact_only["later_matching_rule"] is False
    assert (
        broad_binding["effective_policy_fingerprint"]
        != exact_only["effective_policy_fingerprint"]
    )

    # A non-matching rule may keep unsupported tags and unusual command syntax.
    allowed = "header\n" + "".join(f"    {line}\n" for line in (
        "(root) NOEXEC: /usr/bin/unrelated arg",
        helper_rule,
    ))
    with patch.object(v2, "_run", return_value=allowed):
        binding = v2.effective_sudo_policy_binding()
    assert binding["final_effective_authorization"] == "root_nopasswd_allowed"
    assert binding["matching_rule_count"] == 1
    assert binding["supported_authorization_tags"] == ["NOPASSWD", "PASSWD"]

    # An escaped comma inside an argument stays one specification and is refused
    # only because it is not the exact helper rule, not because it split wrongly.
    escaped = "header\n" + "".join(f"    {line}\n" for line in (
        helper_rule,
        r"(root) PASSWD: /bin/echo one\,two",
    ))
    with patch.object(v2, "_run", return_value=escaped):
        escaped_binding = v2.effective_sudo_policy_binding()
    assert escaped_binding["final_effective_authorization"] == "root_nopasswd_allowed"
    assert [
        row["specification"]
        for row in escaped_binding["normalized_command_policy_records"][1]["commands"]
    ] == ["/bin/echo one,two"]


def test_sudo_policy_fingerprint_is_structure_sensitive():
    helper_rule = v2.HELPER_RULE
    def records(policy_lines):
        output = "header\n" + "".join(f"    {line}\n" for line in policy_lines)
        return v2.normalize_sudo_command_policy(output)

    baseline = records([helper_rule, "(root) NOPASSWD: /bin/true"])
    baseline_fingerprint = v2.sudo_policy_fingerprint(baseline)
    variants = {
        "rule_order": ["(root) NOPASSWD: /bin/true", helper_rule],
        "broad_all_added": [helper_rule, "(root) NOPASSWD: /bin/true", "(ALL : ALL) NOPASSWD: ALL"],
        "password_tag": [helper_rule, "(root) PASSWD: /bin/true"],
        "command_list": [helper_rule, "(root) NOPASSWD: /bin/true, /bin/false"],
        "run_as_target": [helper_rule, "(nobody) NOPASSWD: /bin/true"],
        "negation": [helper_rule, "(root) NOPASSWD: !/bin/true"],
        "unsupported_rule": [helper_rule, "(root) NOPASSWD: UNRESOLVED_ALIAS"],
        "unsupported_tag": [helper_rule, "(root) NOEXEC: /bin/true"],
    }
    fingerprints = {"baseline": baseline_fingerprint}
    for label, policy_lines in variants.items():
        fingerprints[label] = v2.sudo_policy_fingerprint(records(policy_lines))
    assert len(set(fingerprints.values())) == len(fingerprints), fingerprints


def test_sudo_policy_accepts_representative_host_output():
    host = (
        "Matching Defaults entries for operator on host:\n"
        "    env_reset, mail_badpass, secure_path=/usr/local/sbin\\:/usr/local/bin, use_pty\n"
        "\n"
        "Runas and Command-specific defaults for operator:\n"
        "    Defaults!/usr/local/sbin/log-platform-runtime-identity-inspector"
        " inspect-runtime-identity-sources env_reset, !setenv, secure_path=/usr/sbin\\:/usr/bin\n"
        "\n"
        "User operator may run the following commands on host:\n"
        "    (ALL : ALL) ALL\n"
        f"    (root) NOPASSWD: {v2.HELPER_PATH} *\n"
        "    (root) NOPASSWD: /usr/local/sbin/log-platform-runtime-identity-inspector"
        " inspect-runtime-identity-sources\n"
    )
    polish = (
        host.replace("Matching Defaults entries for", "Pasujace wpisy Defaults dla")
        .replace("Runas and Command-specific defaults for", "Domyslne ustawienia Runas i polecen dla")
        .replace("may run the following commands on", "moze uruchamiac nastepujace polecenia na")
    )
    with patch.object(v2, "_run", return_value=host):
        binding = v2.effective_sudo_policy_binding()
    with patch.object(v2, "_run", return_value=polish):
        translated = v2.effective_sudo_policy_binding()
    assert binding["final_effective_authorization"] == "root_nopasswd_allowed"
    assert binding["matching_rule_count"] == 2
    assert binding["exact_helper_rule_count"] == 1
    assert len(binding["normalized_command_policy_records"]) == 3
    assert binding["effective_policy_fingerprint"] == translated["effective_policy_fingerprint"]


def _repository_import_closure(root: Path, seeds: set[str]) -> set[str]:
    closure: set[str] = set()
    pending = list(seeds)
    while pending:
        relative = pending.pop()
        if relative in closure:
            continue
        path = root / relative
        assert path.is_file(), relative
        closure.add(relative)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        dependencies: set[str] = set()
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.module in {"ops", "jobs", "jobs.common"}:
                    names = [f"{node.module}.{alias.name}" for alias in node.names]
                elif node.module:
                    names = [node.module]
            for name in names:
                candidate = name.replace(".", "/") + ".py"
                if candidate.startswith(("ops/", "jobs/")) and (root / candidate).is_file():
                    dependencies.add(candidate)
        pending.extend(sorted(dependencies - closure))
    return closure


def test_dual_head_and_assets():
    repository_root = Path(__file__).resolve().parents[2]
    closure = _repository_import_closure(
        repository_root,
        {"ops/promote_environment_identity.py", "ops/resume_plan_v2.py"},
    )
    assert closure == set(v2.RESUME_IMPLEMENTATION_ASSET_PATHS)
    assert "ops/failed_promotion_recovery.py" in closure
    assert "jobs/common/environment_identity.py" in closure

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for relative in v2.RESUME_IMPLEMENTATION_ASSET_PATHS:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(relative, encoding="utf-8")
        assets = v2.implementation_asset_bindings(root)
        assert [row["path"] for row in assets] == sorted(v2.RESUME_IMPLEMENTATION_ASSET_PATHS)
        assert "ops/resume_plan_v2.py" in [row["path"] for row in assets]
        def ancestry_run(command, **kwargs):
            assert command == [
                "git", "-C", str(root), "merge-base", "--is-ancestor",
                ORIGINAL_HEAD, RESUME_HEAD,
            ]
            assert kwargs["check"] is False and kwargs["capture_output"] is True
            return SimpleNamespace(returncode=0)
        with patch.object(v2.subprocess, "run", side_effect=ancestry_run):
            assert v2.head_relationship(root, ORIGINAL_HEAD, RESUME_HEAD) == "descendant"
        assert v2.head_relationship(root, ORIGINAL_HEAD, ORIGINAL_HEAD) == "identical"
        with patch.object(v2.subprocess, "run", return_value=SimpleNamespace(returncode=1)):
            expect_code("RESUME_IMPLEMENTATION_HEAD_UNRELATED", lambda: v2.head_relationship(root, ORIGINAL_HEAD, RESUME_HEAD))
        (root / "ops/resume_plan_v2.py").write_text("drift", encoding="utf-8")
        assert assets != v2.implementation_asset_bindings(root)


def test_retirement_missing_approval_and_no_fallback():
    expect_code("RESUME_V1_NON_EXECUTABLE", lambda: v2.reject_retired_execution(attestation_value="x contract=resume-v1", resume_plan_sha256="a" * 64))
    expect_code("RESUME_V1_NON_EXECUTABLE", lambda: v2.reject_retired_execution(attestation_value="x", resume_plan_sha256=v2.RETIRED_RESUME_V1_HASH))
    expect_code("RESUME_V1_NON_EXECUTABLE", lambda: v2.reject_retired_execution(attestation_value="x", resume_plan_sha256="a" * 64, contract="other"))
    expect_code("RESUME_IMMUTABLE_BINDING_DRIFT", lambda: v2.reject_retired_execution(
        attestation_value="x", resume_plan_sha256=v2.REJECTED_INCOMPLETE_RESUME_V2_HASH,
    ))
    expect_code("RESUME_APPROVAL_ARGUMENT_MISSING", lambda: v2.reject_retired_execution(attestation_value="x", resume_plan_sha256=None))
    diagnostic = v2.diagnostic_v1()
    assert diagnostic["executable"] is False and diagnostic["contract_retired"] is True
    parser = cli._parser()
    assert parser.parse_args(["--resume-plan"]).resume_plan is True
    source = inspect.getsource(cli._execute_resume_v2)
    assert "_execute(" not in source and "resume-v1" not in source
    args = parser.parse_args(["--promotion-id", PROMOTION_ID, "--execute"])
    with patch.object(cli, "_repository_state", return_value=SimpleNamespace(head=RESUME_HEAD)):
        expect_code("RESUME_V1_NON_EXECUTABLE", lambda: cli._execute(args))
    original = {"contract_version": 5, "promotion_plan_contract_version": 5}
    expect_code(
        "RESUME_APPROVAL_IDENTITY_DRIFT",
        lambda: v2._validate_original_plan(
            original, {"plan_sha256": "0" * 64},
        ),
    )


def test_state_aware_pre_runtime_routing():
    steps = [
        "client_marker:TEST00001", promotion.STEP_CONTROL_PLANE,
        promotion.STEP_PLATFORM_MARKER, promotion.STEP_RUNTIME_FILE,
        promotion.STEP_RUNTIME_RELOAD, promotion.STEP_RUNTIME_PROCESSES,
        promotion.STEP_FINAL_VERIFY,
    ]
    final_journal = {
        "state": "in_progress",
        "completed_steps": steps[:4],
        "current_step": promotion.STEP_RUNTIME_RELOAD,
    }
    assert v2.require_resume_v2_finalization_route(
        journal=final_journal, original_steps=steps,
    ) == "resume-v2-finalization"
    pre_runtime = {
        "state": "in_progress",
        "completed_steps": steps[:2],
        "current_step": promotion.STEP_PLATFORM_MARKER,
    }
    expect_code(
        "PRE_RUNTIME_FORWARD_RESUME_CONTRACT_REQUIRED",
        lambda: v2.require_resume_v2_finalization_route(
            journal=pre_runtime, original_steps=steps,
        ),
    )
    completed = dict(final_journal, state="completed")
    expect_code(
        "PROMOTION_TERMINAL",
        lambda: v2.require_resume_v2_finalization_route(
            journal=completed, original_steps=steps,
        ),
    )

    class RouteCursor:
        def __enter__(self): return self
        def __exit__(self, *unused): return False

        def execute(self, query, params=None):
            assert "information_schema.columns" in " ".join(str(query).split())
            assert tuple(params or ()) == (
                promotion.JOURNAL_SCHEMA, promotion.JOURNAL_TABLE_NAME,
                list(promotion.RESUME_AUDIT_COLUMNS),
            )

        def fetchall(self):
            return [
                {"column_name": name}
                for name in promotion.RESUME_AUDIT_COLUMNS
            ]

    class RouteConnection:
        def __enter__(self): return self
        def __exit__(self, *unused): return False
        def cursor(self): return RouteCursor()

    args = SimpleNamespace(
        promotion_id=PROMOTION_ID,
        attestation=None,
        resume_plan_sha256=None,
    )
    with patch.object(cli, "_repository_state", return_value=SimpleNamespace(head=RESUME_HEAD)), \
         patch.object(cli, "_validate_scope", return_value={}), \
         patch.object(cli, "_require_arguments", return_value=None), \
         patch.object(cli, "_runtime", return_value=SimpleNamespace()), \
         patch.object(cli, "_platform_conn", return_value=RouteConnection()), \
         patch.object(cli, "_resume_plan", return_value=({"steps": steps}, [], pre_runtime)):
        expect_code(
            "PRE_RUNTIME_FORWARD_RESUME_CONTRACT_REQUIRED",
            lambda: cli._execute_resume_v2(args),
        )


def test_category_drift_classifications():
    base = fixture_plan()
    expectations = {
        "approval_identity": "RESUME_APPROVAL_IDENTITY_DRIFT",
        "remote_repository_binding": "RESUME_REMOTE_HEAD_DRIFT",
        "migration_054_schema_contract": "RESUME_SCHEMA_CONTRACT_DRIFT",
        "journal_state": "RESUME_JOURNAL_STATE_DRIFT",
        "persistent_state": "RESUME_PERSISTENT_STATE_DRIFT",
        "systemd_runtime": "RESUME_SYSTEMD_RUNTIME_DRIFT",
        "docker_runtime": "RESUME_DOCKER_RUNTIME_DRIFT",
        "remaining_execution_contract": "RESUME_EXECUTION_CONTRACT_DRIFT",
    }
    for key, expected in expectations.items():
        changed = deepcopy(base)
        changed[key]["drift"] = True
        assert v2.drift_classification(base, changed) == expected
    changed = deepcopy(base)
    changed["security_and_recovery"]["privileged_helper"]["sha256"] = "9" * 64
    assert v2.drift_classification(base, changed) == "RESUME_SECURITY_BINDING_DRIFT"
    changed = deepcopy(base)
    changed["security_and_recovery"]["historical_promotion_counts"]["total"] = 3
    assert v2.drift_classification(base, changed) == "RESUME_HISTORY_DRIFT"


def test_security_binding_field_hash_mutations():
    baseline = fixture_plan()
    baseline_hash = promotion.plan_hash(baseline)
    schema = baseline["migration_054_schema_contract"]
    mutations = (
        ("remote_repository_binding", "local_head", "9" * 40),
        ("remote_repository_binding", "remote_head", "8" * 40),
        ("remote_repository_binding", "remote_identity", "ssh://git@example.invalid/other.git"),
        ("security_and_recovery", "resume_implementation_assets", [
            {"path": "ops/resume_plan_v2.py", "sha256": "9" * 64},
        ]),
        ("migration_054_schema_contract", "schema_migration", {
            **schema["schema_migration"], "filename_count": 2,
        }),
        ("migration_054_schema_contract", "columns", [
            {**schema["columns"][0], "not_null": True}, *schema["columns"][1:],
        ]),
        ("migration_054_schema_contract", "constraints", [
            {**schema["constraints"][0], "definition": "CHECK (false)"},
            *schema["constraints"][1:],
        ]),
        ("migration_054_schema_contract", "constraints", [
            {**schema["constraints"][0], "validated": False},
            *schema["constraints"][1:],
        ]),
        ("migration_054_schema_contract", "trigger_function", {
            **schema["trigger_function"], "prosrc_sha256": "9" * 64,
        }),
        ("migration_054_schema_contract", "trigger_function", {
            **schema["trigger_function"], "security_definer": True,
        }),
        ("migration_054_schema_contract", "trigger", {
            **schema["trigger"], "enabled": "D",
        }),
        ("migration_054_schema_contract", "trigger", {
            **schema["trigger"], "definition": "changed",
        }),
        ("migration_054_schema_contract", "target_promotion_resume_audit_state", {
            "promotion_id": PROMOTION_ID,
            "non_null_resume_audit_row_count": 1,
        }),
    )
    for section, key, value in mutations:
        changed = deepcopy(baseline)
        changed[section][key] = value
        assert promotion.plan_hash(changed) != baseline_hash, (section, key)


def test_target_promotion_id_is_canonical_and_hash_bound():
    baseline = fixture_plan()
    other_id = "666ff6cc-aa5b-4c07-8eaa-3a95d3a4bd2c"
    changed = deepcopy(baseline)
    changed["approval_identity"]["promotion_id"] = other_id
    changed["journal_state"]["promotion_id"] = other_id
    changed["migration_054_schema_contract"][
        "target_promotion_resume_audit_state"
    ]["promotion_id"] = other_id
    changed["remaining_execution_contract"] = v2.remaining_execution_contract(other_id)
    v2.validate_plan(changed)
    assert promotion.plan_hash(changed) != promotion.plan_hash(baseline)

    for section, field in (
        ("approval_identity", "promotion_id"),
        ("journal_state", "promotion_id"),
        ("migration_054_schema_contract", "target_promotion_resume_audit_state"),
        ("remaining_execution_contract", "writable_promotion_id"),
    ):
        mismatch = deepcopy(baseline)
        if section == "migration_054_schema_contract":
            mismatch[section][field]["promotion_id"] = other_id
        else:
            mismatch[section][field] = other_id
        expect_code("RESUME_SCHEMA_CONTRACT_DRIFT", lambda mismatch=mismatch: v2.validate_plan(mismatch))

    serialized = promotion.canonical_json(baseline)
    assert "target_promotion_resume_audit_state" in serialized
    assert '"non_null_resume_audit_row_count":0' in serialized
    assert '"expected_non_null_resume_audit_row_count"' not in serialized


def test_schema_catalog_query_errors_use_precise_classification():
    class BrokenConnection:
        def cursor(self):
            raise RuntimeError("catalog unavailable")

    expect_code(
        "RESUME_SCHEMA_CONTRACT_DRIFT",
        lambda: v2.migration_054_schema_contract(
            BrokenConnection(),
            expected_database_name="test_platform",
            expected_database_uuid=PLATFORM_UUID,
            target_promotion_id=PROMOTION_ID,
        ),
    )


def test_schema_absence_sentinels_are_exact_and_fail_closed():
    expected_raw = {
        "identity_kind": "",
        "generated_kind": "",
        "default_expression": None,
        "identity_arguments": "",
        "proconfig": None,
    }
    assert {
        field: v2._canonical_validated_schema_absence(field, raw)
        for field, raw in expected_raw.items()
    } == v2.CANONICAL_SCHEMA_ABSENCE
    for field, unexpected in {
        "identity_kind": "a",
        "generated_kind": "s",
        "default_expression": "'resume-v2'::text",
        "identity_arguments": "unexpected text",
        "proconfig": ["search_path=pg_catalog"],
    }.items():
        expect_code(
            "RESUME_SCHEMA_CONTRACT_DRIFT",
            lambda field=field, unexpected=unexpected:
                v2._canonical_validated_schema_absence(field, unexpected),
        )

    base = fixture_plan()
    assert base["contract_version"] == 3
    columns = base["migration_054_schema_contract"]["columns"]
    for row in columns:
        assert row["identity_kind"] == v2.CANONICAL_SCHEMA_ABSENCE["identity_kind"]
        assert row["generated_kind"] == v2.CANONICAL_SCHEMA_ABSENCE["generated_kind"]
        assert row["default_expression"] == v2.CANONICAL_SCHEMA_ABSENCE["default_expression"]
    function = base["migration_054_schema_contract"]["trigger_function"]
    assert function["identity_arguments"] == v2.CANONICAL_SCHEMA_ABSENCE["identity_arguments"]
    assert function["proconfig"] == v2.CANONICAL_SCHEMA_ABSENCE["proconfig"]

    for replacement in (None, ""):
        changed = deepcopy(base)
        changed["migration_054_schema_contract"]["columns"][0]["identity_kind"] = replacement
        expect_code(
            "RESUME_IMMUTABLE_BINDING_DRIFT",
            lambda changed=changed: v2.validate_plan(changed),
        )
    missing = deepcopy(base)
    del missing["migration_054_schema_contract"]["columns"][0]["identity_kind"]
    expect_code(
        "RESUME_SCHEMA_CONTRACT_DRIFT",
        lambda: v2.validate_plan(missing),
    )
    incomplete = deepcopy(base)
    del incomplete["migration_054_schema_contract"]["trigger_function"]["owner"]
    expect_code(
        "RESUME_SCHEMA_CONTRACT_DRIFT",
        lambda: v2.validate_plan(incomplete),
    )


def test_health_ports_fail_closed_and_readiness_split():
    called = []
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): return False
    with patch.object(v2, "urlopen", side_effect=lambda url, timeout: (called.append(url) or Response())):
        assert v2._health("http://127.0.0.1:8001/docs", "RESUME_SYSTEMD_RUNTIME_DRIFT")["passed"]
        assert v2._health("http://127.0.0.1:8000/docs", "RESUME_DOCKER_RUNTIME_DRIFT")["passed"]
    assert called == ["http://127.0.0.1:8001/docs", "http://127.0.0.1:8000/docs"]
    with patch.object(v2, "urlopen", side_effect=OSError("down")):
        expect_code("RESUME_SYSTEMD_RUNTIME_DRIFT", lambda: v2._health("http://127.0.0.1:8001/docs", "RESUME_SYSTEMD_RUNTIME_DRIFT"))
    with patch.object(readiness, "urlopen", side_effect=lambda url, timeout: (called.append(url) or Response())), \
         patch.object(readiness, "_process_environment", return_value="production"), \
         patch.object(readiness.subprocess, "run", return_value=SimpleNamespace(stdout="MainPID=1\nExecMainStartTimestamp=n/a\n")):
        readiness._systemd_api_probe()
    assert called[-1] == "http://127.0.0.1:8001/docs"


def test_systemd_cgroup_and_listener_ownership_proof():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        proc_root = root / "proc"
        cgroup_root = root / "cgroup"
        service = cgroup_root / "system.slice/log-platform-api.service"
        service.mkdir(parents=True)
        (service / "cgroup.procs").write_text("123\n", encoding="ascii")
        (proc_root / "net").mkdir(parents=True)
        (proc_root / "net/tcp").write_text(
            "sl local_address rem_address st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode\n"
            "0: 0100007F:1F41 00000000:0000 0A 0:0 00:0 0 1000 0 777\n",
            encoding="ascii",
        )
        (proc_root / "net/tcp6").write_text("header\n", encoding="ascii")
        main_fd = proc_root / "123/fd"
        main_fd.mkdir(parents=True)
        (main_fd / "5").symlink_to("socket:[777]")

        valid = v2.systemd_process_listener_proof(
            main_pid=123,
            control_group="/system.slice/log-platform-api.service",
            proc_root=proc_root,
            cgroup_root=cgroup_root,
        )
        assert valid["unique_main_process"] is True
        assert valid["listener"]["owned_by_main_pid"] is True

        (service / "cgroup.procs").write_text("123\n999\n", encoding="ascii")
        expect_code(
            "RESUME_SYSTEMD_RUNTIME_DRIFT",
            lambda: v2.systemd_process_listener_proof(
                main_pid=123, control_group="/system.slice/log-platform-api.service",
                proc_root=proc_root, cgroup_root=cgroup_root,
            ),
        )
        (service / "cgroup.procs").write_text("999\n", encoding="ascii")
        expect_code(
            "RESUME_SYSTEMD_RUNTIME_DRIFT",
            lambda: v2.systemd_process_listener_proof(
                main_pid=123, control_group="/system.slice/log-platform-api.service",
                proc_root=proc_root, cgroup_root=cgroup_root,
            ),
        )
        (service / "cgroup.procs").write_text("123\n", encoding="ascii")
        (main_fd / "5").unlink()
        other_fd = proc_root / "999/fd"
        other_fd.mkdir(parents=True)
        (other_fd / "7").symlink_to("socket:[777]")
        expect_code(
            "RESUME_SYSTEMD_RUNTIME_DRIFT",
            lambda: v2.systemd_process_listener_proof(
                main_pid=123, control_group="/system.slice/log-platform-api.service",
                proc_root=proc_root, cgroup_root=cgroup_root,
            ),
        )


def test_systemd_collector_derives_every_convergence_component():
    show = (
        "MainPID=123\nInvocationID=" + "d" * 32
        + "\nNeedDaemonReload=no\nFragmentPath=/unit\nDropInPaths=/dropin\n"
        + "ActiveState=active\nSubState=running\n"
        + "ControlGroup=/system.slice/log-platform-api.service\n"
    )
    proof = {
        "control_group": "/system.slice/log-platform-api.service",
        "process_members": [123], "main_pid_present": True,
        "uniqueness_rule": "service_cgroup_contains_exactly_MainPID",
        "unique_main_process": True,
        "listener": {"host": "127.0.0.1", "port": 8001, "owned_by_main_pid": True},
    }
    def run(command, **kwargs):
        return show if command[:2] == ["systemctl", "show"] else "EnvironmentFile=/etc/log-platform/environment-identity.env\n"
    with patch.object(v2, "_run", side_effect=run), \
         patch.object(v2, "_file_metadata", return_value={"path": "/dropin"}), \
         patch.object(v2, "_process_identity", return_value="production"), \
         patch.object(v2, "canonical_source_effective", return_value=True), \
         patch.object(v2, "systemd_process_listener_proof", return_value=proof), \
         patch.object(v2, "_health", return_value={"passed": True}):
        binding = v2.systemd_runtime_binding(expected_environment="production")
    assert all(binding["convergence_components"].values())
    assert binding["listener_ownership"]["owned_by_main_pid"] is True

    changed_show = show.replace("MainPID=123", "MainPID=124").replace(
        "InvocationID=" + "d" * 32,
        "InvocationID=" + "e" * 32,
    )
    changed_proof = dict(proof, process_members=[124])
    changed_proof["listener"] = {
        "host": "127.0.0.1", "port": 8001, "owned_by_main_pid": True,
    }
    with patch.object(v2, "_run", side_effect=lambda command, **kwargs: changed_show if command[:2] == ["systemctl", "show"] else "EnvironmentFile=/etc/log-platform/environment-identity.env\n"), \
         patch.object(v2, "_file_metadata", return_value={"path": "/dropin"}), \
         patch.object(v2, "_process_identity", return_value="production"), \
         patch.object(v2, "canonical_source_effective", return_value=True), \
         patch.object(v2, "systemd_process_listener_proof", return_value=changed_proof), \
         patch.object(v2, "_health", return_value={"passed": True}):
        changed_binding = v2.systemd_runtime_binding(expected_environment="production")
    assert changed_binding["pid"] == 124
    assert changed_binding["invocation_id"] == "e" * 32
    assert changed_binding != binding

    with patch.object(v2, "_run", side_effect=run), \
         patch.object(v2, "_file_metadata", return_value={"path": "/dropin"}), \
         patch.object(v2, "_process_identity", return_value="production"), \
         patch.object(v2, "canonical_source_effective", return_value=True), \
         patch.object(v2, "systemd_process_listener_proof", side_effect=promotion.PromotionError("RESUME_SYSTEMD_RUNTIME_DRIFT", "unrelated listener")), \
         patch.object(v2, "_health", return_value={"passed": True}):
        expect_code(
            "RESUME_SYSTEMD_RUNTIME_DRIFT",
            lambda: v2.systemd_runtime_binding(expected_environment="production"),
        )


def test_runtime_and_execution_contract_failures():
    base = fixture_plan()
    for key in ("pid", "invocation_id", "observed_identity", "need_daemon_reload"):
        changed = deepcopy(base)
        changed["systemd_runtime"][key] = "drift"
        assert v2.drift_classification(base, changed) == "RESUME_SYSTEMD_RUNTIME_DRIFT"
    for key in ("container_id", "image_digest", "compose_configuration_fingerprint", "observed_identity"):
        changed = deepcopy(base)
        changed["docker_runtime"][key] = "drift"
        assert v2.drift_classification(base, changed) == "RESUME_DOCKER_RUNTIME_DRIFT"
    duplicate = iter(["a" * 64 + "\n" + "b" * 64])
    with patch.object(v2, "_run", side_effect=lambda *args, **kwargs: next(duplicate)):
        expect_code("RESUME_DOCKER_RUNTIME_DRIFT", lambda: v2.docker_runtime_binding(repository_root=Path("/repo"), expected_environment="production"))
    changed = deepcopy(base)
    changed["journal_state"]["state"] = "failed"
    expect_code("RESUME_JOURNAL_STATE_DRIFT", lambda: v2.validate_plan(changed))
    changed = deepcopy(base)
    changed["runtime_convergence"]["reload_required_consumers"] = ["systemd_api"]
    expect_code("RESUME_IMMUTABLE_BINDING_DRIFT", lambda: v2.validate_plan(changed))
    changed = deepcopy(base)
    changed["remaining_execution_contract"]["prohibited_actions"].pop()
    expect_code("RESUME_EXECUTION_CONTRACT_DRIFT", lambda: v2.validate_plan(changed))


def _exercise_executor(*, fail_after: str | None = None,
                       audit_columns: tuple[str, ...] = promotion.RESUME_AUDIT_COLUMNS):
    steps = [
        "client_marker:TEST00001", promotion.STEP_CONTROL_PLANE,
        promotion.STEP_PLATFORM_MARKER, promotion.STEP_RUNTIME_FILE,
        promotion.STEP_RUNTIME_RELOAD, promotion.STEP_RUNTIME_PROCESSES,
        promotion.STEP_FINAL_VERIFY,
    ]
    plan = fixture_plan()
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
    v2.validate_plan(plan)
    digest = promotion.plan_hash(plan)
    exact_attestation = v2.attestation(plan)
    args = SimpleNamespace(
        attestation=exact_attestation,
        resume_plan_sha256=digest,
        promotion_id=PROMOTION_ID,
    )
    route_journal = {
        "state": "in_progress",
        "completed_steps": steps[:4],
        "current_step": promotion.STEP_RUNTIME_RELOAD,
    }
    original_plan = {"steps": steps}
    calls: list[str] = []
    collect_count = 0

    open_count = 0

    class FakeCursor:
        def __enter__(self): return self
        def __exit__(self, *unused): return False

        def execute(self, query, params=None):
            assert "information_schema.columns" in " ".join(str(query).split())
            calls.append("resume_audit_schema_probe")

        def fetchall(self):
            return [{"column_name": name} for name in audit_columns]

    class FakeConnection:
        def __init__(self, label):
            self.label = label
        def __enter__(self):
            calls.append(f"enter:{self.label}")
            return self
        def __exit__(self, *unused):
            self.close()
            return False
        def cursor(self):
            return FakeCursor()
        def close(self):
            calls.append(f"close:{self.label}")
            if fail_after == "write_connection_close_raw" and self.label.startswith("write:"):
                raise OSError("injected write connection close failure")
            if fail_after == "lock_connection_close_raw" and self.label == "read:autocommit":
                raise OSError("injected advisory-lock connection close failure")

    def open_connection(runtime, *, read_only, autocommit=False):
        nonlocal open_count
        open_count += 1
        label = f"{'read' if read_only else 'write'}:{'autocommit' if autocommit else 'transactional'}"
        calls.append(f"open:{label}")
        if fail_after == "fresh_connection_open_raw" and open_count == 8:
            raise psycopg.OperationalError("injected post-finalization connection failure")
        return FakeConnection(label)

    def collect(*unused, **unused_kwargs):
        nonlocal collect_count
        collect_count += 1
        calls.append(f"collect:{collect_count}")
        race_failures = {
            ("remote_before_lock", 1): "RESUME_REMOTE_HEAD_DRIFT",
            ("remote_under_lock", 2): "RESUME_REMOTE_HEAD_DRIFT",
            ("remote_after_first_write", 3): "RESUME_REMOTE_HEAD_DRIFT",
            ("schema_under_lock", 2): "RESUME_SCHEMA_CONTRACT_DRIFT",
            ("schema_after_first_write", 3): "RESUME_SCHEMA_CONTRACT_DRIFT",
        }
        race_code = race_failures.get((str(fail_after), collect_count))
        if race_code:
            raise promotion.PromotionError(
                race_code, "synthetic binding race",
                details={"writes_performed": False},
            )
        if len(audit_columns) != len(promotion.RESUME_AUDIT_COLUMNS):
            raise promotion.PromotionError(
                "RESUME_SCHEMA_CONTRACT_DRIFT",
                "synthetic migration-054 contract drift",
                details={"writes_performed": False},
            )
        if fail_after == "runtime_reload_required" and collect_count == 3:
            raise promotion.PromotionError(
                "TEST_DRIFT", "after reload write",
                details={"writes_performed": False},
            )
        if fail_after == "runtime_processes_verified" and collect_count == 4:
            raise promotion.PromotionError(
                "TEST_DRIFT", "after process write",
                details={"writes_performed": False},
            )
        if fail_after == "pre_write_raw" and collect_count == 1:
            raise psycopg.OperationalError("injected pre-write database failure")
        if fail_after == "runtime_reload_required_raw" and collect_count == 3:
            raise psycopg.OperationalError("injected failure after reload commit")
        if fail_after == "runtime_processes_verified_raw" and collect_count == 4:
            raise RuntimeError("injected failure after process commit")
        return plan, [], route_journal

    def progress(conn, promotion_id, *, completed_step, current_step):
        calls.append(f"write:{completed_step}")
        assert promotion_id == PROMOTION_ID

    def finalize(conn, promotion_id, *, resume_plan_sha256, expected_completed_steps):
        calls.append("write:atomic_finalization")
        assert promotion_id == PROMOTION_ID
        assert resume_plan_sha256 == digest
        assert list(expected_completed_steps) == steps[:-1]
        if fail_after == "inside_atomic_transaction_raw":
            raise psycopg.OperationalError("injected failure inside the atomic transaction")

    def inspect(*unused, **unused_kwargs):
        calls.append("fresh_journal_read")
        if fail_after == "atomic_final_commit":
            raise promotion.PromotionError(
                "TEST_FRESH_READ_FAILED", "after final commit",
                details={"writes_performed": False},
            )
        if fail_after == "fresh_read_raw":
            raise psycopg.OperationalError("injected fresh verification read failure")
        return [{
            "state": "completed", "current_step": None, "completed_at": "observed",
            "resume_contract": "resume-v2", "resume_plan_sha256": digest,
            "completed_steps": steps,
        }]

    def release(conn):
        calls.append("lock_release")
        if fail_after == "lock_release_raw":
            raise RuntimeError("injected advisory unlock failure")

    original_reject = v2.reject_retired_execution
    def reject(**kwargs):
        calls.append("approval_validation")
        return original_reject(**kwargs)

    with ExitStack() as stack:
        stack.enter_context(patch.object(cli, "_repository_state", return_value=SimpleNamespace(head=RESUME_HEAD)))
        stack.enter_context(patch.object(cli, "_validate_scope", return_value={"TEST00001": CLIENT_UUID}))
        stack.enter_context(patch.object(cli, "_require_arguments", return_value=None))
        stack.enter_context(patch.object(cli, "_runtime", return_value=SimpleNamespace()))
        stack.enter_context(patch.object(cli, "_resume_plan", return_value=(original_plan, [], route_journal)))
        stack.enter_context(patch.object(cli, "_collect_resume_v2_plan", side_effect=collect))
        stack.enter_context(patch.object(cli, "_platform_conn", side_effect=open_connection))
        stack.enter_context(patch.object(v2, "reject_retired_execution", side_effect=reject))
        stack.enter_context(patch.object(promotion, "try_promotion_lock", side_effect=lambda conn: (calls.append("lock_acquire") or True)))
        stack.enter_context(patch.object(promotion, "release_promotion_lock", side_effect=release))
        stack.enter_context(patch.object(promotion, "journal_resume_progress_v2", side_effect=progress))
        stack.enter_context(patch.object(promotion, "journal_finalize_resume_v2", side_effect=finalize))
        stack.enter_context(patch.object(promotion, "inspect_journals", side_effect=inspect))
        stack.enter_context(patch.object(cli, "_emit", side_effect=lambda value: calls.append("emit_success")))
        try:
            result = cli._execute_resume_v2(args)
        except promotion.PromotionError as exc:
            return calls, exc
    return calls, result


def test_executor_reaches_lock_write_connection_and_atomic_flow_in_recorded_order():
    calls, result = _exercise_executor()
    assert result == promotion.EXIT_OK
    required_order = [
        "open:read:transactional",
        "approval_validation",
        "collect:1",
        "open:read:autocommit",
        "lock_acquire",
        "open:read:transactional",
        "collect:2",
        "open:write:transactional",
        "write:runtime_reload_required",
        "collect:3",
        "write:runtime_processes_verified",
        "collect:4",
        "write:atomic_finalization",
        "fresh_journal_read",
        "emit_success",
    ]
    positions = []
    cursor = 0
    for item in required_order:
        position = calls.index(item, cursor)
        positions.append(position)
        cursor = position + 1
    assert positions == sorted(positions), calls
    assert calls.index("lock_acquire") < calls.index("open:write:transactional")
    assert calls.count("write:atomic_finalization") == 1


def test_resume_v2_refuses_before_any_write_when_migration_054_is_absent():
    for audit_columns in ((), ("resume_contract",), ("resume_plan_sha256",)):
        calls, exc = _exercise_executor(audit_columns=audit_columns)
        assert isinstance(exc, promotion.PromotionError), audit_columns
        expected = (
            "RESUME_V2_AUDIT_SCHEMA_REQUIRED" if not audit_columns
            else "ENVIRONMENT_IDENTITY_RESUME_AUDIT_SCHEMA_INCOMPLETE"
        )
        assert exc.code == expected, (audit_columns, exc.code)
        assert exc.exit_code == promotion.EXIT_PRECONDITION
        assert exc.details["writes_performed"] is False
        assert exc.details["reconciliation_required"] is False
        assert exc.details["committed_journal_writes"] == []
        assert exc.details["finalization_may_have_committed"] is False
        assert exc.details["execution_phase"] == "resume_audit_schema_gate"
        # No write connection or advisory lock may occur.
        assert not any(item.startswith("write:") for item in calls), calls
        assert "lock_acquire" not in calls


def test_partial_write_reporting_overrides_stale_inner_details():
    for failure_point, committed_call in (
        ("runtime_reload_required", "write:runtime_reload_required"),
        ("runtime_processes_verified", "write:runtime_processes_verified"),
        ("atomic_final_commit", "write:atomic_finalization"),
    ):
        calls, exc = _exercise_executor(fail_after=failure_point)
        assert isinstance(exc, promotion.PromotionError)
        assert committed_call in calls
        assert exc.details["writes_performed"] is True
        assert exc.exit_code == promotion.EXIT_PARTIAL


def test_remote_and_schema_races_report_exact_phase_and_write_truth():
    cases = (
        ("remote_before_lock", "RESUME_REMOTE_HEAD_DRIFT",
         "approved_plan_collection", False),
        ("remote_under_lock", "RESUME_REMOTE_HEAD_DRIFT",
         "prewrite_binding_reverification", False),
        ("schema_under_lock", "RESUME_SCHEMA_CONTRACT_DRIFT",
         "prewrite_binding_reverification", False),
        ("remote_after_first_write", "RESUME_REMOTE_HEAD_DRIFT",
         "runtime_process_reverification", True),
        ("schema_after_first_write", "RESUME_SCHEMA_CONTRACT_DRIFT",
         "runtime_process_reverification", True),
    )
    for mode, code, phase, writes in cases:
        calls, exc = _exercise_executor(fail_after=mode)
        assert isinstance(exc, promotion.PromotionError), mode
        assert exc.code == code, mode
        assert exc.details["execution_phase"] == phase, (mode, exc.details)
        assert exc.details["writes_performed"] is writes, mode
        assert exc.details["reconciliation_required"] is writes, mode
        assert exc.exit_code == (
            promotion.EXIT_PARTIAL if writes else promotion.EXIT_PRECONDITION
        ), mode
        assert ("write:runtime_reload_required" in calls) is writes, calls


def test_raw_exception_before_first_write_reports_no_writes():
    calls, exc = _exercise_executor(fail_after="pre_write_raw")
    assert isinstance(exc, promotion.PromotionError)
    assert exc.code == "RESUME_V2_EXECUTION_INTERRUPTED"
    assert exc.exit_code == promotion.EXIT_PRECONDITION
    assert exc.details["writes_performed"] is False
    assert exc.details["reconciliation_required"] is False
    assert exc.details["committed_journal_writes"] == []
    assert exc.details["finalization_may_have_committed"] is False
    assert exc.details["original_exception_class"] == "OperationalError"
    assert exc.details["execution_phase"] == "approved_plan_collection"
    assert not any(item.startswith("write:") for item in calls)


def test_raw_exception_after_every_write_boundary_reports_partial():
    expectations = (
        ("runtime_reload_required_raw", "write:runtime_reload_required",
         "runtime_process_reverification", "OperationalError", False),
        ("runtime_processes_verified_raw", "write:runtime_processes_verified",
         "final_cross_surface_verification", "RuntimeError", False),
        ("inside_atomic_transaction_raw", "write:atomic_finalization",
         "journal_write:atomic_finalization", "OperationalError", True),
        ("fresh_connection_open_raw", "write:atomic_finalization",
         "fresh_journal_verification", "OperationalError", True),
        ("fresh_read_raw", "write:atomic_finalization",
         "fresh_journal_verification", "OperationalError", True),
    )
    for mode, committed_call, phase, exception_class, finalization in expectations:
        calls, exc = _exercise_executor(fail_after=mode)
        assert isinstance(exc, promotion.PromotionError), mode
        assert exc.code == "RESUME_V2_EXECUTION_INTERRUPTED", mode
        assert exc.exit_code == promotion.EXIT_PARTIAL, mode
        assert exc.details["writes_performed"] is True, mode
        assert exc.details["reconciliation_required"] is True, mode
        assert exc.details["execution_phase"] == phase, (mode, exc.details)
        assert exc.details["original_exception_class"] == exception_class, mode
        assert exc.details["finalization_may_have_committed"] is finalization, mode
        assert committed_call in calls, mode
        assert "injected" in str(exc)
        # The advisory lock is still released even though the write failed.
        assert "lock_release" in calls, mode


def test_cleanup_failure_after_finalization_preserves_durable_state():
    for mode, operation in (
        ("lock_release_raw", "advisory_lock_release"),
        ("write_connection_close_raw", "journal_write_connection_close"),
        ("lock_connection_close_raw", "advisory_lock_connection_close"),
    ):
        calls, exc = _exercise_executor(fail_after=mode)
        assert isinstance(exc, promotion.PromotionError), mode
        assert exc.code == "RESUME_V2_CLEANUP_FAILED", mode
        assert exc.exit_code == promotion.EXIT_PARTIAL, mode
        assert exc.details["writes_performed"] is True, mode
        assert exc.details["reconciliation_required"] is True, mode
        assert "write:atomic_finalization" in calls, mode
        operations = [row["operation"] for row in exc.details["cleanup_failures"]]
        assert operation in operations, (mode, operations)
        # A cleanup failure never hides or downgrades the durable completed state.
        assert exc.details["journal_state"] == "completed", mode
        assert exc.details["committed_journal_writes"][-1] == "atomic_finalization", mode
        assert "fresh_journal_read" in calls, mode
        assert "emit_success" not in calls, mode


def test_interrupted_reporting_is_structured_and_secret_free():
    class Failing:
        def __init__(self, message):
            self.message = message
        def __str__(self):
            return self.message

    redacted = cli._sanitized_exception_detail(
        RuntimeError(
            "connection failed dsn=postgresql://admin:s3cr3t@127.0.0.1:5432/logdb "
            "password=hunter2 token: abc123"
        )
    )
    assert "s3cr3t" not in redacted and "hunter2" not in redacted and "abc123" not in redacted
    assert "<redacted" in redacted

    _calls, exc = _exercise_executor(fail_after="fresh_read_raw")
    buffer = io.StringIO()
    with patch.object(cli, "cli", side_effect=exc), redirect_stdout(buffer):
        code = cli.main()
    payload = json.loads(buffer.getvalue())
    assert code == promotion.EXIT_PARTIAL
    assert payload["classification"] == "RESUME_V2_EXECUTION_INTERRUPTED"
    assert payload["writes_performed"] is True
    assert payload["reconciliation_required"] is True
    assert payload["execution_phase"] == "fresh_journal_verification"
    assert payload["committed_journal_writes"][-1] == "atomic_finalization"


def test_docker_convergence_components_are_derived_from_evidence():
    baseline = dict(
        observed_state="running", active_count=1, canonical_source=True,
        observed_identity="production", expected_environment="production",
        health_passed=True,
    )
    assert v2.docker_convergence_components(**baseline) == {
        "running": True, "unique_active_api_container": True,
        "canonical_source": True, "identity_matches": True, "health_passed": True,
    }
    for override, key in (
        ({"observed_state": "paused"}, "running"),
        ({"active_count": 2}, "unique_active_api_container"),
        ({"canonical_source": False}, "canonical_source"),
        ({"observed_identity": "local_dev"}, "identity_matches"),
        ({"health_passed": False}, "health_passed"),
    ):
        components = v2.docker_convergence_components(**{**baseline, **override})
        assert components[key] is False, override
        assert not all(components.values()), override

    def binding(*, container_id, digest, fingerprint, status, identity, count=1):
        ids = "\n".join(container_id for _ in range(count))
        inspect_payload = json.dumps([{
            "Id": container_id, "Image": digest,
            "State": {"Status": status},
            "Config": {
                "Image": "test-api",
                "Labels": {
                    "com.docker.compose.project": "test",
                    "com.docker.compose.service": "api",
                    "com.docker.compose.project.working_dir": "/repo",
                    "com.docker.compose.project.config_files": "/repo/docker-compose.yml",
                },
            },
        }])
        config = json.dumps({"services": {"api": {
            "environment": {promotion.TARGET_ENVIRONMENT_KEY: "production"},
        }}})

        def run(command, **unused):
            if command[:3] == ["docker", "compose", "ps"]:
                return ids + "\n"
            if command[:2] == ["docker", "inspect"]:
                return inspect_payload
            if command[-2:] == ["--hash", "api"]:
                return f"api {fingerprint}\n"
            return config

        with patch.object(v2, "_run", side_effect=run), \
             patch.object(v2, "resolved_path", side_effect=lambda value, label: str(value)), \
             patch.object(v2, "_docker_environment", return_value=identity), \
             patch.object(Path, "read_text", return_value="path: /etc/log-platform/environment-identity.env"), \
             patch.object(v2, "_health", return_value={"probe": "docker", "passed": True}):
            return v2.docker_runtime_binding(
                repository_root=Path("/repo"), expected_environment="production",
            )

    healthy_arguments = dict(
        container_id="a" * 64, digest="sha256:" + "b" * 64,
        fingerprint="c" * 64, status="running", identity="production",
    )
    healthy = binding(**healthy_arguments)
    assert healthy["convergence_components"] == {
        "running": True, "unique_active_api_container": True,
        "canonical_source": True, "identity_matches": True, "health_passed": True,
    }
    assert healthy["active_api_container_count"] == 1
    assert healthy["duplicate_active_container"] is False
    assert healthy["observed_container_state"] == "running"

    changed = binding(
        container_id="d" * 64, digest="sha256:" + "e" * 64,
        fingerprint="f" * 64, status="running", identity="production",
    )
    assert changed["container_id"] != healthy["container_id"]
    assert changed["image_digest"] != healthy["image_digest"]
    assert changed != healthy

    for override in (
        {"status": "paused"},
        {"identity": "local_dev"},
        {"count": 2},
    ):
        expect_code(
            "RESUME_DOCKER_RUNTIME_DRIFT",
            lambda override=override: binding(**{**healthy_arguments, **override}),
        )


def test_neither_executor_can_create_the_finalization_dead_end():
    forward_source = inspect.getsource(cli._execute)
    resume_source = inspect.getsource(cli._execute_resume_v2)
    promotion_source = inspect.getsource(promotion)

    # The split append-then-complete primitive no longer exists at all, so no
    # executable path can leave all steps complete while state is in_progress.
    assert not hasattr(promotion, "journal_complete")
    assert "journal_complete(" not in promotion_source
    assert "journal_complete(" not in forward_source
    assert "journal_complete(" not in resume_source

    assert forward_source.count("promotion.journal_finalize_forward_v5(") == 1
    assert resume_source.count("promotion.journal_finalize_resume_v2(") == 1
    assert "journal_finalize_resume_v2(" not in forward_source
    assert "journal_finalize_forward_v5(" not in resume_source

    # Forward finalization stays schema-053 compatible: it must not touch the
    # migration-054 resume audit columns.
    forward_atomic = inspect.getsource(promotion.journal_finalize_forward_v5)
    assert "resume_contract" not in forward_atomic
    assert "resume_plan_sha256" not in forward_atomic
    assert "state='completed'" in forward_atomic
    assert "current_step=NULL" in forward_atomic
    assert forward_atomic.count("cur.execute(") == 1
    assert forward_atomic.count("with conn.transaction():") == 1

    for atomic in (
        inspect.getsource(promotion.journal_finalize_forward_v5),
        inspect.getsource(promotion.journal_finalize_resume_v2),
    ):
        assert "AND state='in_progress'" in atomic
        assert f"AND current_step=%s" in atomic
        assert "AND completed_steps=%s::jsonb" in atomic
        assert "cur.rowcount != 1" in atomic

    # The forward executor sets final_verification as current_step and then
    # performs exactly one committed transition, never two.
    forward_tail = forward_source[forward_source.index("STEP_FINAL_VERIFY"):]
    assert forward_tail.count("promotion.journal_step(") == 1
    assert forward_tail.index("journal_step(") < forward_tail.index("journal_finalize_forward_v5(")


def test_forward_v5_atomic_finalization_call_path():
    steps = [
        "client_marker:TEST00001", promotion.STEP_CONTROL_PLANE,
        promotion.STEP_PLATFORM_MARKER, promotion.STEP_RUNTIME_FILE,
        promotion.STEP_RUNTIME_RELOAD, promotion.STEP_RUNTIME_PROCESSES,
        promotion.STEP_FINAL_VERIFY,
    ]
    idle = SimpleNamespace(info=SimpleNamespace(transaction_status="IDLE"))

    # The contract guard refuses any prefix that is not the exact pre-final set,
    # before it ever opens a transaction.
    for bad in ([], steps, steps[:4], steps[:-1] + [promotion.STEP_FINAL_VERIFY]):
        expect_code(
            "PROMOTION_EXECUTION_CONTRACT_DRIFT",
            lambda bad=bad: promotion.journal_finalize_forward_v5(
                idle, PROMOTION_ID, expected_completed_steps=bad,
            ),
        )

    # The forward executor binds the exact frozen pre-final prefix from the plan.
    recorded = {}

    def finalize(conn, promotion_id, *, expected_completed_steps):
        recorded["promotion_id"] = promotion_id
        recorded["expected"] = list(expected_completed_steps)

    forward_source = inspect.getsource(cli._execute)
    call_site = forward_source[forward_source.index("journal_finalize_forward_v5("):]
    assert 'expected_completed_steps=[str(step) for step in plan["steps"][:-1]]' in call_site

    with patch.object(promotion, "journal_finalize_forward_v5", side_effect=finalize):
        promotion.journal_finalize_forward_v5(
            idle, PROMOTION_ID,
            expected_completed_steps=[str(step) for step in steps[:-1]],
        )
    assert recorded["promotion_id"] == PROMOTION_ID
    assert recorded["expected"] == steps[:-1]
    assert recorded["expected"][-1] == promotion.STEP_RUNTIME_PROCESSES
    assert promotion.STEP_FINAL_VERIFY not in recorded["expected"]


def test_no_forbidden_reachability_and_read_only_discipline():
    resume_source = inspect.getsource(v2)
    executor_source = inspect.getsource(cli._execute_resume_v2)
    forbidden_module_tokens = (
        "from jobs", "import jobs", "provider_client.", "selenium.",
        "smtplib.", "imaplib.", "snapshot.sh", "backup.sh", "prune.sh",
    )
    assert not any(token in resume_source for token in forbidden_module_tokens)
    forbidden_executor_calls = (
        "mutate_client_marker_durably(", "update_control_plane(",
        "update_platform_marker(", "invoke_identity_helper(",
        "atomic_update_runtime_file(", "systemctl restart", "force-recreate",
    )
    assert not any(token in executor_source for token in forbidden_executor_calls)
    assert "read_only=True" in executor_source
    assert executor_source.index("prewrite_plan") < executor_source.index("read_only=False")
    assert list(v2.PROHIBITED_ACTIONS) == sorted(set(v2.PROHIBITED_ACTIONS))
    assert [row["order"] for row in fixture_plan()["remaining_execution_contract"]["allowed_actions"]] == list(range(1, 10))

    generic_source = inspect.getsource(cli._execute)
    assert "_resume_command" not in generic_source
    assert "contract=resume-v1" not in generic_source
    assert not hasattr(cli, "_resume_command")
    command_args = SimpleNamespace(
        from_environment="local_dev", to_environment="production",
        platform_uuid=PLATFORM_UUID, client_code=["BRAVO00016"],
        expected_client_db_uuid=[f"BRAVO00016={CLIENT_UUID}"],
        runtime_environment_file=Path("/canonical"),
        backup_reference=Path("/checkpoint"), recovery_root=Path("/recovery"),
        preserved_recovery_backup=Path("/backup"), recovery_evidence=Path("/evidence"),
    )
    phase_one = cli._resume_plan_command(command_args, PROMOTION_ID)
    assert "--resume-plan" in phase_one
    assert "--execute" not in phase_one and "--resume-plan-sha256" not in phase_one
    phase_two = cli._resume_v2_command(command_args, PROMOTION_ID, "a" * 64, "exact")
    assert "--execute" in phase_two and "--resume-plan-sha256" in phase_two
    assert "--attestation exact" in phase_two

    columns = v2.remaining_execution_contract(PROMOTION_ID)
    writable = set(columns["writable_journal_columns"])
    immutable = set(columns["immutable_journal_columns"])
    assert not writable & immutable
    assert writable | immutable == {
        "promotion_id", "source_environment", "target_environment",
        "platform_identity_id", "selected_clients", "immutable_plan_json",
        "plan_sha256", "state", "started_at", "completed_at", "failed_at",
        "current_step", "completed_steps", "error", "operator_attestation_hash",
        "backup_reference", "runtime_file_backup_path", "runtime_file_before_sha256",
        "runtime_file_after_sha256", "created_at", "updated_at", "resume_contract",
        "resume_plan_sha256",
    }


def main():
    test_schema_determinism_normalization_and_attestation()
    test_locale_stable_sudo_policy()
    test_sudo_command_list_tokenizer()
    test_sudo_command_classification_is_fail_closed()
    test_sudo_policy_refuses_broad_and_unresolved_helper_rules()
    test_sudo_policy_fingerprint_is_structure_sensitive()
    test_sudo_policy_accepts_representative_host_output()
    test_dual_head_and_assets()
    test_retirement_missing_approval_and_no_fallback()
    test_state_aware_pre_runtime_routing()
    test_category_drift_classifications()
    test_security_binding_field_hash_mutations()
    test_target_promotion_id_is_canonical_and_hash_bound()
    test_schema_catalog_query_errors_use_precise_classification()
    test_schema_absence_sentinels_are_exact_and_fail_closed()
    test_health_ports_fail_closed_and_readiness_split()
    test_systemd_cgroup_and_listener_ownership_proof()
    test_systemd_collector_derives_every_convergence_component()
    test_runtime_and_execution_contract_failures()
    test_executor_reaches_lock_write_connection_and_atomic_flow_in_recorded_order()
    test_resume_v2_refuses_before_any_write_when_migration_054_is_absent()
    test_partial_write_reporting_overrides_stale_inner_details()
    test_remote_and_schema_races_report_exact_phase_and_write_truth()
    test_raw_exception_before_first_write_reports_no_writes()
    test_raw_exception_after_every_write_boundary_reports_partial()
    test_cleanup_failure_after_finalization_preserves_durable_state()
    test_interrupted_reporting_is_structured_and_secret_free()
    test_docker_convergence_components_are_derived_from_evidence()
    test_neither_executor_can_create_the_finalization_dead_end()
    test_forward_v5_atomic_finalization_call_path()
    test_no_forbidden_reachability_and_read_only_discipline()
    print("resume plan v2 pure tests: OK")


if __name__ == "__main__":
    main()
