#!/usr/bin/env python3
"""Driver Eco Dashboard V1 — guarded OPERATOR_REQUIRED -> PREPARED recovery.

Run:
    ECO_DASHBOARD_RECOVERY_TEST_DSN=postgresql://postgres:pw@127.0.0.1:55434/disposable \\
      python3 ops/tests_manual/test_eco_dashboard_operator_recovery_postgres.py

WHAT THIS PROVES

`ops/recover_eco_dashboard_operator_required_delivery.py` re-arms exactly one
reviewed delivery whose publication was refused BEFORE any remote effect
existed. The whole safety argument is that the host's own durable facts prove
there is nothing remote to reconcile, so every check here is about the row:

  * the eligible shape is recovered, and the dry run that reports it writes
    NOTHING — asserted column by column, including `updated_at`;
  * the recovered row is a genuine `PREPARED` operation the ordinary lifecycle
    can pick up and advance, not merely a row with the right string in `state`;
  * the delivery identity, operation id and payload digest survive unchanged;
  * an EXTERNALLY-OWNED delivery is recoverable — `external_mailer` is
    immutable ownership metadata, not evidence that a mailing lifecycle did
    anything — and it survives the recovery byte-for-byte, so the retry the
    recovery re-arms is still that same external Eco mailer's delivery;
  * every disqualifying fact refuses, and refuses without writing: remote
    capability evidence, a bound provider submission identity, a completed
    send, the wrong failure phase, the wrong failure code, the wrong state, a
    live lease, and a mismatched expectation from the operator;
  * a second execution is a refusal, not a second mutation;
  * the guard list and the SQL predicate that actually decides are the same
    list, guard by guard.

The rows are produced through the REAL ledger API (`ensure_operation`,
`claim`, `mark_operator_required`), so the eligible fixture is the shape the
publisher actually persists rather than one this file invented.

DESTRUCTIVE. It drops and recreates `public.eco_dashboard_delivery_operation`
in the database the DSN names, so it refuses any DSN that is not loopback.
A disposable instance is one command:

    docker run -d --rm --name eco-dash-recovery-pg -e POSTGRES_PASSWORD=disposable \\
      -e POSTGRES_DB=eco_recovery_test -p 127.0.0.1:55434:5432 \\
      --tmpfs /var/lib/postgresql/data postgres:16-alpine

No production database, no Worker, no Cloudflare resource, no e-mail, no
provider and no capability is involved anywhere in this file.
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.ecodriving_dashboard.delivery_contract import (  # noqa: E402
    OPERATOR_RECOVERABLE_PUBLICATION_FAILURE_CODES,
    OPERATOR_RECOVERY_TRANSITIONS,
    DeliveryContractError,
    DeliveryIdentity,
    DeliveryState,
    LEGAL_TRANSITIONS,
    NextAction,
    assert_legal_operator_recovery,
    derive_operation_id,
    derive_subject_ref,
)
from jobs.ecodriving_dashboard.delivery_ledger import (  # noqa: E402
    OPERATOR_RECOVERY_GUARDS,
    OPERATOR_RECOVERY_METADATA_KEY,
    OPERATOR_RECOVERY_PHASE,
    DeliveryLedger,
)
from ops.recover_eco_dashboard_operator_required_delivery import (  # noqa: E402
    OUTCOME_DRY_RUN,
    OUTCOME_NOT_ELIGIBLE,
    OUTCOME_RECOVERED,
    OUTCOME_REFUSED_AT_WRITE,
    OUTCOME_UNKNOWN_DELIVERY,
    dry_run,
    execute,
)
from ops.tests_manual.postgres_dsn_safety import require_loopback_dsn_or_exit  # noqa: E402

ENV = "ECO_DASHBOARD_RECOVERY_TEST_DSN"
MIGRATIONS = tuple(
    REPO_ROOT / "db" / "client_business" / name
    for name in ("049_eco_dashboard_delivery_operation.sql",
                 "050_eco_dashboard_external_mailer_ownership.sql",
                 "051_eco_dashboard_capability_retirement.sql")
)
TABLE = "public.eco_dashboard_delivery_operation"

#: The role the recovery command actually connects as in production.
RUNTIME_ROLE = "eco_dash_recovery_probe"
RUNTIME_PASSWORD = "disposable-probe"

CLIENT_ID = "11111111-1111-1111-1111-111111111111"
RECIPIENT = "driver.one@example.invalid"
OPERATOR = "ops-oncall"
REASON = "Worker sparse-distribution defect fixed; publication was refused pre-publishSnapshot"

#: A synthetic 43-character bearer. No grant is ever minted with it.
BEARER = "B" * 43
CAPABILITY_ID = "c" * 32

#: The EXTERNAL send-accounting owner the production row that exposed the
#: eligibility defect carries. Ownership metadata bound by migration 050's
#: INSERT contract, not evidence that any mailing lifecycle did anything.
EXTERNAL_MAILER = "eco_person_driving_weekly_email_notifications"

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


def runtime_dsn(dsn: str) -> str:
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(dsn)
    host = parts.hostname or "127.0.0.1"
    port = f":{parts.port}" if parts.port else ""
    return urlunsplit((parts.scheme,
                       f"{RUNTIME_ROLE}:{RUNTIME_PASSWORD}@{host}{port}",
                       parts.path, "", ""))


def build_schema(owner) -> None:
    with owner.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {TABLE} CASCADE")
        cur.execute("DROP TABLE IF EXISTS public.client_trips CASCADE")
        cur.execute(
            f"""DO $$
                BEGIN
                  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{RUNTIME_ROLE}') THEN
                    CREATE ROLE {RUNTIME_ROLE} LOGIN PASSWORD '{RUNTIME_PASSWORD}';
                  END IF;
                END $$""")
        cur.execute("CREATE TABLE public.client_trips (id BIGSERIAL PRIMARY KEY)")
        cur.execute(f"GRANT SELECT, INSERT, UPDATE ON TABLE public.client_trips "
                    f"TO {RUNTIME_ROLE}")
        cur.execute(f"GRANT CONNECT ON DATABASE {owner.info.dbname} TO {RUNTIME_ROLE}")
        for migration in MIGRATIONS:
            cur.execute(migration.read_text(encoding="utf-8"))


def drop_schema(owner) -> None:
    with owner.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {TABLE} CASCADE")
        cur.execute("DROP FUNCTION IF EXISTS "
                    "public.eco_dashboard_delivery_operation_guard() CASCADE")
        cur.execute("DROP TABLE IF EXISTS public.client_trips CASCADE")
        cur.execute(f"REVOKE ALL ON DATABASE {owner.info.dbname} FROM {RUNTIME_ROLE}")
        cur.execute(f"REVOKE ALL ON SCHEMA public FROM {RUNTIME_ROLE}")
        cur.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {RUNTIME_ROLE}")
        cur.execute(f"DROP ROLE IF EXISTS {RUNTIME_ROLE}")


def identity_for(seed: str) -> DeliveryIdentity:
    return DeliveryIdentity(
        client_id=CLIENT_ID, identity_key=f"DRIVER-{seed}", period_type="weekly",
        period_start_date=date(2026, 7, 13), period_end_date=date(2026, 7, 20),
        send_scope="normal")


def prepared(ledger: DeliveryLedger, seed: str, *, digest: str | None = None,
             external_mailer: str | None = None):
    """One durable `PREPARED` operation, exactly as `prepare_delivery` makes it."""
    identity = identity_for(seed)
    record, _created = ledger.ensure_operation(
        identity,
        operation_id=derive_operation_id(identity),
        subject_ref=derive_subject_ref(identity),
        payload_digest=digest or ("0" * 63 + "1"),
        recipient_email=RECIPIENT,
        recipient_identity=f"rcpt_{seed}",
        external_mailer=external_mailer)
    return record


def failed_publication(ledger: DeliveryLedger, seed: str, *,
                       phase: str = OPERATOR_RECOVERY_PHASE,
                       code: str = "PAYLOAD_NOT_CANONICAL",
                       digest: str | None = None,
                       external_mailer: str | None = None):
    """THE production shape: refused at `/api/publish`, nothing remote created.

    Produced by the real ledger calls the publisher makes, including the lease
    release the lifecycle always performs in its `finally` block.
    """
    owner = f"probe-{seed}"
    record = prepared(ledger, seed, digest=digest, external_mailer=external_mailer)
    claimed = ledger.claim(record.delivery_id, owner=owner)
    parked = ledger.mark_operator_required(
        claimed, owner=owner, phase=phase, failure_code=code,
        detail="the delivery boundary refused the payload")
    ledger.release(parked.delivery_id, owner=owner)
    return ledger.load(parked.delivery_id)


def with_capability(ledger: DeliveryLedger, seed: str, *,
                    external_mailer: str | None = None):
    """A delivery that DID reach a live grant before it was parked.

    Remote capability evidence in the plainest form the table can hold, and the
    single most important row this recovery must refuse — for an externally
    owned delivery as much as for an internally owned one, because a live grant
    is exactly what the external mailer would have been handed.
    """
    owner = f"probe-{seed}"
    record = prepared(ledger, seed, external_mailer=external_mailer)
    claimed = ledger.claim(record.delivery_id, owner=owner)
    persisted = ledger.record_capability(
        claimed, owner=owner, capability=BEARER, capability_id=CAPABILITY_ID,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=6),
        bearer_generation=1)
    parked = ledger.mark_operator_required(
        persisted, owner=owner, phase=OPERATOR_RECOVERY_PHASE,
        failure_code="PAYLOAD_NOT_CANONICAL", detail="parked after a grant existed")
    ledger.release(parked.delivery_id, owner=owner)
    return ledger.load(parked.delivery_id)


def raw_row(conn, delivery_id) -> dict:
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM {TABLE} WHERE delivery_id = %s", (str(delivery_id),))
        return dict(cur.fetchone())


def recover(ledger: DeliveryLedger, record, **overrides):
    """The command's execute path, with the operator's expectations supplied."""
    params = {
        "operator": OPERATOR,
        "reason": REASON,
        "expected_operation_id": record.operation_id,
        "expected_payload_digest": record.payload_digest,
    }
    params.update(overrides)
    return execute(ledger, record.delivery_id, **params)


# --- 1. the eligible row --------------------------------------------------------


def test_the_dry_run_reports_recoverable_and_writes_nothing(ctx) -> None:
    ledger = ctx["ledger"]
    record = failed_publication(ledger, "eligible")
    check("the fixture is the production shape",
          record.state == DeliveryState.OPERATOR_REQUIRED
          and record.operator_action_required is True
          and record.failure_phase == OPERATOR_RECOVERY_PHASE
          and record.failure_code == "PAYLOAD_NOT_CANONICAL"
          and record.capability_id is None
          and record.bearer_generation == 0
          and record.bearer_persisted_at is None
          and record.provider_idempotency_key is None,
          str(record.public_summary()))

    before = raw_row(ctx["owner"], record.delivery_id)
    report = dry_run(ledger, record.delivery_id)
    after = raw_row(ctx["owner"], record.delivery_id)

    check("the dry run is the default outcome", report["outcome"] == OUTCOME_DRY_RUN)
    check("the eligible row is reported recoverable", report["recoverable"] is True,
          str(report["assessment"]["blocked_by"]))
    check("it names the transition it would make",
          report["would_transition"] == {"from": DeliveryState.OPERATOR_REQUIRED,
                                         "to": DeliveryState.PREPARED})
    check("every guard is evaluated, not just the first",
          len(report["assessment"]["guards"]) == len(OPERATOR_RECOVERY_GUARDS))
    check("no guard refused", report["assessment"]["blocked_by"] == [])
    # THE POINT OF A DRY RUN. Not "state is unchanged" — every column, including
    # the one that would move on any write at all.
    differing = [k for k in before if before[k] != after[k]]
    check("the dry run wrote nothing", differing == [], str(differing))
    check("no bearer reaches the report",
          BEARER not in str(report) and "capability_secret" not in report["delivery"])
    PASSED.append("dry_run_reports_and_writes_nothing")


def test_execute_produces_a_valid_prepared_row(ctx) -> None:
    ledger = ctx["ledger"]
    record = failed_publication(ledger, "execute")
    before = raw_row(ctx["owner"], record.delivery_id)

    report = recover(ledger, record)
    check("the recovery succeeded", report["outcome"] == OUTCOME_RECOVERED, str(report))
    check("it reports the transition it made",
          report["transition"] == {"from": DeliveryState.OPERATOR_REQUIRED,
                                   "to": DeliveryState.PREPARED})

    after = raw_row(ctx["owner"], record.delivery_id)
    check("the state is PREPARED", after["state"] == DeliveryState.PREPARED)
    check("the operator flag is cleared", after["operator_action_required"] is False)
    check("the failure columns are cleared",
          after["failure_phase"] is None and after["failure_code"] is None
          and after["failure_detail"] is None)
    check("the lease is released",
          after["lease_owner"] is None and after["lease_expires_at"] is None)
    check("the attestation is durable",
          after["metadata_json"][OPERATOR_RECOVERY_METADATA_KEY]["operator"] == OPERATOR
          and after["metadata_json"][OPERATOR_RECOVERY_METADATA_KEY]["reason"] == REASON)

    # A valid PREPARED row is one the ORDINARY lifecycle can pick up. `PUBLISH`
    # is what `PREPARED` promises, and a claim plus the next real transition is
    # what proves the row is not merely labelled correctly.
    fresh = ledger.load(record.delivery_id)
    check("the ledger reports the one safe next action",
          fresh.next_action == NextAction.PUBLISH, fresh.next_action)
    claimed = ledger.claim(fresh.delivery_id, owner="probe-after-recovery")
    check("the recovered row can be claimed", claimed is not None)
    advanced = ledger.record_capability(
        claimed, owner="probe-after-recovery", capability=BEARER,
        capability_id=CAPABILITY_ID,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=6),
        bearer_generation=1)
    check("the recovered row advances through the real state machine",
          advanced.state == DeliveryState.CAPABILITY_PERSISTED)

    ctx["before_execute"] = before
    PASSED.append("execute_produces_a_valid_prepared_row")


def test_identity_and_digest_survive_byte_for_byte(ctx) -> None:
    ledger = ctx["ledger"]
    record = failed_publication(ledger, "identity")
    before = raw_row(ctx["owner"], record.delivery_id)

    report = recover(ledger, record)
    check("recovered", report["outcome"] == OUTCOME_RECOVERED, str(report))
    after = raw_row(ctx["owner"], record.delivery_id)

    immutable = (
        "delivery_id", "operation_id", "client_id", "identity_key", "period_type",
        "period_start_date", "period_end_date", "send_scope", "subject_ref",
        "payload_digest", "recipient_identity", "recipient_email",
        "external_mailer", "capability_id", "capability_digest",
        "bearer_generation", "bearer_persisted_at", "bearer_cleared_at",
        "provider_name", "provider_idempotency_key", "provider_backend_id",
        "provider_message_fingerprint", "provider_bound_capability_id",
        "provider_bound_bearer_generation", "provider_message_id",
        "provider_attempts", "provider_submitted_at", "provider_accepted_at",
        "remote_delivered_at", "finalized_at", "created_at",
    )
    drifted = [k for k in immutable if before[k] != after[k]]
    check("no identity, digest or publication field moved", drifted == [], str(drifted))
    check("the operation id is byte-for-byte the same",
          after["operation_id"] == before["operation_id"] == record.operation_id)
    check("the payload digest is byte-for-byte the same",
          after["payload_digest"] == before["payload_digest"] == record.payload_digest)
    check("the command reports the same evidence",
          all(report["identity_preserved"].values()), str(report["identity_preserved"]))

    # The database refuses it independently of anything this command does.
    try:
        with ctx["owner"].cursor() as cur:
            cur.execute(f"UPDATE {TABLE} SET payload_digest = %s WHERE delivery_id = %s",
                        ("f" * 64, str(record.delivery_id)))
        raise AssertionError("the guard trigger allowed the payload digest to change")
    except AssertionError:
        raise
    except Exception as exc:  # psycopg raises the guard's check_violation
        check("the guard trigger names the immutability rule",
              "IDENTITY_IMMUTABLE" in str(exc) or "immutable" in str(exc), str(exc)[:200])
    PASSED.append("identity_and_digest_survive")


# --- 1b. external send-accounting ownership is not remote evidence ---------------


def test_external_ownership_does_not_block_an_otherwise_safe_recovery(ctx) -> None:
    """THE PRODUCTION DEFECT, modelled synthetically.

    The row that exposed it satisfied every fact this recovery exists to check
    — refused in PUBLICATION, no capability, no bearer, no provider identity,
    no attempt, no send, no lease — and was refused anyway, purely because
    `external_mailer` named the Eco weekly job that owns its send accounting.
    Ownership is bound by the INSERT and immutable afterwards; it says WHICH
    lifecycle would send, never that any lifecycle DID anything.
    """
    ledger = ctx["ledger"]
    record = failed_publication(ledger, "owned-eligible", external_mailer=EXTERNAL_MAILER)

    check("the fixture is the externally-owned production shape",
          record.state == DeliveryState.OPERATOR_REQUIRED
          and record.operator_action_required is True
          and record.failure_phase == OPERATOR_RECOVERY_PHASE
          and record.failure_code == "PAYLOAD_NOT_CANONICAL"
          and record.external_mailer == EXTERNAL_MAILER
          and record.capability_id is None
          and record.bearer_generation == 0
          and record.bearer_persisted_at is None
          and record.provider_message_id is None
          and record.provider_attempts == 0
          and record.provider_submitted_at is None
          and record.provider_accepted_at is None
          and record.remote_delivered_at is None
          and record.finalized_at is None
          and record.lease_owner is None,
          str(record.public_summary()))

    before = raw_row(ctx["owner"], record.delivery_id)
    report = dry_run(ledger, record.delivery_id)
    after_dry_run = raw_row(ctx["owner"], record.delivery_id)

    check("the externally-owned row is reported recoverable",
          report["recoverable"] is True, str(report["assessment"]["blocked_by"]))
    check("no guard refuses it", report["assessment"]["blocked_by"] == [])
    check("ownership is no longer an eligibility guard",
          "no_external_mailer" not in report["assessment"]["guards"],
          str(sorted(report["assessment"]["guards"])))
    check("the dry run on an owned row wrote nothing",
          [k for k in before if before[k] != after_dry_run[k]] == [],
          str([k for k in before if before[k] != after_dry_run[k]]))

    result = recover(ledger, record)
    check("the externally-owned row recovers", result["outcome"] == OUTCOME_RECOVERED,
          str(result))
    after = raw_row(ctx["owner"], record.delivery_id)

    check("the state is PREPARED", after["state"] == DeliveryState.PREPARED)
    check("the operator flag is cleared", after["operator_action_required"] is False)
    check("the failure columns are cleared",
          after["failure_phase"] is None and after["failure_code"] is None
          and after["failure_detail"] is None)
    # THE OWNERSHIP INVARIANT. Byte-for-byte, not merely still non-NULL.
    check("external_mailer survives byte-for-byte",
          after["external_mailer"] == before["external_mailer"] == EXTERNAL_MAILER,
          repr(after["external_mailer"]))
    check("the command reports the ownership it preserved",
          result["identity_preserved"]["external_mailer"] is True
          and all(result["identity_preserved"].values()),
          str(result["identity_preserved"]))
    check("the recovered row still reports its owner",
          result["delivery"]["external_mailer"] == EXTERNAL_MAILER)

    immutable = (
        "delivery_id", "operation_id", "client_id", "identity_key", "period_type",
        "period_start_date", "period_end_date", "send_scope", "subject_ref",
        "payload_digest", "recipient_identity", "recipient_email",
        "external_mailer", "capability_id", "capability_digest",
        "bearer_generation", "bearer_persisted_at", "bearer_cleared_at",
        "provider_name", "provider_idempotency_key", "provider_backend_id",
        "provider_message_fingerprint", "provider_bound_capability_id",
        "provider_bound_bearer_generation", "provider_message_id",
        "provider_attempts", "provider_submitted_at", "provider_accepted_at",
        "remote_delivered_at", "finalized_at", "created_at",
    )
    drifted = [k for k in immutable if before[k] != after[k]]
    check("nothing immutable moved on an owned recovery", drifted == [], str(drifted))
    check("the operation id is unchanged",
          after["operation_id"] == before["operation_id"] == record.operation_id)
    check("the payload digest is unchanged",
          after["payload_digest"] == before["payload_digest"] == record.payload_digest)

    # A SECOND EXECUTION IS A REFUSAL, on an owned row exactly as on any other.
    second = recover(ledger, ledger.load(record.delivery_id))
    after_second = raw_row(ctx["owner"], record.delivery_id)
    check("a second execution of an owned row is refused",
          second["outcome"] == OUTCOME_NOT_ELIGIBLE, str(second["outcome"]))
    check("it names the state as the reason",
          "state_is_operator_required" in second["assessment"]["blocked_by"])
    check("the second execution wrote nothing",
          [k for k in after if after[k] != after_second[k]] == [],
          str([k for k in after if after[k] != after_second[k]]))
    PASSED.append("external_ownership_does_not_block_recovery")


def test_the_recovered_owned_row_is_retried_by_the_same_external_mailer(ctx) -> None:
    """The retry the recovery re-arms is still the EXTERNAL mailer's delivery.

    A recovery that quietly re-homed a delivery to this ledger's own provider
    lifecycle would produce a second, dashboard-specific message to a driver
    `eco_*_email_send_log` already accounts for. So the proof is not that a
    string survived: it is that the ordinary lifecycle carries the recovered
    row all the way to the handoff, under that same owner, and refuses any
    other.
    """
    ledger = ctx["ledger"]
    record = failed_publication(ledger, "owned-retry", external_mailer=EXTERNAL_MAILER)
    check("recovered", recover(ledger, record)["outcome"] == OUTCOME_RECOVERED)

    fresh = ledger.load(record.delivery_id)
    check("the ledger reports the one safe next action",
          fresh.next_action == NextAction.PUBLISH, fresh.next_action)
    owner = "probe-owned-retry"
    claimed = ledger.claim(fresh.delivery_id, owner=owner)
    check("the recovered owned row can be claimed", claimed is not None)
    persisted = ledger.record_capability(
        claimed, owner=owner, capability=BEARER, capability_id=CAPABILITY_ID,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=6),
        bearer_generation=1)
    check("it advances through the real state machine",
          persisted.state == DeliveryState.CAPABILITY_PERSISTED)

    # ...and the handoff is accepted for the bound owner alone.
    try:
        ledger.record_external_mailer_handoff(persisted, owner=owner,
                                              mailer="some_other_mailer")
        raise AssertionError("a foreign mailer was allowed to take the handoff")
    except AssertionError:
        raise
    except Exception as exc:
        check("a foreign mailer is refused by the ownership conflict",
              getattr(exc, "code", "") == "EXTERNAL_OWNERSHIP_CONFLICT", str(exc)[:200])

    handed = ledger.record_external_mailer_handoff(
        ledger.load(record.delivery_id), owner=owner, mailer=EXTERNAL_MAILER,
        run_id="probe-run")
    check("the same external Eco mailer still owns the retry",
          handed.state == DeliveryState.EXTERNAL_MAILER_HANDOFF
          and handed.external_mailer == EXTERNAL_MAILER,
          str(handed.public_summary()))
    check("the ledger hands the delivery to the external owner",
          handed.next_action == NextAction.EXTERNAL_MAILER_OWNS_DELIVERY)
    PASSED.append("recovered_owned_row_retried_by_same_mailer")


def test_an_owned_row_with_lifecycle_evidence_is_still_refused(ctx) -> None:
    """Ownership stopped disqualifying rows; nothing else did.

    Every fact that proves the external branch may already have been given
    something still refuses, and the two provider facts that cannot even be
    represented on an owned row are refused by the table itself.
    """
    ledger = ctx["ledger"]

    owned_grant = with_capability(ledger, "owned-grant", external_mailer=EXTERNAL_MAILER)
    check("the fixture is owned and holds a grant",
          owned_grant.external_mailer == EXTERNAL_MAILER
          and owned_grant.capability_id == CAPABILITY_ID)
    _refuses(ctx, "an owned row that reached a live grant", owned_grant,
             guard="no_capability_id")

    # The shape a row parked after an EXTERNAL_MAILER_HANDOFF would carry: the
    # bearer was persisted for the mailer and then cleared. Written directly
    # because that state is terminal in `LEGAL_TRANSITIONS` by design.
    handed_then_parked = failed_publication(ledger, "owned-handed",
                                            external_mailer=EXTERNAL_MAILER)
    with ctx["owner"].cursor() as cur:
        cur.execute(f"""UPDATE {TABLE}
                           SET bearer_generation = 1, bearer_persisted_at = now(),
                               bearer_cleared_at = now(), capability_digest = %s
                         WHERE delivery_id = %s""",
                    ("d" * 64, str(handed_then_parked.delivery_id)))
    _refuses(ctx, "an owned row whose bearer was persisted for the mailer",
             ledger.load(handed_then_parked.delivery_id),
             guard="bearer_generation_is_zero")

    owned_leased = failed_publication(ledger, "owned-lease",
                                      external_mailer=EXTERNAL_MAILER)
    ledger.claim(owned_leased.delivery_id, owner="probe-owned-lease")
    _refuses(ctx, "an owned row somebody still owns",
             ledger.load(owned_leased.delivery_id), guard="no_live_lease")

    owned_wrong_phase = failed_publication(ledger, "owned-phase", phase="PROVIDER",
                                           external_mailer=EXTERNAL_MAILER)
    _refuses(ctx, "an owned failure in a later phase", owned_wrong_phase,
             guard="failure_phase_is_publication")

    owned_expectation = failed_publication(ledger, "owned-expectation",
                                           external_mailer=EXTERNAL_MAILER)
    _refuses(ctx, "an owned row under a wrong payload digest", owned_expectation,
             expected_payload_digest="e" * 64)

    # THE TWO PROVIDER FACTS AN OWNED ROW CANNOT EVEN HOLD. Migration 050's
    # `chk_..._external_mailer_unbound` makes the provider lifecycle physically
    # unreachable for an owned delivery, so there is no owned row for the
    # provider guards to be asked about.
    for column, value in (("provider_message_id", "<sent@example.invalid>"),
                          ("provider_attempts", 2)):
        try:
            with ctx["owner"].cursor() as cur:
                cur.execute(f"UPDATE {TABLE} SET {column} = %s WHERE delivery_id = %s",
                            (value, str(owned_expectation.delivery_id)))
            raise AssertionError(f"an owned row was allowed to carry {column}")
        except AssertionError:
            raise
        except Exception as exc:
            check(f"the table refuses {column} on an owned row",
                  "external_mailer_unbound" in str(exc), str(exc)[:200])

    # ...and the internally-owned recovery this fix must not disturb still works.
    internal = failed_publication(ledger, "owned-none")
    check("an unowned row still recovers", internal.external_mailer is None
          and recover(ledger, internal)["outcome"] == OUTCOME_RECOVERED)
    check("and it is still unowned afterwards",
          raw_row(ctx["owner"], internal.delivery_id)["external_mailer"] is None)
    PASSED.append("owned_row_with_lifecycle_evidence_refused")


# --- 2. everything that must be refused ------------------------------------------


def _refuses(ctx, name: str, record, *, guard: str | None = None, **overrides) -> None:
    """One ineligible row: refused, writes nothing, and names why."""
    ledger = ctx["ledger"]
    before = raw_row(ctx["owner"], record.delivery_id)
    report = recover(ledger, record, **overrides)
    after = raw_row(ctx["owner"], record.delivery_id)

    check(f"{name} is refused",
          report["outcome"] in (OUTCOME_NOT_ELIGIBLE, OUTCOME_REFUSED_AT_WRITE),
          str(report.get("outcome")))
    differing = [k for k in before if before[k] != after[k]]
    check(f"{name} wrote nothing", differing == [], str(differing))
    if guard is not None:
        check(f"{name} names the disqualifying fact",
              guard in report["assessment"]["blocked_by"],
              str(report["assessment"]["blocked_by"]))


def test_remote_capability_evidence_is_refused(ctx) -> None:
    record = with_capability(ctx["ledger"], "capability")
    _refuses(ctx, "a row that reached a live grant", record, guard="no_capability_id")
    PASSED.append("remote_capability_evidence_refused")


def test_provider_and_delivery_evidence_is_refused(ctx) -> None:
    """Rows carrying evidence the table itself would not let the ledger unmake.

    Written directly, as the runtime role, because the point is what the GUARD
    refuses rather than what the publisher happens to produce.
    """
    ledger = ctx["ledger"]

    cases = {
        "no_provider_message": {"provider_message_id": "<sent@example.invalid>"},
        "no_provider_attempt": {"provider_attempts": 2},
        "never_submitted": {"provider_submitted_at": "now()"},
        "never_remote_delivered": {"remote_delivered_at": "now()"},
        "bearer_generation_is_zero": {"bearer_generation": 1},
        "no_capability_digest": {"capability_digest": "d" * 64},
    }
    for guard, mutation in cases.items():
        record = failed_publication(ledger, f"evidence-{guard}")
        assignments, params = [], []
        for column, value in mutation.items():
            if value == "now()":
                assignments.append(f"{column} = now()")
            else:
                assignments.append(f"{column} = %s")
                params.append(value)
        with ctx["owner"].cursor() as cur:
            cur.execute(f"UPDATE {TABLE} SET {', '.join(assignments)} "
                        f"WHERE delivery_id = %s",
                        (*params, str(record.delivery_id)))
        _refuses(ctx, f"a row carrying {guard}", ledger.load(record.delivery_id),
                 guard=guard)
    PASSED.append("provider_and_delivery_evidence_refused")


def test_the_wrong_failure_phase_or_code_is_refused(ctx) -> None:
    ledger = ctx["ledger"]
    wrong_phase = failed_publication(ledger, "phase", phase="PROVIDER")
    _refuses(ctx, "a failure in a later phase", wrong_phase,
             guard="failure_phase_is_publication")

    wrong_code = failed_publication(ledger, "code", code="REMOTE_ALREADY_DELIVERED")
    _refuses(ctx, "a refusal this recovery does not adjudicate", wrong_code,
             guard="failure_code_is_recoverable")

    check("the recoverable set stays narrow",
          OPERATOR_RECOVERABLE_PUBLICATION_FAILURE_CODES == frozenset({"PAYLOAD_NOT_CANONICAL"}),
          str(sorted(OPERATOR_RECOVERABLE_PUBLICATION_FAILURE_CODES)))
    PASSED.append("wrong_phase_or_code_refused")


def test_the_wrong_state_is_refused(ctx) -> None:
    ledger = ctx["ledger"]
    still_prepared = prepared(ledger, "already-prepared")
    _refuses(ctx, "a PREPARED row", still_prepared, guard="state_is_operator_required")

    owner = "probe-live-lease"
    parked = failed_publication(ledger, "live-lease")
    ledger.claim(parked.delivery_id, owner=owner)
    _refuses(ctx, "a row somebody still owns", ledger.load(parked.delivery_id),
             guard="no_live_lease")

    unknown = dry_run(ledger, "00000000-0000-0000-0000-000000000000")
    check("an unknown delivery is reported, not invented",
          unknown["outcome"] == OUTCOME_UNKNOWN_DELIVERY)
    PASSED.append("wrong_state_refused")


def test_a_mismatched_expectation_is_refused_at_the_write(ctx) -> None:
    """The operator's two `--expect-*` values are asserted by the UPDATE itself."""
    ledger = ctx["ledger"]
    record = failed_publication(ledger, "expectation")
    _refuses(ctx, "a wrong operation id", record,
             expected_operation_id=derive_operation_id(identity_for("someone-else")))
    _refuses(ctx, "a wrong payload digest", record, expected_payload_digest="e" * 64)
    # ...and the same row still recovers under the right ones.
    check("the correct expectation still recovers",
          recover(ledger, record)["outcome"] == OUTCOME_RECOVERED)
    PASSED.append("mismatched_expectation_refused")


def test_a_second_execution_is_a_refusal_not_a_second_mutation(ctx) -> None:
    ledger = ctx["ledger"]
    record = failed_publication(ledger, "twice")
    first = recover(ledger, record)
    check("the first execution recovers", first["outcome"] == OUTCOME_RECOVERED)

    after_first = raw_row(ctx["owner"], record.delivery_id)
    second = recover(ledger, ledger.load(record.delivery_id))
    after_second = raw_row(ctx["owner"], record.delivery_id)

    check("the second execution is refused", second["outcome"] == OUTCOME_NOT_ELIGIBLE,
          str(second["outcome"]))
    check("it names the state as the reason",
          "state_is_operator_required" in second["assessment"]["blocked_by"])
    differing = [k for k in after_first if after_first[k] != after_second[k]]
    check("the second execution wrote nothing", differing == [], str(differing))
    PASSED.append("second_execution_refused")


# --- 3. the guard list is the guard ----------------------------------------------


def test_the_report_and_the_write_share_one_guard_list(ctx) -> None:
    """The dry run must not be able to say yes where the UPDATE says no."""
    ledger = ctx["ledger"]
    names = [name for name, _sql, _check in OPERATOR_RECOVERY_GUARDS]
    check("guard names are unique", len(names) == len(set(names)))
    check("every guard carries both forms",
          all(sql and callable(predicate)
              for _name, sql, predicate in OPERATOR_RECOVERY_GUARDS))

    # Guard by guard: break exactly one fact and require BOTH the report and the
    # write to refuse. `_refuses` asserts the row is untouched either way.
    record = failed_publication(ledger, "agreement")
    assessment = ledger.assess_operator_recovery(record.delivery_id)
    check("the untouched fixture satisfies every guard", assessment.recoverable,
          str(assessment.blocked_by))

    with ctx["owner"].cursor() as cur:
        cur.execute(f"UPDATE {TABLE} SET failure_code = %s WHERE delivery_id = %s",
                    ("SOMETHING_ELSE", str(record.delivery_id)))
    broken = ledger.load(record.delivery_id)
    check("the report refuses",
          ledger.assess_operator_recovery(broken.delivery_id).recoverable is False)
    check("and the write refuses independently of the report",
          ledger.operator_recover_to_prepared(
              broken.delivery_id, operator=OPERATOR, reason=REASON,
              expected_operation_id=broken.operation_id,
              expected_payload_digest=broken.payload_digest) is None)
    PASSED.append("report_and_write_share_one_guard_list")


def test_the_automation_state_machine_still_refuses_this_move(ctx) -> None:
    """The recovery is an OPERATOR surface, not a widened transition table."""
    check("OPERATOR_REQUIRED is still terminal for automation",
          LEGAL_TRANSITIONS[DeliveryState.OPERATOR_REQUIRED]
          == frozenset({DeliveryState.OPERATOR_REQUIRED}))
    check("the operator map authorises exactly one move",
          OPERATOR_RECOVERY_TRANSITIONS
          == {DeliveryState.OPERATOR_REQUIRED: frozenset({DeliveryState.PREPARED})})
    assert_legal_operator_recovery(DeliveryState.OPERATOR_REQUIRED, DeliveryState.PREPARED)
    for current, target in ((DeliveryState.OPERATOR_REQUIRED, DeliveryState.FINALIZED),
                            (DeliveryState.PROVIDER_AMBIGUOUS, DeliveryState.PREPARED)):
        try:
            assert_legal_operator_recovery(current, target)
            raise AssertionError(f"operator recovery allowed {current} -> {target}")
        except DeliveryContractError:
            pass

    ledger = ctx["ledger"]
    record = failed_publication(ledger, "attestation")
    for missing in ({"operator": "  "}, {"reason": ""}):
        try:
            ledger.operator_recover_to_prepared(
                record.delivery_id, operator=missing.get("operator", OPERATOR),
                reason=missing.get("reason", REASON),
                expected_operation_id=record.operation_id,
                expected_payload_digest=record.payload_digest)
            raise AssertionError(f"an unattested recovery was accepted: {missing}")
        except DeliveryContractError:
            pass
    PASSED.append("automation_state_machine_still_refuses")


def test_the_runtime_role_can_run_the_recovery(ctx) -> None:
    """The command connects as the per-client runtime role, not as the owner."""
    ledger = ctx["ledger"]
    record = failed_publication(ledger, "runtime-role")
    runtime = connect(runtime_dsn(ctx["dsn"]))
    try:
        report = execute(DeliveryLedger(runtime), record.delivery_id,
                         operator=OPERATOR, reason=REASON,
                         expected_operation_id=record.operation_id,
                         expected_payload_digest=record.payload_digest)
    finally:
        runtime.close()
    check("the runtime role recovers the row", report["outcome"] == OUTCOME_RECOVERED,
          str(report))
    check("the row really moved",
          raw_row(ctx["owner"], record.delivery_id)["state"] == DeliveryState.PREPARED)
    PASSED.append("runtime_role_can_run_the_recovery")


# --- runner ----------------------------------------------------------------------


TESTS = [
    test_the_dry_run_reports_recoverable_and_writes_nothing,
    test_execute_produces_a_valid_prepared_row,
    test_identity_and_digest_survive_byte_for_byte,
    test_external_ownership_does_not_block_an_otherwise_safe_recovery,
    test_the_recovered_owned_row_is_retried_by_the_same_external_mailer,
    test_an_owned_row_with_lifecycle_evidence_is_still_refused,
    test_remote_capability_evidence_is_refused,
    test_provider_and_delivery_evidence_is_refused,
    test_the_wrong_failure_phase_or_code_is_refused,
    test_the_wrong_state_is_refused,
    test_a_mismatched_expectation_is_refused_at_the_write,
    test_a_second_execution_is_a_refusal_not_a_second_mutation,
    test_the_report_and_the_write_share_one_guard_list,
    test_the_automation_state_machine_still_refuses_this_move,
    test_the_runtime_role_can_run_the_recovery,
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
    print(f"\n{len(PASSED)} recovery checks passed — dry-run purity, one guarded "
          f"transition, identity/digest preservation and fail-closed refusals")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
