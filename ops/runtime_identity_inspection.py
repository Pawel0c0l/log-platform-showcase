"""Strict unprivileged client for the fixed privileged identity inspector."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INSPECTOR_SOURCE_PATH = REPO_ROOT / "ops/systemd/proposed/log-platform-runtime-identity-inspector"
INSPECTOR_INSTALL_PATH = Path("/usr/local/sbin/log-platform-runtime-identity-inspector")
SUDOERS_SOURCE_PATH = REPO_ROOT / "ops/systemd/proposed/log-platform-runtime-identity-inspector.sudoers"
SUDOERS_INSTALL_PATH = Path("/etc/sudoers.d/log-platform-runtime-identity-inspector")
INSPECTOR_COMMAND = (
    "sudo", "-n", str(INSPECTOR_INSTALL_PATH), "inspect-runtime-identity-sources",
)
SCHEMA_VERSION = 1
HELPER_VERSION = "1"
HELPER_SOURCE = "log-platform-runtime-identity-inspector"
EXPECTED_ENVIRONMENT = "local_dev"
ALLOW_LIST = (
    ("host_environment", "/etc/log-platform-host.env"),
    ("runtime_environment", "/etc/log-platform/runtime.env"),
    ("backup_environment", "/etc/log-platform/backup.env"),
)
SOURCE_FIELDS = {
    "logical_source", "path", "exists", "regular_file", "symlink", "owner_uid",
    "owner_name", "group_gid", "group_name", "mode", "size",
    "modification_timestamp", "sha256", "has_active_assignment",
    "active_assignment_count", "canonical_value", "supported",
    "duplicate_definitions", "malformed_identity_assignments",
    "conflicts_expected_environment", "error_code",
}
TOP_FIELDS = {
    "schema_version", "helper_version", "operation", "helper_source",
    "helper_sha256", "expected_environment", "sources",
}
SOURCE_ERROR_CODES = {
    "PERMISSION_DENIED", "METADATA_ERROR", "SYMLINK_REJECTED",
    "NON_REGULAR_FILE", "OVERSIZED_FILE", "OPEN_FAILED", "FILE_CHANGED",
    "MALFORMED_IDENTITY_ASSIGNMENT", "DUPLICATE_IDENTITY_ASSIGNMENT",
    "UNSUPPORTED_ENVIRONMENT", "ENVIRONMENT_CONFLICT",
}
SUPPORTED_ENVIRONMENTS = {"local_dev", "staging", "production"}


class RootInspectionError(RuntimeError):
    def __init__(self, classification: str, reason_code: str, details: dict[str, object] | None = None) -> None:
        self.classification = classification
        self.reason_code = reason_code
        self.details = details or {}
        super().__init__(f"{classification}:{reason_code}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def installation_requirements() -> dict[str, object]:
    return {
        "expected_installed_path": str(INSPECTOR_INSTALL_PATH),
        "expected_source_path": str(INSPECTOR_SOURCE_PATH.relative_to(REPO_ROOT)),
        "expected_source_sha256": sha256_file(INSPECTOR_SOURCE_PATH),
        "expected_installed_owner": "root",
        "expected_installed_group": "root",
        "expected_installed_mode": "0755",
        "proposed_sudoers_fragment_path": str(SUDOERS_SOURCE_PATH.relative_to(REPO_ROOT)),
        "expected_sudoers_installed_path": str(SUDOERS_INSTALL_PATH),
        "exact_command": " ".join(INSPECTOR_COMMAND),
    }


def _check_installed(path: Path = INSPECTOR_INSTALL_PATH, *, enforce_root_metadata: bool = True) -> str:
    requirements = installation_requirements()
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise RootInspectionError(
            "ROOT_IDENTITY_INSPECTOR_NOT_INSTALLED", "INSPECTOR_MISSING", requirements
        ) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RootInspectionError("ROOT_IDENTITY_INSPECTION_FAILED", "INSPECTOR_UNSAFE_FILE", requirements)
    if enforce_root_metadata and (
        metadata.st_uid != 0 or metadata.st_gid != 0 or stat.S_IMODE(metadata.st_mode) != 0o755
    ):
        raise RootInspectionError("ROOT_IDENTITY_INSPECTION_FAILED", "INSPECTOR_METADATA_MISMATCH", requirements)
    installed_hash = sha256_file(path)
    if installed_hash != requirements["expected_source_sha256"]:
        raise RootInspectionError("ROOT_IDENTITY_INSPECTION_FAILED", "INSPECTOR_HASH_MISMATCH", requirements)
    return installed_hash


def validate_payload(payload: object, *, installed_sha256: str) -> dict[str, object]:
    if not isinstance(payload, dict) or set(payload) != TOP_FIELDS:
        raise RootInspectionError("ROOT_IDENTITY_INSPECTION_FAILED", "INVALID_OUTPUT_SCHEMA")
    if (
        type(payload["schema_version"]) is not int
        or payload["schema_version"] != SCHEMA_VERSION
        or payload["helper_version"] != HELPER_VERSION
        or payload["operation"] != "inspect-runtime-identity-sources"
        or payload["helper_source"] != HELPER_SOURCE
        or payload["helper_sha256"] != installed_sha256
        or payload["expected_environment"] != EXPECTED_ENVIRONMENT
        or not isinstance(payload["sources"], list)
    ):
        raise RootInspectionError("ROOT_IDENTITY_INSPECTION_FAILED", "HELPER_CONTRACT_MISMATCH")
    expected = dict(ALLOW_LIST)
    seen: set[str] = set()
    for row in payload["sources"]:
        if not isinstance(row, dict) or set(row) != SOURCE_FIELDS:
            raise RootInspectionError("ROOT_IDENTITY_INSPECTION_FAILED", "INVALID_SOURCE_SCHEMA")
        name = row.get("logical_source")
        if not isinstance(name, str) or name in seen or expected.get(name) != row.get("path"):
            raise RootInspectionError("ROOT_IDENTITY_INSPECTION_FAILED", "UNEXPECTED_SOURCE")
        seen.add(name)
        bool_fields = (
            "exists", "regular_file", "symlink", "has_active_assignment", "supported",
            "duplicate_definitions", "malformed_identity_assignments", "conflicts_expected_environment",
        )
        if any(type(row[name]) is not bool for name in bool_fields):
            raise RootInspectionError("ROOT_IDENTITY_INSPECTION_FAILED", "INVALID_SOURCE_TYPE")
        if type(row["active_assignment_count"]) is not int or row["active_assignment_count"] < 0:
            raise RootInspectionError("ROOT_IDENTITY_INSPECTION_FAILED", "INVALID_SOURCE_TYPE")
        for name in ("owner_uid", "group_gid", "mode", "size"):
            if row[name] is not None and (type(row[name]) is not int or row[name] < 0):
                raise RootInspectionError("ROOT_IDENTITY_INSPECTION_FAILED", "INVALID_SOURCE_TYPE")
        for name in ("owner_name", "group_name", "modification_timestamp", "sha256", "canonical_value", "error_code"):
            if row[name] is not None and not isinstance(row[name], str):
                raise RootInspectionError("ROOT_IDENTITY_INSPECTION_FAILED", "INVALID_SOURCE_TYPE")
        if row["error_code"] is not None and row["error_code"] not in SOURCE_ERROR_CODES:
            raise RootInspectionError("ROOT_IDENTITY_INSPECTION_FAILED", "UNKNOWN_HELPER_ERROR_CODE")
        if row["sha256"] is not None and (
            len(row["sha256"]) != 64
            or any(character not in "0123456789abcdef" for character in row["sha256"])
        ):
            raise RootInspectionError("ROOT_IDENTITY_INSPECTION_FAILED", "INVALID_SOURCE_VALUE")
        if row["canonical_value"] is not None and row["canonical_value"] not in SUPPORTED_ENVIRONMENTS:
            raise RootInspectionError("ROOT_IDENTITY_INSPECTION_FAILED", "INVALID_SOURCE_VALUE")
        if (
            row["has_active_assignment"] != (row["active_assignment_count"] > 0)
            or row["duplicate_definitions"] != (row["active_assignment_count"] > 1)
            or row["supported"] != (row["canonical_value"] in SUPPORTED_ENVIRONMENTS)
            or row["conflicts_expected_environment"]
               != (row["canonical_value"] is not None and row["canonical_value"] != EXPECTED_ENVIRONMENT)
            or (row["canonical_value"] is not None and row["active_assignment_count"] != 1)
        ):
            raise RootInspectionError("ROOT_IDENTITY_INSPECTION_FAILED", "INCONSISTENT_SOURCE_STATE")
        if not row["exists"] and any(row[name] is not None for name in (
            "owner_uid", "owner_name", "group_gid", "group_name", "mode", "size",
            "modification_timestamp", "sha256", "canonical_value", "error_code",
        )):
            raise RootInspectionError("ROOT_IDENTITY_INSPECTION_FAILED", "INCONSISTENT_SOURCE_STATE")
    if seen != set(expected):
        raise RootInspectionError("ROOT_IDENTITY_INSPECTION_FAILED", "MISSING_REQUIRED_SOURCE")
    failures = sorted({str(row["error_code"]) for row in payload["sources"] if row["error_code"]})
    if failures:
        raise RootInspectionError("ROOT_IDENTITY_INSPECTION_FAILED", failures[0], {"reason_codes": failures})
    return payload


def inspect_root_sources(
    *, installed_path: Path = INSPECTOR_INSTALL_PATH,
    runner=subprocess.run, enforce_root_metadata: bool = True,
) -> dict[str, object]:
    installed_hash = _check_installed(installed_path, enforce_root_metadata=enforce_root_metadata)
    command = ("sudo", "-n", str(installed_path), "inspect-runtime-identity-sources")
    safe_env = {
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C", "LANG": "C",
        "PYTHONNOUSERSITE": "1",
    }
    try:
        result = runner(command, check=False, capture_output=True, text=True, timeout=10, env=safe_env)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RootInspectionError("ROOT_IDENTITY_INSPECTOR_NOT_AUTHORIZED", "SUDO_EXECUTION_FAILED") from exc
    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        if result.returncode == 1:
            raise RootInspectionError("ROOT_IDENTITY_INSPECTOR_NOT_AUTHORIZED", "SUDO_NONINTERACTIVE_REFUSED")
        raise RootInspectionError("ROOT_IDENTITY_INSPECTION_FAILED", "MALFORMED_HELPER_JSON")
    validated = validate_payload(payload, installed_sha256=installed_hash)
    if result.returncode != 0:
        raise RootInspectionError("ROOT_IDENTITY_INSPECTION_FAILED", "HELPER_NONZERO_EXIT")
    return validated
