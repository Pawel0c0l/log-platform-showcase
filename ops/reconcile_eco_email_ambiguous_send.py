#!/usr/bin/env python3
"""Operator entry point for AMBIGUOUS Eco Driving SMTP submissions.

WHAT AN AMBIGUOUS ROW IS

example.invalid SMTP accepted (or may have accepted) a message and this host never
learned the answer: the connection dropped during DATA, the final response
timed out, the socket died. The send log keeps the row as its own reservation,
marked as ambiguous, and no automatic run will ever submit it again — not a
normal run, and not a `--force-resend` one. See
`jobs/common/eco_email_reconciliation.py`.

Only a human can end that, because only a human can look outside this host:
the Sent-folder archive (BRAVO weekly preserves the exact MIME), the provider's
own logs, or the recipient.

    --list                       show every unresolved ambiguous row
    --resolve <send_log_id>
      --as delivered             the message DID reach the recipient
      --as not-delivered         it did NOT
      --operator <who>           who is attesting
      --reason <why>             on what evidence

`delivered` marks the row `sent`, so no rerun mails it again. `not-delivered`
marks it `failed`, which is retryable, so the next normal run mails it exactly
once. Both record the attestation in `metadata_json`.

SAFETY. Listing is read-only. A resolution writes exactly one row, only when
that row is still an unresolved ambiguous submission, and never sends anything:
this script opens no SMTP connection and constructs no message. Choosing a
resolution without evidence is the one thing it cannot protect against, which
is why `--operator` and `--reason` are mandatory and stored.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from psycopg import connect
from psycopg.rows import dict_row

from jobs.api.telematics.secret_resolver import resolve_secret
from jobs.common.eco_email_reconciliation import (
    RESOLUTION_DELIVERED,
    RESOLUTION_NOT_DELIVERED,
    list_unresolved_ambiguous_sends,
    resolve_ambiguous_send,
)

#: The four Eco send logs and the subject column each one is keyed by.
SEND_LOGS = (
    ("eco_driving_weekly_email_send_log", "assigned_id"),
    ("eco_driving_monthly_email_send_log", "assigned_id"),
    ("eco_person_weekly_email_send_log", "person_name_group_key"),
    ("eco_person_monthly_email_send_log", "person_name_group_key"),
)

RESOLUTION_BY_FLAG = {
    "delivered": RESOLUTION_DELIVERED,
    "not-delivered": RESOLUTION_NOT_DELIVERED,
}


def _client_rows(pc, client_code: str | None):
    sql = """SELECT client_id::text, client_code, client_db_host, client_db_port,
                    client_db_name, client_db_user, client_db_password_secret_ref
               FROM workflow_a_control.client_account
              WHERE enabled IS TRUE"""
    params: tuple = ()
    if client_code:
        sql += " AND client_code = %s"
        params = (client_code,)
    return pc.execute(sql + " ORDER BY client_code", params).fetchall()


def _connect_client(row):
    return connect(host=row["client_db_host"], port=row["client_db_port"],
                   dbname=row["client_db_name"], user=row["client_db_user"],
                   password=resolve_secret(row["client_db_password_secret_ref"]),
                   row_factory=dict_row)


def _table_exists(conn, table: str) -> bool:
    found = conn.execute("SELECT to_regclass(%s) AS t", (f"public.{table}",)).fetchone()
    return bool(found and found["t"])


def main() -> int:
    load_dotenv(ROOT / ".env", override=False)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-code")
    parser.add_argument("--list", action="store_true",
                        help="read-only listing of unresolved ambiguous rows")
    parser.add_argument("--resolve", metavar="SEND_LOG_ID")
    parser.add_argument("--as", dest="resolution", choices=sorted(RESOLUTION_BY_FLAG))
    parser.add_argument("--send-log-table",
                        choices=[table for table, _ in SEND_LOGS],
                        help="required with --resolve")
    parser.add_argument("--operator")
    parser.add_argument("--reason")
    parser.add_argument("--smtp-message-id",
                        help="optional; the Message-ID established for a "
                             "'delivered' resolution")
    args = parser.parse_args()

    if not args.list and not args.resolve:
        parser.error("choose --list or --resolve")
    if args.resolve and not (args.resolution and args.operator and args.reason
                             and args.send_log_table and args.client_code):
        parser.error("--resolve requires --client-code, --send-log-table, --as, "
                     "--operator and --reason")

    out: dict = {"unresolved": [], "resolved": None}
    with connect(host=os.environ["POSTGRES_HOST"], port=int(os.environ["POSTGRES_PORT"]),
                 dbname=os.environ["POSTGRES_DB"], user=os.environ["POSTGRES_USER"],
                 password=os.getenv("POSTGRES_PASSWORD", ""),
                 row_factory=dict_row) as pc:
        clients = _client_rows(pc, args.client_code)
        if not clients:
            print(json.dumps({"error": "NO_ENABLED_CLIENT_MATCHED"}))
            return 2
        for client in clients:
            with _connect_client(client) as cc:
                if args.list:
                    cc.execute("SET TRANSACTION READ ONLY")
                    with cc.cursor() as cur:
                        for table, subject_column in SEND_LOGS:
                            if not _table_exists(cc, table):
                                continue
                            for row in list_unresolved_ambiguous_sends(
                                    cur, send_log_table=f"public.{table}",
                                    subject_column=subject_column):
                                out["unresolved"].append(
                                    {"client_code": client["client_code"],
                                     "send_log_table": table, **row})
                    cc.rollback()
                    continue

                with cc.cursor() as cur:
                    done = resolve_ambiguous_send(
                        cur,
                        send_log_table=f"public.{args.send_log_table}",
                        send_log_id=args.resolve,
                        resolution=RESOLUTION_BY_FLAG[args.resolution],
                        operator=args.operator,
                        reason=args.reason,
                        smtp_message_id=args.smtp_message_id,
                    )
                if not done:
                    cc.rollback()
                    print(json.dumps({"error": "NOT_AN_UNRESOLVED_AMBIGUOUS_SEND",
                                      "send_log_id": args.resolve}))
                    return 3
                cc.commit()
                out["resolved"] = {"client_code": client["client_code"],
                                   "send_log_table": args.send_log_table,
                                   "send_log_id": args.resolve,
                                   "resolution": RESOLUTION_BY_FLAG[args.resolution],
                                   "operator": args.operator}
        pc.rollback()
    print(json.dumps(out, ensure_ascii=False, default=str, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
