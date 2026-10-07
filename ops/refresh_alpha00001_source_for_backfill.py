#!/usr/bin/env python3
"""Plan or execute the ALPHA-only committed source refresh for backfill review."""
from __future__ import annotations

import argparse
import getpass
import json
import socket
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api.client import LogPlatformClient, run_context  # noqa: E402
from jobs.common import environment_identity  # noqa: E402
from jobs.reports.stage3 import job_stage3  # noqa: E402
from jobs.reports.workflow_b import orchestrator  # noqa: E402


EXPECTED_REPO = Path("/opt/log-platform")
EXPECTED_HOST = "logplatform-ThinkPad-T14s-Gen-1"
EXPECTED_USER = "logplatform"
EXPECTED_PLATFORM_ID = "52517750-7438-4558-8490-2736ae4cc629"
EXPECTED_CLIENT_ID = "9536f715-2fd0-4ffd-86ed-ba06f5490c5e"
EXPECTED_CLIENT_DB = "alpha_main"
EXPECTED_CLIENT_DB_ID = "5879ec53-e3f6-4a46-89cf-eaae9f57b27e"
EXPECTED_CLIENT_CODE = "ALPHA00001"
EXPECTED_REPORT = "Alpha_GPS_Baza_LOG"
EXPECTED_DESTINATION = "telematics_reports.Alpha_GPS_Baza_LOG"
EXACT_ATTESTATION = (
    "ALPHA00001/Alpha_GPS_Baza_LOG/"
    "telematics_reports.Alpha_GPS_Baza_LOG/postprocessor=dry_run"
)
SOURCE = "ops.refresh_alpha00001_source_for_backfill"

EXIT_OK = 0
EXIT_INVALID_INTENT = 2
EXIT_IDENTITY_NOT_VERIFIED = 3
EXIT_BLOCKED = 4
EXIT_POSTPROCESSOR_NOT_READY = 5


class SourceRefreshOperationError(RuntimeError):
    def __init__(self, result: orchestrator.AlphaSourceRefreshResult):
        self.result = result
        super().__init__(result.outcome.value)


def _load_dotenv() -> None:
    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        from dotenv import load_dotenv
        load_dotenv(env_file, override=False)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Plan the ALPHA00001 source refresh by default. "
            "--execute-source-refresh commits only the exact source and runs "
            "Dysponent enrichment in dry-run mode."
        )
    )
    parser.add_argument("--execute-source-refresh", action="store_true")
    parser.add_argument(
        "--attestation",
        help="Exact ALPHA/report/destination/dry-run execution attestation.",
    )
    return parser


def _configured_identity_plan() -> dict[str, object]:
    runtime = environment_identity.load_runtime_identity()
    if runtime.platform_identity_id != EXPECTED_PLATFORM_ID:
        raise environment_identity.EnvironmentIdentityError(
            "DB_MARKER_IDENTITY_MISMATCH",
            "configured platform identity is not the approved logdb identity",
        )
    return {
        "operation": "alpha00001_source_refresh_for_backfill",
        "mode": "plan",
        "writes_enabled": False,
        "imap_connection": False,
        "source_table_write": False,
        "client_trips_write": False,
        "client_code": EXPECTED_CLIENT_CODE,
        "report_type": EXPECTED_REPORT,
        "destination": EXPECTED_DESTINATION,
        "postprocessor": "alpha00001_dysponent_id_enrichment",
        "postprocessor_execution_mode": "dry_run",
        "configured_platform_identity_verified": True,
        "required_execute_flag": "--execute-source-refresh",
        "required_attestation": EXACT_ATTESTATION,
    }


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, check=True,
        capture_output=True, text=True, timeout=10,
    ).stdout.strip()


def _require_runtime_checkout() -> None:
    if Path.cwd().resolve() != EXPECTED_REPO.resolve():
        raise RuntimeError("runtime repository path is not the approved checkout")
    if socket.gethostname() != EXPECTED_HOST or getpass.getuser() != EXPECTED_USER:
        raise RuntimeError("runtime host/user identity is not approved")
    if _git("branch", "--show-current") != "main":
        raise RuntimeError("runtime branch must be main")
    if _git("status", "--short", "--untracked-files=all"):
        raise RuntimeError("runtime worktree must be clean")
    if _git("rev-parse", "HEAD") != _git("rev-parse", "origin/main"):
        raise RuntimeError("runtime HEAD must equal the deployed origin/main ref")


def _attest_execute_environment(attestation: str | None):
    if attestation != EXACT_ATTESTATION:
        raise ValueError("exact --attestation is required for --execute-source-refresh")
    _require_runtime_checkout()
    runtime = environment_identity.load_runtime_identity()
    if runtime.platform_identity_id != EXPECTED_PLATFORM_ID or runtime.postgres_db != "logdb":
        raise environment_identity.EnvironmentIdentityError(
            "DB_MARKER_IDENTITY_MISMATCH", "platform identity is not approved"
        )
    platform_conn = orchestrator._platform_pg_conn()
    try:
        platform = environment_identity.attest_platform_identity(platform_conn, runtime)
        with platform_conn.cursor() as cur:
            config = job_stage3._load_client_account_by_code(cur, EXPECTED_CLIENT_CODE)
        if (
            config.client_code != EXPECTED_CLIENT_CODE
            or config.client_id != EXPECTED_CLIENT_ID
            or config.client_db_name != EXPECTED_CLIENT_DB
            or config.client_db_identity_id != EXPECTED_CLIENT_DB_ID
        ):
            raise environment_identity.EnvironmentIdentityError(
                "CLIENT_EXPECTED_IDENTITY_MISSING",
                "ALPHA control-plane identity is not the approved target",
            )
        expectation = environment_identity.ClientIdentityExpectation(
            client_code=config.client_code,
            environment=config.client_db_environment,
            database_identity_id=config.client_db_identity_id,
            database_name=config.client_db_name,
            database_user=config.client_db_user,
        )
        with job_stage3._client_business_pg_conn(config) as client_conn:
            client_identity = environment_identity.attest_client_identity(
                client_conn, runtime, expectation
            )
        with platform_conn.cursor() as cur:
            cur.execute(
                """
                SELECT EXISTS (
                  SELECT 1 FROM pg_stat_activity
                  WHERE pid <> pg_backend_pid() AND state <> 'idle'
                    AND (query ILIKE '%apply_client_business_migrations%'
                         OR query ILIKE '%044_eco_email_fail_closed_idempotency%')
                ) AS migration_active
                """
            )
            migration_active = bool(cur.fetchone()["migration_active"])
        platform_conn.rollback()
        if migration_active:
            raise RuntimeError("a client-business migration appears to be active")
        if not orchestrator._try_global_lock(platform_conn):
            raise RuntimeError("a conflicting Workflow B operation is active")
        orchestrator._release_global_lock(platform_conn)
        return runtime, platform.context(), client_identity.context()
    finally:
        platform_conn.close()


def _execute_source_refresh(attestation: str | None) -> dict[str, object]:
    runtime, platform_identity, client_identity = _attest_execute_environment(attestation)
    params = {
        "operation": "alpha00001_source_refresh_for_backfill",
        "client_code": EXPECTED_CLIENT_CODE,
        "report_type": EXPECTED_REPORT,
        "destination": EXPECTED_DESTINATION,
        "postprocessor_execution_mode": "dry_run",
        "target_rows_modified_required": 0,
    }
    client = LogPlatformClient.from_env()
    with run_context(
        client, trigger="MANUAL", source=SOURCE,
        actor=getpass.getuser(), params=params,
    ) as run_id:
        result = orchestrator.run_alpha_source_refresh_batch(client, run_id)
        payload = {
            **result.to_dict(),
            "workflow_b_run_id": run_id,
            "environment": runtime.environment,
            "platform_identity": platform_identity,
            "client_identity": client_identity,
        }
        client.log(
            "INFO", "SCRIPT", SOURCE, "ALPHA source refresh operation finished",
            run_id=run_id, context=payload,
        )
        if result.outcome in {
            orchestrator.AlphaSourceRefreshOutcome.SOURCE_LOADED_POSTPROCESSOR_DRY_RUN_FAILED,
            orchestrator.AlphaSourceRefreshOutcome.BLOCKED_TARGET_WRITE_DETECTED,
            orchestrator.AlphaSourceRefreshOutcome.SKIPPED_LOCKED,
        }:
            raise SourceRefreshOperationError(result)
        return payload


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    try:
        args = _parser().parse_args(argv)
        if not args.execute_source_refresh:
            if args.attestation:
                raise ValueError("--attestation is valid only with --execute-source-refresh")
            print(json.dumps(_configured_identity_plan(), sort_keys=True))
            return EXIT_OK
        payload = _execute_source_refresh(args.attestation)
        print(json.dumps(payload, sort_keys=True, default=str))
        return EXIT_OK
    except SourceRefreshOperationError as exc:
        print(json.dumps(exc.result.to_dict(), sort_keys=True, default=str), file=sys.stderr)
        return (
            EXIT_BLOCKED
            if exc.result.outcome is orchestrator.AlphaSourceRefreshOutcome.BLOCKED_TARGET_WRITE_DETECTED
            else EXIT_POSTPROCESSOR_NOT_READY
        )
    except environment_identity.EnvironmentIdentityError as exc:
        print(json.dumps({"status": "ERROR", "error_code": "ENVIRONMENT_IDENTITY_NOT_VERIFIED",
                          "message": str(exc)}, sort_keys=True), file=sys.stderr)
        return EXIT_IDENTITY_NOT_VERIFIED
    except ValueError as exc:
        print(json.dumps({"status": "ERROR", "error_code": "INVALID_INTENT",
                          "message": str(exc)}, sort_keys=True), file=sys.stderr)
        return EXIT_INVALID_INTENT
    except Exception as exc:
        print(json.dumps({"status": "ERROR", "error_code": type(exc).__name__,
                          "message": str(exc)}, sort_keys=True), file=sys.stderr)
        return EXIT_BLOCKED


if __name__ == "__main__":
    raise SystemExit(main())
