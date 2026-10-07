"""Read-only inventory with configuration and process provenance checks."""
from __future__ import annotations

import grp
import json
import os
import pwd
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.request import urlopen

from ops.environment_identity_file import (
    CANONICAL_IDENTITY_FILE, HELPER_INSTALL_PATH, HELPER_LIBRARY_INSTALL_PATH,
    IDENTITY_KEY, IdentityFileError, identity_assignments_in_env_file,
    inspect_identity_file, public_state, sha256_bytes,
)
from ops.release_boundary import expected_wrapper_source_relative, wrapper_replaceability
from ops.runtime_identity_inspection import RootInspectionError, inspect_root_sources
from ops.systemd_environment_files import canonical_source_effective, merged_dropin_text

CONFLICT_SOURCE_PATHS = (
    Path("/opt/log-platform/.env"), Path("/etc/log-platform-host.env"),
    Path("/etc/log-platform/runtime.env"), Path("/etc/log-platform/backup.env"),
)
ROOT_OWNED_CONFLICT_SOURCE_PATHS = frozenset(CONFLICT_SOURCE_PATHS[1:])
CONSUMERS = (
    {"component": "dispatcher", "cached": False, "refresh_action": "none; next invocation"},
    {"component": "workflow_a", "cached": False, "refresh_action": "none; next invocation"},
    {"component": "workflow_b", "cached": False, "refresh_action": "none; next invocation"},
    {"component": "retention", "cached": False, "refresh_action": "none; next invocation"},
    {"component": "prune", "cached": False, "refresh_action": "none; next invocation"},
    {"component": "backup", "cached": False, "refresh_action": "none; next invocation"},
    {"component": "systemd_api", "cached": True, "refresh_action": "systemctl restart log-platform-api.service"},
    {"component": "docker_api", "cached": True, "refresh_action": "docker compose -f docker-compose.yml up -d --no-deps --force-recreate api"},
    {"component": "suspected_bug_worker", "cached": False, "refresh_action": "none while disabled"},
    {"component": "manual_commands", "cached": False, "refresh_action": "none; next invocation"},
)
CONFIG_EVIDENCE = {
    "dispatcher": "ops/systemd/proposed/log-job-runner.sh", "workflow_a": "ops/run_with_environment_identity.py",
    "workflow_b": "ops/systemd/proposed/log-job-runner.sh", "retention": "ops/systemd/proposed/log-job-runner.sh",
    "prune": "ops/systemd/log-platform-prune.service", "backup": "ops/systemd/log-backup.service",
    "systemd_api": "ops/systemd/proposed/log-platform-api.service.d/zz-environment-identity.conf",
    "docker_api": "docker-compose.yml", "suspected_bug_worker": "ops/systemd/proposed/suspected-bug-email-worker.service",
    "manual_commands": "ops/run_with_environment_identity.py",
}
INSTALL_EVIDENCE = {
    "dispatcher": ("ops/systemd/proposed/log-job-runner.sh", "/usr/local/bin/log-job-runner.sh", 0o755),
    "workflow_a": ("ops/systemd/proposed/log-job-runner.sh", "/usr/local/bin/log-job-runner.sh", 0o755),
    "workflow_b": ("ops/systemd/proposed/log-job-runner.sh", "/usr/local/bin/log-job-runner.sh", 0o755),
    "retention": ("ops/systemd/proposed/log-job-runner.sh", "/usr/local/bin/log-job-runner.sh", 0o755),
    "prune": ("ops/systemd/proposed/log-platform-prune.service.d/90-environment-identity.conf", "/etc/systemd/system/log-platform-prune.service.d/90-environment-identity.conf", 0o644),
    "backup": ("ops/systemd/proposed/log-backup.service.d/90-environment-identity.conf", "/etc/systemd/system/log-backup.service.d/90-environment-identity.conf", 0o644),
    "systemd_api": ("ops/systemd/proposed/log-platform-api.service.d/zz-environment-identity.conf", "/etc/systemd/system/log-platform-api.service.d/zz-environment-identity.conf", 0o644),
}
MANUAL_CONTRACT_TOKENS = (
    "identity_assignments_in_env_file(repo_env)", "load_dotenv(repo_env, override=False)",
    "apply_identity_to_environ(path=CANONICAL_IDENTITY_FILE", "os.execvpe(command[0], command, os.environ)",
)

def _collector_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment.update({"LC_ALL": "C", "LANG": "C", "LC_MESSAGES": "C"})
    environment.pop("COLUMNS", None)
    return environment


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value: return None
    value = value.strip()
    if not value or value == "n/a": return None
    try: return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        try: return datetime.strptime(value, "%a %Y-%m-%d %H:%M:%S %Z").replace(tzinfo=timezone.utc)
        except ValueError: return None


def _process_environment(pid: int) -> str | None:
    try: raw = Path(f"/proc/{pid}/environ").read_bytes()
    except (FileNotFoundError, PermissionError, ProcessLookupError): return None
    prefix = (IDENTITY_KEY + "=").encode()
    for item in raw.split(b"\0"):
        if item.startswith(prefix): return item[len(prefix):].decode("utf-8", errors="replace")
    return None


def _api_health(url: str) -> bool:
    try:
        with urlopen(url, timeout=3) as response:
            return 200 <= response.status < 400
    except Exception: return False


def _systemd_api_probe() -> dict[str, object]:
    try:
        result = subprocess.run(["systemctl", "show", "log-platform-api.service", "--property=MainPID,ExecMainStartTimestamp"], check=False, capture_output=True, text=True, timeout=5, env=_collector_environment())
        values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        pid = int(values.get("MainPID") or "0")
    except (ValueError, OSError, subprocess.SubprocessError): return {"status": "unknown", "effective_environment": None, "started_at": None, "health": False}
    return {"status": "running" if pid > 0 else "inactive", "effective_environment": _process_environment(pid) if pid else None,
            "started_at": _parse_timestamp(values.get("ExecMainStartTimestamp")),
            "health": _api_health("http://127.0.0.1:8001/docs") if pid else False,
            "health_probe": "http://127.0.0.1:8001/docs"}


def _docker_api_probe(repo_root: Path) -> dict[str, object]:
    try:
        query = subprocess.run(["docker", "compose", "ps", "-q", "api"], cwd=repo_root, check=False, capture_output=True, text=True, timeout=8, env=_collector_environment())
        container = query.stdout.strip()
        if not container: return {"status": "inactive", "effective_environment": None, "started_at": None, "health": False}
        result = subprocess.run(["docker", "inspect", "-f", "{{.Created}}|{{range .Config.Env}}{{println .}}{{end}}", container], check=False, capture_output=True, text=True, timeout=8, env=_collector_environment())
    except (OSError, subprocess.SubprocessError): return {"status": "unknown", "effective_environment": None, "started_at": None, "health": False}
    lines = result.stdout.splitlines(); value = None
    environment_lines = lines[1:]
    if lines and "|" in lines[0]: environment_lines = [lines[0].partition("|")[2], *environment_lines]
    for line in environment_lines:
        if line.startswith(IDENTITY_KEY + "="): value = line.partition("=")[2]
    return {"status": "running", "effective_environment": value, "started_at": _parse_timestamp(lines[0].partition("|")[0] if lines else None),
            "health": _api_health("http://127.0.0.1:8000/docs"),
            "health_probe": "http://127.0.0.1:8000/docs"}


def _systemd_api_value() -> tuple[str, str | None]:
    probe = _systemd_api_probe()
    return str(probe["status"]), probe.get("effective_environment")  # compatibility for redacted inventory


def _docker_api_value(repo_root: Path) -> tuple[str, str | None]:
    probe = _docker_api_probe(repo_root)
    return str(probe["status"]), probe.get("effective_environment")  # compatibility for redacted inventory


def _systemd_unit_text(installation_root: Path) -> str:
    if installation_root != Path("/"):
        return merged_dropin_text(installation_root / "etc/systemd/system/log-platform-api.service.d")
    try:
        return subprocess.run(["systemctl", "cat", "log-platform-api.service"], check=False, capture_output=True, text=True, timeout=8, env=_collector_environment()).stdout
    except (OSError, subprocess.SubprocessError): return ""


def _compose_source_state(repo_root: Path, *, allow_static_only: bool) -> tuple[bool, str | None]:
    compose = repo_root / "docker-compose.yml"
    static = compose.is_file() and "path: /etc/log-platform/environment-identity.env" in compose.read_text(encoding="utf-8")
    if allow_static_only: return static, None
    try:
        result = subprocess.run(["docker", "compose", "-f", "docker-compose.yml", "config", "--format", "json"], cwd=repo_root,
                                check=False, capture_output=True, text=True, timeout=10, env=_collector_environment())
        payload = json.loads(result.stdout) if result.returncode == 0 else {}
        value = payload.get("services", {}).get("api", {}).get("environment", {}).get(IDENTITY_KEY)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError): value = None
    return static and value is not None, value


def _oneshot_state(unit: str, *, installation_root: Path) -> str:
    if installation_root != Path("/"):
        directory = installation_root / f"etc/systemd/system/{unit}.d"
        return "PER_INVOCATION_READY" if canonical_source_effective(merged_dropin_text(directory)) else "BLOCKED"
    try:
        shown = subprocess.run(["systemctl", "show", unit, "--property=ActiveState,NeedDaemonReload"],
                               check=False, capture_output=True, text=True, timeout=5, env=_collector_environment())
        values = dict(line.split("=", 1) for line in shown.stdout.splitlines() if "=" in line)
        unit_text = subprocess.run(["systemctl", "cat", unit], check=False, capture_output=True, text=True, timeout=5, env=_collector_environment()).stdout
    except (OSError, subprocess.SubprocessError): return "BLOCKED"
    return "PER_INVOCATION_READY" if (values.get("ActiveState") == "inactive" and values.get("NeedDaemonReload") == "no"
                                        and canonical_source_effective(unit_text)) else "BLOCKED"


def _configuration_time(paths: list[Path]) -> datetime | None:
    timestamps = [path.stat().st_mtime for path in paths if path.is_file() and not path.is_symlink()]
    return datetime.fromtimestamp(max(timestamps), timezone.utc) if timestamps else None


def _probe_row(component: str, probe: object, *, expected: str, configured: bool, configured_at: datetime | None) -> dict[str, object]:
    if isinstance(probe, tuple):
        probe = {"status": probe[0], "effective_environment": probe[1], "started_at": None, "health": False}
    assert isinstance(probe, dict)
    started = probe.get("started_at"); started = _parse_timestamp(started) if isinstance(started, str) else started
    after_config = bool(started and configured_at and started >= configured_at)
    semantic = probe.get("effective_environment") == expected
    converged = bool(probe.get("status") == "running" and configured and after_config and semantic and probe.get("health") is True)
    return {"component": component, "status": probe.get("status"), "effective_environment": probe.get("effective_environment"),
            "semantic_identity_matches": semantic, "configuration_provenance": configured,
            "process_started_at": started.isoformat() if isinstance(started, datetime) else None,
            "configuration_effective_at": configured_at.isoformat() if configured_at else None,
            "process_provenance": after_config, "health_check_passed": probe.get("health") is True, "converged": converged}


def _contract_covered(component: str, text: str) -> bool:
    if component in {"manual_commands", "workflow_a"}: return all(token in text for token in MANUAL_CONTRACT_TOKENS)
    if component in {"dispatcher", "workflow_b", "retention"}: return "run_with_environment_identity.py" in text
    if component == "docker_api": return "path: /etc/log-platform/environment-identity.env" in text
    return "EnvironmentFile=/etc/log-platform/environment-identity.env" in text


def inspect_runtime_convergence(repo_root: Path, *, canonical_path: Path = CANONICAL_IDENTITY_FILE,
        conflict_sources: tuple[Path, ...] = CONFLICT_SOURCE_PATHS, probe_processes: bool = True,
        process_probe: Callable[[str], object] | None = None, installation_root: Path = Path("/"),
        enforce_canonical_metadata: bool = True, enforce_install_metadata: bool = True) -> dict[str, object]:
    try:
        metadata = {"expected_uid": pwd.getpwnam("root").pw_uid, "expected_gid": grp.getgrnam("logplatform").gr_gid, "expected_mode": 0o640} if enforce_canonical_metadata else {}
        canonical = inspect_identity_file(canonical_path, **metadata)
    except IdentityFileError as exc:
        return {"classification": "RUNTIME_IDENTITY_NOT_PROVISIONED", "error_code": exc.code, "execute_allowed": False, "consumers": list(CONSUMERS)}
    conflicts = []
    for path in (item for item in conflict_sources if item not in ROOT_OWNED_CONFLICT_SOURCE_PATHS):
        values = identity_assignments_in_env_file(path)
        if values: conflicts.append({"path": str(path), "definition_count": len(values), "matches_canonical": len(values) == 1 and values[0] == canonical.environment})
    privileged_paths = {str(path) for path in conflict_sources if path in ROOT_OWNED_CONFLICT_SOURCE_PATHS}
    if privileged_paths:
        try: privileged = inspect_root_sources()
        except RootInspectionError as exc:
            return {"classification": "RUNTIME_IDENTITY_NOT_PROVISIONED", "root_inspection_classification": exc.classification, "error_code": exc.reason_code, "execute_allowed": False, "consumers": list(CONSUMERS)}
        rows = {str(row["path"]): row for row in privileged["sources"]}
        if set(rows) != privileged_paths: return {"classification": "RUNTIME_IDENTITY_NOT_PROVISIONED", "error_code": "ROOT_INSPECTION_SCOPE_MISMATCH", "execute_allowed": False, "consumers": list(CONSUMERS)}
        for path in (item for item in conflict_sources if item in ROOT_OWNED_CONFLICT_SOURCE_PATHS):
            row = rows[str(path)]
            if row["has_active_assignment"]: conflicts.append({"path": str(path), "definition_count": row["active_assignment_count"], "matches_canonical": row["canonical_value"] == canonical.environment})
    configured=[]; unconverted=[]
    for consumer in CONSUMERS:
        component=str(consumer["component"]); evidence=repo_root/CONFIG_EVIDENCE[component]
        text=evidence.read_text(encoding="utf-8") if evidence.is_file() else ""; covered=_contract_covered(component,text)
        state = "PER_INVOCATION_READY" if component in {"prune", "backup"} and covered else ("CONFIGURED" if covered else "BLOCKED")
        configured.append({**consumer, "configuration_evidence": str(evidence), "converted": covered, "consumer_state": state})
        if not covered: unconverted.append(component)
    installation=[]
    # Once the release boundary is live, /usr/local/bin/log-job-runner.sh is the
    # release wrapper and the development-tree wrapper is no longer the correct
    # expectation. Comparing against the stale one would report four components
    # as mismatched and send the operator to re-provision, which would overwrite
    # the release wrapper and silently revert the cutover.
    wrapper_target = Path("/usr/local/bin/log-job-runner.sh")
    wrapper_installed = wrapper_target if installation_root==Path("/") else installation_root/wrapper_target.relative_to("/")
    wrapper_source_relative = expected_wrapper_source_relative(repo_root=repo_root, installed_wrapper=wrapper_installed)
    # Report the boundary state as a fact of its own. A wrapper mismatch alone is
    # ambiguous — it reads as "reinstall the development wrapper", which is the
    # one action that silently destroys a live release boundary. Naming the
    # variant, and whether provisioning may touch it, makes the correct remedy
    # (promote a release) visible instead of inferred.
    wrapper_decision = wrapper_replaceability(repo_root=repo_root, installed_wrapper=wrapper_installed)
    release_boundary_state = {"installed_wrapper": str(wrapper_installed),
                              "installed_wrapper_variant": wrapper_decision["variant"],
                              "provisioning_may_replace": wrapper_decision["replaceable"],
                              "expected_wrapper_source": wrapper_source_relative,
                              "refusal_classification": wrapper_decision["classification"],
                              "reason": wrapper_decision["reason"]}
    for component,(source_relative,target_absolute,expected_mode) in INSTALL_EVIDENCE.items():
        if Path(target_absolute)==wrapper_target: source_relative=wrapper_source_relative
        source=repo_root/source_relative; target=Path(target_absolute) if installation_root==Path("/") else installation_root/Path(target_absolute).relative_to("/")
        source_hash=sha256_bytes(source.read_bytes()) if source.is_file() else None
        target_stat=target.stat() if target.is_file() and not target.is_symlink() else None
        target_hash=sha256_bytes(target.read_bytes()) if target_stat else None
        metadata_matches=bool(target_stat) and (not enforce_install_metadata or (target_stat.st_uid==0 and target_stat.st_gid==0 and (target_stat.st_mode&0o7777)==expected_mode))
        matches=source_hash is not None and source_hash==target_hash and metadata_matches
        installation.append({"component":component,"source":str(source),"target":str(target),"source_sha256":source_hash,"installed_sha256":target_hash,"expected_mode":f"{expected_mode:04o}","metadata_matches":metadata_matches,"matches":matches})
        if not matches: unconverted.append(component)
    unit_text=_systemd_unit_text(installation_root); systemd_configured=canonical_source_effective(unit_text)
    compose=repo_root/"docker-compose.yml"; compose_configured, compose_value = _compose_source_state(repo_root, allow_static_only=installation_root != Path("/"))
    oneshot_states = {"prune": _oneshot_state("log-platform-prune.service", installation_root=installation_root),
                      "backup": _oneshot_state("log-backup.service", installation_root=installation_root)}
    for row in configured:
        if row["component"] in oneshot_states: row["consumer_state"] = oneshot_states[str(row["component"])]
    processes=[]
    if probe_processes:
        canonical_actual=canonical_path
        systemd_target=Path("/etc/systemd/system/log-platform-api.service.d/zz-environment-identity.conf") if installation_root==Path("/") else installation_root/"etc/systemd/system/log-platform-api.service.d/zz-environment-identity.conf"
        systemd_time=_configuration_time([canonical_actual,systemd_target])
        docker_time=_configuration_time([canonical_actual,compose])
        for component,configured_flag,configured_at,default in (("systemd_api",systemd_configured,systemd_time,_systemd_api_probe),("docker_api",compose_configured,docker_time,lambda:_docker_api_probe(repo_root))):
            result=process_probe(component) if process_probe else default()
            processes.append(_probe_row(component,result,expected=canonical.environment,configured=configured_flag,configured_at=configured_at))
    stale=[row["component"] for row in processes if not row["converged"]]
    helper_source=repo_root/"ops/systemd/proposed/log-platform-environment-identity-helper"; dependency_source=repo_root/"ops/environment_identity_file.py"
    helper_target=HELPER_INSTALL_PATH if installation_root==Path("/") else installation_root/HELPER_INSTALL_PATH.relative_to("/")
    dependency_target=HELPER_LIBRARY_INSTALL_PATH if installation_root==Path("/") else installation_root/HELPER_LIBRARY_INSTALL_PATH.relative_to("/")
    helper_stat=helper_target.stat() if helper_target.is_file() and not helper_target.is_symlink() else None
    dependency_stat=dependency_target.stat() if dependency_target.is_file() and not dependency_target.is_symlink() else None
    helper={"source_sha256":sha256_bytes(helper_source.read_bytes()) if helper_source.is_file() else None,
            "installed_sha256":sha256_bytes(helper_target.read_bytes()) if helper_stat else None,
            "dependency_source_sha256":sha256_bytes(dependency_source.read_bytes()) if dependency_source.is_file() else None,
            "dependency_sha256":sha256_bytes(dependency_target.read_bytes()) if dependency_stat else None,
            "installed_metadata_matches":bool(helper_stat) and (not enforce_install_metadata or (helper_stat.st_uid==0 and helper_stat.st_gid==0 and stat.S_IMODE(helper_stat.st_mode)==0o755)),
            "dependency_metadata_matches":bool(dependency_stat) and (not enforce_install_metadata or (dependency_stat.st_uid==0 and dependency_stat.st_gid==0 and stat.S_IMODE(dependency_stat.st_mode)==0o644)),
            "contract_check":"deterministic_hash_and_invocation_contract"}
    helper_ok=(helper["source_sha256"]==helper["installed_sha256"] and helper["dependency_source_sha256"]==helper["dependency_sha256"]
               and helper["installed_metadata_matches"] and helper["dependency_metadata_matches"])
    if conflicts or unconverted: classification="RUNTIME_IDENTITY_MIXED"
    elif not helper_ok: classification="RUNTIME_IDENTITY_NOT_PROVISIONED"
    elif stale: classification="RUNTIME_RELOAD_REQUIRED"
    else: classification="PRODUCTION_PROMOTION_READY"
    return {"classification":classification,"execute_allowed":classification=="PRODUCTION_PROMOTION_READY","canonical_file":public_state(canonical),
            "conflicting_declarations":conflicts,"unconverted_consumers":sorted(set(unconverted)),"installation_evidence":installation,
            "consumers":configured,"running_processes":processes,"reload_required":stale,"helper":helper,
            "systemd_effective_canonical_source":systemd_configured,"docker_compose_canonical_source":compose_configured,
            "docker_compose_resolved_environment":compose_value, "per_invocation_consumers": oneshot_states,
            "release_boundary":release_boundary_state,
            "timer_restarts_required":False}
