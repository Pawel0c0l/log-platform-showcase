#!/usr/bin/env python3
"""Read-only inspection of suspected_bug incidents, occurrences and email outbox.

Never sends, retries or mutates anything: every statement runs in a read-only
transaction. Recipient addresses are redacted unless `--show-recipients` is given.

Examples:
  python3 ops/inspect_suspected_bugs.py --open
  python3 ops/inspect_suspected_bugs.py --incident-id <uuid> --occurrences
  python3 ops/inspect_suspected_bugs.py --fingerprint <sha256>
  python3 ops/inspect_suspected_bugs.py --outbox pending --outbox retry --outbox dead_letter
  python3 ops/inspect_suspected_bugs.py --delivery-history --client-code ALPHA00001 --since 2026-07-01
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api.suspected_bug import platform_db_conn, redact_email  # noqa: E402

OUTBOX_STATUSES = ("pending", "sending", "sent", "retry", "dead_letter", "suppressed")


def _load_dotenv() -> None:
    path = REPO_ROOT / ".env"
    if not path.exists():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(path, override=False)


def _parse_date(value: str | None, name: str) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise SystemExit(f"{name} must be an ISO date or timestamp")
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _incident_filters(args) -> tuple[list[str], list]:
    where, params = [], []
    if args.incident_id:
        where.append("i.incident_id = %s::uuid")
        params.append(args.incident_id)
    if args.fingerprint:
        where.append("i.fingerprint = %s")
        params.append(args.fingerprint)
    if args.client_code:
        where.append("i.client_code = %s")
        params.append(args.client_code)
    if args.incident_code:
        where.append("i.incident_code = %s")
        params.append(args.incident_code)
    if args.since:
        where.append("i.last_seen_at >= %s")
        params.append(_parse_date(args.since, "--since"))
    if args.until:
        where.append("i.last_seen_at <= %s")
        params.append(_parse_date(args.until, "--until"))
    return where, params


def query_incidents(cur, args) -> list[dict]:
    where, params = _incident_filters(args)
    if args.open_only and not (args.incident_id or args.fingerprint):
        where.append("i.state = 'open'")
    sql = """
        SELECT i.incident_id::text, i.fingerprint, i.classification, i.incident_code, i.title,
               i.severity, i.state, i.environment, i.component, i.client_code, i.client_id::text,
               i.first_seen_at, i.last_seen_at, i.occurrence_count, i.latest_log_id,
               i.latest_run_id::text, i.last_email_enqueued_at, i.last_email_sent_at, i.resolved_at
        FROM suspected_bug_incidents AS i
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY i.last_seen_at DESC, i.incident_id LIMIT %s"
    cur.execute(sql, [*params, args.limit])
    return [dict(row) for row in cur.fetchall()]


def query_occurrences(cur, args) -> list[dict]:
    where, params = _incident_filters(args)
    sql = """
        SELECT o.occurrence_id::text, o.incident_id::text, i.incident_code, i.client_code,
               o.occurrence_no, o.occurred_at, o.log_id, o.run_id::text, o.component,
               o.email_decision, o.email_decision_reason
        FROM suspected_bug_occurrences AS o
        JOIN suspected_bug_incidents AS i ON i.incident_id = o.incident_id
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY o.occurred_at DESC, o.occurrence_id LIMIT %s"
    cur.execute(sql, [*params, args.limit])
    return [dict(row) for row in cur.fetchall()]


def query_outbox(cur, args, *, statuses: list[str], show_recipients: bool) -> list[dict]:
    where, params = _incident_filters(args)
    where.append("b.status = ANY(%s)")
    params.append(statuses)
    sql = """
        SELECT b.outbox_id::text, b.incident_id::text, i.incident_code, i.client_code,
               b.notification_key, b.notification_reason, b.recipient_config_ref,
               b.recipients, b.subject, b.status, b.attempts, b.max_attempts,
               b.available_at, b.claimed_at, b.sent_at, b.last_error, b.provider_message_id,
               b.created_at, b.updated_at
        FROM suspected_bug_email_outbox AS b
        JOIN suspected_bug_incidents AS i ON i.incident_id = b.incident_id
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY b.created_at DESC, b.outbox_id LIMIT %s"
    cur.execute(sql, [*params, args.limit])

    rows = []
    for row in cur.fetchall():
        item = dict(row)
        recipients = list(item.get("recipients") or [])
        item["recipients"] = recipients if show_recipients else [redact_email(a) for a in recipients]
        rows.append(item)
    return rows


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only suspected_bug incident, occurrence and outbox inspection."
    )
    parser.add_argument("--open", dest="open_only", action="store_true",
                        help="Only open incidents (default when no other view is selected)")
    parser.add_argument("--incident-id", help="Inspect one incident by id")
    parser.add_argument("--fingerprint", help="Inspect one incident by fingerprint")
    parser.add_argument("--occurrences", action="store_true", help="Include recent occurrences")
    parser.add_argument("--outbox", action="append", choices=OUTBOX_STATUSES,
                        help="Include outbox rows with this status (repeatable)")
    parser.add_argument("--delivery-history", action="store_true",
                        help="Include sent and dead-lettered outbox rows")
    parser.add_argument("--client-code", help="Filter by client code")
    parser.add_argument("--incident-code", help="Filter by incident code")
    parser.add_argument("--since", help="Only incidents last seen at/after this ISO date")
    parser.add_argument("--until", help="Only incidents last seen at/before this ISO date")
    parser.add_argument("--limit", type=int, default=50, help="Row cap per section (default 50)")
    parser.add_argument("--show-recipients", action="store_true",
                        help="Show full recipient addresses instead of redacted ones")
    return parser


def main() -> int:
    _load_dotenv()
    args = build_parser().parse_args()
    if args.limit < 1 or args.limit > 1000:
        raise SystemExit("--limit must be between 1 and 1000")

    want_outbox = list(args.outbox or [])
    if args.delivery_history:
        want_outbox = sorted(set(want_outbox) | {"sent", "dead_letter"})
    if not (args.occurrences or want_outbox or args.incident_id or args.fingerprint):
        args.open_only = True

    output: dict = {
        "inspection_only": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "recipients_redacted": not args.show_recipients,
        "filters": {
            "open_only": bool(args.open_only),
            "incident_id": args.incident_id,
            "fingerprint": args.fingerprint,
            "client_code": args.client_code,
            "incident_code": args.incident_code,
            "since": args.since,
            "until": args.until,
            "limit": args.limit,
        },
    }

    conn = platform_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            output["incidents"] = query_incidents(cur, args)
            if args.occurrences:
                output["occurrences"] = query_occurrences(cur, args)
            if want_outbox:
                output["outbox"] = query_outbox(cur, args, statuses=want_outbox,
                                                show_recipients=args.show_recipients)
        conn.rollback()
    finally:
        conn.close()

    print(json.dumps(output, indent=2, sort_keys=True, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
