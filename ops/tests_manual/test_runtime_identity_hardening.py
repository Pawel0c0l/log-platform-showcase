#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ops import provision_runtime_environment_identity as provision
from ops import remediate_runtime_identity_recovery_permissions as permission_remediation
from ops import remediate_runtime_identity_systemd as remediation
from ops import runtime_identity_readiness as readiness
from ops.runtime_identity_recovery import (
    RecoveryOwner, RecoveryStorageError, RecoveryStore, discover_backup,
    resolve_recovery_owner, sha256_file, validate_preserved_recovery_pair,
    validate_recovery_artifact, validate_recovery_root,
)
from ops.systemd_environment_files import (
    canonical_source_effective, effective_environment_files, merged_dropin_text,
)


def make_git_repo(parent: Path) -> tuple[Path, str]:
    repo = parent / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Identity Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "identity@example.invalid"], cwd=repo, check=True)
    (repo / "tracked").write_text("ok\n")
    subprocess.run(["git", "add", "tracked"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    return repo, head


def current_owner() -> RecoveryOwner:
    return resolve_recovery_owner(environ={}, real_uid=os.getuid(), effective_uid=os.geteuid())


def make_legacy_pair(base: Path, owner: RecoveryOwner, *, plan_sha256: str = "a" * 64) -> tuple[Path, Path, Path]:
    root = base / "recovery"
    root.mkdir(mode=0o700)
    plan_dir = root / plan_sha256
    plan_dir.mkdir(mode=0o700)
    backup = plan_dir / "repository.env.pre-provision.bak"
    backup.write_bytes(b"sensitive-test-value\n")
    backup.chmod(0o600)
    evidence = plan_dir / "recovery-evidence.json"
    payload = {
        "plan_sha256": plan_sha256, "repository_head": "b" * 40,
        "preserved_path": str(backup), "sha256": sha256_file(backup),
        "size": backup.stat().st_size, "source_removed_after_verified_preservation": True,
        "metadata": {"owner": owner.user, "group": owner.group, "mode": "0600"},
    }
    evidence.write_text(json.dumps(payload, sort_keys=True) + "\n")
    evidence.chmod(0o600)
    return root, backup, evidence


def expect_error(code: str, function) -> RecoveryStorageError:
    try:
        function()
    except RecoveryStorageError as exc:
        assert exc.code == code, (exc.code, exc.details)
        return exc
    raise AssertionError(f"expected {code}")


def test_external_recovery_contract() -> None:
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        repo, head = make_git_repo(base)
        recovery = base / "recovery"
        source = repo / ".env"
        source.write_text("PASSWORD=do-not-print\nLOG_PLATFORM_TARGET_ENVIRONMENT=local_dev\n")
        source.chmod(0o600)
        owner = current_owner()
        store = RecoveryStore(
            root=recovery, repository_root=repo, operation="provision",
            plan_sha256="a" * 64, repository_head=head, checkpoint=base / "checkpoint.json",
            checkpoint_sha256="b" * 64, owner=owner,
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            store.prepare()
            row = store.preserve(source, logical_path=Path("/logical/.env"))
            evidence = store.write_evidence(state="prepared")
        assert output.getvalue() == "" and "do-not-print" not in json.dumps(evidence)
        backup = Path(row["backup_path"])
        assert backup.read_bytes() == source.read_bytes()
        assert row["source_sha256"] == row["backup_sha256"] == sha256_file(source)
        assert stat.S_IMODE(recovery.stat().st_mode) == 0o700
        assert stat.S_IMODE(backup.stat().st_mode) == 0o600
        evidence_path = Path(evidence["evidence_path"])
        assert stat.S_IMODE(evidence_path.stat().st_mode) == 0o600
        assert discover_backup(
            evidence_path, logical_source=Path("/logical/.env"), root=recovery,
            repository_root=repo, owner=owner,
        ) == backup
        assert subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo, check=True, capture_output=True, text=True
        ).stdout == "?? .env\n"


def test_owner_contract_across_privilege_transition() -> None:
    owner = current_owner()
    non_root = resolve_recovery_owner(
        environ={}, real_uid=owner.uid, effective_uid=owner.uid
    )
    sudo_root = resolve_recovery_owner(
        environ={"SUDO_USER": owner.user, "SUDO_UID": str(owner.uid), "SUDO_GID": str(owner.gid)},
        real_uid=0, effective_uid=0,
    )
    direct_root = resolve_recovery_owner(environ={}, real_uid=0, effective_uid=0)
    assert non_root == sudo_root == direct_root == owner
    assert provision.resolve_recovery_owner is remediation.resolve_recovery_owner
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        repo, _ = make_git_repo(base)
        root = base / "recovery"
        root.mkdir(mode=0o700)
        assert validate_recovery_root(root, repository_root=repo, owner=non_root) == root
        assert validate_recovery_root(root, repository_root=repo, owner=sudo_root) == root
    for env in (
        {"SUDO_USER": "unexpected", "SUDO_UID": str(owner.uid), "SUDO_GID": str(owner.gid)},
        {"SUDO_USER": owner.user, "SUDO_UID": str(owner.uid + 1), "SUDO_GID": str(owner.gid)},
        {"SUDO_USER": owner.user, "SUDO_UID": str(owner.uid)},
    ):
        expect_error(
            "RECOVERY_SUDO_ORIGIN_INVALID",
            lambda env=env: resolve_recovery_owner(environ=env, real_uid=0, effective_uid=0),
        )


def test_recovery_metadata_rejections_and_bounded_diagnostics() -> None:
    owner = current_owner()
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        repo, _ = make_git_repo(base)
        for mode in (0o755, 0o750, 0o710):
            root = base / f"recovery-{mode:o}"
            root.mkdir(mode=mode)
            root.chmod(mode)
            exc = expect_error(
                "RECOVERY_ROOT_PERMISSIONS",
                lambda root=root: validate_recovery_root(root, repository_root=repo, owner=owner),
            )
            assert exc.details["path"] == str(root)
            assert exc.details["expected_mode"] == "0700"
            assert exc.details["actual_mode"] == f"{mode:04o}"
            assert "sensitive" not in json.dumps(exc.details)
        root = base / "approved"
        root.mkdir(mode=0o700)
        wrong_uid = RecoveryOwner(owner.user, owner.group, owner.uid + 1, owner.gid)
        wrong_gid = RecoveryOwner(owner.user, owner.group, owner.uid, owner.gid + 1)
        expect_error("RECOVERY_ROOT_OWNER_MISMATCH", lambda: validate_recovery_root(root, repository_root=repo, owner=wrong_uid))
        expect_error("RECOVERY_ROOT_OWNER_MISMATCH", lambda: validate_recovery_root(root, repository_root=repo, owner=wrong_gid))
        link = base / "link"
        link.symlink_to(root, target_is_directory=True)
        expect_error("RECOVERY_PATH_SYMLINK", lambda: validate_recovery_root(link, repository_root=repo, owner=owner))
        expect_error("RECOVERY_ROOT_INSIDE_WORKTREE", lambda: validate_recovery_root(repo / "backups", repository_root=repo, owner=owner))


def test_artifact_symlink_mode_hash_and_evidence_binding_rejections() -> None:
    owner = current_owner()
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        repo, _ = make_git_repo(base)
        root, backup, evidence = make_legacy_pair(base, owner)
        binding = validate_preserved_recovery_pair(
            evidence_path=evidence, backup_path=backup, root=root, repository_root=repo, owner=owner,
            expected_evidence_sha256=sha256_file(evidence), expected_backup_sha256=sha256_file(backup),
        )
        assert binding["plan_sha256"] == "a" * 64
        backup.chmod(0o640)
        expect_error(
            "RECOVERY_ARTIFACT_PERMISSIONS",
            lambda: validate_recovery_artifact(backup, root=root, repository_root=repo, owner=owner),
        )
        backup.chmod(0o600)
        expect_error(
            "RECOVERY_ARTIFACT_HASH_MISMATCH",
            lambda: validate_recovery_artifact(
                backup, root=root, repository_root=repo, owner=owner, expected_sha256="0" * 64
            ),
        )
        real_backup = backup
        link_backup = backup.parent / "linked.bak"
        link_backup.symlink_to(real_backup)
        expect_error(
            "RECOVERY_PATH_SYMLINK",
            lambda: validate_recovery_artifact(link_backup, root=root, repository_root=repo, owner=owner),
        )
        link_evidence = evidence.parent / "linked-evidence.json"
        link_evidence.symlink_to(evidence)
        expect_error(
            "RECOVERY_PATH_SYMLINK",
            lambda: validate_recovery_artifact(link_evidence, root=root, repository_root=repo, owner=owner),
        )
        linked_root = base / "linked-root"
        linked_root.mkdir(mode=0o700)
        external_plan = base / "external-plan"
        external_plan.mkdir(mode=0o700)
        (linked_root / ("d" * 64)).symlink_to(external_plan, target_is_directory=True)
        linked_file = linked_root / ("d" * 64) / "backup"
        expect_error(
            "RECOVERY_PATH_SYMLINK",
            lambda: validate_recovery_artifact(linked_file, root=linked_root, repository_root=repo, owner=owner),
        )
        payload = json.loads(evidence.read_text())
        payload["plan_sha256"] = "c" * 64
        evidence.write_text(json.dumps(payload) + "\n")
        evidence.chmod(0o600)
        expect_error(
            "RECOVERY_EVIDENCE_PLAN_MISMATCH",
            lambda: validate_preserved_recovery_pair(
                evidence_path=evidence, backup_path=backup, root=root, repository_root=repo, owner=owner,
                expected_evidence_sha256=sha256_file(evidence), expected_backup_sha256=sha256_file(backup),
            ),
        )


def test_systemd_reset_and_corrected_order() -> None:
    old = ("[Service]\nEnvironmentFile=/etc/log-platform/environment-identity.env\n"
           "[Service]\nEnvironmentFile=\nEnvironmentFile=/etc/log-platform-host.env\n")
    assert effective_environment_files(old) == ["/etc/log-platform-host.env"]
    assert not canonical_source_effective(old)
    corrected = old + "[Service]\nEnvironmentFile=/etc/log-platform/environment-identity.env\n"
    assert effective_environment_files(corrected) == [
        "/etc/log-platform-host.env", "/etc/log-platform/environment-identity.env"
    ]
    assert canonical_source_effective(corrected)
    with tempfile.TemporaryDirectory() as directory:
        dropins = Path(directory)
        (dropins / "90-environment-identity.conf").write_text("[Service]\nEnvironmentFile=/etc/log-platform/environment-identity.env\n")
        (dropins / "override.conf").write_text("[Service]\nEnvironmentFile=\nEnvironmentFile=/etc/log-platform-host.env\n")
        assert not canonical_source_effective(merged_dropin_text(dropins))
        (dropins / "zz-environment-identity.conf").write_text("[Service]\nEnvironmentFile=/etc/log-platform/environment-identity.env\n")
        assert canonical_source_effective(merged_dropin_text(dropins))


def test_process_provenance_never_uses_semantic_equality_alone() -> None:
    configured = datetime.now(timezone.utc)
    old = readiness._probe_row(
        "systemd_api", {"status": "running", "effective_environment": "local_dev",
        "started_at": configured - timedelta(seconds=1), "health": True},
        expected="local_dev", configured=True, configured_at=configured,
    )
    assert old["semantic_identity_matches"] and not old["process_provenance"] and not old["converged"]
    new = readiness._probe_row(
        "systemd_api", {"status": "running", "effective_environment": "local_dev",
        "started_at": configured + timedelta(seconds=1), "health": True},
        expected="local_dev", configured=True, configured_at=configured,
    )
    assert new["converged"]
    assert not readiness._probe_row(
        "docker_api", ("running", "local_dev"), expected="local_dev",
        configured=True, configured_at=configured,
    )["converged"]
    assert all(
        row["refresh_action"] == "none; next invocation"
        for row in readiness.CONSUMERS if row["component"] in {"prune", "backup"}
    )


def test_provision_plan_and_partial_validation() -> None:
    assert all(
        "restart log-platform-prune.timer" not in action and "restart log-backup.timer" not in action
        for action in provision.RELOAD_ACTIONS
    )
    assert provision.API_DROPIN_TARGET.name == "zz-environment-identity.conf"
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        canonical = root / provision.CANONICAL_IDENTITY_FILE.relative_to("/")
        canonical.parent.mkdir(parents=True)
        canonical.write_bytes(provision.render_identity_file("local_dev"))
        canonical.chmod(0o640)
        api_dir = root / "etc/systemd/system/log-platform-api.service.d"
        api_dir.mkdir(parents=True)
        (api_dir / "90-environment-identity.conf").write_text("[Service]\nEnvironmentFile=/etc/log-platform/environment-identity.env\n")
        (api_dir / "override.conf").write_text("[Service]\nEnvironmentFile=\nEnvironmentFile=/etc/log-platform-host.env\n")
        evidence = root / "evidence.json"
        evidence.write_text("{}\n")
        evidence.chmod(0o600)
        plan = {"environment": "local_dev", "canonical_identity_sha256": provision.intended_checksum("local_dev"), "install_assets": []}
        args = SimpleNamespace(root=root)
        with patch.object(provision, "CONFLICT_SOURCES", ()):
            try:
                provision._validate_post_write(args, plan, {"evidence_path": str(evidence)})
            except provision.ProvisioningError as exc:
                assert exc.code == "POST_WRITE_SYSTEMD_CANONICAL_SOURCE_INEFFECTIVE"
            else:
                raise AssertionError("canceled canonical source accepted")


def test_remediation_plan_validates_recovery_contract_and_is_read_only() -> None:
    owner = current_owner()
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        repo, head = make_git_repo(base)
        recovery_root, backup, evidence = make_legacy_pair(base, owner)
        canonical = base / "canonical"
        canonical.write_text("LOG_PLATFORM_TARGET_ENVIRONMENT=local_dev\n")
        source = base / "zz.conf"
        source.write_text("[Service]\nEnvironmentFile=/etc/log-platform/environment-identity.env\n")
        override = base / "override.conf"
        override.write_text("[Service]\nEnvironmentFile=\nEnvironmentFile=/etc/log-platform-host.env\n")
        obsolete = base / "90.conf"
        obsolete.write_text(source.read_text())
        args = SimpleNamespace(
            expected_host="host", expected_repository_head=head, expected_canonical_sha256=sha256_file(canonical),
            preserved_backup=backup, expected_preserved_backup_sha256=sha256_file(backup),
            recovery_evidence=evidence, expected_recovery_evidence_sha256=sha256_file(evidence),
        )
        unit = obsolete.read_text() + override.read_text()
        patches = (
            patch.object(remediation, "REPO_ROOT", repo), patch.object(remediation, "CANONICAL", canonical),
            patch.object(remediation, "SOURCE", source), patch.object(remediation, "OVERRIDE", override),
            patch.object(remediation, "OBSOLETE", obsolete), patch.object(remediation.socket, "gethostname", return_value="host"),
            patch.object(remediation, "_unit_text", return_value=unit),
            patch.object(remediation, "default_recovery_root", return_value=recovery_root),
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7]:
            before = {path: path.stat().st_mtime_ns for path in (canonical, source, override, obsolete, backup, evidence)}
            plan = remediation.build_plan(args)
            required = remediation.attestation(plan)
            assert plan["recovery_contract"]["owner"] == owner.as_plan()
            assert plan["preserved_recovery_binding"]["plan_sha256"] == "a" * 64
            assert plan["current_canonical_source_effective"] is False
            assert plan["restarts"] == [] and plan["timer_restarts"] == []
            assert plan["docker_recreation"] is False
            assert plan["actions"].count("systemctl daemon-reload") == 2
            assert required.startswith("REMEDIATE_RUNTIME_IDENTITY_SYSTEMD ")
            assert before == {path: path.stat().st_mtime_ns for path in before}
        recovery_root.chmod(0o755)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7]:
            try:
                remediation.build_plan(args)
            except remediation.RemediationError as exc:
                assert exc.code == "RECOVERY_ROOT_PERMISSIONS" and exc.details["actual_mode"] == "0755"
            else:
                raise AssertionError("dry-run accepted permissive recovery root")


def test_permission_remediation_plan_is_separate_and_attested() -> None:
    owner = current_owner()
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        repo, head = make_git_repo(base)
        root, backup, evidence = make_legacy_pair(base, owner)
        root.chmod(0o755)
        args = SimpleNamespace(
            expected_host="host", expected_repository_head=head, expected_current_mode="0755",
            preserved_backup=backup, expected_preserved_backup_sha256=sha256_file(backup),
            recovery_evidence=evidence, expected_recovery_evidence_sha256=sha256_file(evidence),
        )
        with patch.object(permission_remediation, "REPO_ROOT", repo), \
             patch.object(permission_remediation, "default_recovery_root", return_value=root), \
             patch.object(permission_remediation.socket, "gethostname", return_value="host"):
            plan = permission_remediation.build_plan(args)
            assert plan["recovery_root"]["mode"] == "0755"
            assert plan["recovery_root"]["target_mode"] == "0700"
            assert plan["systemd_changes"] is False and plan["daemon_reload"] is False
            assert plan["restarts"] == [] and plan["docker_recreation"] is False
            required = permission_remediation.attestation(plan)
            assert required.startswith("REMEDIATE_RUNTIME_IDENTITY_RECOVERY_PERMISSIONS ")
            assert stat.S_IMODE(root.stat().st_mode) == 0o755
            with patch.object(permission_remediation.os, "geteuid", return_value=0), \
                 patch.dict(os.environ, {"SUDO_USER": owner.user, "SUDO_UID": str(owner.uid), "SUDO_GID": str(owner.gid)}, clear=False):
                result = permission_remediation.execute(args, plan)
            assert result["changed_paths"] == [str(root)]
            assert stat.S_IMODE(root.stat().st_mode) == 0o700


def main() -> None:
    test_external_recovery_contract()
    test_owner_contract_across_privilege_transition()
    test_recovery_metadata_rejections_and_bounded_diagnostics()
    test_artifact_symlink_mode_hash_and_evidence_binding_rejections()
    test_systemd_reset_and_corrected_order()
    test_process_provenance_never_uses_semantic_equality_alone()
    test_provision_plan_and_partial_validation()
    test_remediation_plan_validates_recovery_contract_and_is_read_only()
    test_permission_remediation_plan_is_separate_and_attested()
    print("runtime identity hardening tests: OK")


if __name__ == "__main__":
    main()
