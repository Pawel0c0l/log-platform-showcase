#!/usr/bin/env python3
"""Dry-run-first remediation of API canonical EnvironmentFile precedence."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import socket
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path: sys.path.insert(0, str(REPO_ROOT))

from ops.git_repository_state import RepositoryStateError, require_clean_repository  # noqa: E402
from ops.runtime_identity_recovery import (  # noqa: E402
    DIRECTORY_MODE, FILE_MODE, RecoveryStorageError, RecoveryStore,
    default_recovery_root, resolve_recovery_owner, sha256_file,
    validate_preserved_recovery_pair, validate_recovery_root,
)
from ops.systemd_environment_files import canonical_source_effective  # noqa: E402

CANONICAL = Path("/etc/log-platform/environment-identity.env")
SOURCE = REPO_ROOT / "ops/systemd/proposed/log-platform-api.service.d/zz-environment-identity.conf"
TARGET = Path("/etc/systemd/system/log-platform-api.service.d/zz-environment-identity.conf")
OBSOLETE = Path("/etc/systemd/system/log-platform-api.service.d/90-environment-identity.conf")
OVERRIDE = Path("/etc/systemd/system/log-platform-api.service.d/override.conf")
UNIT = "log-platform-api.service"


class RemediationError(RuntimeError):
    def __init__(self, code: str, details: dict[str, object] | None = None) -> None:
        self.code=code; self.details=details or {}; super().__init__(code)


def canonical_json(value: object) -> str:
    return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=True)


def _regular_hash(path: Path, code: str) -> str:
    if path.is_symlink() or not path.is_file(): raise RemediationError(code)
    return sha256_file(path)


def _unit_text() -> str:
    result=subprocess.run(["systemctl","cat",UNIT],check=True,capture_output=True,text=True,timeout=10)
    return result.stdout


def build_plan(args: argparse.Namespace) -> dict[str, object]:
    try: state=require_clean_repository(REPO_ROOT)
    except RepositoryStateError as exc: raise RemediationError(exc.classification,exc.details) from exc
    if state.head!=args.expected_repository_head: raise RemediationError("REPOSITORY_HEAD_MISMATCH")
    if socket.gethostname()!=args.expected_host: raise RemediationError("HOST_IDENTITY_MISMATCH")
    canonical_hash=_regular_hash(CANONICAL,"CANONICAL_IDENTITY_INVALID")
    if canonical_hash!=args.expected_canonical_sha256: raise RemediationError("CANONICAL_IDENTITY_HASH_MISMATCH")
    artifact_hash=_regular_hash(args.preserved_backup,"PRESERVED_BACKUP_INVALID")
    evidence_hash=_regular_hash(args.recovery_evidence,"RECOVERY_EVIDENCE_INVALID")
    if artifact_hash!=args.expected_preserved_backup_sha256: raise RemediationError("PRESERVED_BACKUP_HASH_MISMATCH")
    if evidence_hash!=args.expected_recovery_evidence_sha256: raise RemediationError("RECOVERY_EVIDENCE_HASH_MISMATCH")
    try:
        owner=resolve_recovery_owner()
        recovery_root=default_recovery_root(REPO_ROOT)
        validate_recovery_root(recovery_root,repository_root=REPO_ROOT,owner=owner,phase="systemd_remediation_plan")
        recovery_binding=validate_preserved_recovery_pair(
            evidence_path=args.recovery_evidence,backup_path=args.preserved_backup,root=recovery_root,
            repository_root=REPO_ROOT,owner=owner,expected_evidence_sha256=evidence_hash,
            expected_backup_sha256=artifact_hash)
    except RecoveryStorageError as exc:
        raise RemediationError(exc.code,exc.details) from exc
    source_hash=_regular_hash(SOURCE,"CORRECTED_SOURCE_MISSING")
    override_hash=_regular_hash(OVERRIDE,"API_OVERRIDE_MISSING")
    obsolete_hash=_regular_hash(OBSOLETE,"OBSOLETE_DROPIN_MISSING")
    unit_text=_unit_text()
    return {"contract_version":1,"operation":"remediate_runtime_identity_systemd","host":args.expected_host,
            "repository_head":state.head,"canonical_identity_file":str(CANONICAL),"canonical_identity_sha256":canonical_hash,
            "unit":UNIT,"current_effective_unit_sha256":hashlib.sha256(unit_text.encode()).hexdigest(),
            "current_canonical_source_effective":canonical_source_effective(unit_text),
            "override":{"path":str(OVERRIDE),"sha256":override_hash},
            "obsolete_dropin":{"path":str(OBSOLETE),"sha256":obsolete_hash,"action":"retire_after_verified_replacement"},
            "corrected_dropin":{"source":str(SOURCE),"source_sha256":source_hash,"target":str(TARGET),
                                "owner":"root","group":"root","mode":"0644"},
            "recovery_contract":{"root":str(recovery_root),"owner":owner.as_plan(),"directory_mode":f"{DIRECTORY_MODE:04o}","file_mode":f"{FILE_MODE:04o}"},
            "preserved_recovery_binding":recovery_binding,
            "preserved_provisioning_backup":{"path":str(args.preserved_backup.absolute()),"sha256":artifact_hash},
            "preserved_recovery_evidence":{"path":str(args.recovery_evidence.absolute()),"sha256":evidence_hash},
            "actions":["install_corrected_dropin","verify_hash_and_metadata","systemctl daemon-reload",
                       "verify_canonical_source_effective_after_all_resets","retire_obsolete_dropin",
                       "systemctl daemon-reload","verify_final_canonical_source_effective"],
            "restarts":[],"timer_restarts":[],"docker_recreation":False}


def attestation(plan: dict[str,object]) -> str:
    digest=hashlib.sha256(canonical_json(plan).encode()).hexdigest()
    return f"REMEDIATE_RUNTIME_IDENTITY_SYSTEMD host={plan['host']} head={plan['repository_head']} plan_sha256={digest} no_restart=true"


def execution_command(args: argparse.Namespace, required: str) -> str:
    return shlex.join(["sudo",str(REPO_ROOT/".venv/bin/python"),"-I",str(REPO_ROOT/"ops/remediate_runtime_identity_systemd.py"),
        "--expected-host",args.expected_host,"--expected-repository-head",args.expected_repository_head,
        "--expected-canonical-sha256",args.expected_canonical_sha256,"--preserved-backup",str(args.preserved_backup),
        "--expected-preserved-backup-sha256",args.expected_preserved_backup_sha256,"--recovery-evidence",str(args.recovery_evidence),
        "--expected-recovery-evidence-sha256",args.expected_recovery_evidence_sha256,"--execute","--attestation",required])


def _atomic_install(path:Path,raw:bytes)->None:
    if path.is_symlink() or (path.exists() and not path.is_file()): raise RemediationError("TARGET_UNSAFE")
    path.parent.mkdir(parents=True,exist_ok=True); fd,tmp=tempfile.mkstemp(prefix=f".{path.name}.",dir=path.parent)
    try:
        os.fchmod(fd,0o644); os.fchown(fd,0,0); os.write(fd,raw); os.fsync(fd); os.close(fd); fd=-1
        os.replace(tmp,path); d=os.open(path.parent,os.O_RDONLY)
        try: os.fsync(d)
        finally: os.close(d)
    finally:
        if fd>=0: os.close(fd)
        Path(tmp).unlink(missing_ok=True)


def execute(args: argparse.Namespace,plan:dict[str,object])->dict[str,object]:
    if os.geteuid()!=0: raise RemediationError("ROOT_REQUIRED")
    raw=SOURCE.read_bytes(); plan_sha=hashlib.sha256(canonical_json(plan).encode()).hexdigest()
    try:
        owner=resolve_recovery_owner()
        if plan.get("recovery_contract")!={"root":str(default_recovery_root(REPO_ROOT)),"owner":owner.as_plan(),
                                           "directory_mode":"0700","file_mode":"0600"}:
            raise RemediationError("RECOVERY_CONTRACT_PLAN_MISMATCH")
        store=RecoveryStore(root=default_recovery_root(REPO_ROOT),repository_root=REPO_ROOT,operation="remediate_systemd",
            plan_sha256=plan_sha,repository_head=str(plan["repository_head"]),checkpoint=args.recovery_evidence,
            checkpoint_sha256=args.expected_recovery_evidence_sha256,owner=owner)
        store.prepare(); store.preserve(TARGET,logical_path=TARGET); store.preserve(OBSOLETE,logical_path=OBSOLETE)
        store.write_evidence(state="prepared")
    except RecoveryStorageError as exc: raise RemediationError(exc.code,exc.details) from exc
    _atomic_install(TARGET,raw)
    if sha256_file(TARGET)!=plan["corrected_dropin"]["source_sha256"] or stat.S_IMODE(TARGET.stat().st_mode)!=0o644 or TARGET.stat().st_uid!=0 or TARGET.stat().st_gid!=0:
        raise RemediationError("CORRECTED_DROPIN_VERIFICATION_FAILED")
    subprocess.run(["systemctl","daemon-reload"],check=True)
    if not canonical_source_effective(_unit_text()): raise RemediationError("CANONICAL_SOURCE_NOT_EFFECTIVE")
    if sha256_file(OBSOLETE)!=plan["obsolete_dropin"]["sha256"]: raise RemediationError("OBSOLETE_DROPIN_CHANGED")
    OBSOLETE.unlink(); d=os.open(OBSOLETE.parent,os.O_RDONLY)
    try: os.fsync(d)
    finally: os.close(d)
    subprocess.run(["systemctl","daemon-reload"],check=True)
    if not canonical_source_effective(_unit_text()): raise RemediationError("FINAL_CANONICAL_SOURCE_NOT_EFFECTIVE")
    recovery=store.write_evidence(state="validated")
    return {"writes_performed":True,"reloads_performed":["systemctl daemon-reload","systemctl daemon-reload"],
            "restarts_performed":[],"docker_recreation":False,"recovery":recovery}


def main(argv:list[str]|None=None)->int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-host",required=True); parser.add_argument("--expected-repository-head",required=True)
    parser.add_argument("--expected-canonical-sha256",required=True); parser.add_argument("--preserved-backup",type=Path,required=True)
    parser.add_argument("--expected-preserved-backup-sha256",required=True); parser.add_argument("--recovery-evidence",type=Path,required=True)
    parser.add_argument("--expected-recovery-evidence-sha256",required=True); parser.add_argument("--execute",action="store_true")
    parser.add_argument("--attestation"); args=parser.parse_args(argv)
    plan=build_plan(args); digest=hashlib.sha256(canonical_json(plan).encode()).hexdigest(); required=attestation(plan)
    output={"mode":"execute" if args.execute else "dry_run","writes_performed":False,"plan":plan,"plan_sha256":digest,
            "required_attestation":required,"execution_command":execution_command(args,required)}
    if not args.execute: print(json.dumps(output,indent=2,sort_keys=True)); return 0
    if args.attestation!=required: raise RemediationError("ATTESTATION_MISMATCH")
    output["execution"]=execute(args,plan); output["writes_performed"]=True
    print(json.dumps(output,indent=2,sort_keys=True)); return 0


if __name__=="__main__":
    try: raise SystemExit(main())
    except RemediationError as exc:
        print(json.dumps({"classification":exc.code,"writes_performed":False,**exc.details},indent=2,sort_keys=True)); raise SystemExit(2)
