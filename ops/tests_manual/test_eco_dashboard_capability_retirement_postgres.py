#!/usr/bin/env python3
"""Driver Eco Dashboard V1 — retirement of expired capability authorization.

Run:
    ECO_DASHBOARD_RETIREMENT_TEST_DSN=postgresql://postgres:pw@127.0.0.1:55471/disposable \\
      python3 ops/tests_manual/test_eco_dashboard_capability_retirement_postgres.py

WHAT THIS PROVES

Capability lifetimes are period-scoped — weekly 10 days, monthly 60 — so expiry
is the ordinary end of every dashboard link rather than a rare corner. The
question this suite answers is what the HOST does about it:

  * the raw bearer of an expired grant is destroyed by the system's own
    lifecycle, WITHOUT anybody re-mailing that exact historical period;
  * a still-valid bearer is never touched, at any margin;
  * the sweep is idempotent, safe to interrupt, safe to repeat and safe to run
    concurrently with both another sweep and a live delivery;
  * an expired delivery stops being operationally open work, and stops being
    operator-actionable, without claiming a delivery that never happened;
  * the non-secret audit identity — which grant, which generation, when it
    stopped working, and any bound provider submission identity — survives;
  * a delivery that was published and then failed to send is retired once its
    grant expires, rather than holding secret material forever;
  * the ambiguous-SMTP contract is untouched: `PROVIDER_AMBIGUOUS` is never
    swept, keeps its bearer, and keeps its operator flag;
  * a resend of the SAME reporting period after retirement rotates through the
    explicit recovery operation and stays that period's delivery;
  * a grant issued under the OLD universal 45-day policy keeps the validity its
    recipient was given — neither shortened nor lengthened — and is retired by
    the same rule once it genuinely expires.

Every row is produced through the REAL ledger API, so the fixtures are the
shapes the publisher actually persists.

DESTRUCTIVE. It drops and recreates `public.eco_dashboard_delivery_operation`
in the database the DSN names, so it refuses any DSN that is not loopback.
A disposable instance is one command:

    docker run -d --rm --name eco-dash-retirement-pg -e POSTGRES_PASSWORD=disposable \\
      -e POSTGRES_DB=eco_retirement_test -p 127.0.0.1:55471:5432 \\
      --tmpfs /var/lib/postgresql/data postgres:16

No production database, no Worker, no Cloudflare resource, no e-mail, no
provider and no real capability is involved anywhere in this file.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.ecodriving_dashboard.delivery_contract import (  # noqa: E402
    BEARER_REQUIRED_STATES,
    CLOSED_STATES,
    LEGAL_TRANSITIONS,
    RETIRABLE_ON_EXPIRY_STATES,
    DeliveryIdentity,
    DeliveryState,
    NextAction,
    derive_message_fingerprint,
    derive_operation_id,
    derive_provider_backend_id,
    derive_subject_ref,
    safe_next_action,
)
from jobs.ecodriving_dashboard.delivery_ledger import (  # noqa: E402
    DeliveryLedger,
    capability_digest,
)
from ops.tests_manual.postgres_dsn_safety import require_loopback_dsn_or_exit  # noqa: E402

ENV = "ECO_DASHBOARD_RETIREMENT_TEST_DSN"
MIGRATIONS = tuple(
    REPO_ROOT / "db" / "client_business" / name
    for name in ("049_eco_dashboard_delivery_operation.sql",
                 "050_eco_dashboard_external_mailer_ownership.sql",
                 "051_eco_dashboard_capability_retirement.sql")
)
TABLE = "public.eco_dashboard_delivery_operation"

CLIENT_ID = "22222222-2222-2222-2222-222222222222"
RECIPIENT = "driver.retirement@example.invalid"
EXTERNAL_MAILER = "eco_driving_weekly_email_notifications"

#: Synthetic 43-character bearers. No grant is ever minted with either.
BEARER = "B" * 43
BEARER_TWO = "C" * 43
CAPABILITY_ID = "a" * 32

#: THE PERIOD-SCOPED LIFETIMES, as the Worker's `capability_ttl.js` defines
#: them. The numbers are asserted against the Worker itself by
#: `test_driver_eco_dashboard_capability_lifecycle.py`; here they only shape the
#: fixtures' expiries.
WEEKLY_TTL = timedelta(days=10)
MONTHLY_TTL = timedelta(days=60)
#: The universal lifetime every grant used to receive. Rows created under it
#: still exist and must keep exactly the validity they were given.
LEGACY_TTL = timedelta(days=45)

PASSED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name}: {detail}" if detail else name)


# --- fixtures -------------------------------------------------------------------


def connect(dsn: str, **kwargs):
    import psycopg
    from psycopg.rows import dict_row

    conn = psycopg.connect(dsn, row_factory=dict_row, **kwargs)
    conn.autocommit = True
    return conn


def build_schema(owner) -> None:
    with owner.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {TABLE} CASCADE")
        cur.execute("DROP TABLE IF EXISTS public.client_trips CASCADE")
        cur.execute("CREATE TABLE public.client_trips (id BIGSERIAL PRIMARY KEY)")
        for migration in MIGRATIONS:
            cur.execute(migration.read_text(encoding="utf-8"))


def drop_schema(owner) -> None:
    with owner.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {TABLE} CASCADE")
        cur.execute("DROP FUNCTION IF EXISTS "
                    "public.eco_dashboard_delivery_operation_guard() CASCADE")
        cur.execute("DROP TABLE IF EXISTS public.client_trips CASCADE")


def truncate(owner) -> None:
    with owner.cursor() as cur:
        cur.execute(f"TRUNCATE {TABLE}")


def identity_for(seed: str, *, period_type: str = "weekly") -> DeliveryIdentity:
    if period_type == "weekly":
        start, end = date(2026, 7, 13), date(2026, 7, 20)
    else:
        start, end = date(2026, 7, 1), date(2026, 8, 1)
    return DeliveryIdentity(
        client_id=CLIENT_ID, identity_key=f"DRIVER-{seed}", period_type=period_type,
        period_start_date=start, period_end_date=end, send_scope="normal")


def prepared(ledger: DeliveryLedger, seed: str, *, period_type: str = "weekly",
             external_mailer: str | None = None):
    identity = identity_for(seed, period_type=period_type)
    record, _created = ledger.ensure_operation(
        identity,
        operation_id=derive_operation_id(identity),
        subject_ref=derive_subject_ref(identity),
        payload_digest=("0" * 63 + "1"),
        recipient_email=RECIPIENT,
        recipient_identity=f"rcpt_{seed}",
        external_mailer=external_mailer)
    return record


def persisted(ledger: DeliveryLedger, seed: str, *, expires_at: datetime,
              period_type: str = "weekly", external_mailer: str | None = None,
              capability: str = BEARER, capability_id: str = CAPABILITY_ID,
              generation: int = 1):
    """A delivery holding a live raw bearer, exactly as `_publish` leaves it."""
    owner = f"probe-{seed}"
    record = prepared(ledger, seed, period_type=period_type,
                      external_mailer=external_mailer)
    claimed = ledger.claim(record.delivery_id, owner=owner)
    out = ledger.record_capability(
        claimed, owner=owner, capability=capability, capability_id=capability_id,
        expires_at=expires_at, bearer_generation=generation)
    return owner, out


def handed_off(ledger: DeliveryLedger, seed: str, *, expires_at: datetime,
               period_type: str = "weekly", release: bool = True):
    """THE production shape: the link was handed to the external Eco mailer."""
    owner, record = persisted(ledger, seed, expires_at=expires_at,
                              period_type=period_type,
                              external_mailer=EXTERNAL_MAILER)
    out = ledger.record_external_mailer_handoff(
        record, owner=owner, mailer=EXTERNAL_MAILER, run_id=f"run-{seed}")
    if release:
        ledger.release(out.delivery_id, owner=owner)
        return ledger.load(out.delivery_id)
    return out


def intent_recorded(ledger: DeliveryLedger, seed: str, *, expires_at: datetime):
    """Provider-owned, submission identity bound, nothing submitted yet."""
    owner, record = persisted(ledger, seed, expires_at=expires_at)
    operation_id = record.operation_id
    out = ledger.record_delivery_intent(
        record, owner=owner,
        provider_name="smtp",
        provider_backend_id=derive_provider_backend_id("smtp", "acct", "host:465"),
        message_fingerprint=derive_message_fingerprint(
            recipient_email=RECIPIENT, subject="Eco", message_id=f"<{seed}@x>",
            html_body="<p>x</p>", text_body="x"),
        bound_capability_id=record.capability_id,
        bound_bearer_generation=record.bearer_generation)
    ledger.release(out.delivery_id, owner=owner)
    assert operation_id
    return ledger.load(out.delivery_id)


def ambiguous(ledger: DeliveryLedger, seed: str, *, expires_at: datetime):
    """The one state a human must resolve, and the sweep must never touch."""
    record = intent_recorded(ledger, seed, expires_at=expires_at)
    owner = f"probe-amb-{seed}"
    claimed = ledger.claim(record.delivery_id, owner=owner)
    pending = ledger.record_submission_pending(claimed, owner=owner)
    out = ledger.record_provider_ambiguous(
        pending, owner=owner, failure_code="SMTP_OUTCOME_UNKNOWN",
        detail="the connection dropped after DATA")
    ledger.release(out.delivery_id, owner=owner)
    return ledger.load(out.delivery_id)


def rejected(ledger: DeliveryLedger, seed: str, *, expires_at: datetime):
    """A DEFINITE refusal: the provider created no message."""
    record = intent_recorded(ledger, seed, expires_at=expires_at)
    owner = f"probe-rej-{seed}"
    claimed = ledger.claim(record.delivery_id, owner=owner)
    pending = ledger.record_submission_pending(claimed, owner=owner)
    out = ledger.record_provider_rejected(
        pending, owner=owner, failure_code="SMTP_550", detail="mailbox unavailable",
        operator_required=True)
    ledger.release(out.delivery_id, owner=owner)
    return ledger.load(out.delivery_id)


def raw_row(conn, delivery_id) -> dict:
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM {TABLE} WHERE delivery_id = %s", (str(delivery_id),))
        return dict(cur.fetchone())


def past(days: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def future(delta: timedelta) -> datetime:
    return datetime.now(timezone.utc) + delta


# --- the contract itself --------------------------------------------------------


def test_the_new_state_is_terminal_without_being_a_delivery_claim(ctx) -> None:
    """`CAPABILITY_RETIRED` says one thing, and it is not "delivered"."""
    check("it has no next action for anybody",
          safe_next_action(DeliveryState.CAPABILITY_RETIRED)
          == NextAction.CAPABILITY_EXPIRED_NO_ACTION)
    check("it is not operational work", DeliveryState.CAPABILITY_RETIRED in CLOSED_STATES)
    check("and neither is FINALIZED, which is the only other closed state",
          CLOSED_STATES == frozenset({DeliveryState.FINALIZED,
                                      DeliveryState.CAPABILITY_RETIRED}))
    check("it never holds a bearer",
          DeliveryState.CAPABILITY_RETIRED not in BEARER_REQUIRED_STATES)
    check("a resend of the same period may still leave it",
          DeliveryState.BEARER_RECOVERY_REQUIRED
          in LEGAL_TRANSITIONS[DeliveryState.CAPABILITY_RETIRED])
    check("but it can never reach a provider submission",
          DeliveryState.DELIVERY_INTENT_RECORDED
          not in LEGAL_TRANSITIONS[DeliveryState.CAPABILITY_RETIRED]
          and DeliveryState.PROVIDER_SUBMISSION_PENDING
          not in LEGAL_TRANSITIONS[DeliveryState.CAPABILITY_RETIRED])
    check("and it can never become FINALIZED, which would claim a send",
          DeliveryState.FINALIZED
          not in LEGAL_TRANSITIONS[DeliveryState.CAPABILITY_RETIRED])
    # The states the sweep may touch, and the ones it must not.
    check("ambiguous submissions are not retirable",
          DeliveryState.PROVIDER_AMBIGUOUS not in RETIRABLE_ON_EXPIRY_STATES)
    check("neither is an in-flight or accepted submission",
          DeliveryState.PROVIDER_SUBMISSION_PENDING not in RETIRABLE_ON_EXPIRY_STATES
          and DeliveryState.PROVIDER_ACCEPTED not in RETIRABLE_ON_EXPIRY_STATES)
    check("nor an operator escalation",
          DeliveryState.OPERATOR_REQUIRED not in RETIRABLE_ON_EXPIRY_STATES)
    PASSED.append("the_new_state_is_terminal_without_being_a_delivery_claim")


# --- the sweep ------------------------------------------------------------------


def test_an_expired_bearer_is_destroyed_without_a_resend(ctx) -> None:
    """THE requirement. Nobody re-mails the period; the bearer still dies."""
    ledger, owner = ctx["ledger"], ctx["owner"]
    truncate(owner)
    record = handed_off(ledger, "expired", expires_at=past(1))
    before = raw_row(owner, record.delivery_id)
    check("the fixture really holds a raw bearer",
          before["capability_secret"] == BEARER)
    check("and it is really in the handed-off state",
          before["state"] == DeliveryState.EXTERNAL_MAILER_HANDOFF)

    retired = ledger.retire_expired_capabilities()
    check("exactly one delivery was retired", len(retired) == 1, str(len(retired)))

    after = raw_row(owner, record.delivery_id)
    check("the state is CAPABILITY_RETIRED",
          after["state"] == DeliveryState.CAPABILITY_RETIRED, after["state"])
    check("THE RAW BEARER IS GONE", after["capability_secret"] is None)
    check("and the moment it stopped existing is recorded",
          after["bearer_cleared_at"] is not None)
    check("no operator is asked to do anything",
          after["operator_action_required"] is False)
    check("no lease is left behind",
          after["lease_owner"] is None and after["lease_expires_at"] is None)
    check("nothing claims a delivery happened",
          after["finalized_at"] is None and after["remote_delivered_at"] is None
          and after["provider_message_id"] is None)

    # The audit identity survives.
    check("the grant identity survives", after["capability_id"] == CAPABILITY_ID)
    check("the digest survives, so WHICH grant is still provable",
          after["capability_digest"] == capability_digest(BEARER))
    check("the digest is not the bearer", after["capability_digest"] != BEARER)
    check("the expiry survives", after["capability_expires_at"] is not None)
    check("the generation survives", after["bearer_generation"] == 1)
    check("the external ownership survives",
          after["external_mailer"] == EXTERNAL_MAILER)
    check("the handoff record survives",
          "external_mailer_handoff" in after["metadata_json"])
    retirement = after["metadata_json"].get("capability_retirement")
    check("and a retirement record was written", isinstance(retirement, dict), str(retirement))
    check("naming why", retirement.get("reason") == "CAPABILITY_EXPIRED")
    check("and when", bool(retirement.get("retired_at")))
    check("the retirement record carries no secret",
          BEARER not in json.dumps(after["metadata_json"]))
    PASSED.append("an_expired_bearer_is_destroyed_without_a_resend")


def test_a_live_bearer_is_never_touched(ctx) -> None:
    ledger, owner = ctx["ledger"], ctx["owner"]
    truncate(owner)
    live = handed_off(ledger, "live", expires_at=future(WEEKLY_TTL))
    monthly = handed_off(ledger, "livemonth", expires_at=future(MONTHLY_TTL),
                         period_type="monthly")
    # One second of remaining validity is still validity.
    barely = handed_off(ledger, "barely",
                        expires_at=datetime.now(timezone.utc) + timedelta(seconds=30))
    persisted_live = persisted(ledger, "persistlive", expires_at=future(WEEKLY_TTL))[1]

    retired = ledger.retire_expired_capabilities()
    check("nothing was retired", retired == [], str([r.state for r in retired]))
    for label, record in (("weekly", live), ("monthly", monthly),
                          ("barely-live", barely), ("persisted", persisted_live)):
        row = raw_row(owner, record.delivery_id)
        check(f"the {label} bearer is intact", row["capability_secret"] == BEARER, label)
        check(f"the {label} state is unchanged", row["state"] == record.state, label)
        check(f"the {label} row was not even written",
              row["bearer_cleared_at"] is None, label)
    PASSED.append("a_live_bearer_is_never_touched")


def test_an_unknown_expiry_is_not_evidence_of_expiry(ctx) -> None:
    """Not knowing when a grant dies is not knowing that it has."""
    ledger, owner = ctx["ledger"], ctx["owner"]
    truncate(owner)
    record = handed_off(ledger, "noexpiry", expires_at=future(WEEKLY_TTL))
    with owner.cursor() as cur:
        # Only reachable by corruption; the protocol always states an expiry.
        cur.execute(f"UPDATE {TABLE} SET capability_expires_at = NULL "
                    f"WHERE delivery_id = %s", (str(record.delivery_id),))
    retired = ledger.retire_expired_capabilities()
    check("a row with no known expiry is left alone", retired == [])
    row = raw_row(owner, record.delivery_id)
    check("and it keeps its bearer rather than being guessed at",
          row["capability_secret"] == BEARER)
    check("so it stays visible as open work",
          row["state"] == DeliveryState.EXTERNAL_MAILER_HANDOFF)
    PASSED.append("an_unknown_expiry_is_not_evidence_of_expiry")


def test_the_sweep_is_idempotent_and_safe_to_repeat(ctx) -> None:
    ledger, owner = ctx["ledger"], ctx["owner"]
    truncate(owner)
    records = [handed_off(ledger, f"idem{i}", expires_at=past(i + 1)) for i in range(4)]

    first = ledger.retire_expired_capabilities()
    check("the first pass retires all four", len(first) == 4, str(len(first)))
    cleared = {str(r.delivery_id): raw_row(owner, r.delivery_id)["bearer_cleared_at"]
               for r in records}
    updated = {str(r.delivery_id): raw_row(owner, r.delivery_id)["updated_at"]
               for r in records}

    for pass_number in (2, 3):
        again = ledger.retire_expired_capabilities()
        check(f"pass {pass_number} retires nothing", again == [], str(len(again)))
    for record in records:
        row = raw_row(owner, record.delivery_id)
        check("the state is stable", row["state"] == DeliveryState.CAPABILITY_RETIRED)
        check("and the audit timestamps were NOT rewritten",
              row["bearer_cleared_at"] == cleared[str(record.delivery_id)]
              and row["updated_at"] == updated[str(record.delivery_id)],
              "a repeat sweep must not move the moment the secret stopped existing")
    PASSED.append("the_sweep_is_idempotent_and_safe_to_repeat")


def test_a_bounded_batch_makes_progress_and_leaves_the_rest_intact(ctx) -> None:
    """An interrupted sweep is a sweep that did less, never one that half-did a row."""
    ledger, owner = ctx["ledger"], ctx["owner"]
    truncate(owner)
    for i in range(5):
        handed_off(ledger, f"batch{i}", expires_at=past(i + 1))

    first = ledger.retire_expired_capabilities(limit=2)
    check("the batch is respected", len(first) == 2, str(len(first)))
    for record in first:
        check("each retired row is complete, not partial",
              record.state == DeliveryState.CAPABILITY_RETIRED
              and record.capability_secret is None
              and record.row["bearer_cleared_at"] is not None)
    with owner.cursor() as cur:
        cur.execute(f"SELECT state, count(*) AS n FROM {TABLE} GROUP BY state")
        counts = {row["state"]: row["n"] for row in cur.fetchall()}
    check("and the remainder is untouched, not half-swept",
          counts.get(DeliveryState.EXTERNAL_MAILER_HANDOFF) == 3
          and counts.get(DeliveryState.CAPABILITY_RETIRED) == 2, str(counts))

    rest = ledger.retire_expired_capabilities(limit=10)
    check("a later pass finishes the job", len(rest) == 3, str(len(rest)))
    check("and then there is nothing left", ledger.retire_expired_capabilities() == [])
    PASSED.append("a_bounded_batch_makes_progress_and_leaves_the_rest_intact")


def test_a_live_lease_is_never_swept_from_under_its_owner(ctx) -> None:
    """A delivery an invocation durably owns is somebody else's right now."""
    ledger, owner = ctx["ledger"], ctx["owner"]
    truncate(owner)
    # Held: the handoff is recorded and the lease is NOT released.
    held = handed_off(ledger, "leased", expires_at=past(1), release=False)
    free = handed_off(ledger, "unleased", expires_at=past(1))

    retired = ledger.retire_expired_capabilities()
    check("only the unowned delivery is retired", len(retired) == 1, str(len(retired)))
    check("and it is the right one",
          str(retired[0].delivery_id) == str(free.delivery_id))
    leased_row = raw_row(owner, held.delivery_id)
    check("the leased delivery keeps its state",
          leased_row["state"] == DeliveryState.EXTERNAL_MAILER_HANDOFF)
    check("and its bearer, because its owner may still be using it",
          leased_row["capability_secret"] == BEARER)

    # Once the lease is gone, the same row is ordinary work again.
    ledger.release(held.delivery_id, owner=f"probe-leased")
    second = ledger.retire_expired_capabilities()
    check("released, it is retired on the next pass", len(second) == 1)
    PASSED.append("a_live_lease_is_never_swept_from_under_its_owner")


def test_concurrent_sweeps_do_not_double_retire(ctx) -> None:
    """Two housekeeping passes on two connections, at the same time."""
    ledger, owner = ctx["ledger"], ctx["owner"]
    truncate(owner)
    for i in range(6):
        handed_off(ledger, f"conc{i}", expires_at=past(i + 1))

    import threading

    results: list = []
    errors: list = []
    barrier = threading.Barrier(2)

    def sweep(dsn):
        conn = connect(dsn)
        try:
            barrier.wait(timeout=30)
            results.append(DeliveryLedger(conn).retire_expired_capabilities())
        except Exception as error:  # noqa: BLE001
            errors.append(error)
        finally:
            conn.close()

    threads = [threading.Thread(target=sweep, args=(ctx["dsn"],)) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    check("neither sweep failed", errors == [], str(errors))
    ids = [str(r.delivery_id) for batch in results for r in batch]
    check("every delivery was retired", len(ids) == 6, str(len(ids)))
    check("and NONE was retired twice", len(set(ids)) == 6, str(sorted(ids)))
    with owner.cursor() as cur:
        cur.execute(f"SELECT count(*) AS n FROM {TABLE} WHERE capability_secret IS NOT NULL")
        check("no bearer survived", cur.fetchone()["n"] == 0)
    PASSED.append("concurrent_sweeps_do_not_double_retire")


# --- the operational surface ----------------------------------------------------


def test_expired_history_stops_being_open_and_actionable(ctx) -> None:
    ledger, owner = ctx["ledger"], ctx["owner"]
    truncate(owner)
    expired = handed_off(ledger, "openexp", expires_at=past(2))
    live = handed_off(ledger, "openlive", expires_at=future(WEEKLY_TTL))
    unresolved = ambiguous(ledger, "openamb", expires_at=past(2))

    check("all three start out as open work",
          len(ledger.open_operations()) == 3, str(len(ledger.open_operations())))
    ledger.retire_expired_capabilities()

    open_now = {str(r.delivery_id): r for r in ledger.open_operations()}
    check("the expired handoff has left the open surface",
          str(expired.delivery_id) not in open_now, str(sorted(open_now)))
    check("the live one has not", str(live.delivery_id) in open_now)
    check("and the unresolved ambiguous send has not either",
          str(unresolved.delivery_id) in open_now)

    with owner.cursor() as cur:
        cur.execute(f"SELECT count(*) AS n FROM {TABLE} WHERE operator_action_required")
        check("exactly one delivery is operator-actionable — the ambiguous one",
              cur.fetchone()["n"] == 1)
        # The partial index and the query must agree, or the index is a lie.
        cur.execute(
            "SELECT pg_get_expr(indpred, indrelid) AS predicate FROM pg_index "
            "WHERE indexrelid = 'public.idx_eco_dashboard_delivery_operation_open'::regclass")
        predicate = cur.fetchone()["predicate"]
    for state in CLOSED_STATES:
        check(f"the open index excludes {state}", state in predicate, predicate)
    PASSED.append("expired_history_stops_being_open_and_actionable")


def test_a_published_delivery_that_never_sent_is_retired_after_expiry(ctx) -> None:
    """SMTP failed after publication. The link stays live for its TTL and dies."""
    ledger, owner = ctx["ledger"], ctx["owner"]
    truncate(owner)
    # Published, capability persisted, and the send never happened.
    _owner, stuck = persisted(ledger, "smtpfail", expires_at=future(WEEKLY_TTL))
    ledger.release(stuck.delivery_id, owner=_owner)
    check("while the grant is live it is left alone",
          ledger.retire_expired_capabilities() == [])
    check("and it keeps its bearer, which is still a usable link",
          raw_row(owner, stuck.delivery_id)["capability_secret"] == BEARER)

    with owner.cursor() as cur:
        cur.execute(f"UPDATE {TABLE} SET capability_expires_at = %s "
                    f"WHERE delivery_id = %s", (past(1), str(stuck.delivery_id)))
    retired = ledger.retire_expired_capabilities()
    check("once expired it is retired without anybody resending",
          len(retired) == 1 and retired[0].state == DeliveryState.CAPABILITY_RETIRED)
    row = raw_row(owner, stuck.delivery_id)
    check("its bearer is destroyed", row["capability_secret"] is None)
    check("and it makes no claim about a message",
          row["provider_message_id"] is None and row["provider_idempotency_key"] is None)
    PASSED.append("a_published_delivery_that_never_sent_is_retired_after_expiry")


def test_a_bound_but_unsubmitted_intent_is_retired_and_a_submitted_one_is_not(ctx) -> None:
    """"Never submitted" is proven from the row, not assumed from the state."""
    ledger, owner = ctx["ledger"], ctx["owner"]
    truncate(owner)
    unsubmitted = intent_recorded(ledger, "intentA", expires_at=past(1))
    submitted = intent_recorded(ledger, "intentB", expires_at=past(1))
    with owner.cursor() as cur:
        # The one fact that makes a message possible.
        cur.execute(f"UPDATE {TABLE} SET provider_attempts = 1, "
                    f"provider_submitted_at = now() WHERE delivery_id = %s",
                    (str(submitted.delivery_id),))

    retired = ledger.retire_expired_capabilities()
    check("only the never-submitted intent is retired", len(retired) == 1, str(len(retired)))
    check("and it is the right one",
          str(retired[0].delivery_id) == str(unsubmitted.delivery_id))
    kept = raw_row(owner, submitted.delivery_id)
    check("a delivery that may have produced a message keeps its state",
          kept["state"] == DeliveryState.DELIVERY_INTENT_RECORDED)
    check("and keeps the bearer a reconciliation would need",
          kept["capability_secret"] == BEARER)
    gone = raw_row(owner, unsubmitted.delivery_id)
    check("the retired one keeps its immutable submission identity as evidence",
          gone["provider_idempotency_key"] is not None
          and gone["provider_message_fingerprint"] is not None
          and gone["provider_bound_capability_id"] is not None)
    PASSED.append("a_bound_but_unsubmitted_intent_is_retired_and_a_submitted_one_is_not")


def test_the_ambiguous_smtp_contract_is_untouched(ctx) -> None:
    """The one state a human owes an answer for. Nothing here may weaken it."""
    ledger, owner = ctx["ledger"], ctx["owner"]
    truncate(owner)
    unresolved = ambiguous(ledger, "amb", expires_at=past(30))
    before = raw_row(owner, unresolved.delivery_id)

    for _ in range(3):
        ledger.retire_expired_capabilities()

    after = raw_row(owner, unresolved.delivery_id)
    check("the state is unchanged",
          after["state"] == DeliveryState.PROVIDER_AMBIGUOUS, after["state"])
    check("the operator is still asked to act", after["operator_action_required"] is True)
    check("the bearer the operator may need is still there",
          after["capability_secret"] == BEARER)
    check("and the submission identity that identifies the message survives",
          after["provider_idempotency_key"] == before["provider_idempotency_key"]
          and after["provider_message_fingerprint"] == before["provider_message_fingerprint"])
    check("the row was not written at all", after["updated_at"] == before["updated_at"])
    PASSED.append("the_ambiguous_smtp_contract_is_untouched")


def test_a_definite_rejection_is_retired_once_its_link_is_dead(ctx) -> None:
    """A retry that would mail a 410 link is not a retry worth keeping open."""
    ledger, owner = ctx["ledger"], ctx["owner"]
    truncate(owner)
    live = rejected(ledger, "rejlive", expires_at=future(WEEKLY_TTL))
    dead = rejected(ledger, "rejdead", expires_at=past(1))

    retired = ledger.retire_expired_capabilities()
    check("only the one whose link is dead is retired", len(retired) == 1, str(len(retired)))
    check("and it is the right one", str(retired[0].delivery_id) == str(dead.delivery_id))
    live_row = raw_row(owner, live.delivery_id)
    check("the retryable rejection is left for its retry",
          live_row["state"] == DeliveryState.PROVIDER_REJECTED
          and live_row["operator_action_required"] is True)
    dead_row = raw_row(owner, dead.delivery_id)
    check("the dead one no longer asks an operator for anything",
          dead_row["operator_action_required"] is False)
    check("its bearer is gone", dead_row["capability_secret"] is None)
    check("but the failure that produced it is still on the record",
          dead_row["failure_code"] == "SMTP_550")
    PASSED.append("a_definite_rejection_is_retired_once_its_link_is_dead")


# --- resend after retirement ----------------------------------------------------


def test_a_resend_of_the_same_period_rotates_and_stays_that_period(ctx) -> None:
    """Retirement is not a dead end, and recovery never retargets the report."""
    ledger, owner = ctx["ledger"], ctx["owner"]
    truncate(owner)
    record = handed_off(ledger, "resend", expires_at=past(1))
    identity_operation = record.operation_id
    identity_subject = record.subject_ref
    identity_digest = record.payload_digest
    ledger.retire_expired_capabilities()

    # A later Eco run asks for this driver and this period again. It converges
    # on the SAME logical delivery rather than creating a second one.
    again = prepared(ledger, "resend", external_mailer=EXTERNAL_MAILER)
    check("the rerun finds the same row",
          str(again.delivery_id) == str(record.delivery_id))
    check("in the retired state", again.state == DeliveryState.CAPABILITY_RETIRED)

    # ...and rotates through the explicit recovery operation.
    lease = "probe-resend-2"
    claimed = ledger.claim(again.delivery_id, owner=lease)
    recovering = ledger.mark_bearer_recovery_required(
        claimed, owner=lease, failure_code="CAPABILITY_EXPIRED",
        reason="the retained capability expired")
    check("the retired delivery may re-enter recovery",
          recovering.state == DeliveryState.BEARER_RECOVERY_REQUIRED)
    rotated = ledger.record_capability(
        recovering, owner=lease, capability=BEARER_TWO, capability_id="b" * 32,
        expires_at=future(WEEKLY_TTL), bearer_generation=2)
    out = ledger.record_external_mailer_handoff(
        rotated, owner=lease, mailer=EXTERNAL_MAILER, run_id="run-resend-2")
    ledger.release(out.delivery_id, owner=lease)

    row = raw_row(owner, record.delivery_id)
    check("the publication identity is unchanged",
          row["operation_id"] == identity_operation
          and row["subject_ref"] == identity_subject
          and row["payload_digest"] == identity_digest,
          "a recovered capability must still resolve to THIS period's snapshot")
    check("the period identity is unchanged",
          row["period_type"] == "weekly"
          and row["period_start_date"] == date(2026, 7, 13))
    check("the generation advanced by exactly one", row["bearer_generation"] == 2)
    check("the fresh bearer is held", row["capability_secret"] == BEARER_TWO)
    check("and the retired bearer exists nowhere in the row",
          BEARER not in json.dumps(row, default=str))
    check("the retirement record is still there as history",
          "capability_retirement" in row["metadata_json"])
    PASSED.append("a_resend_of_the_same_period_rotates_and_stays_that_period")


# --- previously issued grants ---------------------------------------------------


def test_a_legacy_forty_five_day_grant_keeps_the_validity_it_was_given(ctx) -> None:
    """Nothing retroactively shortens or lengthens an already-issued link.

    The lifetime policy decides the expiry of a grant at the moment it is
    MINTED. A row created under the old universal 45-day rule therefore keeps
    exactly the `capability_expires_at` its recipient's e-mail was written
    against — the sweep reads that column, it never recomputes it — and is
    retired by the same rule as any other once it genuinely expires.
    """
    ledger, owner = ctx["ledger"], ctx["owner"]
    truncate(owner)
    legacy_expiry = future(LEGACY_TTL)
    legacy = handed_off(ledger, "legacy", expires_at=legacy_expiry)
    stored = raw_row(owner, legacy.delivery_id)["capability_expires_at"]

    for _ in range(2):
        check("a live 45-day grant is not swept",
              ledger.retire_expired_capabilities() == [])
    row = raw_row(owner, legacy.delivery_id)
    check("its expiry is exactly what was issued — not shortened to 10 days",
          row["capability_expires_at"] == stored,
          f"{row['capability_expires_at']} vs {stored}")
    check("nor lengthened to 60",
          row["capability_expires_at"] < datetime.now(timezone.utc) + timedelta(days=46))
    check("and it keeps working as a link", row["capability_secret"] == BEARER)

    # Wind it past its own 45 days: the ordinary rule applies, unchanged.
    with owner.cursor() as cur:
        cur.execute(f"UPDATE {TABLE} SET capability_expires_at = %s WHERE delivery_id = %s",
                    (past(1), str(legacy.delivery_id)))
    retired = ledger.retire_expired_capabilities()
    check("an expired legacy grant is retired by the same rule", len(retired) == 1)
    check("and its bearer is destroyed",
          raw_row(owner, legacy.delivery_id)["capability_secret"] is None)
    PASSED.append("a_legacy_forty_five_day_grant_keeps_the_validity_it_was_given")


# --- the database itself --------------------------------------------------------


def test_the_database_refuses_a_retired_row_that_holds_a_bearer(ctx) -> None:
    """The guarantee is physical, not merely a statement in one method."""
    import psycopg

    ledger, owner = ctx["ledger"], ctx["owner"]
    truncate(owner)
    record = handed_off(ledger, "physical", expires_at=past(1))
    ledger.retire_expired_capabilities()

    refused = False
    try:
        with owner.cursor() as cur:
            cur.execute(f"UPDATE {TABLE} SET capability_secret = %s WHERE delivery_id = %s",
                        (BEARER, str(record.delivery_id)))
    except psycopg.errors.CheckViolation:
        refused = True
    check("a retired row cannot be given a bearer back", refused)

    forgot = False
    try:
        with owner.cursor() as cur:
            cur.execute(f"UPDATE {TABLE} SET capability_digest = NULL WHERE delivery_id = %s",
                        (str(record.delivery_id),))
    except psycopg.errors.CheckViolation:
        forgot = True
    check("and it cannot forget which grant it held", forgot)
    PASSED.append("the_database_refuses_a_retired_row_that_holds_a_bearer")


def test_051_upgrades_a_populated_050_ledger_without_reclassifying_a_row(ctx) -> None:
    """The migration path a real client takes, over rows rather than over nothing.

    049 and 050 are applied history on every enabled client business database,
    so 051 has to be a forward delta that a POPULATED ledger survives. It is
    exercised here the only way that means anything: build the 049 + 050
    contract, put one row in every state that contract can represent, then run
    051 and check that PostgreSQL itself validated every constraint against
    them and that not one row changed classification.
    """
    import psycopg

    dsn = ctx["dsn"]
    upgrade_db = "eco_retirement_upgrade_probe"
    admin = connect(dsn.rsplit("/", 1)[0] + "/postgres")
    try:
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{upgrade_db}" WITH (FORCE)')
            cur.execute(f'CREATE DATABASE "{upgrade_db}"')
    finally:
        admin.close()

    probe = connect(dsn.rsplit("/", 1)[0] + "/" + upgrade_db)
    try:
        with probe.cursor() as cur:
            cur.execute("CREATE TABLE public.client_trips (id BIGSERIAL PRIMARY KEY)")
            for migration in MIGRATIONS[:2]:
                cur.execute(migration.read_text(encoding="utf-8"))

        ledger = DeliveryLedger(probe)
        # Rows the 050 contract can hold, including one carrying the OLD
        # universal 45-day expiry, produced through the real ledger API.
        legacy = handed_off(ledger, "upgradelegacy", expires_at=future(LEGACY_TTL))
        live = persisted(ledger, "upgradelive", expires_at=future(WEEKLY_TTL))[1]
        unresolved = ambiguous(ledger, "upgradeamb", expires_at=past(1))
        with probe.cursor() as cur:
            cur.execute(f"SELECT delivery_id, state, capability_secret, "
                        f"capability_expires_at FROM {TABLE} ORDER BY delivery_id")
            before = {str(r["delivery_id"]): dict(r) for r in cur.fetchall()}
        check("the pre-051 fixture really has rows", len(before) == 3, str(len(before)))

        with probe.cursor() as cur:
            cur.execute(MIGRATIONS[2].read_text(encoding="utf-8"))
            cur.execute("SELECT conname FROM pg_constraint "
                        f"WHERE conrelid = '{TABLE}'::regclass AND NOT convalidated")
            unvalidated = [r["conname"] for r in cur.fetchall()]
        check("051 applies over a populated ledger", True)
        check("and every constraint was VALIDATED against the existing rows — no "
              "NOT VALID escape", unvalidated == [], str(unvalidated))

        with probe.cursor() as cur:
            cur.execute(f"SELECT delivery_id, state, capability_secret, "
                        f"capability_expires_at FROM {TABLE} ORDER BY delivery_id")
            after = {str(r["delivery_id"]): dict(r) for r in cur.fetchall()}
        check("no row was added or removed", set(after) == set(before))
        for key, row in after.items():
            check("no row changed state", row["state"] == before[key]["state"], key)
            check("no bearer was destroyed by the migration itself",
                  row["capability_secret"] == before[key]["capability_secret"], key)
            check("and NO EXPIRY WAS REWRITTEN — a 45-day grant keeps its 45 days",
                  row["capability_expires_at"] == before[key]["capability_expires_at"], key)
        check("the legacy grant is still the 45-day one it was issued as",
              (after[str(legacy.delivery_id)]["capability_expires_at"]
               - datetime.now(timezone.utc)) > timedelta(days=44))

        # And the new lifecycle works on the upgraded database.
        with probe.cursor() as cur:
            cur.execute(f"UPDATE {TABLE} SET capability_expires_at = %s WHERE delivery_id = %s",
                        (past(1), str(legacy.delivery_id)))
        retired = DeliveryLedger(probe).retire_expired_capabilities()
        check("an expired grant on the upgraded database is retired",
              len(retired) == 1 and str(retired[0].delivery_id) == str(legacy.delivery_id),
              str([r.state for r in retired]))
        check("and the live one is untouched",
              raw_row(probe, live.delivery_id)["capability_secret"] == BEARER)
        check("as is the unresolved ambiguous send",
              raw_row(probe, unresolved.delivery_id)["state"]
              == DeliveryState.PROVIDER_AMBIGUOUS)
    finally:
        probe.close()
        admin = connect(dsn.rsplit("/", 1)[0] + "/postgres")
        try:
            with admin.cursor() as cur:
                cur.execute(f'DROP DATABASE IF EXISTS "{upgrade_db}" WITH (FORCE)')
        finally:
            admin.close()
    PASSED.append("051_upgrades_a_populated_050_ledger_without_reclassifying_a_row")


TESTS = [
    test_the_new_state_is_terminal_without_being_a_delivery_claim,
    test_051_upgrades_a_populated_050_ledger_without_reclassifying_a_row,
    test_an_expired_bearer_is_destroyed_without_a_resend,
    test_a_live_bearer_is_never_touched,
    test_an_unknown_expiry_is_not_evidence_of_expiry,
    test_the_sweep_is_idempotent_and_safe_to_repeat,
    test_a_bounded_batch_makes_progress_and_leaves_the_rest_intact,
    test_a_live_lease_is_never_swept_from_under_its_owner,
    test_concurrent_sweeps_do_not_double_retire,
    test_expired_history_stops_being_open_and_actionable,
    test_a_published_delivery_that_never_sent_is_retired_after_expiry,
    test_a_bound_but_unsubmitted_intent_is_retired_and_a_submitted_one_is_not,
    test_the_ambiguous_smtp_contract_is_untouched,
    test_a_definite_rejection_is_retired_once_its_link_is_dead,
    test_a_resend_of_the_same_period_rotates_and_stays_that_period,
    test_a_legacy_forty_five_day_grant_keeps_the_validity_it_was_given,
    test_the_database_refuses_a_retired_row_that_holds_a_bearer,
]


def main() -> int:
    dsn = os.getenv(ENV)
    if not dsn:
        print(f"SKIP: set {ENV} to a disposable loopback PostgreSQL 16 DSN")
        return 0
    require_loopback_dsn_or_exit(dsn, label=ENV)
    if "log_platform" in dsn or "logplatform" in dsn:
        raise RuntimeError("refusing a DSN that looks like the platform database")
    try:
        import psycopg  # noqa: F401
    except ImportError:
        print("SKIP: psycopg is not installed in this interpreter")
        return 0

    owner = connect(dsn)
    build_schema(owner)
    ctx = {"dsn": dsn, "owner": owner, "ledger": DeliveryLedger(connect(dsn))}
    try:
        for test in TESTS:
            test(ctx)
            print(f"PASS {test.__name__}")
    finally:
        try:
            drop_schema(owner)
        finally:
            owner.close()
    print(f"\n{len(PASSED)} retirement checks passed — expired bearers destroyed, "
          f"live ones untouched, ambiguity preserved, resends still period-scoped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
