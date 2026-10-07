"""Environment identity promotion primitives.

The operation is intentionally staged: PostgreSQL cannot atomically commit a
platform marker, multiple client markers, control-plane rows, and a host file.
Every write is therefore independently verified and journalled.  UUID columns
are selected and compared but are never present in an UPDATE statement.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from uuid import UUID, uuid4

from jobs.common.environment_identity import ALLOWED_ENVIRONMENTS
from ops.environment_identity_file import (
    CANONICAL_IDENTITY_FILE,
    HELPER_INSTALL_PATH,
    HELPER_LIBRARY_INSTALL_PATH,
    HELPER_VERSION,
    IDENTITY_KEY,
    IdentityFileError,
    inspect_identity_file,
    intended_checksum,
)


TARGET_ENVIRONMENT_KEY = "LOG_PLATFORM_TARGET_ENVIRONMENT"
JOURNAL_SCHEMA = "ops_control"
JOURNAL_TABLE_NAME = "environment_identity_promotion"
JOURNAL_TABLE = f"{JOURNAL_SCHEMA}.{JOURNAL_TABLE_NAME}"
ADVISORY_LOCK_NAME = "ops_control.environment_identity_promotion/v1"
RESUME_AUDIT_COLUMNS = ("resume_contract", "resume_plan_sha256")
RESUME_AUDIT_MIGRATION = "054_environment_identity_resume_contract.sql"
RESUME_AUDIT_REQUIRED_ACTION = (
    "apply the reviewed platform migration "
    f"db/migrations/{RESUME_AUDIT_MIGRATION} through the separate migration gate"
)
STEP_CLIENT_PREFIX = "client_marker:"
STEP_CONTROL_PLANE = "platform_control_plane"
STEP_PLATFORM_MARKER = "platform_marker"
STEP_RUNTIME_FILE = "runtime_environment_file"
STEP_RUNTIME_RELOAD = "runtime_reload_required"
STEP_RUNTIME_PROCESSES = "runtime_processes_verified"
STEP_FINAL_VERIFY = "final_verification"

EXIT_OK = 0
EXIT_INVALID = 2
EXIT_PRECONDITION = 3
EXIT_INCOMPATIBLE = 4
EXIT_BUSY = 5
EXIT_PARTIAL = 6


class PromotionError(RuntimeError):
    def __init__(
        self, code: str, message: str, *, exit_code: int = EXIT_PRECONDITION,
        details: Mapping[str, object] | None = None,
    ):
        self.code = code
        self.exit_code = exit_code
        self.details = dict(details) if details is not None else None
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class ClientPlan:
    client_id: str
    client_code: str
    database_name: str
    database_user: str
    database_host: str
    database_port: int
    database_uuid: str
    password_secret_ref: str

    def public_dict(self) -> dict[str, object]:
        return {
            "client_id": self.client_id,
            "client_code": self.client_code,
            "database_name": self.database_name,
            "database_user": self.database_user,
            "database_host": self.database_host,
            "database_port": self.database_port,
            "database_uuid": self.database_uuid,
        }


@dataclass(frozen=True)
class RuntimeFileState:
    path: Path
    values: Mapping[str, str]
    checksum: str
    uid: int
    gid: int
    mode: int


def canonical_uuid(value: object, label: str) -> str:
    text = str(value or "")
    try:
        canonical = str(UUID(text))
    except (TypeError, ValueError) as exc:
        raise PromotionError("INVALID_UUID", f"{label} must be a canonical UUID", exit_code=EXIT_INVALID) from exc
    if text != canonical:
        raise PromotionError("INVALID_UUID", f"{label} must use canonical lowercase UUID form", exit_code=EXIT_INVALID)
    return canonical


def validate_environments(source: str, target: str) -> None:
    if source not in ALLOWED_ENVIRONMENTS:
        raise PromotionError("SOURCE_ENVIRONMENT_INVALID", f"unsupported source environment: {source}", exit_code=EXIT_INVALID)
    if target not in ALLOWED_ENVIRONMENTS:
        raise PromotionError("TARGET_ENVIRONMENT_INVALID", f"unsupported target environment: {target}", exit_code=EXIT_INVALID)
    if source == target:
        raise PromotionError("ENVIRONMENTS_EQUAL", "source and target environments must differ", exit_code=EXIT_INVALID)


def parse_expected_clients(items: Iterable[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in items:
        code, separator, value = item.partition("=")
        code = code.strip()
        if not separator or not code:
            raise PromotionError("CLIENT_UUID_ARGUMENT_INVALID", "client UUID arguments must use CLIENT_CODE=UUID", exit_code=EXIT_INVALID)
        if code in result:
            raise PromotionError("DUPLICATE_CLIENT", f"client selected more than once: {code}", exit_code=EXIT_INVALID)
        result[code] = canonical_uuid(value.strip(), f"database UUID for {code}")
    if not result:
        raise PromotionError("EMPTY_CLIENT_SCOPE", "at least one client must be explicitly selected", exit_code=EXIT_INVALID)
    return result


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def plan_hash(plan: Mapping[str, object]) -> str:
    return sha256_bytes(canonical_json(plan).encode("utf-8"))


def transaction_status_name(conn) -> str:
    """Return Psycopg's transaction status without importing it at module load."""
    status = conn.info.transaction_status
    return str(getattr(status, "name", status)).upper()


def require_idle_connection(conn, *, code: str, purpose: str) -> None:
    status = transaction_status_name(conn)
    if status != "IDLE":
        raise PromotionError(
            code,
            f"{purpose} requires an IDLE PostgreSQL connection; observed {status}",
        )


def require_read_only_connection(conn, *, purpose: str) -> None:
    with conn.cursor() as cur:
        cur.execute("SHOW transaction_read_only")
        row = dict(cur.fetchone() or {})
    if row.get("transaction_read_only") != "on":
        raise PromotionError(
            "READ_ONLY_VERIFICATION_REQUIRED",
            f"{purpose} requires an explicit read-only PostgreSQL connection",
        )


def require_historical_v4_plan(plan: Mapping[str, object]) -> None:
    if plan.get("promotion_plan_contract_version") != 4 or plan.get("contract_version") != 4:
        raise PromotionError(
            "PROMOTION_PLAN_CONTRACT_UNSUPPORTED",
            "historical rollback inspection requires immutable plan contract v4",
        )


def require_v5_forward_plan(plan: Mapping[str, object]) -> None:
    version = plan.get("promotion_plan_contract_version")
    compatibility_version = plan.get("contract_version")
    if version in {3, 4} or compatibility_version in {3, 4}:
        raise PromotionError(
            "PROMOTION_PLAN_CONTRACT_SUPERSEDED",
            "v3 and v4 forward promotion contracts are superseded and non-executable",
        )
    if version != 5 or compatibility_version != 5:
        raise PromotionError(
            "PROMOTION_PLAN_CONTRACT_UNSUPPORTED",
            "new forward promotion execution requires immutable plan contract v5",
        )


def promotion_attestation(plan: Mapping[str, object]) -> str:
    require_v5_forward_plan(plan)
    clients = plan["clients"]
    assert isinstance(clients, list)
    client_text = ",".join(
        f"{row['client_code']}:{row['database_uuid']}" for row in clients
    )
    return (
        "PROMOTE_ENVIRONMENT_IDENTITY "
        f"host={plan['operation_identity']['host']} head={plan['repository_head']} contract=v5 "
        f"source={plan['source_environment']} target={plan['target_environment']} "
        f"platform_uuid={plan['platform_uuid']} clients={client_text} "
        f"plan_sha256={plan_hash(plan)} UUIDS_UNCHANGED=true"
    )


def rollback_attestation(plan: Mapping[str, object], promotion_id: str) -> str:
    require_historical_v4_plan(plan)
    clients = plan["clients"]
    assert isinstance(clients, list)
    client_text = ",".join(
        f"{row['client_code']}:{row['database_uuid']}" for row in clients
    )
    return (
        "ROLLBACK_ENVIRONMENT_IDENTITY "
        f"promotion_id={promotion_id} source={plan['target_environment']} "
        f"target={plan['source_environment']} platform_uuid={plan['platform_uuid']} "
        f"clients={client_text} plan_sha256={plan_hash(plan)} UUIDS_UNCHANGED=true"
    )


_ACTIVE_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def _parse_env_bytes(raw: bytes) -> tuple[dict[str, str], list[str]]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PromotionError("RUNTIME_FILE_MALFORMED", "runtime environment file must be UTF-8") from exc
    values: dict[str, str] = {}
    active_keys: list[str] = []
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _ACTIVE_ASSIGNMENT.match(line)
        if not match:
            raise PromotionError("RUNTIME_FILE_MALFORMED", f"malformed active line {number}")
        key, raw_value = match.groups()
        if key in values:
            raise PromotionError("RUNTIME_FILE_DUPLICATE_KEY", f"duplicate active definition: {key}")
        value = raw_value.strip()
        if value and value[0:1] in {"'", '"'}:
            quote = value[0]
            if len(value) < 2 or value[-1] != quote:
                raise PromotionError("RUNTIME_FILE_MALFORMED", f"unterminated quote on line {number}")
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
        active_keys.append(key)
    return values, active_keys


def approved_runtime_paths(repo_root: Path) -> tuple[Path, ...]:
    del repo_root
    return (CANONICAL_IDENTITY_FILE,)


def inspect_runtime_file(path: Path, *, allowed_paths: Iterable[Path], expected_owner_uid: int | None = None) -> RuntimeFileState:
    absolute = path.absolute()
    allowed = {candidate.absolute() for candidate in allowed_paths}
    if absolute not in allowed:
        raise PromotionError("RUNTIME_FILE_NOT_ALLOWLISTED", "runtime environment file must be the canonical identity file", exit_code=EXIT_INVALID)
    try:
        identity = inspect_identity_file(absolute, expected_uid=expected_owner_uid)
    except IdentityFileError as exc:
        raise PromotionError(exc.code, str(exc)) from exc
    return RuntimeFileState(
        identity.path,
        {TARGET_ENVIRONMENT_KEY: identity.environment},
        identity.checksum,
        identity.uid,
        identity.gid,
        identity.mode,
    )

def _render_environment_update(raw: bytes, source: str, target: str) -> bytes:
    text = raw.decode("utf-8")
    matches: list[int] = []
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        match = _ACTIVE_ASSIGNMENT.match(line.rstrip("\r\n"))
        if match and match.group(1) == TARGET_ENVIRONMENT_KEY:
            matches.append(index)
    if len(matches) != 1:
        raise PromotionError("RUNTIME_FILE_TARGET_KEY_COUNT", f"runtime file must define {TARGET_ENVIRONMENT_KEY} exactly once")
    index = matches[0]
    match = _ACTIVE_ASSIGNMENT.match(lines[index].rstrip("\r\n"))
    assert match is not None
    current = match.group(2).strip().strip("'\"")
    if current != source:
        raise PromotionError("RUNTIME_FILE_SOURCE_MISMATCH", "runtime environment does not match the expected source")
    ending = "\r\n" if lines[index].endswith("\r\n") else "\n" if lines[index].endswith("\n") else ""
    prefix = lines[index][: lines[index].index(match.group(1))]
    export = "export " if lines[index][len(prefix):].startswith("export ") else ""
    lines[index] = f"{prefix}{export}{TARGET_ENVIRONMENT_KEY}={target}{ending}"
    rendered = "".join(lines).encode("utf-8")
    parsed, _ = _parse_env_bytes(rendered)
    if parsed.get(TARGET_ENVIRONMENT_KEY) != target:
        raise PromotionError("RUNTIME_FILE_POSTWRITE_PARSE", "rendered runtime environment did not parse to the target")
    return rendered


def atomic_update_runtime_file(
    state: RuntimeFileState,
    *,
    source: str,
    target: str,
    promotion_id: str,
    fail_before_replace: bool = False,
) -> dict[str, str]:
    current = state.path.read_bytes()
    if sha256_bytes(current) != state.checksum:
        raise PromotionError("RUNTIME_FILE_CHANGED", "runtime environment file changed after planning")
    rendered = _render_environment_update(current, source, target)
    backup = state.path.with_name(f"{state.path.name}.promotion-{promotion_id}.bak")
    if backup.is_symlink():
        raise PromotionError("RUNTIME_FILE_BACKUP_EXISTS", "promotion runtime backup must not be a symlink")
    if backup.exists():
        backup_stat = backup.stat()
        if (
            not stat.S_ISREG(backup_stat.st_mode)
            or stat.S_IMODE(backup_stat.st_mode) != 0o600
            or backup_stat.st_uid != state.uid
            or backup_stat.st_gid != state.gid
            or sha256_bytes(backup.read_bytes()) != state.checksum
        ):
            raise PromotionError("RUNTIME_FILE_BACKUP_EXISTS", "existing promotion backup does not match the planned source")
    else:
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            view = memoryview(current)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise PromotionError("RUNTIME_FILE_BACKUP_WRITE", "runtime backup write did not progress")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chown(backup, state.uid, state.gid)
        os.chmod(backup, 0o600)
        backup_fd = os.open(backup, os.O_RDONLY)
        try:
            os.fsync(backup_fd)
        finally:
            os.close(backup_fd)
        backup_directory_fd = os.open(state.path.parent, os.O_RDONLY)
        try:
            os.fsync(backup_directory_fd)
        finally:
            os.close(backup_directory_fd)
    temp_fd, temp_name = tempfile.mkstemp(prefix=f".{state.path.name}.promotion-", dir=state.path.parent)
    try:
        os.fchmod(temp_fd, state.mode)
        os.fchown(temp_fd, state.uid, state.gid)
        rendered_view = memoryview(rendered)
        while rendered_view:
            written = os.write(temp_fd, rendered_view)
            if written <= 0:
                raise PromotionError("RUNTIME_FILE_TEMP_WRITE", "runtime temporary-file write did not progress")
            rendered_view = rendered_view[written:]
        os.fsync(temp_fd)
        os.close(temp_fd)
        temp_fd = -1
        _parse_env_bytes(Path(temp_name).read_bytes())
        if fail_before_replace:
            raise PromotionError("INJECTED_WRITE_FAILURE", "injected failure before atomic replace")
        os.replace(temp_name, state.path)
        directory_fd = os.open(state.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temp_fd >= 0:
            os.close(temp_fd)
        try:
            Path(temp_name).unlink()
        except FileNotFoundError:
            pass
    after = inspect_runtime_file(state.path, allowed_paths=(state.path,), expected_owner_uid=state.uid)
    if after.values.get(TARGET_ENVIRONMENT_KEY) != target:
        raise PromotionError("RUNTIME_FILE_POSTWRITE_PARSE", "runtime environment file verification failed")
    return {"backup_path": str(backup), "before_sha256": state.checksum, "after_sha256": after.checksum}


def atomic_restore_runtime_file(
    state: RuntimeFileState,
    *,
    backup_path: Path,
    expected_backup_sha256: str,
    expected_target_environment: str,
) -> str:
    if state.values.get(TARGET_ENVIRONMENT_KEY) != expected_target_environment:
        raise PromotionError("ROLLBACK_RUNTIME_ENVIRONMENT_MISMATCH", "runtime file is not at the promotion target")
    if backup_path.is_symlink() or not backup_path.is_file():
        raise PromotionError("ROLLBACK_BACKUP_INVALID", "runtime backup is missing or is a symlink")
    backup_raw = backup_path.read_bytes()
    if sha256_bytes(backup_raw) != expected_backup_sha256:
        raise PromotionError("ROLLBACK_BACKUP_CHECKSUM", "runtime backup checksum does not match the journal")
    _parse_env_bytes(backup_raw)
    temp_fd, temp_name = tempfile.mkstemp(prefix=f".{state.path.name}.rollback-", dir=state.path.parent)
    try:
        os.fchmod(temp_fd, state.mode)
        os.fchown(temp_fd, state.uid, state.gid)
        backup_view = memoryview(backup_raw)
        while backup_view:
            written = os.write(temp_fd, backup_view)
            if written <= 0:
                raise PromotionError("ROLLBACK_TEMP_WRITE", "rollback temporary-file write did not progress")
            backup_view = backup_view[written:]
        os.fsync(temp_fd)
        os.close(temp_fd)
        temp_fd = -1
        os.replace(temp_name, state.path)
        directory_fd = os.open(state.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temp_fd >= 0:
            os.close(temp_fd)
        try:
            Path(temp_name).unlink()
        except FileNotFoundError:
            pass
    return sha256_bytes(state.path.read_bytes())



def privileged_helper_attestation(state: RuntimeFileState, target: str) -> str:
    return (
        "SET_LOG_PLATFORM_ENVIRONMENT_IDENTITY "
        f"source={state.values[TARGET_ENVIRONMENT_KEY]} target={target} "
        f"before_sha256={state.checksum} after_sha256={intended_checksum(target)} "
        f"helper_version={HELPER_VERSION}"
    )


def privileged_helper_command(*arguments: str) -> list[str]:
    """Build the only allowed non-interactive sudo invocation for the helper."""
    return ["sudo", "-n", str(HELPER_INSTALL_PATH), *arguments]


def _raise_privileged_helper_failure(result, *, fallback_code: str, action: str) -> None:
    stderr = str(result.stderr or "").lower()
    authorization_markers = (
        "password is required",
        "not allowed to execute",
        "may not run sudo",
        "no valid sudoers sources found",
    )
    if any(marker in stderr for marker in authorization_markers):
        raise PromotionError(
            "BLOCKED_BY_CANONICAL_IDENTITY_PRIVILEGE_PATH",
            "non-interactive canonical identity helper authorization is unavailable",
        )
    raise PromotionError(fallback_code, f"canonical identity helper refused or failed {action}")


def invoke_identity_helper(state: RuntimeFileState, *, source: str, target: str) -> dict[str, str]:
    if state.path != CANONICAL_IDENTITY_FILE:
        raise PromotionError("RUNTIME_FILE_NOT_CANONICAL", "privileged helper accepts only the canonical identity path")
    if state.values.get(TARGET_ENVIRONMENT_KEY) != source:
        raise PromotionError("RUNTIME_FILE_SOURCE_MISMATCH", "canonical identity is not at the planned source")
    attestation = privileged_helper_attestation(state, target)
    command = privileged_helper_command(
        "--set-environment", target,
        "--expected-old-value", source,
        "--expected-current-sha256", state.checksum,
        "--attestation", attestation,
    )
    result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        _raise_privileged_helper_failure(
            result, fallback_code="PRIVILEGED_HELPER_FAILED", action="the update"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PromotionError("PRIVILEGED_HELPER_OUTPUT", "canonical identity helper returned invalid output") from exc
    expected_after = intended_checksum(target)
    if payload.get("after_sha256") != expected_after or payload.get("environment") != target:
        raise PromotionError("PRIVILEGED_HELPER_VERIFY", "canonical identity helper result mismatched the immutable plan")
    return {
        "backup_path": str(payload.get("backup_path") or ""),
        "before_sha256": state.checksum,
        "after_sha256": expected_after,
    }


def invoke_identity_restore_helper(
    state: RuntimeFileState, *, backup_path: Path, expected_backup_sha256: str,
) -> str:
    attestation = (
        "RESTORE_LOG_PLATFORM_ENVIRONMENT_IDENTITY "
        f"current={state.values[TARGET_ENVIRONMENT_KEY]} current_sha256={state.checksum} "
        f"backup_sha256={expected_backup_sha256} helper_version={HELPER_VERSION}"
    )
    result = subprocess.run(
        privileged_helper_command("--restore-backup", str(backup_path),
         "--expected-old-value", str(state.values[TARGET_ENVIRONMENT_KEY]),
         "--expected-current-sha256", state.checksum,
         "--expected-backup-sha256", expected_backup_sha256,
         "--attestation", attestation),
        check=False, capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        _raise_privileged_helper_failure(
            result, fallback_code="PRIVILEGED_HELPER_RESTORE_FAILED", action="restore"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PromotionError("PRIVILEGED_HELPER_OUTPUT", "canonical identity helper returned invalid restore output") from exc
    if payload.get("after_sha256") != expected_backup_sha256:
        raise PromotionError("PRIVILEGED_HELPER_VERIFY", "restored canonical identity checksum mismatched the journal")
    return expected_backup_sha256

def resolve_secret(secret_ref: str, values: Mapping[str, str]) -> str:
    if secret_ref.startswith("file:"):
        path = Path(secret_ref[5:])
        if path.is_symlink() or not path.is_file():
            raise PromotionError("CLIENT_SECRET_UNAVAILABLE", "client password secret file is unavailable")
        value = path.read_text(encoding="utf-8").strip()
    else:
        value = str(values.get(secret_ref) or os.environ.get(secret_ref) or "").strip()
    if not value:
        raise PromotionError("CLIENT_SECRET_UNAVAILABLE", "client database password reference could not be resolved")
    return value


def connect_database(
    *, host: str, port: int, dbname: str, user: str, password: str,
    read_only: bool = False, autocommit: bool = False,
):
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise PromotionError("PSYCOPG_UNAVAILABLE", "psycopg is required", exit_code=EXIT_INVALID) from exc
    conn = psycopg.connect(
        host=host, port=port, dbname=dbname, user=user, password=password,
        row_factory=dict_row, autocommit=autocommit,
    )
    if read_only:
        try:
            conn.execute("SET default_transaction_read_only = on")
            if not autocommit:
                conn.commit()
            with conn.cursor() as cur:
                cur.execute("SHOW default_transaction_read_only")
                configured = dict(cur.fetchone() or {}).get("default_transaction_read_only")
            if configured != "on":
                raise PromotionError(
                    "READ_ONLY_VERIFICATION_REQUIRED",
                    "PostgreSQL did not establish the requested read-only session default",
                )
            if not autocommit:
                conn.rollback()
        except Exception:
            conn.close()
            raise
    return conn


def marker_snapshot(conn, *, expected_role: str, expected_database: str, expected_client_code: str | None) -> dict[str, str | None]:
    with conn.cursor() as cur:
        cur.execute("SELECT current_database() AS database_name")
        connected = dict(cur.fetchone() or {}).get("database_name")
        cur.execute(
            """SELECT identity_key, environment, database_identity_id::text AS database_uuid,
                      database_role, database_name, client_code
                 FROM ops_control.environment_identity ORDER BY identity_key"""
        )
        rows = [dict(row) for row in cur.fetchall()]
    if connected != expected_database:
        raise PromotionError("DATABASE_NAME_MISMATCH", "connected database name is not the expected database")
    if len(rows) != 1 or rows[0].get("identity_key") != "primary":
        raise PromotionError("MARKER_CARDINALITY", "database must contain exactly one primary identity marker")
    row = rows[0]
    if row.get("database_role") != expected_role or row.get("database_name") != expected_database:
        raise PromotionError("MARKER_IDENTITY_MISMATCH", "database marker role or name is inconsistent")
    if row.get("client_code") != expected_client_code:
        raise PromotionError("MARKER_CLIENT_MISMATCH", "database marker client code is inconsistent")
    row["database_uuid"] = canonical_uuid(row.get("database_uuid"), "database marker UUID")
    return row


def update_marker_environment(
    conn,
    *,
    expected_database: str,
    expected_uuid: str,
    expected_role: str,
    expected_client_code: str | None,
    source: str,
    target: str,
) -> str:
    """Commit an environment-only platform marker mutation from an IDLE session."""
    require_idle_connection(conn, code="PLATFORM_TRANSACTION_NOT_IDLE", purpose="platform marker mutation")
    body_completed = False
    result = "updated"
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute("SELECT current_database() AS database_name")
                if dict(cur.fetchone() or {}).get("database_name") != expected_database:
                    raise PromotionError("DATABASE_NAME_MISMATCH", "connected database name is not expected")
                cur.execute(
                    """SELECT identity_key, environment, database_identity_id::text AS database_uuid,
                              database_role, database_name, client_code
                         FROM ops_control.environment_identity
                        WHERE identity_key='primary' FOR UPDATE"""
                )
                rows = [dict(row) for row in cur.fetchall()]
                if len(rows) != 1:
                    raise PromotionError("MARKER_CARDINALITY", "expected exactly one locked primary marker")
                row = rows[0]
                immutable = (row.get("database_uuid"), row.get("database_role"), row.get("database_name"), row.get("client_code"))
                expected = (expected_uuid, expected_role, expected_database, expected_client_code)
                if immutable != expected:
                    raise PromotionError("MARKER_IDENTITY_MISMATCH", "locked marker immutable identity does not match the plan")
                if row.get("environment") == target:
                    result = "already_completed"
                elif row.get("environment") != source:
                    raise PromotionError("MARKER_SOURCE_MISMATCH", "locked marker is neither source nor target environment")
                else:
                    cur.execute(
                        """UPDATE ops_control.environment_identity SET environment=%s
                             WHERE identity_key='primary' AND environment=%s
                               AND database_identity_id=%s::uuid""",
                        (target, source, expected_uuid),
                    )
                    if cur.rowcount != 1:
                        raise PromotionError("MARKER_UPDATE_COUNT", "marker update did not affect exactly one row")
                cur.execute(
                    """SELECT environment, database_identity_id::text AS database_uuid
                         FROM ops_control.environment_identity WHERE identity_key='primary'"""
                )
                verified = dict(cur.fetchone() or {})
                if verified != {"environment": target, "database_uuid": expected_uuid}:
                    raise PromotionError("MARKER_POSTWRITE_VERIFY", "marker post-write verification failed")
                body_completed = True
        return result
    except PromotionError:
        conn.rollback()
        raise
    except Exception as exc:
        conn.rollback()
        code = "PLATFORM_COMMIT_FAILED" if body_completed else "PLATFORM_MARKER_TRANSACTION_FAILED"
        raise PromotionError(code, "platform marker transaction did not commit durably") from exc


CLIENT_PROMOTION_SIGNATURE = "ops_control.promote_environment_identity_v1(uuid,text,text,text,uuid,text)"


def client_promotion_capability(conn, *, expected_user: str) -> dict[str, object]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT to_regprocedure(%s)::text AS signature,
                      CASE WHEN to_regprocedure(%s) IS NULL THEN false
                           ELSE has_function_privilege(current_user, %s, 'EXECUTE') END AS can_execute,
                      has_table_privilege(current_user, 'ops_control.environment_identity', 'UPDATE') AS table_update,
                      has_column_privilege(current_user, 'ops_control.environment_identity', 'environment', 'UPDATE') AS column_update,
                      current_user AS database_user""",
            (CLIENT_PROMOTION_SIGNATURE, CLIENT_PROMOTION_SIGNATURE, CLIENT_PROMOTION_SIGNATURE),
        )
        row = dict(cur.fetchone() or {})
    available = row.get("signature") == CLIENT_PROMOTION_SIGNATURE and row.get("can_execute") is True and row.get("database_user") == expected_user
    safe = available and row.get("table_update") is False and row.get("column_update") is False
    return {**row, "available": available, "least_privilege_safe": safe, "version": 1 if available else None}


def promote_client_marker(
    conn, *, expected_database: str, expected_uuid: str, expected_user: str,
    source: str, target: str, promotion_id: str, attestation_hash: str,
) -> str:
    """Run migration 045 in one top-level transaction and commit on success."""
    require_idle_connection(conn, code="CLIENT_MUTATION_TRANSACTION_NOT_IDLE", purpose="guarded client marker mutation")
    body_completed = False
    try:
        with conn.transaction():
            capability = client_promotion_capability(conn, expected_user=expected_user)
            if not capability["available"]:
                raise PromotionError("CLIENT_PROMOTION_PRIMITIVE_MISSING", "reviewed client promotion function is unavailable")
            if not capability["least_privilege_safe"]:
                raise PromotionError("CLIENT_PROMOTION_PRIVILEGE_UNSAFE", "runtime user retains direct marker UPDATE")
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT database_uuid::text, old_environment, new_environment,
                              database_name, changed_row_count
                         FROM ops_control.promote_environment_identity_v1(
                              %s::uuid,%s,%s,'client_business',%s::uuid,%s)""",
                    (expected_uuid, source, target, promotion_id, attestation_hash),
                )
                rows = [dict(row) for row in cur.fetchall()]
                if len(rows) != 1:
                    raise PromotionError("CLIENT_PROMOTION_RESULT_CARDINALITY", "client primitive returned an unexpected row count")
                row = rows[0]
                if (row.get("database_uuid") != expected_uuid or row.get("new_environment") != target
                    or row.get("database_name") != expected_database or row.get("changed_row_count") not in (0, 1)):
                    raise PromotionError("CLIENT_PROMOTION_RESULT_MISMATCH", "client primitive result did not match the immutable plan")
                if row.get("changed_row_count") == 1 and row.get("old_environment") != source:
                    raise PromotionError("CLIENT_PROMOTION_RESULT_MISMATCH", "client primitive returned the wrong source environment")
                cur.execute(
                    """SELECT environment, database_identity_id::text AS database_uuid
                         FROM ops_control.environment_identity WHERE identity_key='primary'"""
                )
                verified = dict(cur.fetchone() or {})
                if verified != {"environment": target, "database_uuid": expected_uuid}:
                    raise PromotionError("CLIENT_PROMOTION_IN_TRANSACTION_VERIFY", "client marker did not match inside the guarded transaction")
                body_completed = True
        return "already_completed" if row["changed_row_count"] == 0 else "updated"
    except PromotionError:
        conn.rollback()
        raise
    except Exception as exc:
        conn.rollback()
        code = "CLIENT_MARKER_COMMIT_FAILED" if body_completed else "CLIENT_MARKER_TRANSACTION_FAILED"
        raise PromotionError(code, "guarded client marker transaction did not commit durably") from exc


def mutate_client_marker_durably(
    *, open_connection: Callable[[bool], Any], expected_database: str,
    expected_uuid: str, expected_user: str, expected_client_code: str,
    source: str, target: str, promotion_id: str, attestation_hash: str,
) -> dict[str, object]:
    """Commit one guarded mutation and verify it using a fresh read-only session."""
    mutation_conn = open_connection(False)
    try:
        require_idle_connection(mutation_conn, code="CLIENT_MUTATION_TRANSACTION_NOT_IDLE", purpose=f"client marker mutation for {expected_client_code}")
        result = promote_client_marker(
            mutation_conn, expected_database=expected_database, expected_uuid=expected_uuid,
            expected_user=expected_user, source=source, target=target,
            promotion_id=promotion_id, attestation_hash=attestation_hash,
        )
        require_idle_connection(mutation_conn, code="CLIENT_MARKER_COMMIT_FAILED", purpose=f"post-commit client marker state for {expected_client_code}")
    finally:
        mutation_conn.close()
    verification_conn = open_connection(True)
    try:
        require_read_only_connection(verification_conn, purpose=f"durability verification for {expected_client_code}")
        marker = marker_snapshot(
            verification_conn, expected_role="client_business",
            expected_database=expected_database, expected_client_code=expected_client_code,
        )
        capability = client_promotion_capability(verification_conn, expected_user=expected_user)
        if marker.get("database_uuid") != expected_uuid or marker.get("environment") != target:
            raise PromotionError("CLIENT_POST_COMMIT_DURABILITY_MISMATCH", f"fresh connection did not observe the durable target marker: {expected_client_code}")
        if not capability.get("least_privilege_safe"):
            raise PromotionError("CLIENT_POST_COMMIT_CAPABILITY_MISMATCH", f"fresh connection did not retain least-privilege capability: {expected_client_code}")
        return {"result": result, "marker": marker, "capability": capability}
    finally:
        verification_conn.close()


def load_selected_clients(
    platform_conn,
    expected_clients: Mapping[str, str],
    *,
    source: str,
    target: str | None = None,
) -> list[ClientPlan]:
    codes = sorted(expected_clients)
    with platform_conn.cursor() as cur:
        cur.execute(
            """SELECT client_id::text AS client_id, client_code, enabled,
                      client_db_host, client_db_port, client_db_name, client_db_user,
                      client_db_password_secret_ref, client_db_environment,
                      client_db_identity_id::text AS client_db_identity_id
                 FROM workflow_a_control.client_account
                WHERE client_code = ANY(%s) ORDER BY client_code""",
            (codes,),
        )
        rows = [dict(row) for row in cur.fetchall()]
    if len(rows) != len(codes):
        found = {str(row.get("client_code")) for row in rows}
        raise PromotionError("CLIENT_NOT_FOUND", f"selected client records missing: {', '.join(sorted(set(codes) - found))}")
    result: list[ClientPlan] = []
    for row in rows:
        code = str(row["client_code"])
        if row.get("enabled") is not True:
            raise PromotionError("CLIENT_DISABLED", f"selected client is not enabled: {code}")
        accepted_environments = {source} if target is None else {source, target}
        if row.get("client_db_environment") not in accepted_environments:
            raise PromotionError(
                "CONTROL_PLANE_SOURCE_MISMATCH",
                f"client expected environment is neither allowed resume value: {code}",
            )
        observed_uuid = canonical_uuid(row.get("client_db_identity_id"), f"control-plane UUID for {code}")
        if observed_uuid != expected_clients[code]:
            raise PromotionError("CLIENT_UUID_MISMATCH", f"control-plane database UUID mismatch for {code}")
        result.append(ClientPlan(
            client_id=canonical_uuid(row["client_id"], f"client id for {code}"),
            client_code=code,
            database_name=str(row["client_db_name"]),
            database_user=str(row["client_db_user"]),
            database_host=str(row["client_db_host"]),
            database_port=int(row["client_db_port"]),
            database_uuid=observed_uuid,
            password_secret_ref=str(row["client_db_password_secret_ref"]),
        ))
    return result


def update_control_plane(platform_conn, clients: Iterable[ClientPlan], *, source: str, target: str) -> str:
    client_list = list(clients)
    require_idle_connection(platform_conn, code="PLATFORM_TRANSACTION_NOT_IDLE", purpose="selected control-plane mutation")
    body_completed = False
    try:
        with platform_conn.transaction():
            with platform_conn.cursor() as cur:
                statuses: list[str] = []
                for client in client_list:
                    cur.execute(
                        """SELECT client_id::text AS client_id, client_code, client_db_name,
                                  client_db_environment, client_db_identity_id::text AS database_uuid
                             FROM workflow_a_control.client_account
                            WHERE client_id=%s::uuid AND client_code=%s FOR UPDATE""",
                        (client.client_id, client.client_code),
                    )
                    rows = [dict(row) for row in cur.fetchall()]
                    if len(rows) != 1:
                        raise PromotionError("CONTROL_PLANE_CARDINALITY", f"client control-plane row missing: {client.client_code}")
                    row = rows[0]
                    if row.get("client_db_name") != client.database_name or row.get("database_uuid") != client.database_uuid:
                        raise PromotionError("CONTROL_PLANE_IDENTITY_MISMATCH", f"client immutable control-plane identity mismatch: {client.client_code}")
                    current = row.get("client_db_environment")
                    if current == target:
                        statuses.append("already_completed")
                        continue
                    if current != source:
                        raise PromotionError("CONTROL_PLANE_SOURCE_MISMATCH", f"client expected environment is neither source nor target: {client.client_code}")
                    cur.execute(
                        """UPDATE workflow_a_control.client_account
                              SET client_db_environment=%s
                            WHERE client_id=%s::uuid AND client_code=%s
                              AND client_db_identity_id=%s::uuid
                              AND client_db_environment=%s""",
                        (target, client.client_id, client.client_code, client.database_uuid, source),
                    )
                    if cur.rowcount != 1:
                        raise PromotionError("CONTROL_PLANE_UPDATE_COUNT", f"control-plane update count was not one: {client.client_code}")
                    statuses.append("updated")
                cur.execute(
                    """SELECT client_code, client_id::text AS client_id, client_db_name,
                              client_db_environment, client_db_identity_id::text AS database_uuid
                         FROM workflow_a_control.client_account
                        WHERE client_code = ANY(%s) ORDER BY client_code""",
                    ([client.client_code for client in client_list],),
                )
                verified = {row["client_code"]: dict(row) for row in cur.fetchall()}
                for client in client_list:
                    row = verified.get(client.client_code, {})
                    if (row.get("client_id"), row.get("client_db_name"), row.get("database_uuid"), row.get("client_db_environment")) != (
                        client.client_id, client.database_name, client.database_uuid, target
                    ):
                        raise PromotionError("CONTROL_PLANE_POSTWRITE_VERIFY", f"control-plane verification failed: {client.client_code}")
                body_completed = True
        return "already_completed" if statuses and all(item == "already_completed" for item in statuses) else "updated"
    except PromotionError:
        platform_conn.rollback()
        raise
    except Exception as exc:
        platform_conn.rollback()
        code = "PLATFORM_COMMIT_FAILED" if body_completed else "CONTROL_PLANE_TRANSACTION_FAILED"
        raise PromotionError(code, "selected control-plane transaction did not commit durably") from exc


def rollback_platform_surfaces(
    conn, *, clients: Iterable[ClientPlan], expected_database: str,
    expected_uuid: str, source: str, target: str,
) -> dict[str, object]:
    """Commit platform marker and selected control-plane reversal atomically."""
    client_list = list(clients)
    require_idle_connection(conn, code="PLATFORM_TRANSACTION_NOT_IDLE", purpose="rollback platform transaction")
    body_completed = False
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT environment, database_identity_id::text AS database_uuid,
                              database_role, database_name, client_code
                         FROM ops_control.environment_identity
                        WHERE identity_key='primary' FOR UPDATE"""
                )
                marker = dict(cur.fetchone() or {})
                if (marker.get("database_uuid"), marker.get("database_role"), marker.get("database_name"), marker.get("client_code")) != (expected_uuid, "platform", expected_database, None):
                    raise PromotionError("PLATFORM_IDENTITY_MISMATCH", "rollback platform marker identity differs from plan")
                if marker.get("environment") == source:
                    cur.execute(
                        """UPDATE ops_control.environment_identity SET environment=%s
                             WHERE identity_key='primary' AND environment=%s
                               AND database_identity_id=%s::uuid""",
                        (target, source, expected_uuid),
                    )
                    if cur.rowcount != 1:
                        raise PromotionError("PLATFORM_MARKER_UPDATE_COUNT", "rollback platform marker update count was not one")
                elif marker.get("environment") != target:
                    raise PromotionError("PLATFORM_SOURCE_MISMATCH", "rollback platform marker is neither source nor target")
                for client in client_list:
                    cur.execute(
                        """SELECT client_id::text AS client_id, client_db_name,
                                  client_db_environment, client_db_identity_id::text AS database_uuid
                             FROM workflow_a_control.client_account
                            WHERE client_code=%s FOR UPDATE""",
                        (client.client_code,),
                    )
                    row = dict(cur.fetchone() or {})
                    if (row.get("client_id"), row.get("client_db_name"), row.get("database_uuid")) != (client.client_id, client.database_name, client.database_uuid):
                        raise PromotionError("CONTROL_PLANE_IDENTITY_MISMATCH", f"rollback control-plane identity differs: {client.client_code}")
                    if row.get("client_db_environment") == source:
                        cur.execute(
                            """UPDATE workflow_a_control.client_account
                                  SET client_db_environment=%s
                                WHERE client_id=%s::uuid AND client_code=%s
                                  AND client_db_identity_id=%s::uuid
                                  AND client_db_environment=%s""",
                            (target, client.client_id, client.client_code, client.database_uuid, source),
                        )
                        if cur.rowcount != 1:
                            raise PromotionError("CONTROL_PLANE_UPDATE_COUNT", f"rollback control-plane update count was not one: {client.client_code}")
                    elif row.get("client_db_environment") != target:
                        raise PromotionError("CONTROL_PLANE_SOURCE_MISMATCH", f"rollback control-plane value is neither source nor target: {client.client_code}")
                cur.execute(
                    "SELECT environment, database_identity_id::text AS database_uuid FROM ops_control.environment_identity WHERE identity_key='primary'"
                )
                verified_marker = dict(cur.fetchone() or {})
                cur.execute(
                    """SELECT client_code, client_db_environment, client_db_identity_id::text AS database_uuid
                         FROM workflow_a_control.client_account
                        WHERE client_code=ANY(%s) ORDER BY client_code""",
                    ([client.client_code for client in client_list],),
                )
                controls = {row["client_code"]: dict(row) for row in cur.fetchall()}
                if verified_marker != {"environment": target, "database_uuid": expected_uuid}:
                    raise PromotionError("PLATFORM_POSTWRITE_VERIFY", "rollback platform marker verification failed")
                for client in client_list:
                    row = controls.get(client.client_code, {})
                    if row.get("client_db_environment") != target or row.get("database_uuid") != client.database_uuid:
                        raise PromotionError("CONTROL_PLANE_POSTWRITE_VERIFY", f"rollback control-plane verification failed: {client.client_code}")
                body_completed = True
        return {"platform_marker": target, "control_plane": {client.client_code: target for client in client_list}}
    except PromotionError:
        conn.rollback()
        raise
    except Exception as exc:
        conn.rollback()
        code = "PLATFORM_COMMIT_FAILED" if body_completed else "PLATFORM_ROLLBACK_TRANSACTION_FAILED"
        raise PromotionError(code, "rollback platform transaction did not commit durably") from exc


def build_plan(
    *,
    source: str,
    target: str,
    platform_uuid: str,
    platform_database: str,
    runtime_file: RuntimeFileState,
    clients: Iterable[ClientPlan],
    backup_reference: str,
    runtime_convergence: Mapping[str, object],
    repository_head: str,
    operation_identity: Mapping[str, object],
    canonical_identity: Mapping[str, object],
    database_bindings: Mapping[str, object],
    runtime_bindings: Mapping[str, object],
    checkpoint_binding: Mapping[str, object],
    recovery_binding: Mapping[str, object],
    effective_sudo_policy: Mapping[str, object],
    implementation_assets: Sequence[Mapping[str, object]],
    historical_recovery: Mapping[str, object],
    excluded_actions: Sequence[str],
) -> dict[str, object]:
    ordered_clients = sorted(clients, key=lambda row: row.client_code)
    steps = [f"{STEP_CLIENT_PREFIX}{client.client_code}" for client in ordered_clients]
    steps += [STEP_CONTROL_PLANE, STEP_PLATFORM_MARKER, STEP_RUNTIME_FILE,
              STEP_RUNTIME_RELOAD, STEP_RUNTIME_PROCESSES, STEP_FINAL_VERIFY]
    helper = dict(runtime_convergence.get("helper") or {})
    consumers = list(runtime_convergence.get("consumers") or [])
    ordered_actions = [
        {"order": 1, "action": "create_and_freeze_promotion_journal"},
        *[{"order": index + 2, "action": f"promote_client_marker:{client.client_code}"}
          for index, client in enumerate(ordered_clients)],
        {"order": len(ordered_clients) + 2, "action": "update_selected_control_plane_environments"},
        {"order": len(ordered_clients) + 3, "action": "update_platform_marker"},
        {"order": len(ordered_clients) + 4, "action": "atomically_update_canonical_identity"},
        {"order": len(ordered_clients) + 5, "action": "pause_for_separately_approved_runtime_convergence"},
        {"order": len(ordered_clients) + 6, "action": "verify_runtime_processes_after_resume"},
        {"order": len(ordered_clients) + 7, "action": "final_cross_surface_verification_and_complete_journal"},
    ]
    return {
        "contract_version": 5,
        "promotion_plan_contract_version": 5,
        "operation_identity": dict(operation_identity),
        "repository_head": repository_head,
        "source_environment": source,
        "target_environment": target,
        "platform_uuid": platform_uuid,
        "platform_database": platform_database,
        "canonical_identity": dict(canonical_identity),
        "database_bindings": dict(database_bindings),
        "runtime_bindings": dict(runtime_bindings),
        "checkpoint_binding": dict(checkpoint_binding),
        "recovery_binding": dict(recovery_binding),
        "effective_sudo_policy": dict(effective_sudo_policy),
        "implementation_assets": sorted(
            (dict(row) for row in implementation_assets),
            key=lambda row: str(row["path"]),
        ),
        "historical_recovery": dict(historical_recovery),
        "canonical_identity_file": str(runtime_file.path),
        "runtime_environment_file": str(runtime_file.path),
        "runtime_expected_old_value": source,
        "runtime_target_value": target,
        "runtime_file_before_sha256": runtime_file.checksum,
        "runtime_file_after_sha256": intended_checksum(target),
        "privileged_helper": {
            "path": str(HELPER_INSTALL_PATH),
            "version": HELPER_VERSION,
            "sha256": helper.get("installed_sha256"),
            "source_sha256": helper.get("source_sha256"),
            "dependency_source_path": str(Path(__file__).with_name("environment_identity_file.py")),
            "dependency_source_sha256": helper.get("dependency_source_sha256"),
            "dependency_path": str(HELPER_LIBRARY_INSTALL_PATH),
            "dependency_sha256": helper.get("dependency_sha256"),
        },
        "consumer_inventory": consumers,
        "consumer_installation_evidence": list(runtime_convergence.get("installation_evidence") or []),
        "required_service_actions": [row.get("refresh_action") for row in consumers if row.get("refresh_action") and not str(row.get("refresh_action")).startswith("none")],
        "post_promotion_runtime_requirements": {
            "systemd_api_restart_required": True,
            "docker_api_recreation_required": True,
            "prune": "next_invocation", "backup": "next_invocation",
            "timer_restart_required": False, "manual_consumers": "next_invocation",
        },
        "excluded_actions": sorted(str(action) for action in excluded_actions),
        "ordered_actions": ordered_actions,
        "runtime_verification_commands": [
            "systemctl show log-platform-api.service --property=MainPID",
            "docker compose ps api",
            "ops/promote_environment_identity.py --check-production-readiness <same immutable arguments>",
        ],
        "runtime_convergence_state": runtime_convergence.get("classification"),
        "backup_reference": backup_reference,
        "clients": [client.public_dict() for client in ordered_clients],
        "steps": steps,
        "uuid_policy": "preserve_existing_database_uuids",
        "client_marker_write_contract": "ops_control.promote_environment_identity_v1",
    }

def validate_backup_reference(path: Path, plan: Mapping[str, object]) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise PromotionError("BACKUP_REFERENCE_INVALID", "backup/checkpoint reference must be an existing regular non-symlink file")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise PromotionError("BACKUP_REFERENCE_INVALID", "backup/checkpoint reference is not valid JSON") from exc
    if not isinstance(data, dict):
        raise PromotionError("BACKUP_REFERENCE_INVALID", "backup/checkpoint reference must be a JSON object")
    platform_uuid = data.get("platform_uuid") or data.get("platform_identity_id")
    environment = data.get("environment") or data.get("source_environment")
    if platform_uuid != plan["platform_uuid"] or environment != plan["source_environment"]:
        raise PromotionError("BACKUP_IDENTITY_MISMATCH", "backup/checkpoint identity does not match the immutable plan")
    if data.get("verified") is not True and not data.get("validation"):
        raise PromotionError("BACKUP_NOT_VERIFIED", "backup/checkpoint does not contain positive validation evidence")
    expected = {row["client_code"]: row["database_uuid"] for row in plan["clients"]}  # type: ignore[index]
    observed_clients = data.get("clients") or {}
    if isinstance(observed_clients, list):
        observed_clients = {row.get("client_code"): row.get("database_uuid") for row in observed_clients if isinstance(row, dict)}
    if not isinstance(observed_clients, dict) or any(observed_clients.get(code) != uuid for code, uuid in expected.items()):
        raise PromotionError("BACKUP_CLIENT_SCOPE_MISMATCH", "backup/checkpoint lacks verified evidence for every selected client UUID")
    return {"path": str(path), "sha256": sha256_bytes(path.read_bytes())}


def try_promotion_lock(conn) -> bool:
    if not bool(getattr(conn, "autocommit", False)):
        raise PromotionError("PROMOTION_LOCK_CONNECTION_MODE", "the advisory lock requires a dedicated autocommit connection")
    require_idle_connection(conn, code="PROMOTION_LOCK_CONNECTION_BUSY", purpose="promotion advisory lock")
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS acquired", (ADVISORY_LOCK_NAME,))
        return bool(dict(cur.fetchone() or {}).get("acquired"))


def release_promotion_lock(conn) -> None:
    if not bool(getattr(conn, "autocommit", False)):
        raise PromotionError("PROMOTION_LOCK_CONNECTION_MODE", "the advisory unlock requires the dedicated autocommit connection")
    require_idle_connection(conn, code="PROMOTION_LOCK_CONNECTION_BUSY", purpose="promotion advisory unlock")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0)) AS released", (ADVISORY_LOCK_NAME,))
            released = bool(dict(cur.fetchone() or {}).get("released"))
    except Exception as exc:
        raise PromotionError("PROMOTION_LOCK_RELEASE_FAILED", "promotion advisory unlock failed") from exc
    if not released:
        raise PromotionError("PROMOTION_LOCK_RELEASE_FAILED", "promotion advisory lock was not held by the lock session")


def create_or_resume_journal(
    conn,
    *,
    promotion_id: str | None,
    plan: Mapping[str, object],
    attestation: str,
    backup_reference: str,
) -> tuple[str, dict[str, object]]:
    require_idle_connection(conn, code="JOURNAL_TRANSACTION_NOT_IDLE", purpose="promotion journal creation or resume")
    digest = plan_hash(plan)
    attestation_digest = sha256_bytes(attestation.encode("utf-8"))
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                if promotion_id:
                    cur.execute(f"SELECT * FROM {JOURNAL_TABLE} WHERE promotion_id=%s::uuid FOR UPDATE", (promotion_id,))
                    row = cur.fetchone()
                    if not row:
                        raise PromotionError("PROMOTION_NOT_FOUND", "requested promotion journal row does not exist")
                    record = dict(row)
                    if record["plan_sha256"] != digest or record["immutable_plan_json"] != plan:
                        raise PromotionError("PROMOTION_PLAN_MISMATCH", "resume plan does not match the immutable journal plan")
                    if record["operator_attestation_hash"] != attestation_digest or record["backup_reference"] != backup_reference:
                        raise PromotionError("PROMOTION_RESUME_EVIDENCE_MISMATCH", "resume evidence does not match the journal")
                    if record["state"] in {"completed", "rolled_back"}:
                        raise PromotionError("PROMOTION_TERMINAL", "terminal promotion cannot be resumed")
                    cur.execute(
                        f"""UPDATE {JOURNAL_TABLE}
                               SET state='in_progress', started_at=COALESCE(started_at, now()),
                                   failed_at=NULL, error=NULL, updated_at=now()
                             WHERE promotion_id=%s::uuid RETURNING *""",
                        (promotion_id,),
                    )
                    result = (promotion_id, dict(cur.fetchone()))
                else:
                    new_id = str(uuid4())
                    clients = plan["clients"]
                    cur.execute(
                        f"""INSERT INTO {JOURNAL_TABLE} (
                               promotion_id, source_environment, target_environment,
                               platform_identity_id, selected_clients, immutable_plan_json,
                               plan_sha256, state, started_at, current_step, completed_steps,
                               operator_attestation_hash, backup_reference)
                             VALUES (%s::uuid,%s,%s,%s::uuid,%s::jsonb,%s::jsonb,%s,
                                     'in_progress',now(),NULL,'[]'::jsonb,%s,%s)
                             RETURNING *""",
                        (new_id, plan["source_environment"], plan["target_environment"], plan["platform_uuid"],
                         canonical_json(clients), canonical_json(plan), digest, attestation_digest, backup_reference),
                    )
                    result = (new_id, dict(cur.fetchone()))
        return result
    except PromotionError:
        conn.rollback()
        raise
    except Exception as exc:
        conn.rollback()
        raise PromotionError("JOURNAL_COMMIT_FAILED", "promotion journal creation or resume did not commit") from exc


def journal_resume_progress_v2(
    conn, promotion_id: str, *, completed_step: str, current_step: str,
) -> None:
    """Commit an interruptible resume step without freezing the next approval hash."""
    require_idle_connection(conn, code="JOURNAL_TRANSACTION_NOT_IDLE", purpose="resume-v2 progress update")
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    f"""UPDATE {JOURNAL_TABLE}
                           SET current_step=%s,
                               completed_steps=CASE WHEN completed_steps ? %s THEN completed_steps ELSE completed_steps || to_jsonb(%s::text) END,
                               updated_at=now()
                         WHERE promotion_id=%s::uuid AND state='in_progress'
                           AND resume_contract IS NULL
                           AND resume_plan_sha256 IS NULL""",
                    (current_step, completed_step, completed_step, promotion_id),
                )
                if cur.rowcount != 1:
                    raise PromotionError("RESUME_JOURNAL_STATE_DRIFT", "resume-v2 progress did not affect exactly the approved row")
    except PromotionError:
        conn.rollback()
        raise
    except Exception as exc:
        conn.rollback()
        raise PromotionError("JOURNAL_UPDATE_FAILED", "resume-v2 progress did not commit durably") from exc


def journal_step(conn, promotion_id: str, *, current_step: str | None = None, completed_step: str | None = None, file_result: Mapping[str, str] | None = None) -> None:
    require_idle_connection(conn, code="JOURNAL_TRANSACTION_NOT_IDLE", purpose="promotion journal step update")
    body_completed = False
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                if completed_step:
                    cur.execute(
                        f"""UPDATE {JOURNAL_TABLE}
                               SET current_step=%s,
                                   completed_steps=CASE WHEN completed_steps ? %s THEN completed_steps ELSE completed_steps || to_jsonb(%s::text) END,
                                   runtime_file_backup_path=COALESCE(%s, runtime_file_backup_path),
                                   runtime_file_before_sha256=COALESCE(%s, runtime_file_before_sha256),
                                   runtime_file_after_sha256=COALESCE(%s, runtime_file_after_sha256),
                                   updated_at=now()
                             WHERE promotion_id=%s::uuid AND state='in_progress'""",
                        (current_step, completed_step, completed_step,
                         (file_result or {}).get("backup_path"), (file_result or {}).get("before_sha256"),
                         (file_result or {}).get("after_sha256"), promotion_id),
                    )
                else:
                    cur.execute(f"UPDATE {JOURNAL_TABLE} SET current_step=%s, updated_at=now() WHERE promotion_id=%s::uuid AND state='in_progress'", (current_step, promotion_id))
                if cur.rowcount != 1:
                    raise PromotionError("JOURNAL_UPDATE_COUNT", "promotion journal update did not affect exactly one row")
                body_completed = True
    except PromotionError:
        conn.rollback()
        raise
    except Exception as exc:
        conn.rollback()
        code = "JOURNAL_COMMIT_FAILED" if body_completed else "JOURNAL_UPDATE_FAILED"
        raise PromotionError(code, "promotion journal step did not commit durably") from exc


def journal_finalize_forward_v5(
    conn, promotion_id: str, *, expected_completed_steps: Iterable[str],
) -> None:
    """Atomically append final verification and complete one forward-v5 journal.

    A separate completed-step append followed by a separate state transition can
    be interrupted between the two commits and leave an all-steps-complete row
    that is still `in_progress`, which no executor can resume.  One explicit
    transaction with the exact ordered prefix as its predicate removes that
    window.  Only schema-053 columns are touched, so journals created before the
    resume-v2 audit migration finalize identically.
    """
    require_idle_connection(conn, code="JOURNAL_TRANSACTION_NOT_IDLE", purpose="forward-v5 journal completion")
    expected_before = [str(step) for step in expected_completed_steps]
    if (
        not expected_before
        or expected_before[-1] != STEP_RUNTIME_PROCESSES
        or STEP_FINAL_VERIFY in expected_before
    ):
        raise PromotionError(
            "PROMOTION_EXECUTION_CONTRACT_DRIFT",
            "atomic forward finalization requires the exact pre-final completed-step prefix",
        )
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    f"""UPDATE {JOURNAL_TABLE}
                           SET completed_steps=CASE
                                   WHEN completed_steps ? %s THEN completed_steps
                                   ELSE completed_steps || to_jsonb(%s::text)
                               END,
                               state='completed', completed_at=now(), failed_at=NULL,
                               current_step=NULL, error=NULL, updated_at=now()
                         WHERE promotion_id=%s::uuid AND state='in_progress'
                           AND current_step=%s
                           AND completed_steps=%s::jsonb""",
                    (
                        STEP_FINAL_VERIFY, STEP_FINAL_VERIFY, promotion_id,
                        STEP_FINAL_VERIFY, canonical_json(expected_before),
                    ),
                )
                if cur.rowcount != 1:
                    raise PromotionError(
                        "JOURNAL_COMPLETE_COUNT",
                        "atomic forward-v5 finalization did not affect exactly one active row",
                    )
    except PromotionError:
        conn.rollback()
        raise
    except Exception as exc:
        conn.rollback()
        raise PromotionError(
            "JOURNAL_COMPLETION_FAILED",
            "atomic forward-v5 finalization did not commit durably",
        ) from exc


def journal_finalize_resume_v2(
    conn, promotion_id: str, *, resume_plan_sha256: str,
    expected_completed_steps: Iterable[str],
) -> None:
    """Atomically append final verification and complete one attested journal."""
    require_idle_connection(conn, code="JOURNAL_TRANSACTION_NOT_IDLE", purpose="resume-v2 journal completion")
    if not re.fullmatch(r"[0-9a-f]{64}", resume_plan_sha256):
        raise PromotionError(
            "RESUME_APPROVAL_ARGUMENT_MISSING",
            "resume-v2 plan hash is invalid",
            exit_code=EXIT_INVALID,
        )
    expected_before = [str(step) for step in expected_completed_steps]
    if (
        not expected_before
        or expected_before[-1] != STEP_RUNTIME_PROCESSES
        or STEP_FINAL_VERIFY in expected_before
    ):
        raise PromotionError(
            "RESUME_EXECUTION_CONTRACT_DRIFT",
            "atomic finalization requires the exact pre-final completed-step prefix",
        )
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    f"""UPDATE {JOURNAL_TABLE}
                           SET completed_steps=CASE
                                   WHEN completed_steps ? %s THEN completed_steps
                                   ELSE completed_steps || to_jsonb(%s::text)
                               END,
                               resume_contract=COALESCE(resume_contract, 'resume-v2'),
                               resume_plan_sha256=COALESCE(resume_plan_sha256, %s),
                               state='completed', completed_at=now(), failed_at=NULL,
                               current_step=NULL, error=NULL, updated_at=now()
                         WHERE promotion_id=%s::uuid AND state='in_progress'
                           AND current_step=%s
                           AND completed_steps=%s::jsonb
                           AND (resume_contract IS NULL OR resume_contract='resume-v2')
                           AND (resume_plan_sha256 IS NULL OR resume_plan_sha256=%s)""",
                    (
                        STEP_FINAL_VERIFY, STEP_FINAL_VERIFY, resume_plan_sha256,
                        promotion_id, STEP_FINAL_VERIFY,
                        canonical_json(expected_before), resume_plan_sha256,
                    ),
                )
                if cur.rowcount != 1:
                    raise PromotionError(
                        "RESUME_JOURNAL_STATE_DRIFT",
                        "atomic resume-v2 finalization did not affect exactly the approved row",
                    )
    except PromotionError:
        conn.rollback()
        raise
    except Exception as exc:
        conn.rollback()
        raise PromotionError("JOURNAL_COMPLETION_FAILED", "atomic resume-v2 finalization did not commit durably") from exc


def journal_fail(conn, promotion_id: str, *, current_step: str | None, error: str) -> None:
    require_idle_connection(conn, code="JOURNAL_TRANSACTION_NOT_IDLE", purpose="promotion journal failure transition")
    safe_error = error[:2000]
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    f"""UPDATE {JOURNAL_TABLE} SET state='failed', failed_at=now(),
                           current_step=%s, error=%s, updated_at=now()
                         WHERE promotion_id=%s::uuid AND state IN ('planned','in_progress','failed')""",
                    (current_step, safe_error, promotion_id),
                )
                if cur.rowcount != 1:
                    raise PromotionError("JOURNAL_FAILURE_COUNT", "journal failure transition did not affect one row")
    except Exception:
        conn.rollback()
        raise


def journal_mark_rolled_back(conn, promotion_id: str) -> None:
    require_idle_connection(conn, code="JOURNAL_TRANSACTION_NOT_IDLE", purpose="rollback journal finalization")
    body_completed = False
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    f"""UPDATE {JOURNAL_TABLE} SET state='rolled_back', current_step=NULL,
                           error=NULL, updated_at=now()
                         WHERE promotion_id=%s::uuid AND state='failed'""",
                    (promotion_id,),
                )
                if cur.rowcount != 1:
                    raise PromotionError("JOURNAL_FINALIZATION_COUNT", "rollback journal finalization did not affect one failed row")
                body_completed = True
    except PromotionError:
        conn.rollback()
        raise
    except Exception as exc:
        conn.rollback()
        code = "JOURNAL_FINALIZATION_COMMIT_FAILED" if body_completed else "JOURNAL_FINALIZATION_FAILED"
        raise PromotionError(code, "rollback journal finalization did not commit durably") from exc


def journal_record_rollback_evidence(conn, promotion_id: str, *, evidence_path: str, evidence_sha256: str) -> None:
    require_idle_connection(conn, code="JOURNAL_TRANSACTION_NOT_IDLE", purpose="rollback evidence journal update")
    reference = f"ROLLBACK_EVIDENCE path={evidence_path} sha256={evidence_sha256}"
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {JOURNAL_TABLE} SET error=%s, updated_at=now() WHERE promotion_id=%s::uuid AND state='rolled_back'",
                    (reference, promotion_id),
                )
                if cur.rowcount != 1:
                    raise PromotionError("JOURNAL_EVIDENCE_COUNT", "rollback evidence reference did not affect one rolled-back row")
    except Exception:
        conn.rollback()
        raise


def journal_schema_capability(conn) -> dict[str, object]:
    """Report which optional resume-audit columns the journal table currently has.

    The platform runs on migration 053 until migration 054 is applied through its
    own gate, so the executable paths must be able to tell the two schemas apart
    from the live catalog instead of assuming the newer one.  The probe is
    per-connection and never cached: a different connection may see a different
    schema after the migration gate runs.
    """
    with conn.cursor() as cur:
        cur.execute(
            """SELECT column_name FROM information_schema.columns
                WHERE table_schema=%s AND table_name=%s AND column_name = ANY(%s)""",
            (JOURNAL_SCHEMA, JOURNAL_TABLE_NAME, list(RESUME_AUDIT_COLUMNS)),
        )
        present = {str(dict(row)["column_name"]) for row in cur.fetchall()}
    missing = [column for column in RESUME_AUDIT_COLUMNS if column not in present]
    return {
        "resume_contract_present": "resume_contract" in present,
        "resume_plan_sha256_present": "resume_plan_sha256" in present,
        "resume_audit_schema_ready": not missing,
        "resume_audit_schema_consistent": len(missing) in (0, len(RESUME_AUDIT_COLUMNS)),
        "present_columns": [column for column in RESUME_AUDIT_COLUMNS if column in present],
        "missing_columns": missing,
        "expected_migration": RESUME_AUDIT_MIGRATION,
    }


def require_consistent_journal_schema(conn) -> dict[str, object]:
    """Refuse a half-applied migration 054 before any promotion write."""
    capability = journal_schema_capability(conn)
    if not capability["resume_audit_schema_consistent"]:
        raise PromotionError(
            "ENVIRONMENT_IDENTITY_RESUME_AUDIT_SCHEMA_INCOMPLETE",
            "the promotion journal has only part of the resume audit schema",
            details={
                "writes_performed": False,
                "required_action": RESUME_AUDIT_REQUIRED_ACTION,
                **capability,
            },
        )
    return capability


def require_resume_audit_schema(conn) -> dict[str, object]:
    """Require the complete migration-054 audit schema for resume-v2 only."""
    capability = require_consistent_journal_schema(conn)
    if not capability["resume_audit_schema_ready"]:
        raise PromotionError(
            "RESUME_V2_AUDIT_SCHEMA_REQUIRED",
            "resume-v2 requires the resume audit columns from migration 054",
            details={
                "writes_performed": False,
                "required_action": RESUME_AUDIT_REQUIRED_ACTION,
                **capability,
            },
        )
    return capability


def inspect_journals(conn, *, state: str | None = None, promotion_id: str | None = None) -> list[dict[str, object]]:
    """Read journal rows on either the migration-053 or migration-054 schema.

    ``to_jsonb(journal) ->> '<column>'`` yields SQL NULL when the row type has no
    such field, so the optional migration-054 audit values are reported as their
    stored text on 054 and as ``None`` on 053 without dynamic SQL and without
    swallowing genuine ``UndefinedColumn`` errors on the required columns.
    """
    clauses: list[str] = []
    params: list[object] = []
    if state:
        clauses.append("state=%s")
        params.append(state)
    if promotion_id:
        clauses.append("promotion_id=%s::uuid")
        params.append(promotion_id)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT promotion_id::text AS promotion_id, source_environment, target_environment,
                       platform_identity_id::text AS platform_uuid, selected_clients,
                       immutable_plan_json, plan_sha256, state, started_at, completed_at,
                       failed_at, current_step, completed_steps, error, backup_reference,
                       runtime_file_backup_path, runtime_file_before_sha256,
                       runtime_file_after_sha256,
                       to_jsonb(journal) ->> 'resume_contract' AS resume_contract,
                       to_jsonb(journal) ->> 'resume_plan_sha256' AS resume_plan_sha256,
                       created_at, updated_at
                  FROM {JOURNAL_TABLE} AS journal{where} ORDER BY created_at DESC""",
            params,
        )
        return [dict(row) for row in cur.fetchall()]


def _systemd_enabled(unit: str) -> bool | None:
    try:
        result = subprocess.run(
            ["systemctl", "is-enabled", unit],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    status = result.stdout.strip()
    if status in {"enabled", "enabled-runtime", "static", "indirect", "generated", "alias"}:
        return True
    if status in {"disabled", "masked", "not-found"} or result.returncode != 0:
        return False
    return None


def readiness_report(platform_conn, selected_codes: Iterable[str], runtime_values: Mapping[str, str]) -> dict[str, object]:
    codes = sorted(set(selected_codes))
    with platform_conn.cursor() as cur:
        cur.execute(
            """SELECT client_code, dataset_name FROM workflow_a_control.client_dataset_schedule
                WHERE enabled IS TRUE AND client_code = ANY(%s) ORDER BY client_code,dataset_name""",
            (codes,),
        )
        enabled_schedules = [dict(row) for row in cur.fetchall()]
        cur.execute(
            """SELECT client_code, table_name FROM workflow_a_control.client_table_retention
                WHERE enabled IS TRUE AND client_code = ANY(%s) ORDER BY client_code,table_name""",
            (codes,),
        )
        enabled_retention = [dict(row) for row in cur.fetchall()]

    units = {
        "dispatcher": _systemd_enabled("log-job@dispatcher.timer"),
        "workflow_b": _systemd_enabled("log-workflow-b.timer"),
        "backup": _systemd_enabled("log-backup.timer"),
        "retention": _systemd_enabled("log-job@retention-purge.timer"),
        "prune": _systemd_enabled("log-platform-prune.timer"),
        "suspected_bug_worker": _systemd_enabled("suspected-bug-email-worker.timer"),
        "api_runtimes": _systemd_enabled("log-platform-api.service"),
    }
    components = [
        {"component": "dispatcher", "enabled": units["dispatcher"] or bool(enabled_schedules), "guarded": False, "compatible": True, "environment_source": "none", "uuid_source": "none", "attestation_requirements": "none", "current_invocation_compatibility": "compatible but unguarded", "required_configuration_change": "none"},
        {"component": "workflow_a", "enabled": bool(enabled_schedules), "guarded": False, "compatible": True, "environment_source": "none for current sync/aggregation jobs", "uuid_source": "none", "attestation_requirements": "none", "current_invocation_compatibility": "enabled schedules remain label-compatible but unguarded", "required_configuration_change": "recommended later hardening: adopt the shared guard"},
        {"component": "workflow_b", "enabled": units["workflow_b"], "guarded": True, "compatible": True, "environment_source": TARGET_ENVIRONMENT_KEY, "uuid_source": "runtime + control plane + DB markers", "attestation_requirements": "operation-specific for guarded production writes", "current_invocation_compatibility": "compatible after all promoted surfaces and runtime reload agree", "required_configuration_change": "use each operation's exact production confirmation where required"},
        {"component": "backup", "enabled": units["backup"], "guarded": True, "compatible": True, "environment_source": "platform marker", "uuid_source": "platform marker and manifest", "attestation_requirements": "verified manifest", "current_invocation_compatibility": "compatible; post-promotion manifests record production", "required_configuration_change": "none"},
        {"component": "retention", "enabled": units["retention"] or bool(enabled_retention), "guarded": False, "compatible": True, "environment_source": "none", "uuid_source": "none", "attestation_requirements": "none", "current_invocation_compatibility": "label-compatible but unguarded", "required_configuration_change": "recommended later hardening: attest selected client identity"},
        {"component": "prune", "enabled": units["prune"], "guarded": True, "compatible": True, "environment_source": TARGET_ENVIRONMENT_KEY, "uuid_source": "runtime + platform marker", "attestation_requirements": "explicit --execute", "current_invocation_compatibility": "blocked until the long-running/runtime environment is reloaded", "required_configuration_change": "separately approved restart/recreate after promotion"},
        {"component": "alpha_source_refresh", "enabled": False, "guarded": True, "compatible": False, "environment_source": TARGET_ENVIRONMENT_KEY, "uuid_source": "runtime + platform/client markers", "attestation_requirements": "local_dev-only execute attestation", "current_invocation_compatibility": "production execute is incompatible by design", "required_configuration_change": "keep manual tool disabled; a new reviewed production contract would be required"},
        {"component": "alpha_dysponent_enrichment", "enabled": bool(units["workflow_b"] and "ALPHA00001" in codes), "guarded": True, "compatible": True, "environment_source": TARGET_ENVIRONMENT_KEY, "uuid_source": "runtime + platform/client markers", "attestation_requirements": "identity equality; operation-specific confirmation where exposed", "current_invocation_compatibility": "compatible after selected surfaces agree", "required_configuration_change": "none for dry-run; preserve future production confirmation gates"},
        {"component": "suspected_bug_worker", "enabled": units["suspected_bug_worker"], "guarded": False, "compatible": True, "environment_source": "startup environment for message label", "uuid_source": "none", "attestation_requirements": "none", "current_invocation_compatibility": "email delivery remains outside promotion scope", "required_configuration_change": "remain disabled until separate approval"},
        {"component": "api_runtimes", "enabled": units["api_runtimes"], "guarded": "prune path only", "compatible": True, "environment_source": "process startup environment", "uuid_source": "runtime + marker for prune", "attestation_requirements": "none", "current_invocation_compatibility": "core API remains label-agnostic; guarded prune needs reload", "required_configuration_change": "separately approved recreate/restart after promotion"},
    ]
    essential_incompatible = [
        str(row["component"])
        for row in components
        if row["enabled"] is True and row["compatible"] is False
    ]
    return {
        "selected_clients": codes,
        "enabled_workflow_a_schedules": enabled_schedules,
        "enabled_retention_policies": enabled_retention,
        "systemd_enablement": units,
        "components": components,
        "essential_incompatible": essential_incompatible,
        "execute_allowed": not essential_incompatible,
        "runtime_environment": runtime_values.get(TARGET_ENVIRONMENT_KEY),
    }

def mixed_state_report(*, plan: Mapping[str, object], runtime_environment: str, platform_marker: Mapping[str, object], client_markers: Mapping[str, Mapping[str, object]], control_plane_environments: Mapping[str, str]) -> dict[str, object]:
    target = str(plan["target_environment"])
    surfaces: dict[str, str] = {
        "runtime_file": runtime_environment,
        "platform_marker": str(platform_marker.get("environment")),
    }
    for row in plan["clients"]:  # type: ignore[assignment]
        code = row["client_code"]
        surfaces[f"client_marker:{code}"] = str(client_markers[code].get("environment"))
        surfaces[f"control_plane:{code}"] = str(control_plane_environments[code])
    values = set(surfaces.values())
    return {"surfaces": surfaces, "mixed": len(values) > 1, "all_at_target": values == {target}}
