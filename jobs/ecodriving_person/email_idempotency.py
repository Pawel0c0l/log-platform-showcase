"""Transaction-safe email reservation helpers for Eco Driving Person jobs."""

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

DEFAULT_PENDING_STALE_AFTER_MINUTES = 120
STALE_PENDING_REQUIRES_RECONCILIATION = "STALE_PENDING_REQUIRES_RECONCILIATION"

#: The ambiguous-submission contract is SHARED with the two ALPHA driver
#: mailers, not reimplemented here: one classification, one durable marker, one
#: operator resolution path for all four Eco mailings.
__all__ = [
    "AMBIGUOUS_SUBMISSION_REQUIRES_RECONCILIATION",
    "DEFAULT_PENDING_STALE_AFTER_MINUTES",
    "ReservationResult",
    "STALE_PENDING_REQUIRES_RECONCILIATION",
    "build_idempotency_key",
    "insert_audit_log",
    "mark_send_ambiguous",
    "mark_send_failed",
    "mark_send_sent",
    "mark_sent_archive_result",
    "mark_sent_mime_preserved",
    "normal_send_scope",
    "reserve_send",
    "resolve_ambiguous_send",
    "unresolved_ambiguous_send",
]

#: The subject column of the two BRAVO person send logs.
SUBJECT_COLUMN = "person_name_group_key"


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


def build_idempotency_key(
    *,
    report_type: str,
    client_id: str,
    person_name_group_key: str,
    period_start_date: date,
    period_end_date: date,
    template_type: str | None = None,  # legacy argument; deliberately excluded
) -> str:
    person_digest = hashlib.sha256(
        str(person_name_group_key).encode("utf-8")
    ).hexdigest()
    return "|".join(
        (
            "eco_person",
            report_type,
            str(client_id),
            f"person_sha256:{person_digest}",
            period_start_date.isoformat(),
            period_end_date.isoformat(),
        )
    )


def normal_send_scope(*, force_resend: bool, test_recipient_email: str | None) -> str:
    if force_resend:
        return "forced"
    if test_recipient_email:
        return "test"
    return "normal"


def insert_audit_log(
    cur,
    *,
    send_log_table: str,
    report_type: str,
    run_id: str,
    row: dict[str, Any],
    decision: Any,
    recipient_email: str,
    original_recipient_email: str | None,
    subject: str,
    status: str,
    send_scope: str,
    idempotency_key: str | None = None,
    parent_send_log_id: str | None = None,
    smtp_message_id: str | None = None,
    provider_response: str | None = None,
    error_message: str | None = None,
    metadata_json: dict[str, Any] | None = None,
) -> str | None:
    sent_at_sql = "NOW()" if status == "sent" else "NULL"
    cur.execute(
        f"""
        INSERT INTO {send_log_table} (
          client_id, run_id, person_name_group_key, person_name,
          recipient_email, original_recipient_email,
          ranking_type, report_type, send_scope, idempotency_key, parent_send_log_id,
          template_type, template_filename, qualification_status, ranking_included,
          template_variant, period_start_date, period_end_date, ecodriving_rating_type,
          email_subject, status, smtp_message_id, provider_response,
          error_message, sent_at, metadata_json
        )
        VALUES (
          %s, %s, %s, %s, %s, %s,
          %s, %s, %s, %s, %s::uuid,
          %s, %s, %s, %s,
          %s, %s, %s, %s,
          %s, %s, %s, %s,
          %s, {sent_at_sql}, %s::jsonb
        )
        RETURNING send_log_id::text
        """,
        (
            row.get("client_id"),
            run_id,
            row.get("person_name_group_key") or "",
            row.get("person_name") or "",
            recipient_email,
            original_recipient_email,
            row.get("ranking_type"),
            report_type,
            send_scope,
            idempotency_key,
            parent_send_log_id,
            decision.template_type,
            decision.template_filename,
            row.get("qualification_status"),
            row.get("ranking_included"),
            decision.template_variant,
            row.get("period_start_date"),
            row.get("period_end_date"),
            row.get("ecodriving_rating_type") or "",
            subject,
            status,
            smtp_message_id,
            provider_response,
            error_message,
            json.dumps(metadata_json or {}, ensure_ascii=False, default=str),
        ),
    )
    returned = cur.fetchone()
    if returned is None:
        return None
    if isinstance(returned, dict):
        return returned["send_log_id"]
    return returned[0]


def reserve_send(
    cur,
    *,
    send_log_table: str,
    report_type: str,
    run_id: str,
    row: dict[str, Any],
    decision: Any,
    recipient_email: str,
    original_recipient_email: str | None,
    subject: str,
    send_scope: str,
    pending_stale_after_minutes: int,
    metadata_json: dict[str, Any],
) -> ReservationResult:
    """Reserve before SMTP without ever reclaiming a pending row implicitly."""
    idempotency_key = build_idempotency_key(
        report_type=report_type,
        client_id=str(row.get("client_id")),
        person_name_group_key=str(row.get("person_name_group_key")),
        period_start_date=row["period_start_date"],
        period_end_date=row["period_end_date"],
    )
    identity_params = (
        row.get("client_id"),
        row.get("person_name_group_key") or "",
        report_type,
        row.get("period_start_date"),
        row.get("period_end_date"),
    )
    parent_send_log_id: str | None = None

    if send_scope == "normal":
        cur.execute(
            f"""
            SELECT send_log_id::text, status,
                   attempted_at,
                   (status = 'pending' AND attempted_at <
                     NOW() - (%s::text || ' minutes')::interval) AS is_stale,
                   (status = 'pending'
                    AND metadata_json ->> '{KEY_RESULT}' = '{VALUE_AMBIGUOUS}') AS is_ambiguous
            FROM {send_log_table}
            WHERE client_id = %s
              AND person_name_group_key = %s
              AND report_type = %s
              AND period_start_date = %s
              AND period_end_date = %s
              AND send_scope = 'normal'
              AND status IN ('pending','sent')
            ORDER BY attempted_at DESC
            LIMIT 1
            """,
            (pending_stale_after_minutes, *identity_params),
        )
        existing = cur.fetchone()
        if existing is not None:
            existing_id = existing["send_log_id"] if isinstance(existing, dict) else existing[0]
            existing_status = existing["status"] if isinstance(existing, dict) else existing[1]
            is_stale = bool(existing["is_stale"] if isinstance(existing, dict) else existing[3])
            is_ambiguous = bool(
                existing.get("is_ambiguous") if isinstance(existing, dict)
                else existing[4])
            if is_ambiguous:
                # The previous submission may already be in this person's inbox.
                # Reserving over it would be the second copy.
                outcome = AMBIGUOUS_SUBMISSION_REQUIRES_RECONCILIATION
            elif existing_status == "pending" and is_stale:
                outcome = STALE_PENDING_REQUIRES_RECONCILIATION
            else:
                outcome = "existing"
            return ReservationResult(
                outcome=outcome,
                send_log_id=None,
                idempotency_key=idempotency_key,
                existing_status=existing_status,
                existing_send_log_id=existing_id,
            )
    else:
        # A forced resend overrides an established `sent`. It does not override
        # "we never learned whether it was sent": see the ALPHA mailer's twin of
        # this guard. `test` goes to the test mailbox, never to the person, so
        # it cannot duplicate anything and stays available for diagnosis.
        if send_scope != "test":
            blocked = unresolved_ambiguous_send(
                cur,
                send_log_table=send_log_table,
                subject_column="person_name_group_key",
                client_id=row.get("client_id"),
                subject_value=row.get("person_name_group_key") or "",
                report_type=report_type,
                period_start_date=row["period_start_date"],
                period_end_date=row["period_end_date"],
            )
            if blocked is not None:
                return ReservationResult(
                    outcome=AMBIGUOUS_SUBMISSION_REQUIRES_RECONCILIATION,
                    send_log_id=None,
                    idempotency_key=idempotency_key,
                    existing_status=blocked.status,
                    existing_send_log_id=blocked.send_log_id,
                )
        cur.execute(
            f"""
            SELECT send_log_id::text
            FROM {send_log_table}
            WHERE client_id = %s
              AND person_name_group_key = %s
              AND report_type = %s
              AND period_start_date = %s
              AND period_end_date = %s
              AND send_scope = 'normal'
              AND status = 'sent'
            ORDER BY sent_at DESC, attempted_at DESC
            LIMIT 1
            """,
            identity_params,
        )
        parent = cur.fetchone()
        if parent is not None:
            parent_send_log_id = parent["send_log_id"] if isinstance(parent, dict) else parent[0]

    cur.execute(
        f"""
        INSERT INTO {send_log_table} (
          client_id, run_id, person_name_group_key, person_name,
          recipient_email, original_recipient_email,
          ranking_type, report_type, send_scope, idempotency_key, parent_send_log_id,
          template_type, template_filename, qualification_status, ranking_included,
          template_variant, period_start_date, period_end_date, ecodriving_rating_type,
          email_subject, status, metadata_json
        )
        VALUES (
          %s, %s, %s, %s, %s, %s,
          %s, %s, %s, %s, %s::uuid,
          %s, %s, %s, %s,
          %s, %s, %s, %s,
          %s, 'pending', %s::jsonb
        )
        ON CONFLICT DO NOTHING
        RETURNING send_log_id::text
        """,
        (
            row.get("client_id"), run_id,
            row.get("person_name_group_key") or "", row.get("person_name") or "",
            recipient_email, original_recipient_email, row.get("ranking_type"), report_type,
            send_scope, idempotency_key, parent_send_log_id, decision.template_type,
            decision.template_filename, row.get("qualification_status"),
            row.get("ranking_included"), decision.template_variant,
            row.get("period_start_date"), row.get("period_end_date"),
            row.get("ecodriving_rating_type") or "", subject,
            json.dumps(metadata_json, ensure_ascii=False, default=str),
        ),
    )
    inserted = cur.fetchone()
    if inserted is not None:
        send_log_id = inserted["send_log_id"] if isinstance(inserted, dict) else inserted[0]
        return ReservationResult(
            outcome="reserved", send_log_id=send_log_id,
            idempotency_key=idempotency_key, parent_send_log_id=parent_send_log_id,
        )

    # A concurrent normal reservation may have won after the pre-check.
    if send_scope == "normal":
        cur.execute(
            f"""
            SELECT send_log_id::text, status,
                   (status = 'pending'
                    AND metadata_json ->> '{KEY_RESULT}' = '{VALUE_AMBIGUOUS}') AS is_ambiguous
            FROM {send_log_table}
            WHERE client_id = %s AND person_name_group_key = %s AND report_type = %s
              AND period_start_date = %s AND period_end_date = %s
              AND send_scope = 'normal' AND status IN ('pending','sent')
            ORDER BY attempted_at DESC LIMIT 1
            """,
            identity_params,
        )
        existing = cur.fetchone()
        if existing is not None:
            is_ambiguous = bool(
                existing.get("is_ambiguous") if isinstance(existing, dict)
                else existing[2])
            return ReservationResult(
                outcome=(AMBIGUOUS_SUBMISSION_REQUIRES_RECONCILIATION
                         if is_ambiguous else "existing"),
                send_log_id=None, idempotency_key=idempotency_key,
                existing_status=existing["status"] if isinstance(existing, dict) else existing[1],
                existing_send_log_id=(existing["send_log_id"] if isinstance(existing, dict) else existing[0]),
            )
    raise RuntimeError("EMAIL_RESERVATION_INSERT_FAILED")

def mark_send_sent(
    cur,
    *,
    send_log_table: str,
    send_log_id: str,
    smtp_message_id: str,
    provider_response: str,
    acceptance: Any = None,
) -> None:
    """Record the acceptance, and what example.invalid said when it accepted.

    `status='sent'` means exactly one thing: THE example.invalid RELAY ACCEPTED THIS
    MESSAGE FOR RELAY. `sent_at` is when that was established,
    `provider_response` is the relay's own final reply when it could be
    captured, and `metadata_json.smtp_acceptance` keeps the reply code, its
    verbatim text and the relay's queue identifier if it emitted one. None of
    that is a statement about the recipient's mailbox.
    """
    if acceptance is None:
        cur.execute(
            f"""
            UPDATE {send_log_table}
               SET status = 'sent',
                   smtp_message_id = %s,
                   provider_response = %s,
                   error_message = NULL,
                   sent_at = NOW(),
                   updated_at = NOW()
             WHERE send_log_id = %s::uuid
            """,
            (smtp_message_id, provider_response, send_log_id),
        )
        return
    cur.execute(
        f"""
        UPDATE {send_log_table}
           SET status = 'sent',
               smtp_message_id = %s,
               provider_response = %s,
               error_message = NULL,
               sent_at = NOW(),
               updated_at = NOW(),
               metadata_json = COALESCE(metadata_json, '{{}}'::jsonb) || %s::jsonb
         WHERE send_log_id = %s::uuid
        """,
        (
            smtp_message_id,
            provider_response,
            json.dumps(acceptance.as_metadata(), ensure_ascii=False),
            send_log_id,
        ),
    )


def mark_sent_mime_preserved(
    cur,
    *,
    send_log_table: str,
    send_log_id: str,
    mime_bytes: bytes,
    mime_sha256: str,
) -> None:
    cur.execute(
        f"""
        UPDATE {send_log_table}
           SET sent_mime_bytes = %s,
               sent_mime_sha256 = %s,
               sent_archive_status = 'pending',
               sent_archive_error = NULL,
               updated_at = NOW()
         WHERE send_log_id = %s::uuid
        """,
        (mime_bytes, mime_sha256, send_log_id),
    )


def mark_sent_archive_result(
    cur,
    *,
    send_log_table: str,
    send_log_id: str,
    status: str,
    mailbox: str | None,
    message_id: str,
    error_message: str | None = None,
) -> None:
    verified_at_sql = "NOW()" if status in {"appended", "already_present"} else "NULL"
    cur.execute(
        f"""
        UPDATE {send_log_table}
           SET sent_archive_status = %s,
               sent_archive_mailbox = %s,
               sent_archive_message_id = %s,
               sent_archive_attempted_at = NOW(),
               sent_archive_verified_at = {verified_at_sql},
               sent_archive_error = %s,
               updated_at = NOW()
         WHERE send_log_id = %s::uuid
        """,
        (status, mailbox, message_id, error_message, send_log_id),
    )


def mark_send_failed(
    cur,
    *,
    send_log_table: str,
    send_log_id: str,
    error_message: str,
) -> None:
    cur.execute(
        f"""
        UPDATE {send_log_table}
           SET status = 'failed',
               error_message = %s,
               sent_at = NULL,
               updated_at = NOW()
         WHERE send_log_id = %s::uuid
        """,
        (error_message, send_log_id),
    )
