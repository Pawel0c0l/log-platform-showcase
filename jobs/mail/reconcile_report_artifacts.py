from __future__ import annotations

from jobs.mail.fetch_reports import _pg_conn
from jobs.mail.stage1_artifact_sync import reconcile_stage1_artifacts_batch


JOB_SOURCE = "jobs.mail.reconcile_report_artifacts"


def _execute_requested(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError("execute must be a boolean")


def run(client, run_id: str, params: dict):
    params = params or {}
    execute = _execute_requested(params.get("execute", False))
    result = reconcile_stage1_artifacts_batch(
        client,
        run_id,
        connection_factory=_pg_conn,
        raw_file_ids=[str(value) for value in (params.get("raw_file_ids") or [])] or None,
        limit=int(params.get("limit", 50)),
        roles=params.get("roles"),
        dry_run=not execute,
    )
    client.log(
        "INFO", "SCRIPT", JOB_SOURCE, "Stage 1 artifact reconciliation finished",
        run_id=run_id, context=result.to_dict(),
    )
    return result
