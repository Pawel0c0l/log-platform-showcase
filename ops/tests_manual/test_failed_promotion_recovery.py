#!/usr/bin/env python3
"""Repository-only regressions for durable promotion and recovery-v2 contracts."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ops import environment_identity_promotion as promotion
from ops import failed_promotion_recovery as recovery


PLATFORM_UUID = "52517750-7438-4558-8490-2736ae4cc629"
BRAVO_UUID = "a41f7fe6-e113-42f7-8789-2dc20b2091d7"
ALPHA_UUID = "5879ec53-e3f6-4a46-89cf-eaae9f57b27e"
PROMOTION_ID = "7137fc76-e5e7-49df-8f7a-13570a336f27"


def expect_code(code, fn):
    try:
        fn()
    except promotion.PromotionError as exc:
        assert exc.code == code, exc
    else:
        raise AssertionError(f"expected {code}")


class FakeConnection:
    def __init__(self):
        self.info = SimpleNamespace(transaction_status=SimpleNamespace(name="IDLE"))
        self.closed = False

    def close(self):
        self.closed = True


class PrimitiveCursor:
    def __init__(self):
        self.query = ""

    def __enter__(self): return self
    def __exit__(self, *args): return False

    def execute(self, query, params=None):
        self.query = str(query)

    def fetchone(self):
        if "to_regprocedure" in self.query:
            return {
                "signature": promotion.CLIENT_PROMOTION_SIGNATURE, "can_execute": True,
                "table_update": False, "column_update": False,
                "database_user": "telematics_user",
            }
        if "FROM ops_control.environment_identity WHERE identity_key='primary'" in self.query:
            return {"environment": "production", "database_uuid": BRAVO_UUID}
        raise AssertionError(self.query)

    def fetchall(self):
        if "promote_environment_identity_v1" in self.query:
            return [{
                "database_uuid": BRAVO_UUID, "old_environment": "local_dev",
                "new_environment": "production", "database_name": "telematics_main",
                "changed_row_count": 1,
            }]
        raise AssertionError(self.query)


class PrimitiveTransaction:
    def __init__(self, conn): self.conn = conn
    def __enter__(self):
        assert self.conn.info.transaction_status.name == "IDLE"
        self.conn.top_level_entries += 1
        self.conn.info.transaction_status.name = "INTRANS"
        return self
    def __exit__(self, exc_type, exc, tb):
        self.conn.info.transaction_status.name = "IDLE"
        if exc_type is None and self.conn.fail_commit:
            raise RuntimeError("simulated commit failure")
        return False


class PrimitiveConnection(FakeConnection):
    def __init__(self, *, fail_commit=False):
        super().__init__()
        self.fail_commit = fail_commit
        self.top_level_entries = 0
        self.rollback_calls = 0
    def transaction(self): return PrimitiveTransaction(self)
    def cursor(self): return PrimitiveCursor()
    def rollback(self):
        self.rollback_calls += 1
        self.info.transaction_status.name = "IDLE"


def test_client_primitive_requires_idle_top_level_and_propagates_commit_failure():
    conn = PrimitiveConnection()
    assert promotion.promote_client_marker(
        conn, expected_database="telematics_main", expected_uuid=BRAVO_UUID,
        expected_user="telematics_user", source="local_dev", target="production",
        promotion_id=PROMOTION_ID, attestation_hash="a" * 64,
    ) == "updated"
    assert conn.top_level_entries == 1
    assert conn.info.transaction_status.name == "IDLE"

    busy = PrimitiveConnection()
    busy.info.transaction_status.name = "INTRANS"
    expect_code("CLIENT_MUTATION_TRANSACTION_NOT_IDLE", lambda: promotion.promote_client_marker(
        busy, expected_database="telematics_main", expected_uuid=BRAVO_UUID,
        expected_user="telematics_user", source="local_dev", target="production",
        promotion_id=PROMOTION_ID, attestation_hash="a" * 64,
    ))

    failed = PrimitiveConnection(fail_commit=True)
    expect_code("CLIENT_MARKER_COMMIT_FAILED", lambda: promotion.promote_client_marker(
        failed, expected_database="telematics_main", expected_uuid=BRAVO_UUID,
        expected_user="telematics_user", source="local_dev", target="production",
        promotion_id=PROMOTION_ID, attestation_hash="a" * 64,
    ))
    assert failed.rollback_calls == 1


def test_durable_client_mutation_closes_then_freshly_verifies():
    mutation = FakeConnection()
    verification = FakeConnection()
    opened = []

    def factory(read_only):
        if read_only:
            assert mutation.closed is True
            opened.append("verification")
            return verification
        opened.append("mutation")
        return mutation

    marker = {"environment": "production", "database_uuid": BRAVO_UUID}
    capability = {"least_privilege_safe": True}
    with patch.object(promotion, "promote_client_marker", return_value="updated") as mutate, \
         patch.object(promotion, "require_read_only_connection") as require_read_only, \
         patch.object(promotion, "marker_snapshot", return_value=marker), \
         patch.object(promotion, "client_promotion_capability", return_value=capability):
        result = promotion.mutate_client_marker_durably(
            open_connection=factory, expected_database="telematics_main",
            expected_uuid=BRAVO_UUID, expected_user="telematics_user",
            expected_client_code="BRAVO00016", source="local_dev",
            target="production", promotion_id=PROMOTION_ID,
            attestation_hash="a" * 64,
        )
    assert opened == ["mutation", "verification"]
    assert mutation.closed and verification.closed
    assert result["result"] == "updated"
    mutate.assert_called_once()
    require_read_only.assert_called_once()


def test_fresh_mismatch_blocks_durable_completion():
    connections = [FakeConnection(), FakeConnection()]
    with patch.object(promotion, "promote_client_marker", return_value="already_completed"), \
         patch.object(promotion, "require_read_only_connection"), \
         patch.object(promotion, "marker_snapshot", return_value={"environment": "local_dev", "database_uuid": BRAVO_UUID}), \
         patch.object(promotion, "client_promotion_capability", return_value={"least_privilege_safe": True}):
        expect_code(
            "CLIENT_POST_COMMIT_DURABILITY_MISMATCH",
            lambda: promotion.mutate_client_marker_durably(
                open_connection=lambda read_only: connections[1 if read_only else 0],
                expected_database="telematics_main", expected_uuid=BRAVO_UUID,
                expected_user="telematics_user", expected_client_code="BRAVO00016",
                source="local_dev", target="production", promotion_id=PROMOTION_ID,
                attestation_hash="b" * 64,
            ),
        )
    assert all(conn.closed for conn in connections)


def failed_journal():
    return {
        "promotion_id": PROMOTION_ID,
        "plan_sha256": "4e3b5643340240c758cf1559b5034c7143276a97d42627cb7a1f9a585163b35e",
        "state": "failed", "current_step": "runtime_environment_file",
        "completed_steps": ["client_marker:BRAVO00016", "client_marker:ALPHA00001", "platform_control_plane", "platform_marker"],
        "error": "BLOCKED_BY_CANONICAL_IDENTITY_PRIVILEGE_PATH: denied",
        "runtime_file_backup_path": None, "runtime_file_before_sha256": None,
    }


def original_plan():
    return {
        "contract_version": 4, "source_environment": "local_dev",
        "target_environment": "production",
        "clients": [
            {"client_code": "BRAVO00016", "database_uuid": BRAVO_UUID},
            {"client_code": "ALPHA00001", "database_uuid": ALPHA_UUID},
        ],
    }


def exact_surfaces():
    return {"surfaces": {
        "client_marker:BRAVO00016": "local_dev",
        "client_marker:ALPHA00001": "local_dev",
        "platform_marker": "production", "runtime_file": "local_dev",
        "control_plane": {"BRAVO00016": "production", "ALPHA00001": "production"},
    }}


def build_plan(root: Path):
    journal = failed_journal()
    original = original_plan()
    reconciliation = recovery.reconcile_promotion_steps(
        journal=journal, plan=original, surfaces=exact_surfaces(),
    )
    return recovery.build_plan(
        repository_root=Path(__file__).resolve().parents[2],
        repository_state=SimpleNamespace(branch="main", head="c" * 40),
        journal=journal, original_plan=original,
        canonical_binding={
            "path": "/etc/log-platform/environment-identity.env",
            "current_value": "local_dev", "current_sha256": "1" * 64,
            "promotion_specific_backup_exists": False,
            "already_equals_rollback_target": True,
        },
        database_binding={
            "platform": {"marker": "production", "database_uuid": PLATFORM_UUID},
            "clients": [
                {"client_code": "BRAVO00016", "marker": "local_dev", "control_plane_environment": "production", "database_uuid": BRAVO_UUID},
                {"client_code": "ALPHA00001", "marker": "local_dev", "control_plane_environment": "production", "database_uuid": ALPHA_UUID},
            ],
        },
        runtime_binding={
            "systemd_api": {"pid": 12, "invocation_id": "inv", "semantic_identity": "local_dev", "health": {"passed": True}},
            "docker_api": {"container_id": "container", "compose_configuration_fingerprint": "2" * 64, "semantic_identity": "local_dev", "health": {"passed": True}},
        },
        privilege_binding={"effective_helper_policy": {"matching_rule_count": 1}},
        checkpoint_binding={"sha256": "3" * 64},
        provisioning_recovery_binding={"evidence": {"sha256": "4" * 64}},
        recovery_root=root, reconciliation=reconciliation, active_promotion_rows=0,
    )


def test_exact_failed_state_reconciliation_and_plan_sensitivity():
    with tempfile.TemporaryDirectory() as directory:
        plan = build_plan(Path(directory) / "recovery")
    assert plan["operation"]["contract"] == recovery.CONTRACT
    assert plan["journal"]["promotion_backup_path"] is None
    assert plan["journal"]["promotion_recovery_evidence_path"] is None
    divergences = [row for row in plan["reconciliation"]["steps"] if row["divergence"]]
    assert {row["step"] for row in divergences} == {"client_marker:BRAVO00016", "client_marker:ALPHA00001"}
    assert "canonical_helper_invocation" in plan["excluded_actions"]
    assert plan["expected_final_state"]["readiness"] == "PRODUCTION_PROMOTION_READY"
    first = promotion.plan_hash(plan)
    changed = json.loads(promotion.canonical_json(plan))
    changed["journal"]["state"] = "in_progress"
    assert promotion.plan_hash(changed) != first
    changed = json.loads(promotion.canonical_json(plan))
    changed["current_durable_database_state"]["platform"]["marker"] = "local_dev"
    assert promotion.plan_hash(changed) != first
    changed = json.loads(promotion.canonical_json(plan))
    changed["runtime"]["systemd_api"]["pid"] = 13
    assert promotion.plan_hash(changed) != first
    assert recovery.attestation(plan) == recovery.attestation(json.loads(promotion.canonical_json(plan)))


def test_v1_rejected_and_exact_v2_accepted():
    with tempfile.TemporaryDirectory() as directory:
        plan = build_plan(Path(directory) / "recovery")
    digest = promotion.plan_hash(plan)
    exact = recovery.attestation(plan)
    recovery.validate_execution_approval(plan, provided_plan_sha256=digest, provided_attestation=exact)
    expect_code("RECOVERY_V1_NON_EXECUTABLE", lambda: recovery.validate_execution_approval(
        plan, provided_plan_sha256=recovery.OLD_AUDIT_PLAN_SHA256,
        provided_attestation="RECOVER_FAILED_ENVIRONMENT_IDENTITY contract=recovery-v1",
    ))
    expect_code("RECOVERY_IMMUTABLE_PLAN_MISMATCH", lambda: recovery.validate_execution_approval(
        plan, provided_plan_sha256="0" * 64, provided_attestation=exact,
    ))
    expect_code("RECOVERY_ATTESTATION_MISMATCH", lambda: recovery.validate_execution_approval(
        plan, provided_plan_sha256=digest, provided_attestation=exact + " drift",
    ))


def test_checksum_bound_evidence_is_atomic():
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        repository = base / "repo"
        repository.mkdir(mode=0o700)
        recovery_root = base / "recovery"
        recovery_root.mkdir(mode=0o700)
        plan = build_plan(recovery_root)
        plan["operation"]["repository_path"] = str(repository)
        result = recovery.write_evidence(
            repository_root=repository, plan=plan,
            payload={"journal_final_state": "rolled_back", "all_uuids": [PLATFORM_UUID, BRAVO_UUID, ALPHA_UUID]},
        )
        path = Path(result["path"])
        assert path.is_file() and not path.is_symlink()
        assert path.stat().st_mode & 0o777 == 0o600
        assert promotion.sha256_bytes(path.read_bytes()) == result["sha256"]
        assert json.loads(path.read_text())["recovery_plan_sha256"] == promotion.plan_hash(plan)


def main():
    test_client_primitive_requires_idle_top_level_and_propagates_commit_failure()
    test_durable_client_mutation_closes_then_freshly_verifies()
    test_fresh_mismatch_blocks_durable_completion()
    test_exact_failed_state_reconciliation_and_plan_sensitivity()
    test_v1_rejected_and_exact_v2_accepted()
    test_checksum_bound_evidence_is_atomic()
    print("failed promotion recovery-v2 tests: OK")


if __name__ == "__main__":
    main()
