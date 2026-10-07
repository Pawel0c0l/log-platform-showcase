"""Reservation-before-SMTP helpers for ALPHA Eco Driving driver emails."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from typing import Any

from jobs.common.eco_email_reconciliation import (
    AMBIGUOUS_SUBMISSION_REQUIRES_RECONCILIATION,
    KEY_RESULT,
    VALUE_AMBIGUOUS,
    mark_send_ambiguous,
    resolve_ambiguous_send,
    unresolved_ambiguous_send,
)

STALE_PENDING_REQUIRES_RECONCILIATION = "STALE_PENDING_REQUIRES_RECONCILIATION"
DEFAULT_PENDING_STALE_AFTER_MINUTES = 120

#: Re-exported so the two driver mailers import their whole reservation
#: vocabulary from one place, and so the ambiguous-submission contract is the
#: SAME code for all four Eco mailers rather than four lookalikes.
__all__ = [
    "AMBIGUOUS_SUBMISSION_REQUIRES_RECONCILIATION",
    "DEFAULT_PENDING_STALE_AFTER_MINUTES",
    "ReservationResult",
    "STALE_PENDING_REQUIRES_RECONCILIATION",
    "build_idempotency_key",
    "mark_send_ambiguous",
    "mark_send_failed",
    "mark_send_sent",
    "mark_sent_copy_result",
    "reserve_send",
    "resolve_ambiguous_send",
    "unresolved_ambiguous_send",
]

#: The subject column of the two ALPHA driver send logs.
SUBJECT_COLUMN = "assigned_id"


@dataclass(frozen=True)
class ReservationResult:
    outcome: str
    send_log_id: str | None
    idempotency_key: str
    existing_status: str | None = None
    existing_send_log_id: str | None = None
    parent_send_log_id: str | None = None

    @property
    def reserved(self) -> bool:
        return self.outcome == "reserved"


def build_idempotency_key(*, client_id: str, assigned_id: str, report_type: str,
                          period_start_date: date, period_end_date: date) -> str:
    subject_hash = hashlib.sha256(assigned_id.encode("utf-8")).hexdigest()
    return "|".join(("eco_driver", report_type, client_id,
                     f"assigned_sha256:{subject_hash}",
                     period_start_date.isoformat(), period_end_date.isoformat()))


def reserve_send(cur, *, send_log_table: str, report_type: str, run_id: str,
                 row: dict[str, Any], decision: Any, recipient_email: str,
                 original_recipient_email: str | None, subject: str,
                 send_scope: str, pending_stale_after_minutes: int,
                 force_resend_reason: str | None,
                 metadata_json: dict[str, Any]) -> ReservationResult:
    key = build_idempotency_key(
        client_id=str(row["client_id"]), assigned_id=str(row["assigned_id"]),
        report_type=report_type, period_start_date=row["period_start_date"],
        period_end_date=row["period_end_date"],
    )
    identity = (row["client_id"], row["assigned_id"], report_type,
                row["period_start_date"], row["period_end_date"])
    parent_id = None
    if send_scope == "normal":
        cur.execute(f"""
            SELECT send_log_id::text, status, attempted_at,
                   (status='pending' AND attempted_at <
                    NOW() - (%s::text || ' minutes')::interval) AS is_stale,
                   (status='pending'
                    AND metadata_json->>'{KEY_RESULT}'='{VALUE_AMBIGUOUS}') AS is_ambiguous
            FROM {send_log_table}
            WHERE client_id=%s AND assigned_id=%s AND report_type=%s
              AND period_start_date=%s AND period_end_date=%s
              AND send_scope='normal' AND status IN ('pending','sent')
            ORDER BY attempted_at DESC LIMIT 1
        """, (pending_stale_after_minutes, *identity))
        existing = cur.fetchone()
        if existing:
            get = existing.get if isinstance(existing, dict) else None
            existing_id = get("send_log_id") if get else existing[0]
            status = get("status") if get else existing[1]
            stale = bool(get("is_stale") if get else existing[3])
            ambiguous = bool(get("is_ambiguous") if get else existing[4])
            if ambiguous:
                # STRONGER THAN STALE, AND REPORTED AS SUCH. A stale pending row
                # is a run that may never have reached SMTP; this one reached it
                # and did not learn the answer, so it is not merely blocked, it
                # needs a human.
                outcome = AMBIGUOUS_SUBMISSION_REQUIRES_RECONCILIATION
            elif status == "pending" and stale:
                outcome = STALE_PENDING_REQUIRES_RECONCILIATION
            else:
                outcome = "existing"
            return ReservationResult(outcome, None, key, status, existing_id)
    else:
        # FORCE-RESEND DOES NOT OUTRANK "WE DO NOT KNOW". `force_resend` exists
        # to override an established `sent`, which is a fact. An unresolved
        # ambiguous submission is the absence of a fact, and sending again on
        # the strength of an absence is exactly the duplicate this contract
        # exists to prevent. Only an explicit operator reconciliation clears it.
        #
        # `test` is exempt because a test send goes to the configured test
        # mailbox and never to the driver, so it cannot duplicate anything —
        # and it is often how an operator investigates the blocked row.
        if send_scope != "test":
            blocked = unresolved_ambiguous_send(
                cur, send_log_table=send_log_table, subject_column="assigned_id",
                client_id=row["client_id"], subject_value=row["assigned_id"],
                report_type=report_type,
                period_start_date=row["period_start_date"],
                period_end_date=row["period_end_date"],
            )
            if blocked is not None:
                return ReservationResult(
                    AMBIGUOUS_SUBMISSION_REQUIRES_RECONCILIATION, None, key,
                    blocked.status, blocked.send_log_id)
        cur.execute(f"""
            SELECT send_log_id::text FROM {send_log_table}
            WHERE client_id=%s AND assigned_id=%s AND report_type=%s
              AND period_start_date=%s AND period_end_date=%s
              AND send_scope='normal' AND status='sent'
            ORDER BY sent_at DESC, attempted_at DESC LIMIT 1
        """, identity)
        parent = cur.fetchone()
        if parent:
            parent_id = parent["send_log_id"] if isinstance(parent, dict) else parent[0]

    cur.execute(f"""
        INSERT INTO {send_log_table} (
          client_id, run_id, assigned_id, recipient_email, original_recipient_email,
          ranking_type, report_type, send_scope, idempotency_key, parent_send_log_id,
          template_type, template_filename, qualification_status, ranking_included,
          template_variant, period_start_date, period_end_date, ecodriving_rating_type,
          email_subject, status, force_resend_reason, force_resend_at, metadata_json
        ) VALUES (
          %s,%s,%s,%s,%s,%s,%s,%s,%s,%s::uuid,
          %s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s,
          CASE WHEN %s='forced' THEN NOW() ELSE NULL END,%s::jsonb
        ) ON CONFLICT DO NOTHING RETURNING send_log_id::text
    """, (
        row["client_id"], run_id, row["assigned_id"], recipient_email,
        original_recipient_email, row.get("ranking_type"), report_type, send_scope,
        key, parent_id, decision.template_type, decision.template_filename,
        row.get("qualification_status"), row.get("ranking_included"),
        decision.template_variant, row["period_start_date"], row["period_end_date"],
        row.get("ecodriving_rating_type") or "", subject, force_resend_reason,
        send_scope, json.dumps(metadata_json, ensure_ascii=False, default=str),
    ))
    inserted = cur.fetchone()
    if inserted:
        send_log_id = inserted["send_log_id"] if isinstance(inserted, dict) else inserted[0]
        return ReservationResult("reserved", send_log_id, key, parent_send_log_id=parent_id)
    if send_scope == "normal":
        cur.execute(f"""
            SELECT send_log_id::text, status,
                   (status='pending'
                    AND metadata_json->>'{KEY_RESULT}'='{VALUE_AMBIGUOUS}') AS is_ambiguous
            FROM {send_log_table}
            WHERE client_id=%s AND assigned_id=%s AND report_type=%s
              AND period_start_date=%s AND period_end_date=%s
              AND send_scope='normal' AND status IN ('pending','sent')
            ORDER BY attempted_at DESC LIMIT 1
        """, identity)
        existing = cur.fetchone()
        if existing:
            get = existing.get if isinstance(existing, dict) else None
            ambiguous = bool(get("is_ambiguous") if get else existing[2])
            return ReservationResult(
                AMBIGUOUS_SUBMISSION_REQUIRES_RECONCILIATION if ambiguous else "existing",
                None, key,
                get("status") if get else existing[1],
                get("send_log_id") if get else existing[0])
    raise RuntimeError("EMAIL_RESERVATION_INSERT_FAILED")


def mark_send_sent(cur, *, send_log_table: str, send_log_id: str,
                   smtp_message_id: str, provider_response: str,
                   acceptance: Any = None) -> None:
    """Record the acceptance, and what example.invalid said when it accepted.

    `status='sent'` means exactly one thing: THE example.invalid RELAY ACCEPTED THIS
    MESSAGE FOR RELAY. `sent_at` is when that was established, `provider_response`
    is the relay's own final reply when it could be captured, and
    `metadata_json.smtp_acceptance` keeps the reply code, its verbatim text and
    the relay's queue identifier if it emitted one. None of that is a statement
    about the recipient's mailbox.
    """
    if acceptance is None:
        cur.execute(f"""UPDATE {send_log_table} SET status='sent', smtp_message_id=%s,
            provider_response=%s, error_message=NULL, sent_at=NOW(), updated_at=NOW()
            WHERE send_log_id=%s::uuid""", (smtp_message_id, provider_response, send_log_id))
        return
    cur.execute(f"""UPDATE {send_log_table} SET status='sent', smtp_message_id=%s,
        provider_response=%s, error_message=NULL, sent_at=NOW(), updated_at=NOW(),
        metadata_json=COALESCE(metadata_json,'{{}}'::jsonb) || %s::jsonb
        WHERE send_log_id=%s::uuid""",
        (smtp_message_id, provider_response,
         json.dumps(acceptance.as_metadata(), ensure_ascii=False), send_log_id))


def mark_sent_copy_result(cur, *, send_log_table: str, send_log_id: str,
                          outcome: Any) -> None:
    """Where the Sent-folder copy of an ALREADY-SENT message ended up.

    The two driver send logs have no dedicated archive columns, so the outcome
    goes into the namespaced `metadata_json.sent_folder_copy` object. It is
    written on its own, after `status='sent'` has been committed, and touches
    NO column the reservation guard or the partial unique indexes read — a
    failed copy can therefore never make a delivered message reservable again.
    """
    cur.execute(f"""UPDATE {send_log_table}
        SET metadata_json=COALESCE(metadata_json,'{{}}'::jsonb) || %s::jsonb,
            updated_at=NOW()
        WHERE send_log_id=%s::uuid""",
        (json.dumps(outcome.as_metadata(), ensure_ascii=False), send_log_id))


def mark_send_failed(cur, *, send_log_table: str, send_log_id: str,
                     error_message: str) -> None:
    cur.execute(f"""UPDATE {send_log_table} SET status='failed', error_message=%s,
        sent_at=NULL, updated_at=NOW() WHERE send_log_id=%s::uuid""",
        (error_message, send_log_id))
