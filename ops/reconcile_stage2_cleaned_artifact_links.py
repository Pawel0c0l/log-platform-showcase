#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import boto3
import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

from jobs.common.environment_identity import attest_platform_identity, load_runtime_identity
from jobs.reports.stage2.link_reconciliation import canonical_payload, execute_reviewed_plan, reconcile_read_only, write_plan


def _connection(runtime):
    return psycopg.connect(host=runtime.postgres_host, port=runtime.postgres_port, dbname=runtime.postgres_db, user=runtime.postgres_user, password=os.getenv("POSTGRES_PASSWORD", ""), row_factory=dict_row)


def _s3():
    secure = os.getenv("MINIO_SECURE", "0") in {"1", "true", "True"}
    endpoint = os.getenv("MINIO_ENDPOINT", "127.0.0.1:9000")
    if endpoint == "minio:9000":
        endpoint = "127.0.0.1:9000"
    return boto3.client("s3", endpoint_url=("https://" if secure else "http://") + endpoint, aws_access_key_id=os.getenv("MINIO_ROOT_USER", ""), aws_secret_access_key=os.getenv("MINIO_ROOT_PASSWORD", ""), region_name="us-east-1")


def _active_workflow_b_processes() -> bool:
    result = subprocess.run(["pgrep", "-af", "ops/runner.py.*(fetch_reports|stage2|stage3|workflow_b)|reconcile_stage2_cleaned_artifact_links.py.*--execute"], capture_output=True, text=True, check=False)
    lines = [line for line in result.stdout.splitlines() if "pgrep -af" not in line]
    return bool(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Manual Workflow B Stage 2 cleaned-link reconciliation (dry-run by default)")
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--expected-digest")
    parser.add_argument("--expect-count", type=int)
    args = parser.parse_args()
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    runtime = load_runtime_identity()
    conn = _connection(runtime)
    try:
        identity = attest_platform_identity(conn, runtime)
        s3 = _s3()
        bucket = os.getenv("MINIO_BUCKET", "artifacts")
        if args.execute:
            if not args.plan or not args.expected_digest or args.expect_count is None:
                raise SystemExit("--execute requires --plan, --expected-digest and --expect-count")
            if _active_workflow_b_processes():
                raise SystemExit("Workflow B processing is active")
            document = json.loads(args.plan.read_text())
            reviewed_commit = str((document.get("plan_payload") or {}).get("repository_commit") or "")
            ancestry = subprocess.run(["git", "merge-base", "--is-ancestor", reviewed_commit, "HEAD"], check=False)
            if not reviewed_commit or ancestry.returncode != 0:
                raise SystemExit("reviewed plan implementation commit is not contained in current HEAD")
            affected = execute_reviewed_plan(conn, s3, bucket, document, expected_digest=args.expected_digest, expect_count=args.expect_count, environment=identity.environment, database_name=identity.database_name, platform_identity_id=identity.database_identity_id)
            print(json.dumps({"mode": "execute", "affected_rows": affected}, sort_keys=True))
            return 0
        conn.execute("SET default_transaction_read_only = on")
        result = reconcile_read_only(conn, s3, bucket)
        commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        payload = canonical_payload(result, environment=identity.environment, database_name=identity.database_name, platform_identity_id=identity.database_identity_id, repository_commit=commit)
        plan_path = args.plan or Path("/tmp") / f"workflow_b_stage2_cleaned_links_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
        digest = write_plan(plan_path, payload, created_at=datetime.now(timezone.utc).isoformat())
        result.plan_path = str(plan_path)
        result.plan_digest = digest
        summary = result.to_dict()
        summary.pop("entries", None)
        print(json.dumps(summary, sort_keys=True))
        return 0 if result.blocked_count == 0 else 2
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
