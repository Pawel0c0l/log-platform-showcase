"""Deterministic, runtime-bound approval contract for v5 promotion finalization.

Resume v2 is deliberately journal-only.  It can observe persistent identity
surfaces and running processes, but it has no dependency on any environment
mutation primitive, privileged helper invocation, service restart, Docker
recreation, job, provider, mail, backup, snapshot, prune, or business-data
module.
"""
from __future__ import annotations

import grp
import hashlib
import json
import os
import pwd
import re
import stat
import subprocess
from pathlib import Path
from typing import Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit
from urllib.request import urlopen

from ops import environment_identity_promotion as promotion
from ops.systemd_environment_files import canonical_source_effective


CONTRACT = "environment_identity_resume_plan_v2"
CONTRACT_VERSION = 3
ATTESTATION_CONTRACT = "resume-v2"
RETIRED_RESUME_V1_HASH = "2b4fafc8d4b43446fe2d21a7da5d1ed57ff13f476fd70e8c9a8dbfc3174e67f5"
RETIRED_RECOVERY_V1_HASH = "9d417d3bb88451d1edb1bba4c67564ae0b3947a59cec93060bd25f7ad6278cf5"
REJECTED_INCOMPLETE_RESUME_V2_HASH = "4a409a35ae96cb64d4c6027af0c04db5f2dfe58a3e9dcb6436f58f073dfbb5f5"
SUDO_COLLECTION_METHOD = "sudo_n_l_normalized_v2"
COLLECTION_LOCALE = "C"
HELPER_PATH = "/usr/local/sbin/log-platform-environment-identity-helper"
HELPER_RULE = f"(root) NOPASSWD: {HELPER_PATH} *"
REMOTE_NAME = "origin"
REMOTE_REF = "refs/heads/main"
REMOTE_BRANCH = "main"
RESUME_SCHEMA_MIGRATION = "054_environment_identity_resume_contract.sql"
RESUME_TRIGGER_FUNCTION_PROSRC_SHA256 = "8fb25571bc049a60ba628e2c2bc3a2582d903a51c65734d1302946eef524cd8c"

# PostgreSQL represents these legitimate migration-054 properties with either
# an empty catalog string or SQL NULL.  A resume plan cannot use either form:
# both are reserved for missing security evidence.  These canonical sentinels
# are serialized and hashed only after the raw values match the exact expected
# catalog semantics below.
CANONICAL_SCHEMA_ABSENCE = {
    "identity_kind": "not_identity",
    "generated_kind": "not_generated",
    "default_expression": "no_default",
    "identity_arguments": "no_arguments",
    "proconfig": "not_configured",
}
_EXPECTED_RAW_SCHEMA_ABSENCE = {
    "identity_kind": "",
    "generated_kind": "",
    "default_expression": None,
    "identity_arguments": "",
    "proconfig": None,
}

TOP_LEVEL_SECTIONS = (
    "contract", "contract_version", "attestation_contract", "approval_identity",
    "remote_repository_binding", "migration_054_schema_contract",
    "journal_state", "persistent_state", "runtime_convergence", "systemd_runtime",
    "docker_runtime", "security_and_recovery", "remaining_execution_contract", "supported",
)

RESUME_IMPLEMENTATION_ASSET_PATHS = tuple(sorted((
    "jobs/common/environment_identity.py",
    "ops/environment_identity_file.py",
    "ops/environment_identity_promotion.py",
    "ops/failed_promotion_recovery.py",
    "ops/git_repository_state.py",
    "ops/promote_environment_identity.py",
    "ops/promotion_plan_v4.py",
    "ops/promotion_plan_v5.py",
    "ops/resume_plan_v2.py",
    "ops/runtime_identity_inspection.py",
    "ops/runtime_identity_readiness.py",
    "ops/runtime_identity_recovery.py",
    "ops/systemd_environment_files.py",
)))

ALLOWED_ACTIONS = (
    "reverify_completed_steps_read_only",
    "verify_runtime_processes_against_bound_identity",
    "reconcile_journal_against_persistent_and_runtime_reality",
    "record_completed_step:runtime_reload_required",
    "reverify_runtime_processes_against_bound_identity",
    "record_completed_step:runtime_processes_verified",
    "final_read_only_cross_surface_verification",
    "atomic_record_final_verification_and_transition_completed",
    "fresh_connection_verify_journal_completed",
)

PROHIBITED_ACTIONS = tuple(sorted(set((
    "automatic_retry", "automatic_rollback", "backup_invocation", "backfill_invocation",
    "business_data_mutation", "canonical_helper_invocation", "client_marker_promotion_primitive",
    "direct_client_marker_update", "docker_api_recreation", "docker_api_restart",
    "docker_daemon_restart", "email_send", "imap_access", "job_dispatch", "provider_access",
    "prune_invocation", "runtime_identity_file_restore", "runtime_identity_file_update",
    "schedule_change", "selenium_access", "smtp_access", "snapshot_recalculation",
    "systemctl_daemon_reload", "systemd_api_restart", "timer_restart", "update_control_plane",
    "update_platform_marker", "uuid_change", "uuid_generation", "worker_activation",
))))

_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HEX32 = re.compile(r"^[0-9a-f]{32}$")
PROC_ROOT = Path("/proc")
CGROUP_ROOT = Path("/sys/fs/cgroup")


def _fail(code: str, message: str, *, invalid: bool = False) -> None:
    raise promotion.PromotionError(
        code, message,
        exit_code=promotion.EXIT_INVALID if invalid else promotion.EXIT_PRECONDITION,
        details={"writes_performed": False},
    )


def locale_stable_environment(base: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    env.update({"LC_ALL": "C", "LANG": "C", "LC_MESSAGES": "C"})
    env.pop("COLUMNS", None)
    return env


def _run(command: Sequence[str], *, cwd: Path | None = None, code: str) -> str:
    try:
        result = subprocess.run(
            list(command), cwd=cwd, check=False, capture_output=True, text=True,
            timeout=15, env=locale_stable_environment(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise promotion.PromotionError(code, "resume-v2 collector failed") from exc
    if result.returncode != 0:
        raise promotion.PromotionError(
            code, "resume-v2 collector was rejected",
            details={"writes_performed": False, "command": list(command[:3]), "exit_code": result.returncode},
        )
    return result.stdout


def _required_text(value: object, label: str) -> str:
    text = str(value or "")
    if not text:
        _fail("RESUME_IMMUTABLE_BINDING_DRIFT", f"missing security-critical value: {label}")
    return text

def normalize_absence(value: object) -> object:
    """Represent legitimate absence explicitly; canonical plans never use JSON null."""
    if value is None:
        return "not_applicable"
    if isinstance(value, dict):
        return {str(key): normalize_absence(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize_absence(item) for item in value]
    return value


def _require_no_missing(value: object, path: str = "plan") -> None:
    if value is None or value == "":
        _fail("RESUME_IMMUTABLE_BINDING_DRIFT", f"missing security-critical value: {path}")
    if isinstance(value, Mapping):
        for key, item in value.items():
            _require_no_missing(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _require_no_missing(item, f"{path}[{index}]")


def _require_keys(value: object, path: str, keys: Sequence[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        _fail("RESUME_IMMUTABLE_BINDING_DRIFT", f"{path} must be an object")
    missing = [key for key in keys if key not in value]
    if missing:
        _fail(
            "RESUME_IMMUTABLE_BINDING_DRIFT",
            f"missing security-critical keys in {path}: {','.join(missing)}",
        )
    return value


def _hex(value: object, pattern: re.Pattern[str], label: str) -> str:
    text = _required_text(value, label)
    if not pattern.fullmatch(text):
        _fail("RESUME_IMMUTABLE_BINDING_DRIFT", f"invalid normalized value: {label}")
    return text


def canonical_uuid(value: object, label: str) -> str:
    return promotion.canonical_uuid(value, label)


def normalized_mode(value: int) -> str:
    return f"{value & 0o7777:04o}"


def resolved_path(value: object, label: str) -> str:
    text = _required_text(value, label)
    path = Path(text)
    if not path.is_absolute():
        _fail("RESUME_IMMUTABLE_BINDING_DRIFT", f"{label} must be absolute")
    return str(path.resolve(strict=False))


def head_relationship(repository_root: Path, original_head: str, resume_head: str) -> str:
    original = _hex(original_head, _HEX40, "original execution HEAD")
    resume = _hex(resume_head, _HEX40, "resume implementation HEAD")
    if original == resume:
        return "identical"
    result = subprocess.run(
        ["git", "-C", str(repository_root), "merge-base", "--is-ancestor", original, resume],
        check=False, capture_output=True, text=True, timeout=10,
        env=locale_stable_environment(),
    )
    if result.returncode == 0:
        return "descendant"
    _fail("RESUME_IMPLEMENTATION_HEAD_UNRELATED", "resume implementation HEAD is not the original HEAD or its descendant")
    raise AssertionError("unreachable")


def _remote_fail(message: str, **details: object) -> None:
    raise promotion.PromotionError(
        "RESUME_REMOTE_HEAD_DRIFT", message,
        details={"writes_performed": False, **details},
    )


def normalize_remote_url(value: str, *, repository_root: Path) -> str:
    """Return one credential-free canonical remote URL or fail closed."""
    raw = value.strip()
    if not raw or raw != value or any(character.isspace() or ord(character) < 32 for character in raw):
        _remote_fail("origin URL is absent or malformed")
    scp = re.fullmatch(r"(?:(?P<user>[A-Za-z0-9._-]+)@)?(?P<host>[^/:]+):(?P<path>.+)", raw)
    if scp and "://" not in raw:
        user = f"{scp.group('user')}@" if scp.group("user") else ""
        raw = f"ssh://{user}{scp.group('host').lower()}/{scp.group('path').lstrip('/')}"
    parsed = urlsplit(raw)
    if not parsed.scheme:
        path = Path(raw)
        if not path.is_absolute():
            path = repository_root / path
        return path.resolve(strict=False).as_uri()
    if parsed.scheme not in {"ssh", "https", "git", "file"}:
        _remote_fail("origin URL uses an unsupported transport")
    if parsed.query or parsed.fragment or parsed.password:
        _remote_fail("origin URL contains ambiguous or credential-bearing components")
    if parsed.scheme == "file":
        if parsed.netloc not in {"", "localhost"} or not parsed.path.startswith("/"):
            _remote_fail("file origin URL is not an unambiguous local absolute path")
        return Path(parsed.path).resolve(strict=False).as_uri()
    if not parsed.hostname or not parsed.path or parsed.path == "/":
        _remote_fail("origin URL has no canonical host/repository path")
    host = parsed.hostname.lower()
    try:
        port = parsed.port
    except ValueError:
        _remote_fail("origin URL port is malformed")
        raise AssertionError("unreachable")
    default_port = 22 if parsed.scheme == "ssh" else 443 if parsed.scheme == "https" else None
    port_part = f":{port}" if port is not None and port != default_port else ""
    user_part = f"{parsed.username}@" if parsed.username else ""
    path = "/" + parsed.path.strip("/")
    return urlunsplit((parsed.scheme.lower(), f"{user_part}{host}{port_part}", path, "", ""))


def remote_repository_binding(repository_root: Path, *, local_head: str,
                              local_branch: str) -> dict[str, object]:
    """Bind HEAD to the actual origin refs/heads/main without fetching."""
    root = repository_root.resolve()
    local = _hex(local_head, _HEX40, "local repository HEAD")

    def git(*arguments: str) -> str:
        try:
            result = subprocess.run(
                ["git", "-C", str(root), *arguments],
                check=False, capture_output=True, text=True, timeout=15,
                env=locale_stable_environment(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise promotion.PromotionError(
                "RESUME_REMOTE_HEAD_DRIFT", "origin query failed",
                details={"writes_performed": False},
            ) from exc
        if result.returncode != 0:
            _remote_fail("origin query failed", git_exit_code=result.returncode)
        return result.stdout

    actual_local = git("rev-parse", "HEAD").strip()
    actual_branch = git("branch", "--show-current").strip()
    if (
        not _HEX40.fullmatch(actual_local) or actual_local != local
        or actual_branch != local_branch or actual_branch != REMOTE_BRANCH
    ):
        _remote_fail("local HEAD/branch changed or is not the canonical main checkout")

    urls = [line for line in git("remote", "get-url", "--all", REMOTE_NAME).splitlines() if line]
    if len(urls) != 1:
        _remote_fail("origin must resolve to exactly one fetch URL")
    normalized_url = normalize_remote_url(urls[0], repository_root=root)
    fetch_specs = [line for line in git("config", "--get-all", "remote.origin.fetch").splitlines() if line]
    main_remote = git("config", "--get", "branch.main.remote").strip()
    main_merge = git("config", "--get", "branch.main.merge").strip()
    expected_fetch = "+refs/heads/*:refs/remotes/origin/*"
    if fetch_specs != [expected_fetch] or main_remote != REMOTE_NAME or main_merge != REMOTE_REF:
        _remote_fail("origin/main tracking relationship is unexpected")

    output = git("ls-remote", "--refs", REMOTE_NAME, REMOTE_REF)
    # Re-read the URL after the network query so in-process config drift cannot
    # make the observed ref belong to a different remote identity.
    urls_after = [line for line in git("remote", "get-url", "--all", REMOTE_NAME).splitlines() if line]
    if urls_after != urls:
        _remote_fail("origin identity changed during remote query")
    records = [line for line in output.splitlines() if line]
    if len(records) != 1:
        _remote_fail("origin main query returned a missing or ambiguous record")
    fields = records[0].split("\t")
    if len(fields) != 2 or not _HEX40.fullmatch(fields[0]) or fields[1] != REMOTE_REF:
        _remote_fail("origin main query returned a malformed record")
    remote_head = fields[0]
    equal = local == remote_head
    if not equal:
        _remote_fail("local HEAD does not equal actual origin main HEAD",
                     local_head=local, remote_head=remote_head)
    return {
        "collection_method": "git_ls_remote_refs_v1",
        "remote_name": REMOTE_NAME,
        "remote_url": normalized_url,
        "remote_identity": normalized_url,
        "remote_ref": REMOTE_REF,
        "remote_branch": REMOTE_BRANCH,
        "expected_fetch_refspec": expected_fetch,
        "main_tracking_remote": main_remote,
        "main_tracking_merge_ref": main_merge,
        "local_branch": local_branch,
        "local_head": local,
        "remote_head": remote_head,
        "local_remote_head_equal": equal,
    }


def _schema_fail(message: str, **details: object) -> None:
    raise promotion.PromotionError(
        "RESUME_SCHEMA_CONTRACT_DRIFT", message,
        details={"writes_performed": False, **details},
    )


def _normalized_pg_definition(value: object) -> str:
    return " ".join(str(value or "").split())


def _canonical_validated_schema_absence(field: str, value: object) -> str:
    """Canonicalize one exact raw absence value; never coerce unknown input."""
    if (
        field not in CANONICAL_SCHEMA_ABSENCE
        or value != _EXPECTED_RAW_SCHEMA_ABSENCE[field]
    ):
        _schema_fail(f"migration-054 unexpected catalog value for {field}")
    return CANONICAL_SCHEMA_ABSENCE[field]


def _validate_and_normalize_schema_columns(
    columns: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    expected_columns = [
        {"schema": "ops_control", "table_name": "environment_identity_promotion",
         "name": "resume_contract", "attnum": 22, "data_type": "text", "not_null": False,
         "identity_kind": "", "generated_kind": "", "default_expression": None,
         "collation": "pg_catalog.default"},
        {"schema": "ops_control", "table_name": "environment_identity_promotion",
         "name": "resume_plan_sha256", "attnum": 23, "data_type": "text", "not_null": False,
         "identity_kind": "", "generated_kind": "", "default_expression": None,
         "collation": "pg_catalog.default"},
    ]
    raw = [dict(row) for row in columns]
    if raw != expected_columns:
        _schema_fail("migration-054 column metadata differs")
    return [{
        **row,
        "identity_kind": _canonical_validated_schema_absence(
            "identity_kind", row["identity_kind"],
        ),
        "generated_kind": _canonical_validated_schema_absence(
            "generated_kind", row["generated_kind"],
        ),
        "default_expression": _canonical_validated_schema_absence(
            "default_expression", row["default_expression"],
        ),
    } for row in raw]


def _expected_canonical_schema_columns() -> list[dict[str, object]]:
    return [
        {"schema": "ops_control", "table_name": "environment_identity_promotion",
         "name": "resume_contract", "attnum": 22, "data_type": "text", "not_null": False,
         "identity_kind": CANONICAL_SCHEMA_ABSENCE["identity_kind"],
         "generated_kind": CANONICAL_SCHEMA_ABSENCE["generated_kind"],
         "default_expression": CANONICAL_SCHEMA_ABSENCE["default_expression"],
         "collation": "pg_catalog.default"},
        {"schema": "ops_control", "table_name": "environment_identity_promotion",
         "name": "resume_plan_sha256", "attnum": 23, "data_type": "text", "not_null": False,
         "identity_kind": CANONICAL_SCHEMA_ABSENCE["identity_kind"],
         "generated_kind": CANONICAL_SCHEMA_ABSENCE["generated_kind"],
         "default_expression": CANONICAL_SCHEMA_ABSENCE["default_expression"],
         "collation": "pg_catalog.default"},
    ]


def _validate_and_normalize_trigger_function(
    functions: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    if len(functions) != 1:
        _schema_fail("migration-054 trigger function is missing or overloaded")
    raw = dict(functions[0])
    prosrc_sha256 = hashlib.sha256(str(raw.pop("prosrc")).encode("utf-8")).hexdigest()
    if (
        raw.get("schema") != "ops_control"
        or raw.get("name") != "reject_environment_identity_promotion_plan_update"
        or raw.get("identity_arguments") != _EXPECTED_RAW_SCHEMA_ABSENCE["identity_arguments"]
        or raw.get("return_type") != "trigger"
        or raw.get("kind") != "f"
        or raw.get("language") != "plpgsql"
        or raw.get("security_definer") is not False
        or raw.get("volatility") != "v"
        or raw.get("proconfig") is not _EXPECTED_RAW_SCHEMA_ABSENCE["proconfig"]
        or prosrc_sha256 != RESUME_TRIGGER_FUNCTION_PROSRC_SHA256
    ):
        _schema_fail("migration-054 trigger function contract differs")
    raw["identity_arguments"] = _canonical_validated_schema_absence(
        "identity_arguments", raw["identity_arguments"],
    )
    raw["proconfig"] = _canonical_validated_schema_absence(
        "proconfig", raw["proconfig"],
    )
    raw["prosrc_sha256"] = prosrc_sha256
    return raw


def _migration_054_schema_contract(
    conn, *, expected_database_name: str, expected_database_uuid: str,
    target_promotion_id: str,
) -> dict[str, object]:
    """Collect and validate the complete stable migration-054 catalog contract.

    Attribute numbers 22/23 are deliberately bound: migration 054 is additive
    to the exact migration-053 journal layout, so their positions prove that no
    shadow/reordered partial application is being approved. Catalog OIDs are
    deliberately excluded because they are volatile across restores.

    Both column comments are blocking policy: absence or any text change is
    contract drift, not advisory documentation drift.
    """
    expected_uuid = canonical_uuid(expected_database_uuid, "expected platform database UUID")
    target_id = canonical_uuid(target_promotion_id, "target promotion ID")
    with conn.cursor() as cur:
        cur.execute(
            """SELECT current_database() AS current_database_name,
                      d.datname AS catalog_database_name,
                      pg_get_userbyid(d.datdba) AS database_owner,
                      i.database_identity_id::text AS current_database_uuid,
                      i.database_name AS marker_database_name,
                      i.database_role AS marker_database_role,
                      i.identity_key AS marker_identity_key
                 FROM pg_database d
                 LEFT JOIN ops_control.environment_identity i
                   ON i.identity_key='primary'
                WHERE d.datname=current_database()"""
        )
        identity_rows = [dict(row) for row in cur.fetchall()]
        cur.execute(
            """SELECT count(*) FILTER (WHERE filename=%s) AS exact_count,
                      count(*) AS migration_count,
                      max(filename) AS migration_ceiling
                 FROM public.schema_migrations""",
            (RESUME_SCHEMA_MIGRATION,),
        )
        migration = dict(cur.fetchone() or {})
        cur.execute(
            """SELECT n.nspname AS schema, c.relname AS table_name,
                      a.attname AS name, a.attnum, format_type(a.atttypid,a.atttypmod) AS data_type,
                      a.attnotnull AS not_null, a.attidentity AS identity_kind,
                      a.attgenerated AS generated_kind,
                      pg_get_expr(ad.adbin,ad.adrelid) AS default_expression,
                      CASE WHEN a.attcollation=0 THEN 'not_collatable'
                           ELSE cn.nspname || '.' || coll.collname END AS collation
                 FROM pg_attribute a
                 JOIN pg_class c ON c.oid=a.attrelid
                 JOIN pg_namespace n ON n.oid=c.relnamespace
                 LEFT JOIN pg_attrdef ad ON ad.adrelid=a.attrelid AND ad.adnum=a.attnum
                 LEFT JOIN pg_collation coll ON coll.oid=a.attcollation
                 LEFT JOIN pg_namespace cn ON cn.oid=coll.collnamespace
                WHERE n.nspname='ops_control'
                  AND c.relname='environment_identity_promotion'
                  AND a.attname=ANY(%s) AND NOT a.attisdropped
                ORDER BY a.attnum""",
            (["resume_contract", "resume_plan_sha256"],),
        )
        columns = [dict(row) for row in cur.fetchall()]
        cur.execute(
            """SELECT n.nspname AS table_schema, c.relname AS table_name,
                      con.conname AS name, con.contype AS type, con.convalidated AS validated,
                      pg_get_constraintdef(con.oid,false) AS definition
                 FROM pg_constraint con
                 JOIN pg_class c ON c.oid=con.conrelid
                 JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname='ops_control'
                  AND c.relname='environment_identity_promotion'
                  AND con.conname=ANY(%s)
                ORDER BY con.conname""",
            ([
                "ck_environment_identity_promotion_resume_contract",
                "ck_environment_identity_promotion_resume_plan_hash",
            ],),
        )
        constraints = [dict(row) for row in cur.fetchall()]
        cur.execute(
            """SELECT n.nspname AS schema, p.proname AS name,
                      pg_get_function_identity_arguments(p.oid) AS identity_arguments,
                      pg_get_function_result(p.oid) AS return_type, p.prokind AS kind,
                      pg_get_userbyid(p.proowner) AS owner, l.lanname AS language,
                      p.prosecdef AS security_definer, p.provolatile AS volatility,
                      p.proconfig, p.prosrc
                 FROM pg_proc p
                 JOIN pg_namespace n ON n.oid=p.pronamespace
                 JOIN pg_language l ON l.oid=p.prolang
                WHERE n.nspname='ops_control'
                  AND p.proname='reject_environment_identity_promotion_plan_update'
                ORDER BY pg_get_function_identity_arguments(p.oid)"""
        )
        functions = [dict(row) for row in cur.fetchall()]
        cur.execute(
            """SELECT t.tgname AS name, n.nspname AS table_schema, c.relname AS table_name,
                      t.tgenabled AS enabled, t.tgtype,
                      pn.nspname AS function_schema, p.proname AS function_name,
                      pg_get_triggerdef(t.oid,false) AS definition,
                      pg_get_userbyid(c.relowner) AS table_owner
                 FROM pg_trigger t
                 JOIN pg_class c ON c.oid=t.tgrelid
                 JOIN pg_namespace n ON n.oid=c.relnamespace
                 JOIN pg_proc p ON p.oid=t.tgfoid
                 JOIN pg_namespace pn ON pn.oid=p.pronamespace
                WHERE NOT t.tgisinternal
                  AND n.nspname='ops_control'
                  AND c.relname='environment_identity_promotion'
                  AND t.tgname='trg_environment_identity_promotion_plan_immutable'"""
        )
        triggers = [dict(row) for row in cur.fetchall()]
        cur.execute(
            """SELECT a.attname AS name, col_description(c.oid,a.attnum) AS comment
                 FROM pg_class c
                 JOIN pg_namespace n ON n.oid=c.relnamespace
                 JOIN pg_attribute a ON a.attrelid=c.oid
                WHERE n.nspname='ops_control'
                  AND c.relname='environment_identity_promotion'
                  AND a.attname=ANY(%s)
                ORDER BY a.attnum""",
            (["resume_contract", "resume_plan_sha256"],),
        )
        comments = [dict(row) for row in cur.fetchall()]
        cur.execute(
            """SELECT count(*) AS target_non_null_resume_audit_rows
                 FROM ops_control.environment_identity_promotion
                WHERE promotion_id=%s::uuid
                  AND (resume_contract IS NOT NULL OR resume_plan_sha256 IS NOT NULL)""",
            (target_id,),
        )
        audit_rows = int(
            dict(cur.fetchone() or {}).get("target_non_null_resume_audit_rows", -1)
        )

    if len(identity_rows) != 1:
        _schema_fail("database identity query was missing or ambiguous")
    identity = identity_rows[0]
    expected_name = str(expected_database_name)
    if (
        identity.get("current_database_name") != expected_name
        or identity.get("catalog_database_name") != expected_name
        or identity.get("marker_database_name") != expected_name
        or identity.get("current_database_uuid") != expected_uuid
        or identity.get("marker_database_role") != "platform"
        or identity.get("marker_identity_key") != "primary"
    ):
        _schema_fail("connected database identity/name differs from the approved platform database")
    if (
        int(migration.get("exact_count") or 0) != 1
        or migration.get("migration_ceiling") != RESUME_SCHEMA_MIGRATION
    ):
        _schema_fail("migration-054 filename cardinality or migration ceiling differs")

    canonical_columns = _validate_and_normalize_schema_columns(columns)

    normalized_constraints = [{
        **{key: row[key] for key in (
            "table_schema", "table_name", "name", "type", "validated",
        )},
        "definition": _normalized_pg_definition(row["definition"]),
    } for row in constraints]
    expected_constraint_definitions = {
        "ck_environment_identity_promotion_resume_contract":
            "CHECK (((resume_contract IS NULL) OR (resume_contract = 'resume-v2'::text)))",
        "ck_environment_identity_promotion_resume_plan_hash":
            "CHECK (((resume_plan_sha256 IS NULL) OR (resume_plan_sha256 ~ '^[0-9a-f]{64}$'::text)))",
    }
    if len(normalized_constraints) != 2:
        _schema_fail("migration-054 check constraints are missing or ambiguous")
    for row in normalized_constraints:
        expected_definition = expected_constraint_definitions.get(str(row["name"]))
        if (
            row["table_schema"] != "ops_control"
            or row["table_name"] != "environment_identity_promotion"
            or row["type"] != "c" or row["validated"] is not True
            or row["definition"] != expected_definition
        ):
            _schema_fail("migration-054 check constraint semantics differ")

    function = _validate_and_normalize_trigger_function(functions)

    if len(triggers) != 1:
        _schema_fail("migration-054 journal trigger is missing or ambiguous")
    trigger = triggers[0]
    tgtype = int(trigger.pop("tgtype"))
    trigger["timing"] = "BEFORE" if tgtype & 2 else "AFTER"
    trigger["row_level"] = bool(tgtype & 1)
    trigger["events"] = [
        name for bit, name in ((4, "INSERT"), (8, "DELETE"), (16, "UPDATE"), (32, "TRUNCATE"))
        if tgtype & bit
    ]
    trigger["definition"] = _normalized_pg_definition(trigger["definition"])
    expected_trigger_definition = (
        "CREATE TRIGGER trg_environment_identity_promotion_plan_immutable BEFORE UPDATE "
        "ON ops_control.environment_identity_promotion FOR EACH ROW EXECUTE FUNCTION "
        "ops_control.reject_environment_identity_promotion_plan_update()"
    )
    if (
        trigger.get("enabled") != "O"
        or trigger.get("table_schema") != "ops_control"
        or trigger.get("table_name") != "environment_identity_promotion"
        or trigger.get("function_schema") != "ops_control"
        or trigger.get("function_name") != "reject_environment_identity_promotion_plan_update"
        or trigger.get("table_owner") != function.get("owner")
        or trigger["timing"] != "BEFORE"
        or trigger["row_level"] is not True
        or trigger["events"] != ["UPDATE"]
        or trigger["definition"] != expected_trigger_definition
    ):
        _schema_fail("migration-054 trigger contract differs")

    expected_comments = [
        {"name": "resume_contract", "comment": "Write-once executable resume approval contract; only resume-v2 is accepted."},
        {"name": "resume_plan_sha256", "comment": "Write-once SHA-256 of the exact canonical resume-v2 plan approved for this completion attempt."},
    ]
    if comments != expected_comments:
        _schema_fail("migration-054 blocking column comments differ")
    if audit_rows != 0:
        _schema_fail(
            "target promotion resume audit columns already contain values",
            promotion_id=target_id,
        )

    return {
        "contract": "migration_054_complete_catalog_v1",
        "expected_database": {"name": expected_name, "identity_uuid": expected_uuid},
        "current_database": {
            "name": identity["current_database_name"],
            "catalog_name": identity["catalog_database_name"],
            "identity_uuid": identity["current_database_uuid"],
            "marker_name": identity["marker_database_name"],
            "marker_role": identity["marker_database_role"],
            "marker_identity_key": identity["marker_identity_key"],
            "owner": identity["database_owner"],
        },
        "schema_migration": {
            "filename": RESUME_SCHEMA_MIGRATION,
            "filename_count": int(migration["exact_count"]),
            "migration_row_count": int(migration["migration_count"]),
            "ceiling": migration["migration_ceiling"],
        },
        "columns": canonical_columns,
        "constraints": normalized_constraints,
        "trigger_function": function,
        "trigger": trigger,
        "comments": comments,
        "comment_policy": "exact_text_required_block_on_absent_or_changed",
        "target_promotion_resume_audit_state": {
            "promotion_id": target_id,
            "non_null_resume_audit_row_count": audit_rows,
        },
        "volatile_catalog_oids_bound": False,
    }


def migration_054_schema_contract(
    conn, *, expected_database_name: str, expected_database_uuid: str,
    target_promotion_id: str,
) -> dict[str, object]:
    """Fail closed with one precise classification for all catalog failures."""
    try:
        # Keep schema 053 and the one-column transition distinguishable before
        # issuing queries that require the complete migration-054 row type.
        promotion.require_resume_audit_schema(conn)
        return _migration_054_schema_contract(
            conn,
            expected_database_name=expected_database_name,
            expected_database_uuid=expected_database_uuid,
            target_promotion_id=target_promotion_id,
        )
    except promotion.PromotionError:
        raise
    except Exception as exc:
        raise promotion.PromotionError(
            "RESUME_SCHEMA_CONTRACT_DRIFT",
            "migration-054 catalog query failed or returned malformed data",
            details={"writes_performed": False},
        ) from exc


def implementation_asset_bindings(repository_root: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for relative in RESUME_IMPLEMENTATION_ASSET_PATHS:
        path = repository_root / relative
        if path.is_symlink() or not path.is_file():
            _fail("RESUME_SECURITY_BINDING_DRIFT", f"resume implementation asset is missing or unsafe: {relative}")
        rows.append({
            "path": Path(relative).as_posix(), "regular_file": True, "symlink": False,
            "sha256": promotion.sha256_bytes(path.read_bytes()),
        })
    return rows


_SUDO_RULE = re.compile(
    r"^\s*\((?P<runas>[^)]+)\)\s+"
    r"(?P<tags>(?:(?:[A-Z_]+):\s*)*)(?P<commands>.+?)\s*$"
)

# Only these two tags carry authorization meaning for the helper.  Every other
# sudo tag changes execution semantics in ways this parser does not model.
SUPPORTED_SUDO_TAGS = ("NOPASSWD", "PASSWD")

# Uppercase identifiers in a command position are sudoers Cmnd_Aliases.  `sudo
# -n -l` does not expand them, so they can never be resolved deterministically
# here and are refused instead of being guessed to be non-matching.
_SUDO_ALIAS_TOKEN = re.compile(r"^[A-Z][A-Z0-9_]*$")

# The characters sudo may legitimately backslash-escape inside a command list.
_SUDO_ESCAPABLE = frozenset(",\\:= ")

_SUDO_WILDCARD_CHARACTERS = "*?[]"


def tokenize_sudo_command_list(value: str) -> list[str]:
    """Split one sudo command list into ordered specifications, fail-closed.

    Only an unescaped comma separates specifications; a dangling or unmodelled
    escape sequence is ambiguous and is refused rather than guessed.
    """
    tokens: list[str] = []
    current: list[str] = []
    index = 0
    length = len(value)
    while index < length:
        character = value[index]
        if character == "\\":
            if index + 1 >= length:
                _fail("RESUME_SECURITY_BINDING_DRIFT", "sudo command policy ends in a dangling escape")
            following = value[index + 1]
            if following not in _SUDO_ESCAPABLE:
                _fail("RESUME_SECURITY_BINDING_DRIFT", "sudo command policy contains an unsupported escape sequence")
            current.append(following)
            index += 2
            continue
        if character == ",":
            tokens.append("".join(current))
            current = []
            index += 1
            continue
        current.append(character)
        index += 1
    tokens.append("".join(current))
    specifications = [token.strip() for token in tokens]
    if not specifications or any(not token for token in specifications):
        _fail("RESUME_SECURITY_BINDING_DRIFT", "sudo command policy contains an empty command specification")
    return specifications


def classify_sudo_command(specification: str) -> dict[str, object]:
    """Classify one command specification inside the deliberately narrow grammar.

    Anything this parser cannot fully model is reported as unsupported and as
    potentially helper-matching, so an unknown token can never be optimistically
    treated as harmless.
    """
    text = specification.strip()
    negated = text.startswith("!")
    body = text[1:].strip() if negated else text
    row: dict[str, object] = {
        "specification": text,
        "negated": negated,
        "kind": "unsupported_command_syntax",
        "matches_helper": True,
        "supported": False,
    }
    if not body:
        return row
    if body == "ALL":
        row.update({"kind": "all", "matches_helper": True, "supported": True})
        return row
    if _SUDO_ALIAS_TOKEN.fullmatch(body):
        row["kind"] = "unresolved_command_alias"
        return row
    if not body.startswith("/"):
        return row
    command_path, separator, arguments = body.partition(" ")
    if any(character in command_path for character in _SUDO_WILDCARD_CHARACTERS):
        row["kind"] = "unsupported_command_path_pattern"
        return row
    arguments = arguments.strip()
    if separator and arguments != "*" and any(
        character in arguments for character in _SUDO_WILDCARD_CHARACTERS
    ):
        row["kind"] = "unsupported_argument_pattern"
        return row
    row.update({
        "kind": "absolute_command",
        "matches_helper": command_path == HELPER_PATH,
        "supported": True,
    })
    return row


def _logical_sudo_policy_lines(output: str) -> list[str]:
    """Join wrapped continuation lines into ordered logical policy rules."""
    logical: list[str] = []
    in_policy = False
    for raw_line in output.splitlines():
        if _SUDO_RULE.match(raw_line):
            in_policy = True
            logical.append(raw_line.strip())
            continue
        if not in_policy or not raw_line.strip():
            continue
        if raw_line[:1].isspace() and logical:
            logical[-1] = f"{logical[-1]} {raw_line.strip()}"
            continue
        _fail("RESUME_SECURITY_BINDING_DRIFT", "sudo command policy output is malformed")
    return logical


def normalize_sudo_command_policy(output: str) -> list[dict[str, object]]:
    """Parse the complete ordered command-policy section emitted by sudo -l."""
    records: list[dict[str, object]] = []
    for order, line in enumerate(_logical_sudo_policy_lines(output), 1):
        match = _SUDO_RULE.match(line)
        if not match:
            _fail("RESUME_SECURITY_BINDING_DRIFT", "sudo command policy output is malformed")
            raise AssertionError("unreachable")
        tags = re.findall(r"([A-Z_]+):", match.group("tags"))
        password_tag = "PASSWD"
        for tag in tags:
            if tag in SUPPORTED_SUDO_TAGS:
                password_tag = tag
        records.append({
            "order": order,
            "run_as": " ".join(match.group("runas").split()),
            "tag_state": tags,
            "unsupported_tags": [tag for tag in tags if tag not in SUPPORTED_SUDO_TAGS],
            "password_tag": password_tag,
            "commands": [
                classify_sudo_command(item)
                for item in tokenize_sudo_command_list(match.group("commands"))
            ],
        })
    if not records:
        _fail("RESUME_SECURITY_BINDING_DRIFT", "sudo command policy section is absent")
    return records


def sudo_policy_fingerprint(records: Sequence[Mapping[str, object]]) -> str:
    """Hash the complete normalized ordered structural record set."""
    return promotion.sha256_bytes(promotion.canonical_json({
        "collection_locale": COLLECTION_LOCALE,
        "normalized_command_policy_records": [dict(record) for record in records],
    }).encode("utf-8"))


def _runas_allows_root(runas: str) -> bool:
    users = runas.split(":", 1)[0]
    return any(item.strip() in {"root", "ALL"} for item in users.split(","))


def effective_sudo_policy_binding() -> dict[str, object]:
    output = _run(["sudo", "-n", "-l"], code="RESUME_SECURITY_BINDING_DRIFT")
    records = normalize_sudo_command_policy(output)
    matching: list[dict[str, object]] = []
    exact_orders: list[tuple[int, int]] = []
    unsupported: list[dict[str, object]] = []
    for record in records:
        for command_order, command in enumerate(record["commands"], 1):
            if not command["matches_helper"]:
                continue
            reasons = []
            if not command["supported"]:
                reasons.append(str(command["kind"]))
            if record["unsupported_tags"]:
                reasons.append("unsupported_authorization_tag")
            for reason in reasons:
                unsupported.append({
                    "rule_order": record["order"],
                    "command_order": command_order,
                    "reason": reason,
                })
            matching.append({
                "rule_order": record["order"],
                "command_order": command_order,
                "run_as": record["run_as"],
                "run_as_allows_root": _runas_allows_root(str(record["run_as"])),
                "tag_state": list(record["tag_state"]),
                "unsupported_tags": list(record["unsupported_tags"]),
                "password_tag": record["password_tag"],
                "command": command["specification"],
                "command_kind": command["kind"],
                "supported": command["supported"] and not record["unsupported_tags"],
                "negated": command["negated"],
            })
            if (
                not command["negated"]
                and command["specification"] == f"{HELPER_PATH} *"
                and str(record["run_as"]) == "root"
                and record["password_tag"] == "NOPASSWD"
                and not record["unsupported_tags"]
            ):
                exact_orders.append((int(record["order"]), command_order))
    if unsupported:
        _fail(
            "RESUME_SECURITY_BINDING_DRIFT",
            "sudo policy contains helper-matching syntax outside the supported grammar",
        )

    final = matching[-1] if matching else {}
    last_exact = exact_orders[-1] if exact_orders else None
    later_matching = bool(last_exact and any(
        (int(row["rule_order"]), int(row["command_order"])) > last_exact
        for row in matching
    ))
    later_changes_password = bool(
        later_matching and final.get("password_tag") != "NOPASSWD"
    )
    safe = bool(
        exact_orders
        and final.get("negated") is False
        and final.get("run_as_allows_root") is True
        and final.get("password_tag") == "NOPASSWD"
        and not later_changes_password
    )
    binding = {
        "collection_method_version": SUDO_COLLECTION_METHOD,
        "collection_locale": COLLECTION_LOCALE,
        "helper_absolute_path": HELPER_PATH,
        "supported_authorization_tags": list(SUPPORTED_SUDO_TAGS),
        "normalized_command_policy_records": records,
        "normalized_matching_records": matching,
        "matching_rule_count": len(matching),
        "exact_helper_rule_count": len(exact_orders),
        "exact_helper_specific_rule": HELPER_RULE if exact_orders else "",
        "effective_rule": str(final.get("command") or ""),
        "run_as_user": str(final.get("run_as") or ""),
        "nopasswd": final.get("password_tag") == "NOPASSWD",
        "later_matching_rule": later_matching,
        "later_rule_changes_password_requirement": later_changes_password,
        "no_later_conflicting_matching_rule": not later_changes_password,
        "final_effective_authorization": "root_nopasswd_allowed" if safe else "not_authorized",
        "effective_policy_fingerprint": sudo_policy_fingerprint(records),
    }
    if not safe:
        _fail(
            "RESUME_SECURITY_BINDING_DRIFT",
            "effective sudo policy does not end in an exact-bound root NOPASSWD authorization",
        )
    return binding


def _file_metadata(path: Path, *, include_hash: bool = True) -> dict[str, object]:
    absolute = path.resolve(strict=False)
    if path.is_symlink() or not path.is_file():
        _fail("RESUME_SECURITY_BINDING_DRIFT", f"required file is missing or unsafe: {absolute}")
    info = path.stat()
    row: dict[str, object] = {
        "path": str(absolute), "uid": info.st_uid, "gid": info.st_gid,
        "mode": normalized_mode(stat.S_IMODE(info.st_mode)), "size": info.st_size,
        "regular_file": stat.S_ISREG(info.st_mode), "symlink": False,
    }
    if include_hash:
        row["sha256"] = promotion.sha256_bytes(path.read_bytes())
    return row


def _health(url: str, classification: str) -> dict[str, object]:
    try:
        with urlopen(url, timeout=3) as response:
            passed = 200 <= response.status < 400
            status = int(response.status)
    except Exception as exc:
        raise promotion.PromotionError(classification, f"health probe unavailable: {url}") from exc
    if not passed:
        _fail(classification, f"health probe failed: {url}")
    return {"probe": url, "method": "GET", "accepted_status": "200-399", "status": status, "passed": True}


def _process_identity(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except (OSError, ProcessLookupError) as exc:
        raise promotion.PromotionError("RESUME_SYSTEMD_RUNTIME_DRIFT", "systemd process environment unavailable") from exc
    prefix = (promotion.TARGET_ENVIRONMENT_KEY + "=").encode()
    values = [item[len(prefix):].decode("utf-8") for item in raw.split(b"\0") if item.startswith(prefix)]
    if len(values) != 1:
        _fail("RESUME_SYSTEMD_RUNTIME_DRIFT", "systemd identity assignment is absent or ambiguous")
    return values[0]


def _listening_socket_inodes(*, port: int, proc_root: Path = PROC_ROOT) -> list[int]:
    expected_port = f"{port:04X}"
    allowed_addresses = {"0100007F", "0000000000000000FFFF00000100007F"}
    inodes: set[int] = set()
    for name in ("tcp", "tcp6"):
        path = proc_root / "net" / name
        try:
            lines = path.read_text(encoding="ascii").splitlines()[1:]
        except (OSError, UnicodeError):
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 10 or fields[3] != "0A":
                continue
            address, separator, observed_port = fields[1].partition(":")
            if separator and address in allowed_addresses and observed_port == expected_port:
                try:
                    inodes.add(int(fields[9]))
                except ValueError:
                    _fail("RESUME_SYSTEMD_RUNTIME_DRIFT", "listener socket inode is malformed")
    return sorted(inodes)


def _pid_socket_inodes(pid: int, *, proc_root: Path = PROC_ROOT) -> list[int]:
    fd_root = proc_root / str(pid) / "fd"
    try:
        entries = list(fd_root.iterdir())
    except OSError as exc:
        raise promotion.PromotionError(
            "RESUME_SYSTEMD_RUNTIME_DRIFT",
            "MainPID file descriptors are unavailable for listener ownership proof",
        ) from exc
    inodes: set[int] = set()
    for entry in entries:
        try:
            target = os.readlink(entry)
        except OSError:
            continue
        match = re.fullmatch(r"socket:\[(\d+)\]", target)
        if match:
            inodes.add(int(match.group(1)))
    return sorted(inodes)


def systemd_process_listener_proof(
    *, main_pid: int, control_group: str, listener_port: int = 8001,
    proc_root: Path = PROC_ROOT, cgroup_root: Path = CGROUP_ROOT,
) -> dict[str, object]:
    if not control_group.startswith("/") or ".." in Path(control_group).parts:
        _fail("RESUME_SYSTEMD_RUNTIME_DRIFT", "systemd ControlGroup is malformed")
    membership_path = cgroup_root / control_group.lstrip("/") / "cgroup.procs"
    try:
        members = sorted({int(value) for value in membership_path.read_text(encoding="ascii").split()})
    except (OSError, UnicodeError, ValueError) as exc:
        raise promotion.PromotionError(
            "RESUME_SYSTEMD_RUNTIME_DRIFT",
            "systemd cgroup membership is unavailable or malformed",
        ) from exc
    main_present = main_pid in members
    unexpected = [pid for pid in members if pid != main_pid]
    unique = main_present and not unexpected and len(members) == 1
    listener_inodes = _listening_socket_inodes(port=listener_port, proc_root=proc_root)
    main_sockets = _pid_socket_inodes(main_pid, proc_root=proc_root)
    owned = sorted(set(listener_inodes) & set(main_sockets))
    listener_owned = bool(listener_inodes) and owned == listener_inodes
    proof = {
        "control_group": control_group,
        "membership_path": str(membership_path),
        "process_members": members,
        "main_pid_present": main_present,
        "unexpected_processes": unexpected,
        "uniqueness_rule": "service_cgroup_contains_exactly_MainPID",
        "unique_main_process": unique,
        "listener": {
            "host": "127.0.0.1",
            "port": listener_port,
            "listening_socket_inodes": listener_inodes,
            "main_pid_socket_inodes": main_sockets,
            "owned_socket_inodes": owned,
            "owned_by_main_pid": listener_owned,
            "allowed_listener_pid": main_pid,
        },
    }
    if not unique or not listener_owned:
        _fail(
            "RESUME_SYSTEMD_RUNTIME_DRIFT",
            "systemd MainPID/cgroup uniqueness or listener ownership proof failed",
        )
    return proof


def systemd_runtime_binding(*, expected_environment: str) -> dict[str, object]:
    values: dict[str, str] = {}
    output = _run([
        "systemctl", "show", "log-platform-api.service", "-p", "MainPID", "-p", "InvocationID",
        "-p", "NeedDaemonReload", "-p", "FragmentPath", "-p", "DropInPaths",
        "-p", "ActiveState", "-p", "SubState", "-p", "ControlGroup",
    ], code="RESUME_SYSTEMD_RUNTIME_DRIFT")
    for line in output.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    try:
        pid = int(values.get("MainPID") or "0")
    except ValueError:
        pid = 0
    invocation = _hex(values.get("InvocationID"), _HEX32, "systemd InvocationID")
    dropins = [_file_metadata(Path(item)) for item in values.get("DropInPaths", "").split()]
    if pid <= 0 or not dropins or values.get("ActiveState") != "active" or values.get("SubState") != "running":
        _fail("RESUME_SYSTEMD_RUNTIME_DRIFT", "systemd API provenance is incomplete")
    need_reload = values.get("NeedDaemonReload")
    if need_reload not in {"yes", "no"}:
        _fail("RESUME_SYSTEMD_RUNTIME_DRIFT", "NeedDaemonReload is not normalized")
    observed = _process_identity(pid)
    if observed != expected_environment or need_reload != "no":
        _fail("RESUME_SYSTEMD_RUNTIME_DRIFT", "systemd API is not converged to the bound identity")
    unit_text = _run(["systemctl", "cat", "log-platform-api.service"], code="RESUME_SYSTEMD_RUNTIME_DRIFT")
    canonical_source = canonical_source_effective(unit_text)
    if not canonical_source:
        _fail("RESUME_SYSTEMD_RUNTIME_DRIFT", "systemd canonical identity source is not effective")
    process_proof = systemd_process_listener_proof(
        main_pid=pid,
        control_group=_required_text(values.get("ControlGroup"), "systemd ControlGroup"),
    )
    health = _health("http://127.0.0.1:8001/docs", "RESUME_SYSTEMD_RUNTIME_DRIFT")
    active = values.get("ActiveState") == "active" and values.get("SubState") == "running"
    identity_matches = observed == expected_environment
    manager_metadata_current = need_reload == "no"
    health_passed = health.get("passed") is True
    return {
        "service": "log-platform-api.service",
        "fragment_path": resolved_path(values.get("FragmentPath"), "systemd fragment path"),
        "ordered_dropins": dropins, "pid": pid, "invocation_id": invocation,
        "active_state": values["ActiveState"], "sub_state": values["SubState"],
        "control_group": process_proof["control_group"],
        "cgroup_process_members": process_proof["process_members"],
        "main_pid_present_in_cgroup": process_proof["main_pid_present"],
        "process_uniqueness_rule": process_proof["uniqueness_rule"],
        "listener_ownership": process_proof["listener"],
        "need_daemon_reload": need_reload == "yes",
        "identity_source": "/etc/log-platform/environment-identity.env",
        "canonical_source_result": canonical_source, "observed_identity": observed,
        "health": health,
        "convergence_components": {
            "active": active,
            "main_pid_present_in_cgroup": process_proof["main_pid_present"],
            "unique_main_process": process_proof["unique_main_process"],
            "listener_owned_by_main_pid": process_proof["listener"]["owned_by_main_pid"],
            "canonical_source": canonical_source,
            "identity_matches": identity_matches,
            "health_passed": health_passed,
            "manager_metadata_current": manager_metadata_current,
        },
        "unique_main_process": process_proof["unique_main_process"],
    }


def _docker_environment(container_id: str) -> str:
    output = _run([
        "docker", "inspect", "--format", "{{range .Config.Env}}{{println .}}{{end}}", container_id,
    ], code="RESUME_DOCKER_RUNTIME_DRIFT")
    prefix = promotion.TARGET_ENVIRONMENT_KEY + "="
    values = [line[len(prefix):] for line in output.splitlines() if line.startswith(prefix)]
    if len(values) != 1:
        _fail("RESUME_DOCKER_RUNTIME_DRIFT", "Docker identity assignment is absent or ambiguous")
    return values[0]


def docker_convergence_components(
    *, observed_state: str, active_count: int, canonical_source: bool,
    observed_identity: str, expected_environment: str, health_passed: bool,
) -> dict[str, object]:
    """Derive each Docker convergence component from a concrete observation."""
    return {
        "running": observed_state == "running",
        "unique_active_api_container": active_count == 1,
        "canonical_source": bool(canonical_source),
        "identity_matches": observed_identity == expected_environment,
        "health_passed": bool(health_passed),
    }


def docker_runtime_binding(*, repository_root: Path, expected_environment: str) -> dict[str, object]:
    ids = [line.strip() for line in _run(
        ["docker", "compose", "ps", "--status", "running", "-q", "api"],
        cwd=repository_root, code="RESUME_DOCKER_RUNTIME_DRIFT",
    ).splitlines() if line.strip()]
    active_count = len(ids)
    if active_count != 1:
        _fail("RESUME_DOCKER_RUNTIME_DRIFT", "exactly one active Compose api container is required")
    container_id = _hex(ids[0], _HEX64, "Docker container ID")
    inspect = json.loads(_run(["docker", "inspect", container_id], code="RESUME_DOCKER_RUNTIME_DRIFT"))
    if not isinstance(inspect, list) or len(inspect) != 1:
        _fail("RESUME_DOCKER_RUNTIME_DRIFT", "Docker inspection is ambiguous")
    row = inspect[0]
    labels = dict(row.get("Config", {}).get("Labels") or {})
    project = _required_text(labels.get("com.docker.compose.project"), "Compose project")
    service = _required_text(labels.get("com.docker.compose.service"), "Compose service")
    workdir = Path(resolved_path(labels.get("com.docker.compose.project.working_dir"), "Compose working directory"))
    compose_files = [resolved_path(item, "Compose file") for item in str(labels.get("com.docker.compose.project.config_files") or "").split(",") if item]
    observed_state = str(row.get("State", {}).get("Status") or "")
    if service != "api" or not compose_files:
        _fail("RESUME_DOCKER_RUNTIME_DRIFT", "Docker Compose API provenance is incomplete")
    command = ["docker", "compose", "--project-name", project]
    for compose_file in compose_files:
        command += ["--file", compose_file]
    resolved_config = json.loads(_run(
        command + ["config", "--format", "json"], cwd=workdir,
        code="RESUME_DOCKER_RUNTIME_DRIFT",
    ))
    resolved_service = dict(resolved_config.get("services", {}).get("api") or {})
    resolved_environment = dict(resolved_service.get("environment") or {})
    line = _run(command + ["config", "--hash", "api"], cwd=workdir, code="RESUME_DOCKER_RUNTIME_DRIFT").strip()
    fingerprint = _hex(line.split()[-1] if line else "", _HEX64, "Compose fingerprint")
    image_digest = _required_text(row.get("Image"), "Docker image digest")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest):
        _fail("RESUME_DOCKER_RUNTIME_DRIFT", "Docker image digest is not immutable")
    observed = _docker_environment(container_id)
    canonical = (
        resolved_environment.get(promotion.TARGET_ENVIRONMENT_KEY) == expected_environment
        and any(
            "path: /etc/log-platform/environment-identity.env" in Path(item).read_text(encoding="utf-8")
            for item in compose_files
        )
    )
    health = _health("http://127.0.0.1:8000/docs", "RESUME_DOCKER_RUNTIME_DRIFT")
    components = docker_convergence_components(
        observed_state=observed_state, active_count=active_count,
        canonical_source=canonical, observed_identity=observed,
        expected_environment=expected_environment,
        health_passed=health.get("passed") is True,
    )
    duplicate_active_container = active_count > 1
    if not all(components.values()) or duplicate_active_container:
        _fail("RESUME_DOCKER_RUNTIME_DRIFT", "Docker API is not converged to the canonical identity")
    return {
        "compose_project": project, "compose_service": service,
        "ordered_compose_files": compose_files, "compose_working_directory": str(workdir),
        "container_id": container_id, "image_reference": _required_text(row.get("Config", {}).get("Image"), "Docker image reference"),
        "image_digest": image_digest, "compose_configuration_fingerprint": fingerprint,
        "identity_source": "/etc/log-platform/environment-identity.env",
        "canonical_source_resolution": canonical, "observed_identity": observed,
        "observed_container_state": observed_state,
        "health": health,
        "convergence_components": components,
        "active_api_container_count": active_count,
        "duplicate_active_container": duplicate_active_container,
    }


def remaining_execution_contract(promotion_id: str) -> dict[str, object]:
    return {
        "allowed_actions": [{"order": index, "action": action} for index, action in enumerate(ALLOWED_ACTIONS, 1)],
        "writable_promotion_id": canonical_uuid(promotion_id, "promotion ID"),
        "permitted_state_transition": "in_progress -> completed",
        "writable_journal_columns": [
            "completed_steps", "current_step", "state", "completed_at", "failed_at", "error",
            "resume_contract", "resume_plan_sha256", "updated_at",
        ],
        "immutable_journal_columns": [
            "promotion_id", "source_environment", "target_environment", "platform_identity_id",
            "selected_clients", "immutable_plan_json", "plan_sha256", "operator_attestation_hash",
            "backup_reference", "started_at", "created_at", "runtime_file_backup_path",
            "runtime_file_before_sha256", "runtime_file_after_sha256",
        ],
        "all_other_journal_rows": "read_only",
        "persistent_write_policy": "no_persistent_surface_write_attempt",
        "runtime_action_policy": "no_restart_no_recreate_no_daemon_reload",
        "prohibited_actions": list(PROHIBITED_ACTIONS),
    }


def _validate_original_plan(original_plan: Mapping[str, object], journal: Mapping[str, object]) -> None:
    if original_plan.get("contract_version") != 5 or original_plan.get("promotion_plan_contract_version") != 5:
        _fail("RESUME_APPROVAL_IDENTITY_DRIFT", "resume-v2 requires an original v5 promotion plan")
    digest = promotion.plan_hash(original_plan)
    if digest != journal.get("plan_sha256"):
        _fail("RESUME_APPROVAL_IDENTITY_DRIFT", "original plan payload and journal hash differ")


def build_plan(*, approval_identity: Mapping[str, object],
               remote_repository_binding: Mapping[str, object],
               migration_054_schema_contract: Mapping[str, object],
               journal_state: Mapping[str, object],
               persistent_state: Mapping[str, object], runtime_convergence: Mapping[str, object],
               systemd_runtime: Mapping[str, object], docker_runtime: Mapping[str, object],
               security_and_recovery: Mapping[str, object], promotion_id: str) -> dict[str, object]:
    plan: dict[str, object] = {
        "contract": CONTRACT, "contract_version": CONTRACT_VERSION,
        "attestation_contract": ATTESTATION_CONTRACT,
        "approval_identity": dict(approval_identity),
        "remote_repository_binding": dict(remote_repository_binding),
        "migration_054_schema_contract": dict(migration_054_schema_contract),
        "journal_state": dict(journal_state),
        "persistent_state": dict(persistent_state), "runtime_convergence": dict(runtime_convergence),
        "systemd_runtime": dict(systemd_runtime), "docker_runtime": dict(docker_runtime),
        "security_and_recovery": dict(security_and_recovery),
        "remaining_execution_contract": remaining_execution_contract(promotion_id), "supported": True,
    }
    validate_plan(plan)
    return plan


def validate_plan(plan: Mapping[str, object]) -> None:
    _require_no_missing(plan)
    if tuple(plan.keys()) != TOP_LEVEL_SECTIONS or plan.get("contract") != CONTRACT or plan.get("contract_version") != CONTRACT_VERSION or plan.get("attestation_contract") != ATTESTATION_CONTRACT:
        _fail("RESUME_IMMUTABLE_BINDING_DRIFT", "resume-v2 canonical schema is incomplete or reordered")
    approval = _require_keys(plan.get("approval_identity"), "approval_identity", (
        "host", "promotion_id", "original_execution_head", "resume_implementation_head",
        "original_v5_plan_sha256", "platform_uuid",
    ))
    remote = _require_keys(plan.get("remote_repository_binding"), "remote_repository_binding", (
        "collection_method", "remote_name", "remote_url", "remote_identity",
        "remote_ref", "remote_branch", "expected_fetch_refspec",
        "main_tracking_remote", "main_tracking_merge_ref", "local_branch",
        "local_head", "remote_head", "local_remote_head_equal",
    ))
    if (
        remote.get("collection_method") != "git_ls_remote_refs_v1"
        or remote.get("remote_name") != REMOTE_NAME or remote.get("remote_ref") != REMOTE_REF
        or remote.get("remote_branch") != REMOTE_BRANCH or remote.get("local_branch") != REMOTE_BRANCH
        or remote.get("remote_url") != remote.get("remote_identity")
        or remote.get("expected_fetch_refspec") != "+refs/heads/*:refs/remotes/origin/*"
        or remote.get("main_tracking_remote") != REMOTE_NAME
        or remote.get("main_tracking_merge_ref") != REMOTE_REF
        or remote.get("local_remote_head_equal") is not True
        or remote.get("local_head") != remote.get("remote_head")
    ):
        _remote_fail("canonical origin main binding is not approval-ready")
    schema = _require_keys(plan.get("migration_054_schema_contract"), "migration_054_schema_contract", (
        "contract", "expected_database", "current_database", "schema_migration",
        "columns", "constraints", "trigger_function", "trigger", "comments",
        "comment_policy", "target_promotion_resume_audit_state",
        "volatile_catalog_oids_bound",
    ))
    target_audit_state = _require_keys(
        schema.get("target_promotion_resume_audit_state"),
        "migration_054_schema_contract.target_promotion_resume_audit_state",
        ("promotion_id", "non_null_resume_audit_row_count"),
    )
    if (
        schema.get("contract") != "migration_054_complete_catalog_v1"
        or tuple(target_audit_state.keys()) != (
            "promotion_id", "non_null_resume_audit_row_count",
        )
        or target_audit_state.get("non_null_resume_audit_row_count") != 0
        or schema.get("volatile_catalog_oids_bound") is not False
    ):
        _schema_fail("migration-054 schema binding is not approval-ready")
    expected_database = _require_keys(
        schema.get("expected_database"), "migration_054_schema_contract.expected_database",
        ("name", "identity_uuid"),
    )
    current_database = _require_keys(
        schema.get("current_database"), "migration_054_schema_contract.current_database",
        ("name", "catalog_name", "identity_uuid", "marker_name", "marker_role",
         "marker_identity_key", "owner"),
    )
    migration = _require_keys(
        schema.get("schema_migration"), "migration_054_schema_contract.schema_migration",
        ("filename", "filename_count", "migration_row_count", "ceiling"),
    )
    columns = list(schema.get("columns") or [])
    constraints = list(schema.get("constraints") or [])
    trigger_function = dict(schema.get("trigger_function") or {})
    trigger = dict(schema.get("trigger") or {})
    comments = list(schema.get("comments") or [])
    if (
        expected_database.get("name") != current_database.get("name")
        or expected_database.get("identity_uuid") != current_database.get("identity_uuid")
        or current_database.get("catalog_name") != current_database.get("name")
        or current_database.get("marker_name") != current_database.get("name")
        or current_database.get("marker_role") != "platform"
        or current_database.get("marker_identity_key") != "primary"
        or migration.get("filename") != RESUME_SCHEMA_MIGRATION
        or migration.get("filename_count") != 1
        or migration.get("ceiling") != RESUME_SCHEMA_MIGRATION
        or not isinstance(migration.get("migration_row_count"), int)
        or int(migration.get("migration_row_count") or 0) < 1
        or columns != _expected_canonical_schema_columns()
        or constraints != [
            {
                "table_schema": "ops_control",
                "table_name": "environment_identity_promotion",
                "name": "ck_environment_identity_promotion_resume_contract",
                "type": "c",
                "validated": True,
                "definition": "CHECK (((resume_contract IS NULL) OR (resume_contract = 'resume-v2'::text)))",
            },
            {
                "table_schema": "ops_control",
                "table_name": "environment_identity_promotion",
                "name": "ck_environment_identity_promotion_resume_plan_hash",
                "type": "c",
                "validated": True,
                "definition": "CHECK (((resume_plan_sha256 IS NULL) OR (resume_plan_sha256 ~ '^[0-9a-f]{64}$'::text)))",
            },
        ]
        or trigger_function != {
            "schema": "ops_control",
            "name": "reject_environment_identity_promotion_plan_update",
            "identity_arguments": CANONICAL_SCHEMA_ABSENCE["identity_arguments"],
            "return_type": "trigger",
            "kind": "f",
            "owner": current_database.get("owner"),
            "language": "plpgsql",
            "security_definer": False,
            "volatility": "v",
            "proconfig": CANONICAL_SCHEMA_ABSENCE["proconfig"],
            "prosrc_sha256": RESUME_TRIGGER_FUNCTION_PROSRC_SHA256,
        }
        or trigger != {
            "name": "trg_environment_identity_promotion_plan_immutable",
            "table_schema": "ops_control",
            "table_name": "environment_identity_promotion",
            "enabled": "O",
            "function_schema": "ops_control",
            "function_name": "reject_environment_identity_promotion_plan_update",
            "definition": (
                "CREATE TRIGGER trg_environment_identity_promotion_plan_immutable BEFORE UPDATE "
                "ON ops_control.environment_identity_promotion FOR EACH ROW EXECUTE FUNCTION "
                "ops_control.reject_environment_identity_promotion_plan_update()"
            ),
            "table_owner": current_database.get("owner"),
            "timing": "BEFORE",
            "row_level": True,
            "events": ["UPDATE"],
        }
        or comments != [
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
        ]
        or schema.get("comment_policy") != "exact_text_required_block_on_absent_or_changed"
    ):
        _schema_fail("migration-054 canonical schema shape is incomplete or incorrect")
    journal = _require_keys(plan.get("journal_state"), "journal_state", (
        "promotion_id", "state", "current_step", "completed_steps", "expected_remaining_steps",
        "reconciliation_records",
    ))
    persistent = _require_keys(plan.get("persistent_state"), "persistent_state", (
        "canonical_identity", "platform", "clients", "uuid_consistency",
    ))
    _require_keys(persistent.get("canonical_identity"), "persistent_state.canonical_identity", (
        "path", "environment", "sha256", "mode", "regular_file", "symlink",
    ))
    _require_keys(plan.get("systemd_runtime"), "systemd_runtime", (
        "pid", "invocation_id", "control_group", "cgroup_process_members",
        "main_pid_present_in_cgroup", "listener_ownership", "unique_main_process",
        "convergence_components",
    ))
    _require_keys(plan.get("docker_runtime"), "docker_runtime", (
        "container_id", "image_digest", "compose_configuration_fingerprint",
        "observed_identity", "convergence_components",
    ))
    _require_keys(plan.get("security_and_recovery"), "security_and_recovery", (
        "effective_sudo_policy", "resume_implementation_assets", "retired_contracts",
    ))
    if plan.get("supported") is not True:
        _fail("RESUME_IMMUTABLE_BINDING_DRIFT", "unsupported plan cannot be approved")
    if journal.get("state") != "in_progress":
        _fail("RESUME_JOURNAL_STATE_DRIFT", "only an in-progress journal is executable")
    runtime = dict(plan.get("runtime_convergence") or {})
    if runtime.get("readiness_classification") != "PRODUCTION_PROMOTION_READY" or any(runtime.get(key) for key in ("reload_required_consumers", "unconverted_consumers", "conflicting_declarations")):
        _fail("RESUME_IMMUTABLE_BINDING_DRIFT", "runtime convergence is not approval-ready")
    actions = dict(plan.get("remaining_execution_contract") or {})
    if actions.get("allowed_actions") != [{"order": i, "action": v} for i, v in enumerate(ALLOWED_ACTIONS, 1)] or actions.get("prohibited_actions") != list(PROHIBITED_ACTIONS):
        _fail("RESUME_EXECUTION_CONTRACT_DRIFT", "remaining execution contract differs from resume-v2")
    approval_promotion_id = canonical_uuid(
        approval.get("promotion_id"), "approval promotion ID",
    )
    promotion_ids = (
        approval_promotion_id,
        str(journal.get("promotion_id")),
        str(target_audit_state.get("promotion_id")),
        str(actions.get("writable_promotion_id")),
    )
    if any(value != approval_promotion_id for value in promotion_ids):
        _schema_fail(
            "target promotion ID differs between approval, journal, schema contract, and write scope"
        )
    serialized = promotion.canonical_json(plan)
    if "timestamp" in serialized.lower():
        _fail("RESUME_IMMUTABLE_BINDING_DRIFT", "timestamps are excluded from resume-v2 canonical bytes")


def attestation(plan: Mapping[str, object]) -> str:
    validate_plan(plan)
    approval = dict(plan["approval_identity"])
    journal = dict(plan["journal_state"])
    persistent = dict(plan["persistent_state"])
    clients = sorted(persistent.get("clients", []), key=lambda row: str(row["client_code"]))
    client_token = ",".join(f"{row['client_code']}:{row['database_uuid']}" for row in clients)
    remaining = ",".join(str(item) for item in journal["expected_remaining_steps"])
    return (
        f"RESUME_ENVIRONMENT_IDENTITY_V2 host={approval['host']} promotion_id={approval['promotion_id']} "
        f"contract=resume-v2 original_execution_head={approval['original_execution_head']} "
        f"resume_implementation_head={approval['resume_implementation_head']} "
        f"original_plan_sha256={approval['original_v5_plan_sha256']} resume_plan_sha256={promotion.plan_hash(plan)} "
        f"journal_state={journal['state']} current_step={journal['current_step']} remaining_steps={remaining} "
        f"canonical_sha256={persistent['canonical_identity']['sha256']} platform_uuid={approval['platform_uuid']} "
        f"clients={client_token} systemd_pid={plan['systemd_runtime']['pid']} "
        f"systemd_invocation_id={plan['systemd_runtime']['invocation_id']} docker_container_id={plan['docker_runtime']['container_id']} "
        f"compose_fingerprint={plan['docker_runtime']['compose_configuration_fingerprint']} "
        "UUIDS_UNCHANGED=true NO_PERSISTENT_WRITES=true NO_RUNTIME_RESTART=true"
    )


def require_resume_v2_finalization_route(
    *, journal: Mapping[str, object], original_steps: Sequence[str],
) -> str:
    """Classify a promotion before applying any resume approval contract."""
    state = str(journal.get("state") or "")
    if state in {"completed", "rolled_back"}:
        _fail("PROMOTION_TERMINAL", "terminal promotion cannot be resumed")
    if state != "in_progress":
        _fail("RESUME_JOURNAL_STATE_DRIFT", "resume-v2 requires an in-progress journal")
    steps = [str(step) for step in original_steps]
    completed = [str(step) for step in journal.get("completed_steps") or []]
    if completed != steps[:len(completed)]:
        _fail("RESUME_JOURNAL_STATE_DRIFT", "completed steps are not a frozen-plan prefix")
    remaining = steps[len(completed):]
    final_suffix = [
        promotion.STEP_RUNTIME_RELOAD,
        promotion.STEP_RUNTIME_PROCESSES,
        promotion.STEP_FINAL_VERIFY,
    ]
    if remaining and remaining[0] not in final_suffix:
        _fail(
            "PRE_RUNTIME_FORWARD_RESUME_CONTRACT_REQUIRED",
            "the journal is before persistent/runtime convergence and requires a separately reviewed forward-recovery contract",
        )
    if not remaining:
        _fail(
            "RESUME_JOURNAL_FINALIZATION_DEAD_END",
            "the journal has no remaining suffix but is not terminal; manual independent reconciliation is required",
        )
    if str(journal.get("current_step") or "") != remaining[0]:
        _fail("RESUME_JOURNAL_STATE_DRIFT", "current_step does not match the remaining frozen-plan suffix")
    return "resume-v2-finalization"


def reject_retired_execution(*, attestation_value: str | None, resume_plan_sha256: str | None,
                             contract: object = CONTRACT, contract_version: object = CONTRACT_VERSION) -> None:
    text = str(attestation_value or "")
    digest = str(resume_plan_sha256 or "")
    if digest == REJECTED_INCOMPLETE_RESUME_V2_HASH:
        _fail("RESUME_IMMUTABLE_BINDING_DRIFT", "the known incomplete production resume-v2 plan is permanently rejected", invalid=True)
    if "contract=resume-v1" in text or digest == RETIRED_RESUME_V1_HASH or contract != CONTRACT or contract_version != CONTRACT_VERSION:
        _fail("RESUME_V1_NON_EXECUTABLE", "resume-v1 and non-v2 resume contracts are permanently non-executable", invalid=True)
    if not digest:
        _fail("RESUME_APPROVAL_ARGUMENT_MISSING", "--resume-plan-sha256 is required", invalid=True)
    _hex(digest, _HEX64, "resume plan SHA-256")


def diagnostic_v1() -> dict[str, object]:
    return {
        "contract": "environment_identity_resume_plan_v1", "attestation_contract": "resume-v1",
        "executable": False, "contract_retired": True, "retired_hash": RETIRED_RESUME_V1_HASH,
    }


def drift_classification(planned: Mapping[str, object], current: Mapping[str, object]) -> str:
    categories = (
        ("approval_identity", "RESUME_APPROVAL_IDENTITY_DRIFT"),
        ("remote_repository_binding", "RESUME_REMOTE_HEAD_DRIFT"),
        ("migration_054_schema_contract", "RESUME_SCHEMA_CONTRACT_DRIFT"),
        ("journal_state", "RESUME_JOURNAL_STATE_DRIFT"),
        ("persistent_state", "RESUME_PERSISTENT_STATE_DRIFT"),
        ("systemd_runtime", "RESUME_SYSTEMD_RUNTIME_DRIFT"),
        ("docker_runtime", "RESUME_DOCKER_RUNTIME_DRIFT"),
        ("remaining_execution_contract", "RESUME_EXECUTION_CONTRACT_DRIFT"),
    )
    for key, code in categories:
        if planned.get(key) != current.get(key):
            return code
    if planned.get("runtime_convergence") != current.get("runtime_convergence"):
        return "RESUME_IMMUTABLE_BINDING_DRIFT"
    old_security = dict(planned.get("security_and_recovery") or {})
    new_security = dict(current.get("security_and_recovery") or {})
    if old_security.get("historical_promotions") != new_security.get("historical_promotions") or old_security.get("historical_promotion_counts") != new_security.get("historical_promotion_counts"):
        return "RESUME_HISTORY_DRIFT"
    if old_security != new_security:
        return "RESUME_SECURITY_BINDING_DRIFT"
    return "RESUME_IMMUTABLE_BINDING_DRIFT"
