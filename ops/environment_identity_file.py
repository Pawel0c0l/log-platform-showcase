"""Canonical, secret-free runtime environment identity file contract."""
from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, MutableMapping

ALLOWED_ENVIRONMENTS = ("local_dev", "staging", "production")


IDENTITY_KEY = "LOG_PLATFORM_TARGET_ENVIRONMENT"
CANONICAL_IDENTITY_FILE = Path("/etc/log-platform/environment-identity.env")
CANONICAL_OWNER = "root"
CANONICAL_GROUP = "logplatform"
CANONICAL_MODE = 0o640
HELPER_INSTALL_PATH = Path("/usr/local/sbin/log-platform-environment-identity-helper")
HELPER_LIBRARY_INSTALL_PATH = Path("/usr/local/lib/log-platform/environment_identity_file.py")
HELPER_VERSION = "1"

_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=([^\r\n]*)$")
_ENV_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


class IdentityFileError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class IdentityFileState:
    path: Path
    environment: str
    checksum: str
    uid: int
    gid: int
    mode: int


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _write_all(fd: int, raw: bytes) -> None:
    view = memoryview(raw)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise IdentityFileError("IDENTITY_FILE_WRITE", "file write did not progress")
        view = view[written:]


def render_identity_file(environment: str) -> bytes:
    validate_environment(environment)
    return f"{IDENTITY_KEY}={environment}\n".encode("ascii")


def intended_checksum(environment: str) -> str:
    return sha256_bytes(render_identity_file(environment))


def validate_environment(environment: str) -> str:
    if environment not in ALLOWED_ENVIRONMENTS:
        raise IdentityFileError(
            "IDENTITY_ENVIRONMENT_INVALID",
            f"environment must be exactly one of: {', '.join(ALLOWED_ENVIRONMENTS)}",
        )
    return environment


def parse_identity_bytes(raw: bytes) -> str:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise IdentityFileError("IDENTITY_FILE_MALFORMED", "file must be UTF-8") from exc
    lines = text.splitlines()
    if len(lines) != 1:
        raise IdentityFileError("IDENTITY_FILE_CARDINALITY", "file must contain exactly one assignment")
    match = _ASSIGNMENT.fullmatch(lines[0])
    if not match:
        raise IdentityFileError("IDENTITY_FILE_MALFORMED", "file must contain an unquoted KEY=value assignment")
    key, value = match.groups()
    if key != IDENTITY_KEY:
        raise IdentityFileError("IDENTITY_FILE_UNKNOWN_KEY", "identity-only file contains an unsupported key")
    if any(token in value for token in ("$", "`", "\\", "'", '"')):
        raise IdentityFileError("IDENTITY_FILE_EVALUATION_SYNTAX", "shell evaluation and quoting are not supported")
    return validate_environment(value)


def inspect_identity_file(
    path: Path = CANONICAL_IDENTITY_FILE,
    *,
    expected_uid: int | None = None,
    expected_gid: int | None = None,
    expected_mode: int | None = None,
) -> IdentityFileState:
    absolute = path.absolute()
    try:
        metadata = absolute.lstat()
    except FileNotFoundError as exc:
        raise IdentityFileError("IDENTITY_FILE_MISSING", "canonical identity file is missing") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise IdentityFileError("IDENTITY_FILE_SYMLINK", "canonical identity file must not be a symlink")
    if not stat.S_ISREG(metadata.st_mode):
        raise IdentityFileError("IDENTITY_FILE_NOT_REGULAR", "canonical identity file must be regular")
    mode = stat.S_IMODE(metadata.st_mode)
    if expected_uid is not None and metadata.st_uid != expected_uid:
        raise IdentityFileError("IDENTITY_FILE_OWNER", "canonical identity file has the wrong owner")
    if expected_gid is not None and metadata.st_gid != expected_gid:
        raise IdentityFileError("IDENTITY_FILE_GROUP", "canonical identity file has the wrong group")
    if expected_mode is not None and mode != expected_mode:
        raise IdentityFileError("IDENTITY_FILE_MODE", "canonical identity file has the wrong mode")
    raw = absolute.read_bytes()
    return IdentityFileState(
        path=absolute,
        environment=parse_identity_bytes(raw),
        checksum=sha256_bytes(raw),
        uid=metadata.st_uid,
        gid=metadata.st_gid,
        mode=mode,
    )


def identity_assignments_in_env_file(path: Path) -> list[str]:
    """Return only identity values, never unrelated configuration or secrets."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except UnicodeDecodeError as exc:
        raise IdentityFileError("RUNTIME_SOURCE_MALFORMED", "runtime source must be UTF-8") from exc
    values: list[str] = []
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _ENV_ASSIGNMENT.match(line)
        if match and match.group(1) == IDENTITY_KEY:
            raw_value = match.group(2).strip()
            if raw_value[:1] in {"'", '"'} and raw_value[-1:] == raw_value[:1]:
                raw_value = raw_value[1:-1]
            validate_environment(raw_value)
            values.append(raw_value)
    return values


def apply_identity_to_environ(
    *,
    path: Path = CANONICAL_IDENTITY_FILE,
    environ: MutableMapping[str, str] | None = None,
    reject_conflict: bool = True,
) -> IdentityFileState:
    values = os.environ if environ is None else environ
    state = inspect_identity_file(path)
    current = values.get(IDENTITY_KEY)
    if reject_conflict and current is not None and current != state.environment:
        raise IdentityFileError("RUNTIME_IDENTITY_CONFLICT", "process environment conflicts with canonical identity file")
    values[IDENTITY_KEY] = state.environment
    values["LOG_PLATFORM_ENVIRONMENT_IDENTITY_FILE"] = str(state.path)
    values["LOG_PLATFORM_ENVIRONMENT_IDENTITY_SHA256"] = state.checksum
    return state


def atomic_replace_identity_file(
    state: IdentityFileState,
    *,
    target_environment: str,
    backup_path: Path,
    target_uid: int,
    target_gid: int,
    target_mode: int = CANONICAL_MODE,
    fail_before_replace: bool = False,
) -> dict[str, str]:
    validate_environment(target_environment)
    current = state.path.read_bytes()
    if sha256_bytes(current) != state.checksum:
        raise IdentityFileError("IDENTITY_FILE_CHANGED", "identity file changed after inspection")
    if backup_path.absolute().parent != state.path.parent:
        raise IdentityFileError("IDENTITY_BACKUP_PATH", "backup must be beside the canonical file")
    if backup_path.is_symlink():
        raise IdentityFileError("IDENTITY_BACKUP_SYMLINK", "backup must not be a symlink")
    if backup_path.exists():
        backup_metadata = backup_path.stat()
        if (not stat.S_ISREG(backup_metadata.st_mode)
            or stat.S_IMODE(backup_metadata.st_mode) != 0o600
            or backup_metadata.st_uid != target_uid
            or backup_metadata.st_gid != target_gid
            or sha256_bytes(backup_path.read_bytes()) != state.checksum):
            raise IdentityFileError("IDENTITY_BACKUP_CONFLICT", "existing backup does not match the inspected source")
    else:
        backup_fd = os.open(backup_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            _write_all(backup_fd, current)
            os.fsync(backup_fd)
            os.fchown(backup_fd, target_uid, target_gid)
            os.fchmod(backup_fd, 0o600)
        finally:
            os.close(backup_fd)
        backup_directory_fd = os.open(state.path.parent, os.O_RDONLY)
        try:
            os.fsync(backup_directory_fd)
        finally:
            os.close(backup_directory_fd)
    rendered = render_identity_file(target_environment)
    fd, temporary = tempfile.mkstemp(prefix=f".{state.path.name}.", dir=state.path.parent)
    try:
        os.fchown(fd, target_uid, target_gid)
        os.fchmod(fd, target_mode)
        _write_all(fd, rendered)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        parse_identity_bytes(Path(temporary).read_bytes())
        if fail_before_replace:
            raise IdentityFileError("INJECTED_WRITE_FAILURE", "injected failure before atomic replace")
        os.replace(temporary, state.path)
        directory_fd = os.open(state.path.parent, os.O_RDONLY)
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
    after = inspect_identity_file(
        state.path, expected_uid=target_uid, expected_gid=target_gid, expected_mode=target_mode
    )
    return {
        "backup_path": str(backup_path),
        "before_sha256": state.checksum,
        "after_sha256": after.checksum,
        "environment": after.environment,
    }


def public_state(state: IdentityFileState) -> Mapping[str, object]:
    return {
        "path": str(state.path),
        "environment": state.environment,
        "sha256": state.checksum,
        "uid": state.uid,
        "gid": state.gid,
        "mode": f"{state.mode:04o}",
    }
