#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ops import environment_identity_file as identity
from ops import provision_runtime_environment_identity as provision
from ops import runtime_identity_readiness as readiness


def expect(code: str, callback) -> None:
    try:
        callback()
    except identity.IdentityFileError as exc:
        assert exc.code == code, exc
    else:
        raise AssertionError(f"expected {code}")


def write_identity(path: Path, environment: str = "local_dev") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(identity.render_identity_file(environment))
    path.chmod(identity.CANONICAL_MODE)


def test_parser_contract() -> None:
    from jobs.common.environment_identity import ALLOWED_ENVIRONMENTS as GUARD_ENVIRONMENTS
    assert identity.ALLOWED_ENVIRONMENTS == GUARD_ENVIRONMENTS
    for value in ("local_dev", "staging", "production"):
        assert identity.parse_identity_bytes(identity.render_identity_file(value)) == value
        assert identity.intended_checksum(value) == identity.sha256_bytes(identity.render_identity_file(value))
    for raw, code in (
        (b"LOG_PLATFORM_TARGET_ENVIRONMENT=prod\n", "IDENTITY_ENVIRONMENT_INVALID"),
        (b"LOG_PLATFORM_TARGET_ENVIRONMENT=local_dev\nX=y\n", "IDENTITY_FILE_CARDINALITY"),
        (b"X=local_dev\n", "IDENTITY_FILE_UNKNOWN_KEY"),
        (b"LOG_PLATFORM_TARGET_ENVIRONMENT=$VALUE\n", "IDENTITY_FILE_EVALUATION_SYNTAX"),
        (b"export LOG_PLATFORM_TARGET_ENVIRONMENT=local_dev\n", "IDENTITY_FILE_MALFORMED"),
        (b"LOG_PLATFORM_TARGET_ENVIRONMENT='local_dev'\n", "IDENTITY_FILE_EVALUATION_SYNTAX"),
    ):
        expect(code, lambda raw=raw: identity.parse_identity_bytes(raw))


def test_inspection_and_process_conflict() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "environment-identity.env"
        write_identity(path)
        state = identity.inspect_identity_file(path, expected_uid=os.geteuid(), expected_gid=os.getegid(), expected_mode=0o640)
        assert state.environment == "local_dev"
        values: dict[str, str] = {}
        identity.apply_identity_to_environ(path=path, environ=values)
        assert values[identity.IDENTITY_KEY] == "local_dev"
        values[identity.IDENTITY_KEY] = "production"
        expect("RUNTIME_IDENTITY_CONFLICT", lambda: identity.apply_identity_to_environ(path=path, environ=values))
        link = path.with_name("link.env")
        link.symlink_to(path)
        expect("IDENTITY_FILE_SYMLINK", lambda: identity.inspect_identity_file(link))


def test_atomic_backup_restore_and_interruption() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "environment-identity.env"
        write_identity(path)
        state = identity.inspect_identity_file(path)
        backup = path.with_name("environment-identity.env.backup-test.bak")
        expect(
            "INJECTED_WRITE_FAILURE",
            lambda: identity.atomic_replace_identity_file(
                state, target_environment="production", backup_path=backup,
                target_uid=os.geteuid(), target_gid=os.getegid(), fail_before_replace=True,
            ),
        )
        assert identity.inspect_identity_file(path).environment == "local_dev"
        result = identity.atomic_replace_identity_file(
            identity.inspect_identity_file(path), target_environment="production", backup_path=backup,
            target_uid=os.geteuid(), target_gid=os.getegid(),
        )
        assert identity.inspect_identity_file(path).environment == "production"
        assert Path(result["backup_path"]).read_bytes() == identity.render_identity_file("local_dev")
        assert stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_duplicate_conflict_detection() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / ".env"
        path.write_text("# secret values are not returned\nPASSWORD=SECRET\nLOG_PLATFORM_TARGET_ENVIRONMENT=local_dev\nLOG_PLATFORM_TARGET_ENVIRONMENT=local_dev\n")
        assert identity.identity_assignments_in_env_file(path) == ["local_dev", "local_dev"]


def test_readiness_classifications() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        canonical = root / "etc/log-platform/environment-identity.env"
        write_identity(canonical)
        helper_source = root / "ops/systemd/proposed/log-platform-environment-identity-helper"
        helper_source.parent.mkdir(parents=True)
        helper_source.write_text("helper")
        helper_dependency = root / "ops/environment_identity_file.py"
        helper_dependency.write_text("parser")
        evidence = {
            component: root / path
            for component, path in readiness.CONFIG_EVIDENCE.items()
        }
        for path in evidence.values():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("environment-identity run_with_environment_identity")
        for source_relative, target_absolute, _expected_mode in readiness.INSTALL_EVIDENCE.values():
            source = root / source_relative
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text("[Service]\nEnvironmentFile=/etc/log-platform/environment-identity.env\n" if target_absolute.endswith(".conf") else "installed-asset environment-identity run_with_environment_identity")
            target = root / Path(target_absolute).relative_to("/")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
        with patch.object(readiness, "CONFIG_EVIDENCE", {k: str(v.relative_to(root)) for k, v in evidence.items()}), \
             patch.object(readiness, "HELPER_INSTALL_PATH", helper_source), \
             patch.object(readiness, "HELPER_LIBRARY_INSTALL_PATH", helper_dependency):
            report = readiness.inspect_runtime_convergence(
                root, canonical_path=canonical, conflict_sources=(),
                process_probe=lambda component: {"status": "running", "effective_environment": "local_dev",
                                                   "started_at": "2999-01-01T00:00:00+00:00", "health": True},
                installation_root=root,
                enforce_canonical_metadata=False, enforce_install_metadata=False,
            )
            assert report["classification"] in {"PRODUCTION_PROMOTION_READY", "RUNTIME_IDENTITY_MIXED"}, report
            stale = readiness.inspect_runtime_convergence(
                root, canonical_path=canonical, conflict_sources=(),
                process_probe=lambda component: ("running", "production"), installation_root=root,
                enforce_canonical_metadata=False, enforce_install_metadata=False,
            )
            assert stale["classification"] in {"RUNTIME_RELOAD_REQUIRED", "RUNTIME_IDENTITY_MIXED"}
            unknown = readiness.inspect_runtime_convergence(
                root, canonical_path=canonical, conflict_sources=(),
                process_probe=lambda component: ("unknown", None), installation_root=root,
                enforce_canonical_metadata=False, enforce_install_metadata=False,
            )
            assert unknown["classification"] in {"RUNTIME_RELOAD_REQUIRED", "RUNTIME_IDENTITY_MIXED"}
        conflict = root / ".env"
        conflict.write_text("LOG_PLATFORM_TARGET_ENVIRONMENT=local_dev\n")
        mixed = readiness.inspect_runtime_convergence(
            root, canonical_path=canonical, conflict_sources=(conflict,), probe_processes=False, enforce_canonical_metadata=False,
        )
        assert mixed["classification"] == "RUNTIME_IDENTITY_MIXED"




def test_repository_launch_precedence() -> None:
    root = Path(__file__).resolve().parents[2]
    units = (
        "ops/systemd/log-platform-api.service.example",
        "ops/systemd/log-platform-prune.service",
        "ops/systemd/log-backup.service",
        "ops/systemd/proposed/log-job@dispatcher.service",
        "ops/systemd/proposed/log-job@retention-purge.service",
        "ops/systemd/proposed/suspected-bug-email-worker.service",
    )
    for relative in units:
        lines = [line for line in (root / relative).read_text().splitlines() if line.startswith("EnvironmentFile=")]
        assert lines[-1] == "EnvironmentFile=/etc/log-platform/environment-identity.env", (relative, lines)
    wrapper = (root / "ops/systemd/proposed/log-job-runner.sh").read_text()
    assert "run_with_environment_identity.py" in wrapper
    assert "source " not in wrapper and ". .env" not in wrapper
    compose = (root / "docker-compose.yml").read_text()
    assert "path: /etc/log-platform/environment-identity.env" in compose
    assert "LOG_PLATFORM_TARGET_ENVIRONMENT:" not in compose
    runner = (root / "ops/runner.py").read_text()
    assert runner.index("load_dotenv(env_path") < runner.index("apply_identity_to_environ(")

def test_privileged_helper_black_box() -> None:
    helper = Path("ops/systemd/proposed/log-platform-environment-identity-helper").absolute()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        canonical = root / identity.CANONICAL_IDENTITY_FILE.relative_to("/")
        write_identity(canonical)
        state = identity.inspect_identity_file(canonical)
        inspect = subprocess.run(
            [str(helper), "--inspect", "--test-root", str(root)],
            check=True, capture_output=True, text=True,
        )
        assert json.loads(inspect.stdout)["environment"] == "local_dev"
        attestation = (
            "SET_LOG_PLATFORM_ENVIRONMENT_IDENTITY "
            f"source=local_dev target=production before_sha256={state.checksum} "
            f"after_sha256={identity.intended_checksum('production')} helper_version={identity.HELPER_VERSION}"
        )
        changed = subprocess.run(
            [str(helper), "--set-environment", "production", "--expected-old-value", "local_dev",
             "--expected-current-sha256", state.checksum, "--attestation", attestation,
             "--test-root", str(root)],
            check=True, capture_output=True, text=True,
        )
        payload = json.loads(changed.stdout)
        assert payload["environment"] == "production"
        backup = Path(payload["backup_path"])
        restore_attestation = (
            "RESTORE_LOG_PLATFORM_ENVIRONMENT_IDENTITY "
            f"current=production current_sha256={payload['after_sha256']} "
            f"backup_sha256={state.checksum} helper_version={identity.HELPER_VERSION}"
        )
        subprocess.run(
            [str(helper), "--restore-backup", str(backup), "--expected-old-value", "production",
             "--expected-current-sha256", payload["after_sha256"],
             "--expected-backup-sha256", state.checksum, "--attestation", restore_attestation,
             "--test-root", str(root)],
            check=True, capture_output=True, text=True,
        )
        assert identity.inspect_identity_file(canonical).environment == "local_dev"
        rejected = subprocess.run(
            [str(helper), "--inspect", "--path", str(root / "other"), "--test-root", str(root)],
            check=False, capture_output=True, text=True,
        )
        assert rejected.returncode == 2 and "IDENTITY_PATH_REJECTED" in rejected.stderr

def test_provisioning_default_and_attestation() -> None:
    parser_args = [
        "--expected-host", "test-host", "--expected-repository-head", "a" * 40,
        "--expected-current-environment", "local_dev", "--backup-reference", "/checkpoint.json",
    ]
    assert "--execute" not in parser_args
    with tempfile.TemporaryDirectory() as directory:
        checkpoint = Path(directory) / "checkpoint.json"
        checkpoint.write_text(json.dumps({"verified": True}))
        args = provision.argparse.Namespace(
            expected_host="test-host", expected_repository_head="a" * 40,
            expected_current_environment="local_dev", backup_reference=checkpoint,
            execute=False, attestation=None, root=Path(directory),
        )
        repository_state = SimpleNamespace(head="a" * 40)
        with patch.object(provision.socket, "gethostname", return_value="test-host"), \
             patch.object(provision, "CONFLICT_SOURCES", ()), \
             patch.object(provision, "SOURCE_TARGETS", ()):
            plan = provision._plan(args, repository_state)
            assert provision._attestation(plan).startswith("PROVISION_RUNTIME_ENVIRONMENT_IDENTITY ")
            command = provision._execution_command(args, provision._attestation(plan))
            assert command.startswith("sudo ") and " --execute " in command
            assert "--attestation" in command
            assert plan["environment"] == "local_dev"
            changed_plan = dict(plan)
            changed_plan["install_assets"] = [{"source": "unexpected", "target": "/unexpected", "mode": "0644", "sha256": "0" * 64}]
            try:
                provision._execute(args, changed_plan)
            except provision.ProvisioningError as exc:
                assert str(exc) == "INSTALL_ASSET_SCOPE_MISMATCH"
            else:
                raise AssertionError("expected installation asset scope mismatch")
            assert not (Path(directory) / identity.CANONICAL_IDENTITY_FILE.relative_to("/")).exists()
        conflict = Path(directory) / "legacy.env"
        conflict.write_text("LOG_PLATFORM_TARGET_ENVIRONMENT=production\n")
        with patch.object(provision.socket, "gethostname", return_value="test-host"), \
             patch.object(provision, "CONFLICT_SOURCES", (Path("/legacy.env"),)), \
             patch.object(provision, "SOURCE_TARGETS", ()):
            try:
                provision._plan(args, repository_state)
            except provision.ProvisioningError as exc:
                assert str(exc).startswith("SOURCE_ENVIRONMENT_MISMATCH:")
            else:
                raise AssertionError("expected source environment mismatch")


def test_provisioning_execute_in_isolated_root() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        checkpoint = root / "checkpoint.json"
        checkpoint.write_text(json.dumps({"verified": True}))
        repo_env = root / REPO_ENV.relative_to("/")
        repo_env.parent.mkdir(parents=True)
        repo_env.write_text("PASSWORD=SECRET\nLOG_PLATFORM_TARGET_ENVIRONMENT=local_dev\n")
        args = provision.argparse.Namespace(
            expected_host="test-host", expected_repository_head="a" * 40,
            expected_current_environment="local_dev", backup_reference=checkpoint,
            execute=True, attestation=None, root=root,
        )
        repository_state = SimpleNamespace(head="a" * 40)
        with patch.object(provision.socket, "gethostname", return_value="test-host"), \
             patch.object(provision, "CONFLICT_SOURCES", (REPO_ENV,)):
            plan = provision._plan(args, repository_state)
            args.attestation = provision._attestation(plan)
            provision._execute(args, plan)
            canonical = root / identity.CANONICAL_IDENTITY_FILE.relative_to("/")
            assert identity.inspect_identity_file(canonical).environment == "local_dev"
            content = repo_env.read_text()
            assert "PASSWORD=SECRET" in content
            assert identity.IDENTITY_KEY not in content
            assert not list(repo_env.parent.glob(".env.runtime-identity-*.bak"))
            recovery_root = provision._rooted(provision.default_recovery_root(provision.REPO_ROOT), root)
            evidence = list(recovery_root.rglob("recovery-evidence.json"))
            assert len(evidence) == 1 and stat.S_IMODE(evidence[0].stat().st_mode) == 0o600
            helper = root / identity.HELPER_INSTALL_PATH.relative_to("/")
            assert helper.is_file() and stat.S_IMODE(helper.stat().st_mode) == 0o755


REPO_ENV = Path("/opt/log-platform/.env")

def main() -> None:
    test_parser_contract()
    test_inspection_and_process_conflict()
    test_atomic_backup_restore_and_interruption()
    test_duplicate_conflict_detection()
    test_repository_launch_precedence()
    test_privileged_helper_black_box()
    test_readiness_classifications()
    test_provisioning_default_and_attestation()
    test_provisioning_execute_in_isolated_root()
    print("runtime environment identity tests: OK")


if __name__ == "__main__":
    main()
