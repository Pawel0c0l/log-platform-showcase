"""Eco Driving e-mail — the durable half of the ambiguous-submission contract.

THE POLICY THIS IMPLEMENTS

    SMTP_SAFE_FOR_AUTOMATIC_AMBIGUOUS_RETRY = NO

Once a submission may have crossed the remote-effect boundary and the result is
ambiguous (`jobs.common.eco_smtp_submission`), normal autonomous processing must
not submit that message again until an operator explicitly reconciles it.

WHY NO NEW STATUS VALUE, AND WHY THAT IS THE STRONGER CHOICE

The four `eco_*_email_send_log` tables already have exactly the state this
needs: `pending`. A `pending` normal row is what `reserve_send()` refuses to
reserve over, and it is what the partial unique indexes
`uq_..._normal_identity` / `uq_..._normal_idempotency`
(`db/client_business/044_eco_email_fail_closed_idempotency.sql`) hold the
identity slot with — so a duplicate reservation is refused by PostgreSQL and
not only by application logic.

An ambiguous submission therefore does NOT move the row. It STAYS the
reservation it already was, and gains a durable marker saying why it will never
progress on its own. That means:

  * no status CHECK constraint changes on four applied migrations;
  * no partial-index predicate changes, so the DB-level duplicate refusal that
    already exists keeps covering the ambiguous case unchanged;
  * `failed` keeps its single, honest meaning — "definitely not submitted,
    safe to retry" — instead of becoming a value whose safety depends on a
    second column.

What the marker adds is the distinction `pending` alone cannot make: an
ordinary pending row is a run that has not finished yet, while an ambiguous one
is a run that finished without learning the answer. Only the second one needs a
human, and only the second one must survive `force_resend`.

WHAT AN OPERATOR CAN DO WITH IT

`resolve_ambiguous_send()` is the explicit, deterministic exit, and it takes an
attestation rather than a guess:

    delivered      the operator established the message DID reach the driver
                   (Sent-folder archive, provider log, the recipient). The row
                   becomes `sent`, so no rerun will send it again.
    not_delivered  the operator established it did NOT. The row becomes
                   `failed`, which is retryable, and the next normal run mails
                   the driver exactly once.

Both record who decided, when, and on what basis. Neither is reachable from an
automatic run: nothing in the job path calls this function.

`ops/reconcile_eco_email_ambiguous_send.py` is the operator entry point.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional

#: Reservation outcome. Distinct from `STALE_PENDING_REQUIRES_RECONCILIATION`
#: on purpose: a stale pending row is a run that may simply have died before
#: sending, while this one is a row whose message may already be in a driver's
#: inbox.
AMBIGUOUS_SUBMISSION_REQUIRES_RECONCILIATION = (
    "AMBIGUOUS_SUBMISSION_REQUIRES_RECONCILIATION"
)

#: Keys written into the send log's `metadata_json`. Namespaced so they cannot
#: collide with the job's own diagnostic metadata.
KEY_RESULT = "smtp_submission_result"
KEY_PHASE = "smtp_submission_phase"
KEY_DETAIL = "smtp_submission_detail"
KEY_MARKED_AT = "smtp_submission_ambiguous_at"
KEY_REQUIRES_OPERATOR = "requires_operator_reconciliation"
KEY_RESOLUTION = "smtp_submission_reconciliation"

VALUE_AMBIGUOUS = "AMBIGUOUS"

RESOLUTION_DELIVERED = "delivered"
RESOLUTION_NOT_DELIVERED = "not_delivered"
RESOLUTIONS = (RESOLUTION_DELIVERED, RESOLUTION_NOT_DELIVERED)

#: A SQL predicate, reused everywhere the question is asked, so the reservation
#: guard, the pre-flight eligibility check and the operator listing can never
#: drift apart. It names no table, so it composes with any of the four.
UNRESOLVED_AMBIGUOUS_PREDICATE = (
    "send_scope = 'normal' AND status = 'pending' "
    f"AND metadata_json ->> '{KEY_RESULT}' = '{VALUE_AMBIGUOUS}'"
)

_IDENT = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*(\.[a-zA-Z_][a-zA-Z0-9_]*)?$")


def _identifier(value: str, *, label: str) -> str:
    if not _IDENT.match(value or ""):
        raise ValueError(f"Unsafe SQL identifier for {label}: {value!r}")
    return value


@dataclass(frozen=True)
class AmbiguousSend:
    """One unresolved ambiguous submission, as an operator would read it."""

    send_log_id: str
    status: str
    phase: Optional[str]
    detail: Optional[str]
    marked_at: Optional[str]

    def audit(self) -> dict:
        return {
            "ambiguous_send_log_id": self.send_log_id,
            "ambiguous_submission_phase": self.phase,
            "ambiguous_submission_marked_at": self.marked_at,
        }


def _value(row: Any, key: str, position: int) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    return row[position]


def mark_send_ambiguous(
    cur,
    *,
    send_log_table: str,
    send_log_id: str,
    phase: str,
    detail: str,
    error_message: str,
) -> None:
    """The submission may have been accepted. Freeze the reservation, honestly.

    THE ROW DOES NOT MOVE. It stays `pending`, which is what already blocks a
    normal reservation and what already holds the identity slot in the partial
    unique indexes, and it gains the marker that tells the next run — and an
    operator — that this is not a reservation still in progress but one whose
    outcome was never learned.

    `sent_at` stays NULL because nothing was established. `status` is not
    downgraded to `failed` because `failed` means "safe to send again", and that
    is precisely what this host cannot claim.
    """
    table = _identifier(send_log_table, label="send_log_table")
    marker = {
        KEY_RESULT: VALUE_AMBIGUOUS,
        KEY_PHASE: str(phase),
        KEY_DETAIL: str(detail)[:500],
        KEY_REQUIRES_OPERATOR: True,
    }
    cur.execute(
        f"""
        UPDATE {table}
           SET error_message = %s,
               sent_at = NULL,
               metadata_json = COALESCE(metadata_json, '{{}}'::jsonb)
                 || %s::jsonb
                 || jsonb_build_object(
                      '{KEY_MARKED_AT}',
                      to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')),
               updated_at = NOW()
         WHERE send_log_id = %s::uuid
        """,
        (error_message[:1000], json.dumps(marker, ensure_ascii=False), send_log_id),
    )


def unresolved_ambiguous_send(
    cur,
    *,
    send_log_table: str,
    subject_column: str,
    client_id: Any,
    subject_value: Any,
    report_type: str,
    period_start_date: Any,
    period_end_date: Any,
) -> Optional[AmbiguousSend]:
    """Is there an unresolved ambiguous submission for this logical delivery?

    Asked BEFORE the dashboard capability is published or rotated and again
    inside `reserve_send()`. The first call is what keeps a run from performing
    remote publication work for a message it is not allowed to send; the second
    is what makes the refusal correct under concurrency.
    """
    table = _identifier(send_log_table, label="send_log_table")
    column = _identifier(subject_column, label="subject_column")
    cur.execute(
        f"""
        SELECT send_log_id::text AS send_log_id,
               status AS status,
               metadata_json ->> '{KEY_PHASE}' AS submission_phase,
               metadata_json ->> '{KEY_DETAIL}' AS submission_detail,
               metadata_json ->> '{KEY_MARKED_AT}' AS marked_at
          FROM {table}
         WHERE client_id = %s
           AND {column} = %s
           AND report_type = %s
           AND period_start_date = %s
           AND period_end_date = %s
           AND {UNRESOLVED_AMBIGUOUS_PREDICATE}
         ORDER BY attempted_at DESC
         LIMIT 1
        """,
        (client_id, subject_value, report_type, period_start_date, period_end_date),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return AmbiguousSend(
        send_log_id=_value(row, "send_log_id", 0),
        status=_value(row, "status", 1),
        phase=_value(row, "submission_phase", 2),
        detail=_value(row, "submission_detail", 3),
        marked_at=_value(row, "marked_at", 4),
    )


def resolve_ambiguous_send(
    cur,
    *,
    send_log_table: str,
    send_log_id: str,
    resolution: str,
    operator: str,
    reason: str,
    smtp_message_id: Optional[str] = None,
) -> bool:
    """The EXPLICIT operator exit. Never reachable from an automatic run.

    Returns `True` when a row was actually resolved. A row that is not an
    unresolved ambiguous submission is left completely alone and `False` is
    returned — resolving something that was never ambiguous is not a
    reconciliation, it is a state edit.

    `delivered` and `not_delivered` are attestations about the WORLD, which is
    the only kind of statement that can end this safely: the host genuinely
    does not know, so something outside it has to say.
    """
    table = _identifier(send_log_table, label="send_log_table")
    if resolution not in RESOLUTIONS:
        raise ValueError(f"unsupported resolution: {resolution!r}")
    if not str(operator or "").strip():
        raise ValueError("an operator identity is required")
    if not str(reason or "").strip():
        raise ValueError("a reconciliation reason is required")

    record = {
        "resolution": resolution,
        "operator": str(operator).strip()[:200],
        "reason": str(reason).strip()[:500],
    }
    if resolution == RESOLUTION_DELIVERED:
        assignments = (
            "status = 'sent', "
            "sent_at = NOW(), "
            "smtp_message_id = COALESCE(smtp_message_id, %s), "
            "provider_response = 'operator reconciliation: the ambiguous SMTP "
            "submission was established as delivered', "
            "error_message = NULL"
        )
        params: tuple = (smtp_message_id,)
    else:
        assignments = (
            "status = 'failed', "
            "sent_at = NULL, "
            "error_message = 'operator reconciliation: the ambiguous SMTP "
            "submission was established as NOT delivered'"
        )
        params = ()

    cur.execute(
        f"""
        UPDATE {table}
           SET {assignments},
               metadata_json = COALESCE(metadata_json, '{{}}'::jsonb)
                 || jsonb_build_object(
                      '{KEY_REQUIRES_OPERATOR}', false,
                      '{KEY_RESULT}', 'RECONCILED',
                      '{KEY_RESOLUTION}', %s::jsonb
                        || jsonb_build_object(
                             'resolved_at',
                             to_char(NOW() AT TIME ZONE 'UTC',
                                     'YYYY-MM-DD"T"HH24:MI:SS"Z"'))),
               updated_at = NOW()
         WHERE send_log_id = %s::uuid
           AND {UNRESOLVED_AMBIGUOUS_PREDICATE}
        """,
        (*params, json.dumps(record, ensure_ascii=False), send_log_id),
    )
    return bool(cur.rowcount)


def list_unresolved_ambiguous_sends(cur, *, send_log_table: str,
                                    subject_column: str,
                                    limit: int = 200) -> list[dict]:
    """Everything a human has to decide about, for one send log. Read-only."""
    table = _identifier(send_log_table, label="send_log_table")
    column = _identifier(subject_column, label="subject_column")
    cur.execute(
        f"""
        SELECT send_log_id::text AS send_log_id,
               client_id::text AS client_id,
               {column}::text AS subject_identity,
               report_type,
               period_start_date::text AS period_start_date,
               period_end_date::text AS period_end_date,
               attempted_at,
               metadata_json ->> '{KEY_PHASE}' AS submission_phase,
               metadata_json ->> '{KEY_DETAIL}' AS submission_detail,
               metadata_json ->> '{KEY_MARKED_AT}' AS marked_at
          FROM {table}
         WHERE {UNRESOLVED_AMBIGUOUS_PREDICATE}
         ORDER BY attempted_at DESC
         LIMIT %s
        """,
        (int(limit),),
    )
    return [dict(row) for row in cur.fetchall()]
