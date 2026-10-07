#!/usr/bin/env python3
"""Dedicated Database Explorer async export worker.

Runs outside Uvicorn. It claims at most one queued export job, streams rows from
client DBs into a local temp file, uploads only complete files, and expires
completed exports after the global retention window.
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import tempfile
import threading
import time
from datetime import timedelta
from pathlib import Path
from api import main as api_main

WORKER_ADVISORY_LOCK = 650260526001
DEFAULT_POLL_SECONDS = 30
DEFAULT_CLEANUP_INTERVAL_SECONDS = 60 * 60
MAX_ATTEMPTS = 3

_stop_requested = False
_stop_event = threading.Event()


def _configure_logging() -> logging.Logger:
    logger = logging.getLogger("database_export_worker")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


LOGGER = _configure_logging()


_STAGE_FAILURE_MESSAGES = {
    "authorization": "authorization_failed",
    "query": "query_failed",
    "csv_generation": "csv_generation_failed",
    "xlsx_generation": "xlsx_generation_failed",
    "upload": "upload_failed",
    "publication": "publication_failed",
    "cleanup": "cleanup_failed",
}


def _safe_failure_message(stage: str | None) -> str:
    return _STAGE_FAILURE_MESSAGES.get(str(stage or ""), "unexpected_operation_failed")


def _safe_error_category(stage: str | None) -> str:
    return _safe_failure_message(stage)


def _log_value(value) -> str:
    text = str(value)
    if text and all(ch.isalnum() or ch in "_.:@+-" for ch in text):
        return text
    return json.dumps(text, ensure_ascii=True)


def _job_attempt(job: dict | None) -> int | None:
    if not job:
        return None
    try:
        if job.get("attempt_count") is None:
            return None
        return int(job.get("attempt_count"))
    except Exception:
        return None


def _log_worker_event(event: str, *, job: dict | None = None, stage: str | None = None, failed: bool = False) -> None:
    fields = {"event": event}
    if job is not None:
        job_id = job.get("job_id")
        attempt = _job_attempt(job)
        if job_id:
            fields["job_id"] = str(job_id)
        if attempt is not None:
            fields["attempt"] = attempt
    if stage:
        fields["stage"] = stage
    if failed:
        fields["error_category"] = _safe_error_category(stage)
        fields["message"] = _safe_failure_message(stage)
    LOGGER.info(" ".join(f"{key}={_log_value(value)}" for key, value in fields.items()))


class SafeExportFailure(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.safe_message = message


class StaleClaim(RuntimeError):
    pass


def _handle_stop(_signum, _frame) -> None:
    global _stop_requested
    _stop_requested = True
    _stop_event.set()


def _stop_requested_now() -> bool:
    return _stop_requested or _stop_event.is_set()


def _reset_stop_request_for_tests() -> None:
    global _stop_requested
    _stop_requested = False
    _stop_event.clear()


def _job_select_sql() -> str:
    system_folder_col = "system_folder_id" if api_main._database_export_system_folder_schema_available() else "NULL::UUID AS system_folder_id"
    return f"""
        SELECT job_id, requested_by_user_id, dataset_id, requested_format, request_snapshot_json,
               status, queued_at, started_at, completed_at, expires_at, lease_expires_at,
               claim_token, attempt_count, row_count, artifact_id, object_key, attempt_object_key,
               safe_error_code, safe_error_message, created_at, updated_at, {system_folder_col}
        FROM database_export_jobs
    """


def _claim_token(job: dict) -> str:
    return str(job.get("claim_token") or "")


def _attempt_object_key(job: dict) -> str:
    return str(job.get("attempt_object_key") or "")


# How long a delete that observed no object must wait before it may be recorded
# as terminally clean.
#
# The window exists because "the object is not there" is ambiguous while the
# producing attempt may still be physically uploading it: the delete would
# succeed against nothing, the ledger would go terminal, and a late upload would
# then leave an object nothing will ever collect. The bound is the export lease,
# because that is already the liveness bound this system trusts everywhere else
# — publication requires a live lease, and stale recovery treats an expired one
# as proof the producer is no longer authoritative. A producer that begins
# writing after its lease has expired cannot publish what it writes, and by then
# the settle window has not yet closed either, because the window is measured
# from the moment cleanup was requested.
ATTEMPT_CLEANUP_SETTLE_SECONDS = api_main.PORTAL_DATABASE_EXPORT_LEASE_SECONDS

# Outcomes of one delete attempt.
CLEANUP_REMOVED = "removed"      # an object existed and is gone -> terminal now
CLEANUP_ABSENT = "absent"        # nothing observed -> terminal only once settled
CLEANUP_FAILED = "failed"        # the delete itself failed -> always retryable


def _object_exists(object_key: str) -> bool | None:
    """Whether an object is present: True, False, or None for "cannot tell".

    Only a definite 404 counts as absence. A transient or permission error is
    `None`, which the caller must treat exactly like absence for *retrying* and
    never like absence for *finalizing* — mistaking an unreachable store for an
    empty one is how an orphan becomes permanent.
    """
    probe = getattr(api_main.s3, "head_object", None)
    if not callable(probe):
        return None
    try:
        probe(Bucket=api_main.MINIO_BUCKET, Key=object_key)
        return True
    except Exception as exc:  # noqa: BLE001 - normalized to three outcomes
        response = getattr(exc, "response", None) or {}
        code = str(((response.get("Error") or {}).get("Code")) or "")
        status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        if code in {"404", "NoSuchKey", "NotFound"} or status == 404:
            return False
        return None


def _delete_object_key(object_key: str | None) -> str:
    """Delete one attempt object and report what the delete actually found.

    The distinction is the whole point: a delete that removed a real object
    proves the attempt's object is gone, while a delete that found nothing
    proves only that nothing was there *at that instant*.
    """
    key = str(object_key or "")
    if not key:
        # There is no object to own, so there is nothing that can be recreated.
        return CLEANUP_REMOVED
    existed = _object_exists(key)
    try:
        api_main.s3.delete_object(Bucket=api_main.MINIO_BUCKET, Key=key)
    except Exception:
        return CLEANUP_FAILED
    return CLEANUP_REMOVED if existed is True else CLEANUP_ABSENT


def _upsert_attempt_ledger(cur, *, job_id, claim_token, object_key, state: str) -> dict | None:
    token = str(claim_token or "")
    key = str(object_key or "")
    if not job_id or not token or not key:
        return None
    cur.execute(
        """
        INSERT INTO database_export_attempt_objects
            (job_id, claim_token, object_key, state, cleanup_requested_at)
        VALUES (%s, %s, %s, %s, CASE WHEN %s = 'cleanup_pending' THEN now() ELSE NULL END)
        ON CONFLICT (job_id, claim_token) DO UPDATE
        SET object_key = EXCLUDED.object_key,
            state = EXCLUDED.state,
            cleanup_requested_at = CASE
              WHEN EXCLUDED.state = 'cleanup_pending'
                THEN COALESCE(database_export_attempt_objects.cleanup_requested_at, now())
              ELSE database_export_attempt_objects.cleanup_requested_at
            END,
            updated_at = now()
        RETURNING attempt_object_id, job_id, claim_token, object_key, state,
                  cleanup_requested_at, last_cleanup_attempt_at, last_cleanup_success_at,
                  created_at, updated_at
        """,
        (job_id, token, key, state, state),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def _mark_attempt_cleanup_pending(job: dict) -> dict | None:
    claim_token = _claim_token(job)
    attempt_key = _attempt_object_key(job)
    if not claim_token or not attempt_key:
        return None
    with api_main.db_conn() as conn:
        with conn.cursor() as cur:
            row = _upsert_attempt_ledger(
                cur,
                job_id=job.get("job_id"),
                claim_token=claim_token,
                object_key=attempt_key,
                state="cleanup_pending",
            )
        conn.commit()
    return row


def _record_attempt_cleanup_result(attempt_row: dict, *, outcome: str) -> bool:
    """Record one delete attempt and decide whether cleanup is now terminal.

    Terminal on `removed`. On `absent` the row stays selectable until the settle
    window measured from `cleanup_requested_at` has closed, so a delete that ran
    while the producing attempt was still uploading is retried and the late
    object is collected on a subsequent sweep. `failed` never finalizes.

    Returns whether the row reached terminal cleanup success.
    """
    attempt_object_id = attempt_row.get("attempt_object_id")
    if not attempt_object_id:
        return False
    with api_main.db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE database_export_attempt_objects
                SET last_cleanup_attempt_at = now(),
                    last_cleanup_success_at = CASE
                      WHEN %s THEN now()
                      WHEN %s
                        AND COALESCE(cleanup_requested_at, created_at)
                            + (%s::text || ' seconds')::interval <= now()
                        THEN now()
                      ELSE last_cleanup_success_at
                    END,
                    updated_at = now()
                WHERE attempt_object_id = %s
                  AND state = 'cleanup_pending'
                RETURNING (last_cleanup_success_at IS NOT NULL) AS finalized
                """,
                (
                    outcome == CLEANUP_REMOVED,
                    outcome == CLEANUP_ABSENT,
                    ATTEMPT_CLEANUP_SETTLE_SECONDS,
                    attempt_object_id,
                ),
            )
            row = cur.fetchone()
        conn.commit()
    return bool((row or {}).get("finalized"))


def _cleanup_attempt_object_row(attempt_row: dict) -> bool:
    outcome = _delete_object_key(str(attempt_row.get("object_key") or ""))
    finalized = _record_attempt_cleanup_result(attempt_row, outcome=outcome)
    if outcome == CLEANUP_FAILED:
        _log_worker_event(
            "database_export_cleanup_failed",
            job={"job_id": attempt_row.get("job_id")},
            stage="cleanup",
            failed=True,
        )
    return finalized


def cleanup_unpublished_attempt_objects(limit: int = 100) -> int:
    with api_main.db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT attempt_object_id, job_id, claim_token, object_key, state,
                       cleanup_requested_at, last_cleanup_attempt_at, last_cleanup_success_at,
                       created_at, updated_at
                FROM database_export_attempt_objects
                WHERE state = 'cleanup_pending'
                  AND last_cleanup_success_at IS NULL
                ORDER BY COALESCE(last_cleanup_attempt_at, cleanup_requested_at, created_at) ASC,
                         attempt_object_id ASC
                LIMIT %s
                """,
                (limit,),
            )
            attempts = [dict(row) for row in cur]
    cleaned = 0
    for attempt in attempts:
        if _stop_requested_now():
            break
        if _cleanup_attempt_object_row(attempt):
            cleaned += 1
    return cleaned


def recover_stale_running_jobs(limit: int = 100) -> dict[str, int]:
    requeued = 0
    failed = 0
    with api_main.db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                _job_select_sql()
                + """
                  WHERE status = 'running'
                    AND lease_expires_at <= now()
                    AND artifact_id IS NULL
                  ORDER BY lease_expires_at ASC, job_id ASC
                  LIMIT %s
                """,
                (limit,),
            )
            jobs = [dict(row) for row in cur]
    for job in jobs:
        if _stop_requested_now():
            break
        claim_token = _claim_token(job)
        attempt_key = _attempt_object_key(job)
        cleanup_row = None
        with api_main.db_conn() as conn:
            with conn.cursor() as cur:
                if int(job.get("attempt_count") or 0) < MAX_ATTEMPTS:
                    cur.execute(
                        """
                        UPDATE database_export_jobs
                        SET status = 'queued', lease_expires_at = NULL, claim_token = NULL,
                            attempt_object_key = NULL, updated_at = now(),
                            safe_error_code = NULL, safe_error_message = NULL
                        WHERE job_id = %s
                          AND status = 'running'
                          AND claim_token IS NOT DISTINCT FROM %s
                          AND lease_expires_at <= now()
                          AND artifact_id IS NULL
                        """,
                        (job.get("job_id"), claim_token or None),
                    )
                    updated = cur.rowcount
                    if updated == 1:
                        cleanup_row = _upsert_attempt_ledger(
                            cur,
                            job_id=job.get("job_id"),
                            claim_token=claim_token,
                            object_key=attempt_key,
                            state="cleanup_pending",
                        )
                        requeued += 1
                else:
                    cur.execute(
                        """
                        UPDATE database_export_jobs
                        SET status = 'failed', lease_expires_at = NULL, claim_token = NULL,
                            attempt_object_key = NULL, completed_at = now(), updated_at = now(),
                            safe_error_code = 'WORKER_INTERRUPTED',
                            safe_error_message = 'The export worker stopped before completing this job. Please queue a new export.'
                        WHERE job_id = %s
                          AND status = 'running'
                          AND claim_token IS NOT DISTINCT FROM %s
                          AND lease_expires_at <= now()
                          AND artifact_id IS NULL
                        """,
                        (job.get("job_id"), claim_token or None),
                    )
                    updated = cur.rowcount
                    if updated == 1:
                        cleanup_row = _upsert_attempt_ledger(
                            cur,
                            job_id=job.get("job_id"),
                            claim_token=claim_token,
                            object_key=attempt_key,
                            state="cleanup_pending",
                        )
                        failed += 1
            conn.commit()
        if cleanup_row:
            _cleanup_attempt_object_row(cleanup_row)
    return {"requeued": requeued, "failed": failed}


def claim_one_job() -> dict | None:
    if _stop_requested_now():
        return None
    claim_token = api_main.uuid.uuid4()
    with api_main.db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_xact_lock(%s) AS locked", (WORKER_ADVISORY_LOCK,))
            row = cur.fetchone()
            if not row or not row.get("locked"):
                conn.rollback()
                return None
            if _stop_requested_now():
                conn.rollback()
                return None
            cur.execute(
                """
                WITH candidate AS (
                  SELECT job_id
                  FROM database_export_jobs
                  WHERE status = 'queued'
                    AND NOT EXISTS (
                      SELECT 1 FROM database_export_jobs running
                      WHERE running.status = 'running'
                        AND running.lease_expires_at > now()
                    )
                  ORDER BY queued_at ASC, job_id ASC
                  LIMIT 1
                  FOR UPDATE SKIP LOCKED
                )
                UPDATE database_export_jobs job
                SET status = 'running',
                    started_at = COALESCE(job.started_at, now()),
                    lease_expires_at = now() + (%s::text || ' seconds')::interval,
                    claim_token = %s,
                    attempt_object_key = 'database_explorer/exports/attempts/export_job_id='
                        || job.job_id::text
                        || '/claim=' || %s::text
                        || '/database_export__' || left(job.job_id::text, 8)
                        || '__' || left(%s::text, 8)
                        || '__result.' || CASE WHEN job.requested_format = 'xlsx' THEN 'xlsx' ELSE 'csv' END,
                    attempt_count = job.attempt_count + 1,
                    safe_error_code = NULL,
                    safe_error_message = NULL,
                    updated_at = now()
                FROM candidate
                WHERE job.job_id = candidate.job_id
                RETURNING job.job_id, job.requested_by_user_id, job.dataset_id, job.requested_format,
                          job.request_snapshot_json, job.status, job.queued_at, job.started_at,
                          job.completed_at, job.expires_at, job.lease_expires_at, job.claim_token,
                          job.attempt_count, job.row_count, job.artifact_id, job.object_key,
                          job.attempt_object_key, job.safe_error_code, job.safe_error_message,
                          job.created_at, job.updated_at
                """,
                (api_main.PORTAL_DATABASE_EXPORT_LEASE_SECONDS, claim_token, claim_token, claim_token),
            )
            job = cur.fetchone()
            if job:
                _upsert_attempt_ledger(
                    cur,
                    job_id=job.get("job_id"),
                    claim_token=job.get("claim_token"),
                    object_key=job.get("attempt_object_key"),
                    state="active",
                )
        conn.commit()
    return dict(job) if job else None


def refresh_lease(job: dict, row_count: int | None = None) -> bool:
    claim_token = _claim_token(job)
    if not claim_token:
        return False
    with api_main.db_conn() as conn:
        with conn.cursor() as cur:
            if row_count is None:
                cur.execute(
                    """
                    UPDATE database_export_jobs
                    SET lease_expires_at = now() + (%s::text || ' seconds')::interval,
                        updated_at = now()
                    WHERE job_id = %s AND status = 'running' AND claim_token = %s
                    """,
                    (api_main.PORTAL_DATABASE_EXPORT_LEASE_SECONDS, job.get("job_id"), claim_token),
                )
            else:
                cur.execute(
                    """
                    UPDATE database_export_jobs
                    SET lease_expires_at = now() + (%s::text || ' seconds')::interval,
                        row_count = %s,
                        updated_at = now()
                    WHERE job_id = %s AND status = 'running' AND claim_token = %s
                    """,
                    (api_main.PORTAL_DATABASE_EXPORT_LEASE_SECONDS, row_count, job.get("job_id"), claim_token),
                )
            updated = cur.rowcount
        conn.commit()
    return updated == 1


def mark_failed(job: dict, code: str, message: str, *, clear_attempt_object_key: bool = True) -> bool:
    claim_token = _claim_token(job)
    if not claim_token:
        return False
    cleanup_row = None
    with api_main.db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE database_export_jobs
                SET status = 'failed', completed_at = now(), lease_expires_at = NULL, claim_token = NULL,
                    attempt_object_key = CASE WHEN %s THEN NULL ELSE attempt_object_key END,
                    safe_error_code = %s, safe_error_message = %s, updated_at = now()
                WHERE job_id = %s AND status = 'running' AND claim_token = %s
                """,
                (clear_attempt_object_key, code, message, job.get("job_id"), claim_token),
            )
            updated = cur.rowcount
            if updated == 1 and clear_attempt_object_key:
                cleanup_row = _upsert_attempt_ledger(
                    cur,
                    job_id=job.get("job_id"),
                    claim_token=claim_token,
                    object_key=_attempt_object_key(job),
                    state="cleanup_pending",
                )
        conn.commit()
    if cleanup_row:
        _cleanup_attempt_object_row(cleanup_row)
    if updated == 1:
        api_main._portal_audit_event_safe(
            event_type="database_export_job_failed",
            actor_user_id=str(job.get("requested_by_user_id") or "") or None,
            dataset_id=str(job.get("dataset_id") or "") or None,
            metadata={"job_id": str(job.get("job_id")), "safe_error_code": code},
        )
    return updated == 1


def publish_completed_job(job: dict, artifact: dict, row_count: int, completed_at, expires_at) -> dict | None:
    claim_token = _claim_token(job)
    attempt_key = _attempt_object_key(job)
    if _stop_requested_now():
        _log_worker_event("database_export_job_fence_lost", job=job, stage="publication", failed=True)
        return None
    if not claim_token or not attempt_key or artifact.get("storage_key") != attempt_key:
        _log_worker_event("database_export_job_fence_lost", job=job, stage="publication", failed=True)
        return None
    artifact_id = artifact.get("artifact_id")
    with api_main.db_conn() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT job_id
                    FROM database_export_jobs
                    WHERE job_id = %s
                      AND status = 'running'
                      AND claim_token = %s
                      AND lease_expires_at > now()
                      AND attempt_object_key = %s
                    FOR UPDATE
                    """,
                    (job.get("job_id"), claim_token, attempt_key),
                )
                if not cur.fetchone():
                    conn.rollback()
                    _log_worker_event("database_export_job_fence_lost", job=job, stage="publication", failed=True)
                    return None
                cur.execute(
                    """
                    INSERT INTO artifacts(artifact_id, run_id, created_at, kind, filename, content_type,
                                         size_bytes, sha256, storage_backend, storage_key, raw_file_id,
                                         workflow_name, stage_name, artifact_role, report_type, client_code,
                                         display_filename, original_filename, file_ext,
                                         layout_version, metadata_json, owner_user_id, expires_at)
                    VALUES (%s, NULL, %s, %s, %s, %s,
                            %s, %s, 'S3', %s, NULL,
                            'database_explorer', 'async_export', 'database_export', 'database_export', %s,
                            %s, %s, %s,
                            %s, %s::jsonb, %s, %s)
                    """,
                    (
                        artifact_id,
                        artifact.get("created_at"),
                        artifact.get("kind") or "REPORT",
                        artifact.get("filename"),
                        artifact.get("content_type"),
                        artifact.get("size_bytes"),
                        artifact.get("sha256"),
                        attempt_key,
                        artifact.get("client_code"),
                        artifact.get("display_filename"),
                        artifact.get("original_filename"),
                        artifact.get("file_ext"),
                        artifact.get("layout_version"),
                        api_main.json.dumps(artifact.get("metadata_json") or {}, ensure_ascii=False, sort_keys=True),
                        artifact.get("owner_user_id"),
                        artifact.get("expires_at"),
                    ),
                )
                cur.execute(
                    """
                    UPDATE database_export_jobs
                    SET status = 'completed', completed_at = %s, expires_at = %s,
                        lease_expires_at = NULL, claim_token = NULL, row_count = %s, artifact_id = %s,
                        object_key = %s, attempt_object_key = NULL, updated_at = now()
                    WHERE job_id = %s
                      AND status = 'running'
                      AND claim_token = %s
                      AND lease_expires_at > now()
                      AND attempt_object_key = %s
                    """,
                    (
                        completed_at,
                        expires_at,
                        row_count,
                        artifact_id,
                        attempt_key,
                        job.get("job_id"),
                        claim_token,
                        attempt_key,
                    ),
                )
                if cur.rowcount != 1:
                    conn.rollback()
                    _log_worker_event("database_export_job_fence_lost", job=job, stage="publication", failed=True)
                    return None
                cur.execute(
                    """
                    UPDATE database_export_attempt_objects
                    SET state = 'published', updated_at = now()
                    WHERE job_id = %s
                      AND claim_token = %s
                      AND object_key = %s
                      AND state IN ('active', 'cleanup_pending')
                    """,
                    (job.get("job_id"), claim_token, attempt_key),
                )
                if cur.rowcount != 1:
                    conn.rollback()
                    _log_worker_event("database_export_job_fence_lost", job=job, stage="publication", failed=True)
                    return None
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    api_main._portal_audit_event_safe(
        event_type="database_export_job_completed",
        actor_user_id=str(job.get("requested_by_user_id") or "") or None,
        dataset_id=str(job.get("dataset_id") or "") or None,
        artifact_id=str(artifact_id or "") or None,
        metadata={"job_id": str(job.get("job_id")), "row_count": row_count, "expires_at": expires_at.isoformat()},
    )
    return artifact


def _stage_export_rows(rows_iter, stage_holder: dict[str, str], generation_stage: str):
    iterator = iter(rows_iter)
    while True:
        if _stop_requested_now():
            raise StaleClaim("stop requested during export generation")
        stage_holder["stage"] = "query"
        try:
            row = next(iterator)
        except StopIteration:
            stage_holder["stage"] = generation_stage
            return
        if _stop_requested_now():
            raise StaleClaim("stop requested during export generation")
        stage_holder["stage"] = generation_stage
        yield row


def _safe_failure_from_exception(exc: Exception, stage: str | None) -> tuple[str, str]:
    if isinstance(exc, SafeExportFailure):
        return exc.code, exc.safe_message
    stage_codes = {
        "authorization": "AUTHORIZATION_FAILED",
        "query": "QUERY_FAILED",
        "csv_generation": "CSV_GENERATION_FAILED",
        "xlsx_generation": "XLSX_GENERATION_FAILED",
        "upload": "UPLOAD_FAILED",
        "publication": "PUBLICATION_FAILED",
        "cleanup": "CLEANUP_FAILED",
    }
    return stage_codes.get(str(stage or ""), "EXPORT_FAILED"), "This export could not be generated. Please contact an administrator."


def process_job(job: dict) -> bool:
    job_id = str(job.get("job_id"))
    temp_path: str | None = None
    uploaded_attempt_key: str | None = None
    stage_holder = {"stage": "authorization"}
    _log_worker_event("database_export_job_started", job=job, stage="authorization")
    try:
        api_main._portal_audit_event_safe(
            event_type="database_export_job_running",
            actor_user_id=str(job.get("requested_by_user_id") or "") or None,
            dataset_id=str(job.get("dataset_id") or "") or None,
            metadata={"job_id": job_id, "attempt_count": int(job.get("attempt_count") or 0)},
        )
        if _stop_requested_now():
            return False
        dataset = api_main._get_portal_database_dataset_for_user(str(job.get("dataset_id")), str(job.get("requested_by_user_id")))
        if not dataset or not dataset.get("can_export_rows"):
            raise SafeExportFailure("AUTHORIZATION_REVOKED", "Your access to export this dataset changed before the export ran. No file was generated.")
        visible_columns = api_main._get_portal_database_visible_columns(str(job.get("dataset_id")))
        columns, column_error = api_main._portal_database_columns_from_snapshot(visible_columns, job.get("request_snapshot_json") or {})
        if column_error:
            raise SafeExportFailure("DATASET_POLICY_CHANGED", column_error)
        snapshot = dict(job.get("request_snapshot_json") or {})
        stage_holder["stage"] = "query"
        # The snapshot is replayed through the CANONICAL, lossless converter —
        # the same one saved views use — and refused here if it cannot express
        # its stored filter meaning exactly. A trimmed, split or dropped exact
        # value would make this file describe a BROADER population than the sheet
        # the user exported, so a snapshot that cannot round-trip fails the job
        # instead of running a wider query.
        scope_params, replay_error = api_main._portal_database_export_snapshot_params(
            dataset, columns, snapshot
        )
        if replay_error or scope_params is None:
            raise SafeExportFailure(
                "EXPORT_REQUEST_INVALID",
                replay_error or api_main.PORTAL_DATABASE_EXPORT_REPLAY_REFUSAL,
            )
        _snap, _state, validation_error = api_main._portal_database_canonical_export_snapshot(
            dataset,
            columns,
            scope_params,
            format_name=str(job.get("requested_format") or ""),
        )
        if validation_error:
            raise SafeExportFailure("EXPORT_REQUEST_INVALID", validation_error)
        generation_stage = "xlsx_generation" if job.get("requested_format") == "xlsx" else "csv_generation"
        stage_holder["stage"] = generation_stage
        suffix = ".xlsx" if job.get("requested_format") == "xlsx" else ".csv"
        with tempfile.NamedTemporaryFile(prefix=f"database_export_{job_id}_", suffix=suffix, delete=False) as tmp:
            temp_path = tmp.name
        rows_iter = _stage_export_rows(
            api_main._portal_database_iter_export_rows(dataset, columns, snapshot),
            stage_holder,
            generation_stage,
        )
        heartbeat = lambda count: (not _stop_requested_now()) and refresh_lease(job, count)
        if job.get("requested_format") == "xlsx":
            row_count = api_main._portal_database_write_xlsx_file(
                columns,
                rows_iter,
                temp_path,
                max_rows=api_main.PORTAL_DATABASE_ASYNC_EXPORT_ROW_CAP,
                heartbeat=heartbeat,
            )
        else:
            row_count = api_main._portal_database_write_csv_file(
                columns,
                rows_iter,
                temp_path,
                max_rows=api_main.PORTAL_DATABASE_ASYNC_EXPORT_ROW_CAP,
                heartbeat=heartbeat,
            )
        if _stop_requested_now():
            raise StaleClaim("stop requested after export generation")
        if not refresh_lease(job, row_count):
            raise StaleClaim("lease refresh lost before upload")
        attempt_key = _attempt_object_key(job)
        if not attempt_key:
            raise SafeExportFailure("EXPORT_FAILED", "This export could not be generated. Please contact an administrator.")
        completed_at = api_main.utcnow()
        expires_at = completed_at + timedelta(days=api_main.PORTAL_DATABASE_ASYNC_EXPORT_RETENTION_DAYS)
        artifact = api_main._database_export_artifact_record(
            job=job,
            dataset=dataset,
            file_path=temp_path,
            format_name=str(job.get("requested_format")),
            row_count=row_count,
            completed_at=completed_at,
            expires_at=expires_at,
            storage_key=attempt_key,
        )
        if _stop_requested_now():
            raise StaleClaim("stop requested before upload")
        stage_holder["stage"] = "upload"
        api_main.s3.upload_file(temp_path, api_main.MINIO_BUCKET, attempt_key)
        uploaded_attempt_key = attempt_key
        if _stop_requested_now():
            cleanup_row = _mark_attempt_cleanup_pending(job)
            if cleanup_row:
                _cleanup_attempt_object_row(cleanup_row)
            return False
        stage_holder["stage"] = "publication"
        published = publish_completed_job(job, artifact, row_count, completed_at, expires_at)
        if not published:
            cleanup_row = _mark_attempt_cleanup_pending(job)
            if cleanup_row:
                _cleanup_attempt_object_row(cleanup_row)
            return False
        uploaded_attempt_key = None
        _log_worker_event("database_export_job_completed", job=job, stage="publication")
        return True
    except StaleClaim:
        _log_worker_event("database_export_job_fence_lost", job=job, stage=stage_holder.get("stage") or "publication", failed=True)
        return False
    except Exception as exc:
        stage = stage_holder.get("stage") or "publication"
        code, message = _safe_failure_from_exception(exc, stage)
        _log_worker_event(
            "database_export_job_failed",
            job=job,
            stage=stage,
            failed=True,
        )
        try:
            mark_failed(job, code, message, clear_attempt_object_key=True)
        except Exception:
            _log_worker_event("database_export_mark_failed_error", job=job, stage="publication", failed=True)
        return False
    finally:
        if temp_path:
            try:
                Path(temp_path).unlink(missing_ok=True)
            except Exception:
                _log_worker_event("database_export_cleanup_failed", job=job, stage="cleanup", failed=True)

def cleanup_expired_exports(limit: int = 100) -> int:
    with api_main.db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                _job_select_sql()
                + """
                  WHERE status = 'completed'
                    AND expires_at IS NOT NULL
                    AND expires_at <= now()
                  ORDER BY expires_at ASC, job_id ASC
                  LIMIT %s
                """,
                (limit,),
            )
            jobs = [dict(row) for row in cur]
    expired = 0
    for job in jobs:
        if _stop_requested_now():
            break
        artifact_id = job.get("artifact_id")
        object_key = job.get("object_key")
        if object_key:
            try:
                api_main.s3.delete_object(Bucket=api_main.MINIO_BUCKET, Key=object_key)
            except Exception:
                _log_worker_event("database_export_cleanup_failed", job=job, stage="cleanup", failed=True)
        with api_main.db_conn() as conn:
            with conn.cursor() as cur:
                if artifact_id:
                    cur.execute(
                        """
                        UPDATE artifacts
                        SET expired_at = COALESCE(expired_at, now()),
                            metadata_json = metadata_json || %s::jsonb
                        WHERE artifact_id = %s
                        """,
                        (api_main.json.dumps({"expired": True, "expired_by": "database_export_worker"}), artifact_id),
                    )
                cur.execute(
                    """
                    UPDATE database_export_jobs
                    SET status = 'expired', updated_at = now(), safe_error_code = NULL,
                        safe_error_message = NULL
                    WHERE job_id = %s AND status = 'completed'
                    """,
                    (job.get("job_id"),),
                )
            conn.commit()
        api_main._portal_audit_event_safe(
            event_type="database_export_job_expired",
            actor_user_id=str(job.get("requested_by_user_id") or "") or None,
            dataset_id=str(job.get("dataset_id") or "") or None,
            artifact_id=str(artifact_id or "") or None,
            metadata={"job_id": str(job.get("job_id")), "object_deleted_attempted": bool(object_key)},
        )
        expired += 1
    return expired


def run_once(*, cleanup: bool = True) -> int:
    cleaned_attempts = cleanup_unpublished_attempt_objects()
    if cleaned_attempts:
        _log_worker_event("database_export_cleanup_completed", stage="cleanup")
    recovered = recover_stale_running_jobs()
    if recovered["requeued"] or recovered["failed"]:
        _log_worker_event("database_export_stale_recovery_completed", stage="cleanup")
    if cleanup:
        expired = cleanup_expired_exports()
        if expired:
            _log_worker_event("database_export_expiry_completed", stage="cleanup")
    if _stop_requested_now():
        return 0
    job = claim_one_job()
    if not job:
        return 0
    return 1 if process_job(job) else 2


def run_loop(*, poll_seconds: int = DEFAULT_POLL_SECONDS, cleanup_interval_seconds: int = DEFAULT_CLEANUP_INTERVAL_SECONDS) -> int:
    last_cleanup = 0.0
    while not _stop_requested_now():
        now = time.monotonic()
        do_cleanup = now - last_cleanup >= max(60, cleanup_interval_seconds)
        code = run_once(cleanup=do_cleanup)
        if do_cleanup:
            last_cleanup = now
        if code == 0:
            if _stop_event.wait(max(1, poll_seconds)):
                break
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Database Explorer async export worker")
    parser.add_argument("--once", action="store_true", help="Run one claim/cleanup cycle and exit")
    parser.add_argument("--loop", action="store_true", help="Poll continuously")
    parser.add_argument("--cleanup-only", action="store_true", help="Run expiry cleanup and exit")
    parser.add_argument("--poll-seconds", type=int, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--cleanup-interval-seconds", type=int, default=DEFAULT_CLEANUP_INTERVAL_SECONDS)
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    if args.cleanup_only:
        cleanup_unpublished_attempt_objects()
        _log_worker_event("database_export_cleanup_completed", stage="cleanup")
        cleanup_expired_exports()
        _log_worker_event("database_export_expiry_completed", stage="cleanup")
        return 0
    if args.once or not args.loop:
        code = run_once(cleanup=True)
        return 0 if code in {0, 1} else code

    return run_loop(poll_seconds=args.poll_seconds, cleanup_interval_seconds=args.cleanup_interval_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
