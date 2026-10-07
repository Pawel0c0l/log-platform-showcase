#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from importlib.machinery import SourceFileLoader

from ops import install_runtime_identity_inspector as installer
from ops import provision_runtime_environment_identity as provision
from ops import runtime_identity_inspection as client

REPO_ROOT = Path(__file__).resolve().parents[2]
INSPECTOR = REPO_ROOT / "ops/systemd/proposed/log-platform-runtime-identity-inspector"


def load_inspector():
    spec = importlib.util.spec_from_loader(
        "runtime_identity_inspector_source",
        SourceFileLoader("runtime_identity_inspector_source", str(INSPECTOR)),
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def row_for(module, path: Path, content: bytes) -> dict[str, object]:
    path.write_bytes(content)
    return module.inspect_one("test_source", str(path))


def test_parser_and_redaction() -> None:
    module = load_inspector()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        cases = (
            (b"LOG_PLATFORM_TARGET_ENVIRONMENT=local_dev\nPASSWORD=secret\n", "local_dev", 1, None),
            (b" export LOG_PLATFORM_TARGET_ENVIRONMENT = staging\n", "staging", 1, "ENVIRONMENT_CONFLICT"),
            (b"LOG_PLATFORM_TARGET_ENVIRONMENT='local_dev'\n", "local_dev", 1, None),
            (b'LOG_PLATFORM_TARGET_ENVIRONMENT="production"\n', "production", 1, "ENVIRONMENT_CONFLICT"),
            (b"PASSWORD=secret\n", None, 0, None),
        )
        for index, (raw, value, count, error) in enumerate(cases):
            row = row_for(module, root / f"case-{index}", raw)
            assert row["canonical_value"] == value and row["active_assignment_count"] == count
            assert row["error_code"] == error
            rendered = json.dumps(row)
            assert "secret" not in rendered and "PASSWORD" not in rendered
        duplicate = row_for(module, root / "duplicate", b"LOG_PLATFORM_TARGET_ENVIRONMENT=local_dev\nexport LOG_PLATFORM_TARGET_ENVIRONMENT=local_dev\n")
        assert duplicate["duplicate_definitions"] and duplicate["error_code"] == "DUPLICATE_IDENTITY_ASSIGNMENT"
        malformed = row_for(module, root / "malformed", b"LOG_PLATFORM_TARGET_ENVIRONMENT=$(id)\n")
        assert malformed["malformed_identity_assignments"] and malformed["canonical_value"] is None
        unsupported = row_for(module, root / "unsupported", b"LOG_PLATFORM_TARGET_ENVIRONMENT=prod\n")
        assert not unsupported["supported"] and unsupported["error_code"] == "UNSUPPORTED_ENVIRONMENT"


def test_file_safety_and_hash() -> None:
    module = load_inspector()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        regular = root / "regular"
        regular.write_bytes(b"LOG_PLATFORM_TARGET_ENVIRONMENT=local_dev\n")
        first = module.inspect_one("test", str(regular))
        second = module.inspect_one("test", str(regular))
        assert first["sha256"] == second["sha256"]
        link = root / "link"
        link.symlink_to(regular)
        assert module.inspect_one("test", str(link))["error_code"] == "SYMLINK_REJECTED"
        assert module.inspect_one("test", str(root))["error_code"] == "NON_REGULAR_FILE"
        fifo = root / "fifo"
        os.mkfifo(fifo)
        assert module.inspect_one("test", str(fifo))["error_code"] == "NON_REGULAR_FILE"
        oversized = root / "oversized"
        with oversized.open("wb") as handle:
            handle.truncate(module.MAX_FILE_SIZE + 1)
        assert module.inspect_one("test", str(oversized))["error_code"] == "OVERSIZED_FILE"
        original_fstat = module.os.fstat
        calls = 0
        def changed(fd):
            nonlocal calls
            result = original_fstat(fd)
            calls += 1
            if calls == 2:
                values = list(result)
                values[6] += 1
                return os.stat_result(values)
            return result
        with patch.object(module.os, "fstat", side_effect=changed):
            assert module.inspect_one("test", str(regular))["error_code"] == "FILE_CHANGED"


def test_schema_commands_root_and_no_writes() -> None:
    module = load_inspector()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        paths = tuple((f"source_{index}", str(root / f"file-{index}")) for index in range(3))
        before = set(root.iterdir())
        payload = module.inspect_sources(paths)
        assert set(payload) == client.TOP_FIELDS
        assert len(payload["sources"]) == 3
        assert set(root.iterdir()) == before
    for args in (("unknown",), (module.OPERATION, "/tmp/arbitrary")):
        result = subprocess.run([str(INSPECTOR), *args], capture_output=True, text=True, check=False)
        assert result.returncode == 2 and json.loads(result.stdout)["error_code"] == "UNKNOWN_COMMAND"
    if os.geteuid() != 0:
        result = subprocess.run([str(INSPECTOR), module.OPERATION], capture_output=True, text=True, check=False)
        assert result.returncode == 2 and json.loads(result.stdout)["error_code"] == "ROOT_REQUIRED"


def valid_payload(installed_hash: str) -> dict[str, object]:
    rows = []
    for logical, path in client.ALLOW_LIST:
        rows.append({
            "logical_source": logical, "path": path, "exists": False,
            "regular_file": False, "symlink": False, "owner_uid": None,
            "owner_name": None, "group_gid": None, "group_name": None,
            "mode": None, "size": None, "modification_timestamp": None,
            "sha256": None, "has_active_assignment": False,
            "active_assignment_count": 0, "canonical_value": None,
            "supported": False, "duplicate_definitions": False,
            "malformed_identity_assignments": False,
            "conflicts_expected_environment": False, "error_code": None,
        })
    return {
        "schema_version": 1, "helper_version": "1",
        "operation": "inspect-runtime-identity-sources",
        "helper_source": client.HELPER_SOURCE, "helper_sha256": installed_hash,
        "expected_environment": "local_dev", "sources": rows,
    }


def test_strict_client_and_classifications() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        missing = root / "missing"
        try:
            client.inspect_root_sources(installed_path=missing, enforce_root_metadata=False)
        except client.RootInspectionError as exc:
            assert exc.classification == "ROOT_IDENTITY_INSPECTOR_NOT_INSTALLED"
            assert exc.details["expected_installed_path"] == str(client.INSPECTOR_INSTALL_PATH)
        else:
            raise AssertionError("missing helper accepted")
        installed = root / "inspector"
        installed.write_bytes(client.INSPECTOR_SOURCE_PATH.read_bytes())
        digest = client.sha256_file(installed)
        good = valid_payload(digest)
        runner = lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps(good), stderr="")
        assert client.inspect_root_sources(installed_path=installed, runner=runner, enforce_root_metadata=False) == good
        refused = lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="password required SECRET")
        try:
            client.inspect_root_sources(installed_path=installed, runner=refused, enforce_root_metadata=False)
        except client.RootInspectionError as exc:
            assert exc.classification == "ROOT_IDENTITY_INSPECTOR_NOT_AUTHORIZED"
        else:
            raise AssertionError("sudo refusal accepted")
        for mutation, reason in (
            (lambda value: "not json", "MALFORMED_HELPER_JSON"),
            (lambda value: {**value, "helper_version": "2"}, "HELPER_CONTRACT_MISMATCH"),
            (lambda value: {**value, "sources": value["sources"][:-1]}, "MISSING_REQUIRED_SOURCE"),
            (lambda value: {**value, "sources": value["sources"] + [{**value["sources"][0], "logical_source": "extra", "path": "/extra"}]}, "UNEXPECTED_SOURCE"),
        ):
            output = mutation(valid_payload(digest))
            fake = lambda *a, output=output, **k: SimpleNamespace(returncode=0, stdout=json.dumps(output) if not isinstance(output, str) else output, stderr="")
            try:
                client.inspect_root_sources(installed_path=installed, runner=fake, enforce_root_metadata=False)
            except client.RootInspectionError as exc:
                assert exc.reason_code == reason, (exc.reason_code, reason)
            else:
                raise AssertionError(f"invalid helper output accepted: {reason}")


def test_provisioning_and_installer_contracts() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        checkpoint = root / "checkpoint.json"
        checkpoint.write_text(json.dumps({"verified": True}))
        args = SimpleNamespace(
            expected_host="host", expected_repository_head="a" * 40,
            expected_current_environment="local_dev", backup_reference=checkpoint,
            root=root, execute=False, attestation=None,
        )
        inspected = valid_payload(client.sha256_file(client.INSPECTOR_SOURCE_PATH))
        inspected["sources"][0].update({
            "exists": True, "regular_file": True, "has_active_assignment": True,
            "active_assignment_count": 1, "canonical_value": "local_dev",
            "supported": True, "sha256": "b" * 64,
        })
        repository_state = SimpleNamespace(head="a" * 40)
        with patch.object(provision.socket, "gethostname", return_value="host"), patch.object(provision, "inspect_root_sources", return_value=inspected), patch.object(provision, "SOURCE_TARGETS", ()):
            plan = provision._plan(args, repository_state)
            assert any(row["path"] == "/etc/log-platform-host.env" for row in plan["conflicting_declarations"])
        install_args = SimpleNamespace(expected_host="host", expected_repository_head="a" * 40, backup_reference=checkpoint, root=root, attestation=None)
        with patch.object(installer.socket, "gethostname", return_value="host"), patch.object(installer, "_git_head", return_value="a" * 40):
            plan = installer.build_plan(install_args)
            assert plan["required_attestation"].startswith("INSTALL_RUNTIME_IDENTITY_INSPECTOR ")
            assert not (root / "usr/local/sbin/log-platform-runtime-identity-inspector").exists()
            (root / "usr/local/sbin").mkdir(parents=True)
            (root / "etc/sudoers.d").mkdir(parents=True)
            install_args.attestation = plan["required_attestation"]
            installer.execute(install_args, plan)
            helper = root / "usr/local/sbin/log-platform-runtime-identity-inspector"
            sudoers = root / "etc/sudoers.d/log-platform-runtime-identity-inspector"
            assert stat.S_IMODE(helper.stat().st_mode) == 0o755
            assert stat.S_IMODE(sudoers.stat().st_mode) == 0o440
            before = (helper.stat().st_mtime_ns, sudoers.stat().st_mtime_ns)
            installer.execute(install_args, plan)
            assert before == (helper.stat().st_mtime_ns, sudoers.stat().st_mtime_ns)
            link_root = root / "link-root"
            link_root.mkdir()
            destination = link_root / "helper"
            destination.symlink_to(helper)
            try:
                installer._atomic_install(destination, b"x", 0o755, os.geteuid(), os.getegid())
            except installer.InstallerError as exc:
                assert str(exc).startswith("SYMLINK_DESTINATION_REJECTED")
            else:
                raise AssertionError("symlink destination accepted")


def test_sudoers_contract() -> None:
    fragment = REPO_ROOT / "ops/systemd/proposed/log-platform-runtime-identity-inspector.sudoers"
    subprocess.run(["visudo", "-cf", str(fragment)], check=True, capture_output=True, text=True)
    text = fragment.read_text()
    exact = "/usr/local/sbin/log-platform-runtime-identity-inspector inspect-runtime-identity-sources"
    assert exact in text and "*" not in text and "NOPASSWD" in text
    assert "!setenv" in text and "secure_path=" in text
    assert "/bin/sh" not in text and "/bin/bash" not in text and "sudoedit" not in text


def main() -> None:
    test_parser_and_redaction()
    test_file_safety_and_hash()
    test_schema_commands_root_and_no_writes()
    test_strict_client_and_classifications()
    test_provisioning_and_installer_contracts()
    test_sudoers_contract()
    print("runtime identity inspector tests: OK")


if __name__ == "__main__":
    main()
