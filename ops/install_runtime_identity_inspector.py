#!/usr/bin/env python3
"""Dry-run-first installation of the fixed root identity inspector boundary."""
from __future__ import annotations

import argparse
import grp
import hashlib
import json
import os
import pwd
import shlex
import socket
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ops.runtime_identity_inspection import (
    INSPECTOR_INSTALL_PATH, INSPECTOR_SOURCE_PATH, SUDOERS_INSTALL_PATH,
    SUDOERS_SOURCE_PATH, sha256_file,
)


class InstallerError(RuntimeError):
    pass


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _git_head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, check=True,
        capture_output=True, text=True,
    ).stdout.strip()


def _verify_checkpoint(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise InstallerError("BACKUP_REFERENCE_INVALID")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallerError("BACKUP_REFERENCE_INVALID") from exc
    if not isinstance(payload, dict) or (payload.get("verified") is not True and not payload.get("validation")):
        raise InstallerError("BACKUP_REFERENCE_NOT_VERIFIED")
    return sha256_file(path)


def build_plan(args: argparse.Namespace) -> dict[str, object]:
    if socket.gethostname() != args.expected_host:
        raise InstallerError("HOST_IDENTITY_MISMATCH")
    head = _git_head()
    if head != args.expected_repository_head:
        raise InstallerError("REPOSITORY_HEAD_MISMATCH")
    checkpoint_hash = _verify_checkpoint(args.backup_reference)
    validation = subprocess.run(
        ["visudo", "-cf", str(SUDOERS_SOURCE_PATH)], check=False,
        capture_output=True, text=True, timeout=10,
    )
    if validation.returncode != 0:
        raise InstallerError("SUDOERS_VALIDATION_FAILED")
    assets = (
        (INSPECTOR_SOURCE_PATH, INSPECTOR_INSTALL_PATH, "0755"),
        (SUDOERS_SOURCE_PATH, SUDOERS_INSTALL_PATH, "0440"),
    )
    plan = {
        "contract_version": 1,
        "host": args.expected_host,
        "repository_head": head,
        "backup_reference": str(args.backup_reference.absolute()),
        "backup_reference_sha256": checkpoint_hash,
        "service_user": "logplatform",
        "assets": [
            {
                "source": str(source.relative_to(REPO_ROOT)),
                "source_sha256": sha256_file(source),
                "destination": str(destination),
                "owner": "root", "group": "root", "mode": mode,
            }
            for source, destination, mode in assets
        ],
        "allowed_operation": (
            "/usr/local/sbin/log-platform-runtime-identity-inspector "
            "inspect-runtime-identity-sources"
        ),
        "required_runtime_assets": [],
        "writes_outside_scope": [],
    }
    digest = hashlib.sha256(_canonical(plan).encode("ascii")).hexdigest()
    plan["installation_plan_sha256"] = digest
    plan["required_attestation"] = (
        "INSTALL_RUNTIME_IDENTITY_INSPECTOR "
        f"host={args.expected_host} head={head} plan_sha256={digest}"
    )
    return plan


def _rooted(path: Path, root: Path) -> Path:
    return path if root == Path("/") else root / path.relative_to("/")


def _atomic_install(path: Path, raw: bytes, mode: int, uid: int, gid: int) -> None:
    if path.is_symlink():
        raise InstallerError(f"SYMLINK_DESTINATION_REJECTED:{path}")
    if path.exists():
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise InstallerError(f"NON_REGULAR_DESTINATION_REJECTED:{path}")
        if path.read_bytes() == raw and stat.S_IMODE(metadata.st_mode) == mode and metadata.st_uid == uid and metadata.st_gid == gid:
            return
    try:
        parent_metadata = path.parent.lstat()
    except FileNotFoundError as exc:
        raise InstallerError(f"DESTINATION_DIRECTORY_MISSING:{path.parent}") from exc
    if path.parent.is_symlink() or not stat.S_ISDIR(parent_metadata.st_mode):
        raise InstallerError(f"DESTINATION_DIRECTORY_UNSAFE:{path.parent}")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        os.fchown(fd, uid, gid)
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise InstallerError("FILE_WRITE_DID_NOT_PROGRESS")
            view = view[written:]
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            Path(temporary).unlink()
        except FileNotFoundError:
            pass


def execute(args: argparse.Namespace, plan: dict[str, object]) -> None:
    root = args.root.absolute()
    if root == Path("/") and os.geteuid() != 0:
        raise InstallerError("ROOT_REQUIRED")
    if args.attestation != plan["required_attestation"]:
        raise InstallerError("ATTESTATION_MISMATCH")
    planned = {row["source"]: row for row in plan["assets"]}
    sources = (INSPECTOR_SOURCE_PATH, SUDOERS_SOURCE_PATH)
    for source in sources:
        row = planned.get(str(source.relative_to(REPO_ROOT)))
        if row is None or row["source_sha256"] != sha256_file(source):
            raise InstallerError("SOURCE_HASH_MISMATCH")
    uid = pwd.getpwnam("root").pw_uid if root == Path("/") else os.geteuid()
    gid = grp.getgrnam("root").gr_gid if root == Path("/") else os.getegid()
    for source, destination, mode in (
        (INSPECTOR_SOURCE_PATH, INSPECTOR_INSTALL_PATH, 0o755),
        (SUDOERS_SOURCE_PATH, SUDOERS_INSTALL_PATH, 0o440),
    ):
        _atomic_install(_rooted(destination, root), source.read_bytes(), mode, uid, gid)
    for row in plan["assets"]:
        destination = _rooted(Path(row["destination"]), root)
        metadata = destination.lstat()
        if (
            sha256_file(destination) != row["source_sha256"]
            or stat.S_IMODE(metadata.st_mode) != int(row["mode"], 8)
            or metadata.st_uid != uid or metadata.st_gid != gid
        ):
            raise InstallerError("INSTALLED_ASSET_VALIDATION_FAILED")
    if root == Path("/"):
        safe_env = {
            "PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C", "LANG": "C",
            "PYTHONPATH": "/untrusted", "PYTHONHOME": "/untrusted",
            "LD_LIBRARY_PATH": "/untrusted", "PYTHONNOUSERSITE": "0",
        }
        fixed = subprocess.run(
            ["runuser", "-u", "logplatform", "--", "sudo", "-n",
             str(INSPECTOR_INSTALL_PATH), "inspect-runtime-identity-sources"],
            check=False, capture_output=True, text=True, timeout=15, env=safe_env,
        )
        try:
            fixed_payload = json.loads(fixed.stdout)
        except json.JSONDecodeError as exc:
            raise InstallerError("FIXED_COMMAND_SUDO_VALIDATION_FAILED") from exc
        if fixed.returncode not in (0, 2) or fixed_payload.get("schema_version") != 1:
            raise InstallerError("FIXED_COMMAND_SUDO_VALIDATION_FAILED")
        extra = subprocess.run(
            ["runuser", "-u", "logplatform", "--", "sudo", "-n",
             str(INSPECTOR_INSTALL_PATH), "inspect-runtime-identity-sources", "extra"],
            check=False, capture_output=True, text=True, timeout=10, env=safe_env,
        )
        if extra.returncode == 0:
            raise InstallerError("SUDOERS_ADDITIONAL_ARGUMENT_ALLOWED")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-host", required=True)
    parser.add_argument("--expected-repository-head", required=True)
    parser.add_argument("--backup-reference", required=True, type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--attestation")
    parser.add_argument("--root", type=Path, default=Path("/"), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    plan = build_plan(args)
    output = {
        "mode": "execute" if args.execute else "dry_run",
        "writes_performed": False,
        "plan": plan,
        "installation_command": (
            f"sudo {shlex.quote(str(REPO_ROOT / '.venv/bin/python'))} -I "
            f"{shlex.quote(str(REPO_ROOT / 'ops/install_runtime_identity_inspector.py'))} "
            f"--expected-host {args.expected_host} --expected-repository-head {args.expected_repository_head} "
            f"--backup-reference {shlex.quote(str(args.backup_reference))} --execute "
            f"--attestation {shlex.quote(str(plan['required_attestation']))}"
        ),
    }
    if args.execute:
        execute(args, plan)
        output["writes_performed"] = True
    print(json.dumps(output, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except InstallerError as exc:
        print(json.dumps({"classification": str(exc), "writes_performed": False}, sort_keys=True))
        raise SystemExit(2)
