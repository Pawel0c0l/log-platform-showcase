#!/usr/bin/env python3
"""Delivery worker for the suspected_bug alert email outbox.

Claims due `suspected_bug_email_outbox` rows with `FOR UPDATE SKIP LOCKED`,
sends them through the shared SMTP abstraction (`jobs.common.emailer`), and
records the delivery result. Concurrent workers cannot send the same row twice:
a row is marked `sending` with a claim token and a lease before any SMTP call,
and every terminal update is guarded by that claim token.

Delivery failures are ordinary operational errors. This worker never reports a
suspected_bug — that would let a broken mail path generate its own alert storm.
"""
from __future__ import annotations

import argparse
import json
import os
import smtplib
import signal
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api.suspected_bug import (  # noqa: E402
    OUTBOX_DEAD_LETTER,
    OUTBOX_RETRY,
    OUTBOX_SENDING,
    OUTBOX_SENT,
    SuspectedBugAlertConfig,
    load_alert_config,
    platform_db_conn,
    redact_email,
)
from jobs.common.emailer import send_html_email  # noqa: E402

DEFAULT_POLL_SECONDS = 60
MESSAGE_ID_DOMAIN = "log-platform.suspected-bug"

# Liveness key in `ops_control.scheduler_heartbeat`, read by
# `ops/execution_watchdog.py` as an ordinary heartbeat subject.
HEARTBEAT_COMPONENT = "alerting.email_worker"

# Exit code for "a message exhausted its retries and will never be delivered".
# Distinct from 1 so an operator (and any host-level unit monitoring) can tell a
# terminal delivery failure from a crashed worker.
EXIT_DEAD_LETTER = 3

# SMTP conditions that will not become deliverable by retrying the same message.
PERMANENT_SMTP_ERRORS = (
    smtplib.SMTPRecipientsRefused,
    smtplib.SMTPSenderRefused,
    smtplib.SMTPNotSupportedError,
    smtplib.SMTPAuthenticationError,
)

_stop_event = threading.Event()


def _handle_stop(_signum, _frame) -> None:
    _stop_event.set()


def _log_event(event: str, **fields) -> None:
    payload = {"level": "INFO", "component": "ops.suspected_bug_email_worker", "event": event}
    payload.update({key: value for key, value in fields.items() if value is not None})
    print(json.dumps(payload, sort_keys=True, default=str), flush=True)


def _log_error(event: str, **fields) -> None:
    payload = {
        "level": "ERROR",
        "classification": "operational_error",
        "component": "ops.suspected_bug_email_worker",
        "event": event,
    }
    payload.update({key: value for key, value in fields.items() if value is not None})
    print(json.dumps(payload, sort_keys=True, default=str), file=sys.stderr, flush=True)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def message_id_for(outbox_id: str) -> str:
    return f"<suspected-bug-{outbox_id}@{MESSAGE_ID_DOMAIN}>"


def recover_stale_claims(conn, config: SuspectedBugAlertConfig) -> int:
    """Return rows whose worker died mid-delivery to the retry queue."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE suspected_bug_email_outbox
            SET status = CASE WHEN attempts >= max_attempts THEN %s ELSE %s END,
                claim_token = NULL,
                lease_expires_at = NULL,
                available_at = now(),
                last_error = 'stale_claim_recovered',
                updated_at = now()
            WHERE status = %s AND lease_expires_at IS NOT NULL AND lease_expires_at < now()
            RETURNING outbox_id
            """,
            (OUTBOX_DEAD_LETTER, OUTBOX_RETRY, OUTBOX_SENDING),
        )
        recovered = [str(row["outbox_id"]) for row in cur.fetchall()]
    conn.commit()
    if recovered:
        _log_event("suspected_bug_email_stale_claims_recovered", count=len(recovered))
    return len(recovered)


def claim_batch(conn, config: SuspectedBugAlertConfig, *, limit: int | None = None) -> list[dict]:
    """Atomically claim up to `limit` due rows. Never claims an exhausted row."""
    claim_token = str(uuid.uuid4())
    batch_size = limit if limit is not None else config.worker_batch_size
    lease_seconds = int(config.stale_claim_timeout.total_seconds())
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH candidate AS (
              SELECT outbox_id
              FROM suspected_bug_email_outbox
              WHERE status IN ('pending', 'retry')
                AND available_at <= now()
                AND attempts < max_attempts
              ORDER BY available_at ASC, outbox_id ASC
              LIMIT %s
              FOR UPDATE SKIP LOCKED
            )
            UPDATE suspected_bug_email_outbox AS o
            SET status = %s,
                attempts = o.attempts + 1,
                claimed_at = now(),
                claim_token = %s::uuid,
                lease_expires_at = now() + (%s::text || ' seconds')::interval,
                updated_at = now()
            FROM candidate
            WHERE o.outbox_id = candidate.outbox_id
            RETURNING o.outbox_id, o.incident_id, o.notification_key, o.notification_reason,
                      o.recipient_config_ref, o.recipients, o.subject, o.body_text, o.body_html,
                      o.status, o.attempts, o.max_attempts, o.claim_token
            """,
            (batch_size, OUTBOX_SENDING, claim_token, lease_seconds),
        )
        rows = [dict(row) for row in cur.fetchall()]
    conn.commit()
    return rows


def mark_sent(conn, row: dict, provider_message_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE suspected_bug_email_outbox
            SET status = %s, sent_at = now(), claim_token = NULL, lease_expires_at = NULL,
                provider_message_id = %s, last_error = NULL, updated_at = now()
            WHERE outbox_id = %s::uuid AND status = %s AND claim_token = %s::uuid
            """,
            (OUTBOX_SENT, provider_message_id, row["outbox_id"], OUTBOX_SENDING, row["claim_token"]),
        )
        updated = cur.rowcount
        if updated == 1:
            cur.execute(
                """
                UPDATE suspected_bug_incidents
                SET last_email_sent_at = now(), updated_at = now()
                WHERE incident_id = %s::uuid
                """,
                (row["incident_id"],),
            )
    conn.commit()
    return updated == 1


def mark_failed(conn, row: dict, *, error: str, permanent: bool,
                config: SuspectedBugAlertConfig) -> str:
    """Retry transient failures with bounded exponential backoff; dead-letter the rest."""
    attempts = int(row.get("attempts") or 0)
    exhausted = attempts >= int(row.get("max_attempts") or 1)
    status = OUTBOX_DEAD_LETTER if (permanent or exhausted) else OUTBOX_RETRY
    delay = config.retry_delay(attempts)
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE suspected_bug_email_outbox
            SET status = %s,
                claim_token = NULL,
                lease_expires_at = NULL,
                available_at = CASE WHEN %s = %s THEN now() + (%s::text || ' seconds')::interval
                                    ELSE available_at END,
                last_error = %s,
                updated_at = now()
            WHERE outbox_id = %s::uuid AND status = %s AND claim_token = %s::uuid
            """,
            (
                status, status, OUTBOX_RETRY, int(delay.total_seconds()), error[:2000],
                row["outbox_id"], OUTBOX_SENDING, row["claim_token"],
            ),
        )
    conn.commit()
    return status


def deliver_row(conn, row: dict, *, config: SuspectedBugAlertConfig, sender=send_html_email) -> str:
    """Send one claimed row and record the outcome. Returns the resulting status."""
    outbox_id = str(row["outbox_id"])
    recipients = list(row.get("recipients") or [])
    provider_message_id = message_id_for(outbox_id)
    try:
        result = sender(
            to_addrs=recipients,
            subject=row["subject"],
            html_body=row.get("body_html") or "",
            text_body=row["body_text"],
            message_id=provider_message_id,
        )
    except PERMANENT_SMTP_ERRORS as exc:
        status = mark_failed(conn, row, error=f"{type(exc).__name__}: {exc}", permanent=True, config=config)
        _log_error("suspected_bug_email_permanent_failure", outbox_id=outbox_id,
                   incident_id=str(row.get("incident_id")), status=status, error_type=type(exc).__name__)
        return status
    except Exception as exc:
        status = mark_failed(conn, row, error=f"{type(exc).__name__}: {exc}", permanent=False, config=config)
        _log_error("suspected_bug_email_delivery_failed", outbox_id=outbox_id,
                   incident_id=str(row.get("incident_id")), status=status,
                   attempts=row.get("attempts"), error_type=type(exc).__name__)
        return status

    message_id = getattr(result, "message_id", None) or provider_message_id
    if not mark_sent(conn, row, message_id):
        _log_error("suspected_bug_email_claim_lost_after_send", outbox_id=outbox_id,
                   incident_id=str(row.get("incident_id")))
        return OUTBOX_SENDING
    _log_event(
        "suspected_bug_email_sent",
        outbox_id=outbox_id,
        incident_id=str(row.get("incident_id")),
        notification_reason=row.get("notification_reason"),
        recipient_config_ref=row.get("recipient_config_ref"),
        recipients=[redact_email(address) for address in recipients],
        provider_message_id=message_id,
        attempts=row.get("attempts"),
    )
    return OUTBOX_SENT


def record_heartbeat(conn, *, detail: Mapping[str, Any] | None = None) -> bool:
    """Stamp worker liveness for the independent watchdog.

    A 5-minute oneshot that legitimately has nothing to send exits 0 and writes
    no row anywhere, so a stopped timer and an idle timer were indistinguishable —
    exactly the gap `ops_control.scheduler_heartbeat` already exists to close for
    the Workflow A dispatcher. Reusing that table rather than inventing a second
    liveness store keeps `ops/execution_watchdog.py` unchanged for this subject:
    it is an ordinary heartbeat expectation.

    Written on every completed batch including empty ones, because "I ran and
    reached the database" is the fact being asserted. Never fails the batch:
    liveness reporting is observability, not delivery.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ops_control.scheduler_heartbeat AS h
                    (component, last_beat_at, last_beat_detail, beat_count, updated_at)
                VALUES (%s, now(), %s::jsonb, 1, now())
                ON CONFLICT (component) DO UPDATE
                   SET last_beat_at = now(),
                       last_beat_detail = EXCLUDED.last_beat_detail,
                       beat_count = h.beat_count + 1,
                       updated_at = now()
                """,
                (HEARTBEAT_COMPONENT, json.dumps(dict(detail or {}), default=str)),
            )
        conn.commit()
        return True
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return False


def run_once(*, config: SuspectedBugAlertConfig | None = None, limit: int | None = None,
             sender=send_html_email, conn=None) -> dict:
    config = config or load_alert_config()
    owns_conn = conn is None
    conn = conn or platform_db_conn()
    counts = {"claimed": 0, OUTBOX_SENT: 0, OUTBOX_RETRY: 0, OUTBOX_DEAD_LETTER: 0}
    try:
        recover_stale_claims(conn, config)
        rows = claim_batch(conn, config, limit=limit)
        counts["claimed"] = len(rows)
        for row in rows:
            if _stop_event.is_set():
                mark_failed(conn, row, error="worker_stopped", permanent=False, config=config)
                continue
            status = deliver_row(conn, row, config=config, sender=sender)
            counts[status] = counts.get(status, 0) + 1
        counts["heartbeat_recorded"] = record_heartbeat(
            conn,
            detail={"pid": os.getpid(), "claimed": counts["claimed"],
                    "sent": counts.get(OUTBOX_SENT, 0)},
        )
    finally:
        if owns_conn:
            try:
                conn.close()
            except Exception:
                pass
    return counts


def show_due(*, config: SuspectedBugAlertConfig | None = None, limit: int = 50) -> int:
    """Read-only preview of what the worker would claim. Sends nothing."""
    config = config or load_alert_config()
    conn = platform_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT outbox_id, incident_id, status, attempts, max_attempts,
                       available_at, notification_reason, subject
                FROM suspected_bug_email_outbox
                WHERE status IN ('pending', 'retry') AND attempts < max_attempts
                ORDER BY available_at ASC, outbox_id ASC
                LIMIT %s
                """,
                (limit,),
            )
            rows = [dict(row) for row in cur.fetchall()]
        conn.rollback()
    finally:
        conn.close()
    print(json.dumps({"due_rows": len(rows), "items": rows}, indent=2, sort_keys=True, default=str))
    return 0


def run_loop(*, poll_seconds: int, config: SuspectedBugAlertConfig | None = None) -> int:
    config = config or load_alert_config()
    while not _stop_event.is_set():
        counts = run_once(config=config)
        if not counts["claimed"] and _stop_event.wait(max(5, poll_seconds)):
            break
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="suspected_bug alert email outbox worker")
    parser.add_argument("--once", action="store_true", help="Claim and deliver one batch, then exit")
    parser.add_argument("--loop", action="store_true", help="Poll continuously")
    parser.add_argument("--show-due", action="store_true", help="Read-only: list due rows and exit")
    parser.add_argument("--poll-seconds", type=int, default=DEFAULT_POLL_SECONDS)
    parser.add_argument("--batch-size", type=int, help="Override SUSPECTED_BUG_EMAIL_WORKER_BATCH_SIZE")
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    config = load_alert_config()
    if args.show_due:
        return show_due(config=config)
    if args.loop:
        return run_loop(poll_seconds=args.poll_seconds, config=config)

    counts = run_once(config=config, limit=args.batch_size)
    _log_event("suspected_bug_email_batch_completed", **counts)
    if counts.get(OUTBOX_DEAD_LETTER):
        # A dead-lettered alert is the one failure this unit must not absorb.
        # It cannot report itself by email — that is the anti-recursion contract
        # the unit's own comment states, and why it carries no OnFailure= — so
        # the exit code is the signal: systemd marks the unit failed, which is
        # visible to `systemctl --failed` and to any host-level unit monitoring
        # without involving the mail path at all.
        #
        # Deliberately only on the *transition*: an already dead-lettered row is
        # never re-claimed, so a healthy later batch exits 0 rather than pinning
        # the unit failed forever. Durable visibility of outstanding dead letters
        # is the watchdog's job (ALERT_DELIVERY_FAILED), not this exit code's.
        #
        # `retry` deliberately does NOT fail the unit: a transient SMTP outage is
        # the retry mechanism working, not an operator condition.
        _log_error(
            "suspected_bug_email_dead_letter_batch",
            dead_letter=counts.get(OUTBOX_DEAD_LETTER),
            sent=counts.get(OUTBOX_SENT, 0),
            retry=counts.get(OUTBOX_RETRY, 0),
            remediation=(
                "Alert email is undeliverable. Inspect suspected_bug_email_outbox "
                "rows with status='dead_letter' and their last_error, fix the SMTP "
                "path, then requeue. No email was sent for those incidents."
            ),
        )
        return EXIT_DEAD_LETTER
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
