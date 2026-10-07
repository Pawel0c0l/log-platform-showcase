#!/usr/bin/env python3
"""Re-arm ONE reviewed Driver Eco Dashboard delivery: OPERATOR_REQUIRED -> PREPARED.

WHAT THIS IS FOR, AND WHAT IT IS NOT

A dashboard publication that the Worker REFUSED never created anything remote.
`POST /api/publish` computes the digest, decodes strictly, validates the
snapshot against the v1 allowlist and checks canonical form — and every one of
those refusals happens before `publishSnapshot`, so no publication row, no R2
object and no capability exist. What remains is a host row parked in
`OPERATOR_REQUIRED` describing an attempt that is no longer the current
situation.

When the cause of that refusal has been fixed OUTSIDE the delivery — a Worker
defect, not a defect in these bytes — the row is still exactly the `PREPARED`
operation it was before the attempt. This command says so, once, under a human
attestation, and lets the ordinary lifecycle re-issue the SAME idempotent
publish of the SAME bytes under the SAME operation id.

It is not a generic recovery framework. It performs one transition, for one
delivery, named explicitly, and only when every persisted fact proves no remote
effect was ever created. See `OPERATOR_RECOVERY_GUARDS` in
`jobs/ecodriving_dashboard/delivery_ledger.py` for the full list; every guard is
a predicate of the same UPDATE that writes.

WHAT IT NEVER DOES

It contacts no Worker, publishes nothing, mints no capability, sends no e-mail,
touches no provider and reverses no remote fact. It does not infer remote
cleanup: it refuses to act unless the host's own durable state proves there was
never anything remote to clean up. It changes no identity — not the delivery id,
the operation id, the client or driver identity, the period, the send scope, the
payload or snapshot digest, the recipient binding or the bearer generation.

It also does not change WHO OWNS THE SEND. A delivery whose `external_mailer`
names an Eco weekly/monthly notification job is re-armed as that same
externally-owned delivery: ownership is bound by the INSERT, immutable under
migration 050's guard trigger, and absent from this command's SET list. Being
owned is not evidence that the owner did anything — the capability guards are
what prove the link was never handed over — so it does not, by itself, make a
delivery unrecoverable.

USAGE

    # dry run — the default, writes nothing
    python3 ops/recover_eco_dashboard_operator_required_delivery.py \\
      --client-code BRAVO00016 \\
      --delivery-id 6bcc43f3-e3b0-408f-88a8-c106b5239df8

    # execute — every flag mandatory, nothing inferred
    python3 ops/recover_eco_dashboard_operator_required_delivery.py \\
      --client-code BRAVO00016 \\
      --delivery-id 6bcc43f3-e3b0-408f-88a8-c106b5239df8 \\
      --expect-operation-id <operation_id> \\
      --expect-payload-digest <payload_digest> \\
      --operator "<who>" --reason "<on what evidence>" \\
      --execute

The two `--expect-*` values come from the dry run. They are asserted in the
UPDATE's own WHERE clause, so an operator who has the wrong delivery in mind is
refused rather than silently re-arming a different one.

Exit codes: 0 recovered or reported, 2 no such client, 3 no such delivery,
4 not eligible, 5 refused at the write (the row changed under the dry run).
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
from jobs.ecodriving_dashboard.delivery_contract import (
    OPERATOR_RECOVERABLE_PUBLICATION_FAILURE_CODES,
    DeliveryState,
)
from jobs.ecodriving_dashboard.delivery_ledger import (
    OPERATOR_RECOVERY_PHASE,
    DeliveryLedger,
)

#: Everything an operator needs to decide, and nothing that must not be printed.
#: `DeliveryRecord.public_summary()` already excludes the raw bearer and the
#: recipient address; this command adds nothing to it.
OUTCOME_DRY_RUN = "DRY_RUN"
OUTCOME_RECOVERED = "RECOVERED"
OUTCOME_NOT_ELIGIBLE = "NOT_ELIGIBLE"
OUTCOME_REFUSED_AT_WRITE = "REFUSED_AT_WRITE"
OUTCOME_UNKNOWN_DELIVERY = "UNKNOWN_DELIVERY"


def _client_row(pc, client_code: str):
    return pc.execute(
        """SELECT client_id::text, client_code, client_db_host, client_db_port,
                  client_db_name, client_db_user, client_db_password_secret_ref
             FROM workflow_a_control.client_account
            WHERE client_code = %s AND enabled IS TRUE""",
        (client_code,),
    ).fetchone()


def _connect_client(row):
    return connect(host=row["client_db_host"], port=row["client_db_port"],
                   dbname=row["client_db_name"], user=row["client_db_user"],
                   password=resolve_secret(row["client_db_password_secret_ref"]),
                   row_factory=dict_row, autocommit=True)


def dry_run(ledger: DeliveryLedger, delivery_id: str) -> dict:
    """The default. Reads one row, evaluates every guard, writes nothing."""
    record = ledger.load(delivery_id)
    if record is None:
        return {"outcome": OUTCOME_UNKNOWN_DELIVERY, "delivery_id": str(delivery_id)}
    assessment = ledger.assess_operator_recovery(delivery_id)
    return {
        "outcome": OUTCOME_DRY_RUN,
        "recoverable": assessment.recoverable,
        "would_transition": {"from": record.state, "to": DeliveryState.PREPARED}
        if assessment.recoverable else None,
        "assessment": assessment.as_dict(),
        "delivery": record.public_summary(),
        "recoverable_failure_codes": sorted(OPERATOR_RECOVERABLE_PUBLICATION_FAILURE_CODES),
        "recoverable_failure_phase": OPERATOR_RECOVERY_PHASE,
        "wrote_nothing": True,
    }


def execute(ledger: DeliveryLedger, delivery_id: str, *, operator: str, reason: str,
            expected_operation_id: str, expected_payload_digest: str) -> dict:
    """Re-arm the delivery, or refuse and write nothing.

    The dry run is repeated first so a refusal can NAME the disqualifying fact;
    the write is still guarded independently, so the report is an explanation
    and never the authority.
    """
    report = dry_run(ledger, delivery_id)
    if report["outcome"] == OUTCOME_UNKNOWN_DELIVERY:
        return report
    before = report["delivery"]
    if not report["recoverable"]:
        report["outcome"] = OUTCOME_NOT_ELIGIBLE
        return report

    recovered = ledger.operator_recover_to_prepared(
        delivery_id, operator=operator, reason=reason,
        expected_operation_id=expected_operation_id,
        expected_payload_digest=expected_payload_digest)
    if recovered is None:
        # The row stopped being eligible between the read and the write, or the
        # operator named an operation id / digest this delivery does not carry.
        report["outcome"] = OUTCOME_REFUSED_AT_WRITE
        report["wrote_nothing"] = True
        return report

    after = recovered.public_summary()
    return {
        "outcome": OUTCOME_RECOVERED,
        "transition": {"from": before["state"], "to": after["state"]},
        "operator": operator,
        "delivery": after,
        # The evidence the recovery preserved what it must preserve.
        "identity_preserved": {
            "delivery_id": before["delivery_id"] == after["delivery_id"],
            "operation_id": before["operation_id"] == after["operation_id"],
            "payload_digest": before["payload_digest"] == after["payload_digest"],
            # Send-accounting ownership, reported alongside identity because a
            # recovery that silently re-homed a delivery would be the one way
            # this command could produce a second message to a driver.
            "external_mailer": before["external_mailer"] == after["external_mailer"],
            "recipient_identity": before["recipient_identity"] == after["recipient_identity"],
            "bearer_generation": before["bearer_generation"] == after["bearer_generation"],
            "period": (before["period_type"], before["period_start_date"],
                       before["period_end_date"])
            == (after["period_type"], after["period_start_date"],
                after["period_end_date"]),
        },
        "next_action": after["next_action"],
    }


EXIT_BY_OUTCOME = {
    OUTCOME_DRY_RUN: 0,
    OUTCOME_RECOVERED: 0,
    OUTCOME_UNKNOWN_DELIVERY: 3,
    OUTCOME_NOT_ELIGIBLE: 4,
    OUTCOME_REFUSED_AT_WRITE: 5,
}


def main() -> int:
    load_dotenv(ROOT / ".env", override=False)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-code", required=True)
    parser.add_argument("--delivery-id", required=True)
    parser.add_argument("--execute", action="store_true",
                        help="perform the transition; the default is a dry run")
    parser.add_argument("--expect-operation-id", help="required with --execute")
    parser.add_argument("--expect-payload-digest", help="required with --execute")
    parser.add_argument("--operator", help="required with --execute; who is attesting")
    parser.add_argument("--reason", help="required with --execute; on what evidence")
    args = parser.parse_args()

    if args.execute and not (args.operator and args.reason
                             and args.expect_operation_id and args.expect_payload_digest):
        parser.error("--execute requires --expect-operation-id, --expect-payload-digest, "
                     "--operator and --reason")

    with connect(host=os.environ["POSTGRES_HOST"], port=int(os.environ["POSTGRES_PORT"]),
                 dbname=os.environ["POSTGRES_DB"], user=os.environ["POSTGRES_USER"],
                 password=os.getenv("POSTGRES_PASSWORD", ""),
                 row_factory=dict_row) as pc:
        client = _client_row(pc, args.client_code)
        pc.rollback()
        if client is None:
            print(json.dumps({"outcome": "NO_ENABLED_CLIENT_MATCHED",
                              "client_code": args.client_code}))
            return 2
        with _connect_client(client) as cc:
            ledger = DeliveryLedger(cc)
            if args.execute:
                report = execute(
                    ledger, args.delivery_id, operator=args.operator, reason=args.reason,
                    expected_operation_id=args.expect_operation_id,
                    expected_payload_digest=args.expect_payload_digest)
            else:
                report = dry_run(ledger, args.delivery_id)

    report["client_code"] = client["client_code"]
    print(json.dumps(report, ensure_ascii=False, default=str, indent=2))
    return EXIT_BY_OUTCOME.get(report["outcome"], 1)


if __name__ == "__main__":
    raise SystemExit(main())
