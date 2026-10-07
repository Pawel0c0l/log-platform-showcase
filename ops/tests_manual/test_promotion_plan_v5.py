#!/usr/bin/env python3
"""Focused repository-only tests for immutable forward promotion contract v5."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ops import environment_identity_promotion as promotion
from ops import promote_environment_identity as cli
from ops import promotion_plan_v5 as v5


PROMOTION_ID = "7137fc76-e5e7-49df-8f7a-13570a336f27"


def expect_code(code, fn):
    try:
        fn()
    except promotion.PromotionError as exc:
        assert exc.code == code, exc
    else:
        raise AssertionError(f"expected {code}")


def test_sudo_policy_is_structural_and_fingerprinted():
    output = "header\n    " + v5.HELPER_RULE + "\n"
    with patch.object(v5.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=output)):
        binding = v5.effective_sudo_policy_binding()
    assert binding == {
        "collection_method_version": v5.SUDO_COLLECTION_METHOD,
        "helper_absolute_path": v5.HELPER_PATH,
        "matching_rule_count": 1,
        "effective_rule": v5.HELPER_RULE,
        "nopasswd": True,
        "run_as_user": "root",
        "no_later_conflicting_matching_rule": True,
        "effective_policy_fingerprint": promotion.sha256_bytes(output.rstrip("\n").encode()),
    }
    conflict = output + f"    (root) PASSWD: {v5.HELPER_PATH} *\n"
    with patch.object(v5.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=conflict)):
        expect_code("SUDO_POLICY_DRIFT", v5.effective_sudo_policy_binding)


def test_implementation_assets_are_sorted_hashed_and_fail_closed():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        paths = ("ops/z.py", "ops/a.py")
        for relative in paths:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(relative, encoding="utf-8")
        with patch.object(v5, "IMPLEMENTATION_ASSET_PATHS", paths):
            first = v5.implementation_asset_bindings(root)
            assert [row["path"] for row in first] == ["ops/a.py", "ops/z.py"]
            assert all(row["regular_file"] and not row["symlink"] for row in first)
            (root / "ops/z.py").write_text("changed", encoding="utf-8")
            second = v5.implementation_asset_bindings(root)
            assert promotion.plan_hash({"assets": first}) != promotion.plan_hash({"assets": second})
            (root / "ops/z.py").unlink()
            expect_code("PROMOTION_IMPLEMENTATION_ASSET_DRIFT", lambda: v5.implementation_asset_bindings(root))
            (root / "ops/z.py").symlink_to(root / "ops/a.py")
            expect_code("PROMOTION_IMPLEMENTATION_ASSET_DRIFT", lambda: v5.implementation_asset_bindings(root))


def historical_fixture(root: Path):
    original = {
        "contract_version": 4,
        "promotion_plan_contract_version": 4,
        "source_environment": "local_dev",
        "target_environment": "production",
        "clients": [],
    }
    digest = promotion.plan_hash(original)
    evidence_path = root / "rollback-evidence.json"
    evidence = {
        "schema": "failed_environment_identity_rollback_evidence_v1",
        "promotion_id": PROMOTION_ID,
        "original_promotion_plan_sha256": digest,
        "journal_final_state": "rolled_back",
        "recovery_plan_sha256": "a" * 64,
        "repository_head": "b" * 40,
    }
    evidence_path.write_text(promotion.canonical_json(evidence) + "\n", encoding="utf-8")
    evidence_path.chmod(0o600)
    evidence_sha = promotion.sha256_bytes(evidence_path.read_bytes())
    journal = {
        "promotion_id": PROMOTION_ID,
        "state": "rolled_back",
        "current_step": None,
        "completed_steps": ["historically_inaccurate"],
        "plan_sha256": digest,
        "immutable_plan_json": original,
        "error": f"ROLLBACK_EVIDENCE path={evidence_path} sha256={evidence_sha}",
    }
    return original, journal, evidence_path


def test_historical_v4_and_evidence_are_bound_but_not_forward_executable():
    with tempfile.TemporaryDirectory() as directory:
        original, journal, evidence_path = historical_fixture(Path(directory))
        row = v5._historical_evidence_binding(journal=journal, original_plan=original)
        assert row["promotion_id"] == PROMOTION_ID
        assert row["original_contract"] == "v4"
        assert row["final_journal_state"] == "rolled_back"
        assert row["current_step"] is None
        assert row["rollback_direction"] == "rollback_to_local_dev"
        assert row["journal_evidence_reference_match"] is True
        assert row["historical_completed_steps"]["authority"] == "historical_evidence_only"
        expect_code("PROMOTION_PLAN_CONTRACT_SUPERSEDED", lambda: promotion.promotion_attestation(original))
        before = row["rollback_evidence_sha256"]
        evidence_path.write_text(evidence_path.read_text() + " ", encoding="utf-8")
        expect_code("PROMOTION_HISTORY_DRIFT", lambda: v5._historical_evidence_binding(journal=journal, original_plan=original))
        assert promotion.sha256_bytes(evidence_path.read_bytes()) != before
        evidence_path.unlink()
        expect_code("PROMOTION_HISTORY_DRIFT", lambda: v5._historical_evidence_binding(journal=journal, original_plan=original))


def test_v5_drift_classifications_and_exclusion_determinism():
    base = {
        "contract_version": 5,
        "promotion_plan_contract_version": 5,
        "effective_sudo_policy": {"effective_policy_fingerprint": "a" * 64},
        "implementation_assets": [{"path": "a", "sha256": "b" * 64}],
        "historical_recovery": {"entries": []},
        "excluded_actions": list(v5.EXCLUDED_ACTIONS),
    }
    assert base["excluded_actions"] == sorted(base["excluded_actions"])
    for key, code in (
        ("effective_sudo_policy", "SUDO_POLICY_DRIFT"),
        ("implementation_assets", "PROMOTION_IMPLEMENTATION_ASSET_DRIFT"),
        ("historical_recovery", "PROMOTION_HISTORY_DRIFT"),
    ):
        changed = json.loads(promotion.canonical_json(base))
        changed[key] = {"drift": True}
        assert v5.binding_drift_code(base, changed) == code
        assert promotion.plan_hash(base) != promotion.plan_hash(changed)
    changed = json.loads(promotion.canonical_json(base))
    changed["excluded_actions"].pop()
    assert v5.binding_drift_code(base, changed) == "PROMOTION_EXCLUSION_CONTRACT_DRIFT"
    assert promotion.plan_hash(base) != promotion.plan_hash(changed)
    for version in (3, 4):
        changed = dict(base)
        changed["contract_version"] = version
        changed["promotion_plan_contract_version"] = version
        expect_code("PROMOTION_PLAN_CONTRACT_SUPERSEDED", lambda changed=changed: promotion.promotion_attestation(changed))
    assert set(v5.EXCLUDED_ACTIONS) == {
        "worker_activation", "email_activation", "email_send", "smtp_access", "imap_access",
        "alpha_backfill", "snapshot_recalculation", "schedule_change", "business_data_mutation",
        "unrelated_schema_change", "systemd_api_restart_during_initial_promotion",
        "docker_api_recreation_during_initial_promotion", "timer_restart", "prune_invocation",
        "backup_invocation", "systemctl_daemon_reload", "docker_daemon_restart",
        "recovery_evidence_cleanup", "recovery_artifact_removal", "uuid_generation",
        "uuid_change", "manual_direct_client_marker_update", "automatic_retry",
        "automatic_resume", "automatic_rollback",
    }


def test_execute_static_gate_classifies_drift_before_journal_creation():
    history = {
        "historical_row_count": 1, "total_journal_row_count": 1,
        "active_promotion_row_count": 0, "incomplete_recovery_row_count": 0,
        "rolled_back_rows_excluded_from_active_blocking": True,
        "rolled_back_rows_cryptographically_bound_as_history": True,
        "entries": [{"promotion_id": PROMOTION_ID}],
    }
    sudo = {"effective_policy_fingerprint": "a" * 64}
    assets = [{"path": "ops/x.py", "sha256": "b" * 64}]
    plan = {
        "contract_version": 5, "promotion_plan_contract_version": 5,
        "repository_head": "c" * 40, "effective_sudo_policy": sudo,
        "implementation_assets": assets, "historical_recovery": history,
        "excluded_actions": list(v5.EXCLUDED_ACTIONS),
    }
    state = SimpleNamespace(head="c" * 40)
    with patch.object(cli, "_require_plan_repository"), \
         patch.object(v5, "effective_sudo_policy_binding", return_value=sudo), \
         patch.object(v5, "implementation_asset_bindings", return_value=assets), \
         patch.object(v5, "historical_recovery_bindings", return_value=history):
        cli._validate_v5_static_bindings(plan, platform_conn=object(), repository_state=state, resuming=False)
        with patch.object(v5, "effective_sudo_policy_binding", return_value={"effective_policy_fingerprint": "d" * 64}):
            expect_code("SUDO_POLICY_DRIFT", lambda: cli._validate_v5_static_bindings(plan, platform_conn=object(), repository_state=state, resuming=False))
        with patch.object(v5, "implementation_asset_bindings", return_value=[]):
            expect_code("PROMOTION_IMPLEMENTATION_ASSET_DRIFT", lambda: cli._validate_v5_static_bindings(plan, platform_conn=object(), repository_state=state, resuming=False))
        changed_history = dict(history); changed_history["entries"] = []
        with patch.object(v5, "historical_recovery_bindings", return_value=changed_history):
            expect_code("PROMOTION_HISTORY_DRIFT", lambda: cli._validate_v5_static_bindings(plan, platform_conn=object(), repository_state=state, resuming=False))
        changed_exclusions = dict(plan); changed_exclusions["excluded_actions"] = list(v5.EXCLUDED_ACTIONS[:-1])
        expect_code("PROMOTION_EXCLUSION_CONTRACT_DRIFT", lambda: cli._validate_v5_static_bindings(changed_exclusions, platform_conn=object(), repository_state=state, resuming=False))


def main():
    test_sudo_policy_is_structural_and_fingerprinted()
    test_implementation_assets_are_sorted_hashed_and_fail_closed()
    test_historical_v4_and_evidence_are_bound_but_not_forward_executable()
    test_v5_drift_classifications_and_exclusion_determinism()
    test_execute_static_gate_classifies_drift_before_journal_creation()
    print("promotion plan v5 tests: OK")


if __name__ == "__main__":
    main()
