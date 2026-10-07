#!/usr/bin/env python3
"""Driver Eco Dashboard V1 — host publisher / e-mail delivery lifecycle suite.

Run:
    ECO_DASHBOARD_PUBLISHER_TEST_DSN=postgresql://postgres:pw@127.0.0.1:5433/disposable \\
      python3 ops/tests_manual/test_driver_eco_dashboard_publisher_lifecycle.py

WHAT IS REAL HERE AND WHAT IS NOT

Real: the host publisher, the durable PostgreSQL ledger with its CHECK
constraints, the canonical snapshot bytes produced by the actual host
publication interface, and **the actual Cloudflare Worker** — mounted by
`delivery/driver_eco_dashboard/local/publisher_serve.js` on in-memory D1/R2
bindings and driven over real HTTP. The publication, recovery and delivery
semantics under test are therefore the deployed ones, not a Python imitation.

Synthetic: the e-mail provider, the driver data, the recipient address, the
machine credential and the storage bindings.

NOT DONE, ANYWHERE IN THIS FILE: a real e-mail, a live provider call, a
wrangler invocation, a Cloudflare resource, a deployment, a schedule, or any
mutation of a production database.

DESTRUCTIVE. It drops and recreates `public.eco_dashboard_delivery_operation`
in the database the DSN names, so it refuses any DSN that is not loopback and
must be pointed at a disposable instance. A disposable one is one command:

    docker run -d --rm --name eco-dash-test-pg -e POSTGRES_PASSWORD=disposable \\
      -e POSTGRES_DB=eco_publisher_test -p 127.0.0.1:55433:5432 \\
      --tmpfs /var/lib/postgresql/data postgres:16-alpine

No capability, machine credential, session id or provider secret is printed by
any check. Secret comparisons happen in memory against values the test already
holds.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "ops" / "tests_manual") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "ops" / "tests_manual"))

from jobs.ecodriving_dashboard import email_provider as ep  # noqa: E402
from jobs.ecodriving_dashboard import publisher as pub  # noqa: E402
from jobs.ecodriving_dashboard import secure_delivery_client as sdc  # noqa: E402
from jobs.ecodriving_dashboard.dashboard_email import (  # noqa: E402
    CAPABILITY_FRAGMENT_PREFIX,
)
from jobs.ecodriving_dashboard.delivery_contract import (  # noqa: E402
    DeliveryIdentity,
    DeliveryState,
    NextAction,
    derive_operation_id,
    derive_provider_idempotency_key,
    safe_next_action,
)
from jobs.ecodriving_dashboard.delivery_ledger import (  # noqa: E402
    DeliveryLedger,
    LedgerConflict,
    capability_digest,
)
from ops.tests_manual.postgres_dsn_safety import require_loopback_dsn_or_exit  # noqa: E402

ENV = "ECO_DASHBOARD_PUBLISHER_TEST_DSN"
#: THE CHAIN. 049 creates the ledger and is applied shared history; 050 adds
#: the reviewed external-mailer ownership contract as a forward migration.
#: A fixture that applies only 049 builds a schema the approved runtime
#: cannot use, so both files are applied, in order.
MIGRATIONS = tuple(
    REPO_ROOT / "db" / "client_business" / name
    for name in ("049_eco_dashboard_delivery_operation.sql",
                 "050_eco_dashboard_external_mailer_ownership.sql")
)
MIGRATION = MIGRATIONS[0]
SERVER = REPO_ROOT / "delivery" / "driver_eco_dashboard" / "local" / "publisher_serve.js"

BASE_URL = "https://eco.example.invalid/dashboard"
MESSAGE_DOMAIN = "eco-dashboard.example.invalid"
RECIPIENT = "driver.one@example.invalid"
OTHER_RECIPIENT = "driver.two@example.invalid"

CLIENT_ID = "11111111-1111-1111-1111-111111111111"

PASSED: list[str] = []
#: Every raw secret this suite has ever observed. Asserted absent from every
#: printable surface at the end. Compared in memory, never printed.
OBSERVED_SECRETS: set[str] = set()


def check(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name}: {detail}" if detail else name)


# --- the real Worker, locally ---------------------------------------------------


class WorkerServer:
    """The actual Worker on in-memory bindings, over real HTTP."""

    def __init__(self) -> None:
        self.process = subprocess.Popen(
            ["node", str(SERVER), "0"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=str(REPO_ROOT))
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError("the local Worker did not start: "
                               + (self.process.stderr.read() or "")[:400])
        ready = json.loads(line)
        self.base_url = ready["base_url"]
        self.token = ready["publisher_token"]
        OBSERVED_SECRETS.add(self.token)

    def inspect(self) -> dict:
        with urllib.request.urlopen(self.base_url + "/__local__/inspect", timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))

    def leak_scan(self, needles) -> dict:
        body = json.dumps({"needles": list(needles)}).encode("utf-8")
        request = urllib.request.Request(self.base_url + "/__local__/leakscan",
                                         data=body, method="POST")
        request.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(request, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))

    def close(self) -> None:
        try:
            self.process.terminate()
            self.process.wait(timeout=5)
        except Exception:  # pragma: no cover - best effort
            self.process.kill()


class RecordingTransport:
    """Real HTTP to the local Worker, with deterministic fault injection.

    * `lose_response_on(path, phase)` performs the request — so the Worker
      really commits — and then raises the transport-unknown signal, which is
      exactly the crash window "committed remotely, answer never arrived";
    * every request is recorded, so a test can prove what the host actually
      transmitted (and, in particular, what it did not).
    """

    def __init__(self, base_url: str) -> None:
        self._inner = sdc.HttpSecureDeliveryTransport(base_url)
        self.requests: list[dict] = []
        self._lose: set[tuple] = set()

    def lose_response_on(self, path: str, phase: str | None = None) -> "RecordingTransport":
        self._lose.add((path, phase))
        return self

    def clear_faults(self) -> "RecordingTransport":
        self._lose.clear()
        return self

    def post(self, path, headers, body):
        phase = headers.get("X-Publication-Phase")
        self.requests.append({
            "path": path,
            "headers": dict(headers),
            "body": (body or b"").decode("utf-8", "replace"),
        })
        status, payload = self._inner.post(path, headers, body)
        for key in ((path, phase), (path, None)):
            if key in self._lose:
                self._lose.discard(key)
                raise sdc.TransportOutcomeUnknown(path, "INJECTED_RESPONSE_LOSS")
        return status, payload

    def transmitted(self) -> str:
        return json.dumps(self.requests, ensure_ascii=False)


# --- durable ledger -------------------------------------------------------------


def connect(dsn: str):
    import psycopg

    conn = psycopg.connect(dsn)
    conn.autocommit = True
    return conn


def dict_conn(dsn: str):
    import psycopg
    from psycopg.rows import dict_row

    conn = psycopg.connect(dsn, row_factory=dict_row)
    conn.autocommit = True
    return conn


def reset_schema(dsn: str) -> None:
    with connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS public.eco_dashboard_delivery_operation")
        # `SET LOCAL` outside a transaction is a no-op warning; the DDL itself
        # is what this suite applies, exactly as written.
        for migration in MIGRATIONS:
            cur.execute(migration.read_text(encoding="utf-8"))


def table_dump(dsn: str, *, include_bearer: bool = False) -> str:
    """Every stored value in the ledger, for leak scanning.

    `capability_secret` is excluded by default and that is the point: it is the
    ONE column allowed to hold a raw bearer, and only while an unresolved
    delivery still needs it. Everything else — diagnostics, metadata, provider
    fields, identifiers — must never contain one, and this dump is what that is
    asserted against.
    """
    column = "to_jsonb(t)" if include_bearer else "to_jsonb(t) - 'capability_secret'"
    with dict_conn(dsn) as conn, conn.cursor() as cur:
        cur.execute(f"SELECT {column} AS row FROM "
                    "public.eco_dashboard_delivery_operation t")
        return json.dumps([r["row"] for r in cur.fetchall()], default=str)


def retained_bearers(dsn: str) -> list[dict]:
    with dict_conn(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT state, operation_id FROM "
                    "public.eco_dashboard_delivery_operation "
                    "WHERE capability_secret IS NOT NULL")
        return [dict(r) for r in cur.fetchall()]


# --- canonical bytes from the real host publisher --------------------------------

_PAYLOAD: dict = {}


def canonical_payload() -> tuple[bytes, str]:
    if _PAYLOAD:
        return _PAYLOAD["body"], _PAYLOAD["digest"]
    import eco_dashboard_fixtures as fx

    from jobs.ecodriving_dashboard.publication import (
        PrivacyContext,
        build_publishable_snapshot,
        serialize_publishable_snapshot,
    )

    days = fx.daily_inputs(date(2026, 7, 1), fx.WEEKLY_DAY_KM, fx.ACCEPTABLE_TOTALS)
    current = fx.period_from_days(fx.CURRENT_WEEKLY, days, ranking=fx.RANKED_FACTS)
    previous_days = fx.daily_inputs(date(2026, 7, 1), fx.PREVIOUS_DAY_KM, fx.PREVIOUS_TOTALS)
    previous = fx.period_from_days(fx.PREVIOUS_WEEKLY, previous_days,
                                   ranking=fx.PREVIOUS_RANKED_FACTS)
    snapshot = build_publishable_snapshot(
        privacy=PrivacyContext(identity_key=fx.SYNTHETIC_IDENTITY_KEY,
                               client_code=fx.SYNTHETIC_CLIENT_CODE,
                               email_addresses=(RECIPIENT, OTHER_RECIPIENT)),
        generated_at_utc=fx.GENERATED_AT,
        period_type="weekly",
        current=current,
        previous=previous,
        days=days,
        series=fx.FULL_SERIES,
    )
    body = serialize_publishable_snapshot(snapshot)
    _PAYLOAD["body"] = body
    _PAYLOAD["digest"] = snapshot.payload_digest
    return body, snapshot.payload_digest


_SEQUENCE = {"n": 0}


def fresh_identity(period_type: str = "weekly") -> DeliveryIdentity:
    """A distinct logical delivery per scenario, so nothing shares an operation."""
    _SEQUENCE["n"] += 1
    return DeliveryIdentity(
        client_id=CLIENT_ID,
        identity_key=f"SYNTHETIC-DRIVER-{_SEQUENCE['n']:04d}",
        period_type=period_type,
        period_start_date=date(2026, 7, 1),
        period_end_date=date(2026, 7, 20),
    )


# --- harness --------------------------------------------------------------------


class Harness:
    """One 'process': its own connection, ledger, transport and services.

    A restart is a NEW Harness over the same database, the same Worker and the
    same provider — which is exactly what survives a crash in production.
    """

    def __init__(self, dsn: str, worker: WorkerServer, provider, *,
                 lease_seconds: int = 300, max_provider_attempts: int = 3) -> None:
        self.dsn = dsn
        self.conn = dict_conn(dsn)
        self.ledger = DeliveryLedger(self.conn)
        self.transport = RecordingTransport(worker.base_url)
        self.services = pub.PublisherServices(
            ledger=self.ledger,
            client=sdc.SecureDeliveryClient(self.transport, publisher_token=worker.token),
            provider=provider,
            config=pub.PublisherConfig(dashboard_base_url=BASE_URL,
                                       message_id_domain=MESSAGE_DOMAIN,
                                       lease_seconds=lease_seconds,
                                       max_provider_attempts=max_provider_attempts),
        )

    def advance(self, identity: DeliveryIdentity, *, recipient: str = RECIPIENT,
                owner: str | None = None, max_steps: int = pub.MAX_STEPS,
                body: bytes | None = None):
        payload, digest = canonical_payload()
        result = pub.advance_delivery(
            self.services,
            identity=identity,
            payload_digest=digest,
            recipient_email=recipient,
            body=payload if body is None else body,
            owner=owner or f"owner-{id(self)}",
            max_steps=max_steps,
        )
        if result.record is not None and result.record.row.get("capability_secret"):
            OBSERVED_SECRETS.add(result.record.capability_secret)
        return result

    def row(self, identity: DeliveryIdentity):
        record = self.ledger.find_by_identity(identity)
        if record is not None and record.row.get("capability_secret"):
            OBSERVED_SECRETS.add(record.capability_secret)
        return record

    def close(self) -> None:
        self.conn.close()


# --- 1. pre-publication persistence ---------------------------------------------


def test_operation_is_durable_before_any_remote_call(ctx) -> None:
    identity = fresh_identity()
    before = ctx["worker"].inspect()["operations"]
    host = Harness(ctx["dsn"], ctx["worker"], ep.FakeEmailProvider())
    try:
        _, digest = canonical_payload()
        record, created = pub.prepare_delivery(
            host.ledger, identity, payload_digest=digest, recipient_email=RECIPIENT)
        check("the operation was created", created)
        # THE pre-publication invariant, one field at a time.
        check("operation id is durable", record.operation_id == derive_operation_id(identity))
        check("subject reference is durable", bool(record.subject_ref))
        check("the exact canonical digest is durable", record.payload_digest == digest)
        check("the recipient binding is durable",
              record.recipient_email == RECIPIENT and record.recipient_identity)
        check("the state names the one safe next action",
              record.state == DeliveryState.PREPARED
              and record.next_action == NextAction.PUBLISH)
        # ...and nothing remote exists yet.
        check("no publication was created",
              ctx["worker"].inspect()["operations"] == before)
        check("no capability exists yet", record.capability_secret is None)

        # A second "process" finds the SAME logical operation.
        again = Harness(ctx["dsn"], ctx["worker"], ep.FakeEmailProvider())
        try:
            record2, created2 = pub.prepare_delivery(
                again.ledger, identity, payload_digest=digest, recipient_email=RECIPIENT)
            check("a restart finds the same operation, not a second one",
                  not created2 and record2.delivery_id == record.delivery_id)
        finally:
            again.close()
    finally:
        host.close()
    PASSED.append("operation_is_durable_before_any_remote_call")


# --- 2. the normal lifecycle -----------------------------------------------------


def test_normal_lifecycle_produces_exactly_one_message(ctx) -> None:
    identity = fresh_identity()
    provider = ep.FakeEmailProvider()
    host = Harness(ctx["dsn"], ctx["worker"], provider)
    try:
        before = ctx["worker"].inspect()
        result = host.advance(identity)
        after = ctx["worker"].inspect()
        record = host.row(identity)

        check("the delivery finalised", result.state == DeliveryState.FINALIZED,
              f"{result.state} / {result.detail}")
        check("the invocation reports completion",
              result.invocation == pub.INVOCATION.COMPLETED)
        check("nothing is left to do", record.next_action == NextAction.NONE)

        # Exactly one of everything, remotely.
        check("one publication operation", after["operations"] - before["operations"] == 1)
        check("one R2 object", after["objects"] - before["objects"] == 1)
        check("one grant", after["grants"] - before["grants"] == 1)
        check("one live grant", after["live_grants"] - before["live_grants"] == 1)
        check("the remote operation is DELIVERED",
              "DELIVERED" in after["operation_states"])

        # Exactly one message, to exactly the bound recipient.
        key = derive_provider_idempotency_key(record.operation_id)
        check("one accepted message", provider.accepted_count(key) == 1)
        check("one submission attempt", len(provider.submissions) == 1)
        message = provider.messages[key]
        check("the message went to the bound recipient",
              message["recipient_email"] == RECIPIENT)
        check("the host recorded the provider message id",
              record.provider_message_id == message["provider_message_id"])

        # The bearer is gone; its non-secret identity is not.
        check("the raw bearer was destroyed", record.capability_secret is None)
        check("cleanup is timestamped", record.bearer_cleared_at is not None)
        check("the grant identity survives cleanup",
              bool(record.capability_id) and bool(record.capability_digest))
        check("the canonical digest survives", record.payload_digest == canonical_payload()[1])

        # The canonical bytes reached the Worker unchanged.
        published = [r for r in host.transport.requests if r["path"] == "/api/publish"]
        check("exactly one publish request", len(published) == 1)
        check("the body is the exact canonical bytes",
              published[0]["body"].encode("utf-8") == canonical_payload()[0])
        check("the declared digest is the canonical digest",
              published[0]["headers"]["X-Publication-Payload-Digest"] == canonical_payload()[1])
    finally:
        host.close()
    PASSED.append("normal_lifecycle_produces_exactly_one_message")


def test_message_carries_the_current_capability_only(ctx) -> None:
    """The link in the message is the capability the ledger held — not a stale one."""
    identity = fresh_identity()
    provider = ep.FakeEmailProvider()
    host = Harness(ctx["dsn"], ctx["worker"], provider)
    try:
        host.advance(identity, max_steps=1)          # publish + persist bearer
        record = host.row(identity)
        capability = record.capability_secret
        OBSERVED_SECRETS.add(capability)
        host.advance(identity)                        # finish
        key = derive_provider_idempotency_key(record.operation_id)
        body = provider.messages[key]["text_body"]
        # Compared in memory. Neither value is printed.
        check("the message contains the fragment link",
              f"{CAPABILITY_FRAGMENT_PREFIX}{capability}" in body)
        check("the capability is never in a query string", "?k=" not in body)
        check("the digest recorded matches the delivered bearer",
              host.row(identity).capability_digest == capability_digest(capability))
    finally:
        host.close()
    PASSED.append("message_carries_the_current_capability_only")


# --- 3. publication response loss ------------------------------------------------


def test_publication_response_loss_never_publishes_twice(ctx) -> None:
    identity = fresh_identity()
    provider = ep.FakeEmailProvider()
    before = ctx["worker"].inspect()

    crashed = Harness(ctx["dsn"], ctx["worker"], provider)
    try:
        crashed.transport.lose_response_on("/api/publish")
        result = crashed.advance(identity)
        check("the host did not invent an outcome",
              result.invocation == pub.INVOCATION.RETRY_LATER, result.detail)
        record = crashed.row(identity)
        check("the state is unchanged and still says PUBLISH",
              record.state == DeliveryState.PREPARED
              and record.next_action == NextAction.PUBLISH)
        check("no bearer was persisted", record.capability_secret is None)
    finally:
        crashed.close()

    mid = ctx["worker"].inspect()
    check("the publication DID commit remotely", mid["operations"] - before["operations"] == 1)

    restarted = Harness(ctx["dsn"], ctx["worker"], provider)
    try:
        result = restarted.advance(identity)
        after = ctx["worker"].inspect()
        record = restarted.row(identity)
        check("the restart completed the delivery",
              result.state == DeliveryState.FINALIZED, f"{result.state}/{result.detail}")
        check("no second publication operation",
              after["operations"] - before["operations"] == 1)
        check("no second object", after["objects"] - before["objects"] == 1)
        check("exactly one live grant", after["live_grants"] - before["live_grants"] == 1)
        check("the bearer generation advanced by exactly one recovery",
              record.bearer_generation == 2, str(record.bearer_generation))
        recoveries = [r for r in restarted.transport.requests
                      if r["path"] == "/api/publish/recover"]
        check("recovery was used, not a re-publish", len(recoveries) == 1)
        key = derive_provider_idempotency_key(record.operation_id)
        check("exactly one message", provider.accepted_count(key) == 1)
    finally:
        restarted.close()
    PASSED.append("publication_response_loss_never_publishes_twice")


def test_crash_before_bearer_persistence_uses_recovery(ctx) -> None:
    """The response arrived, the host died before storing the bearer.

    The predecessor bearer is unrecoverable by contract, so the only safe move
    is the explicit recovery operation — and the predecessor must never appear
    in a message.
    """
    identity = fresh_identity()
    provider = ep.FakeEmailProvider()
    lost_bearer = {}

    class LosesTheBearer(DeliveryLedger):
        def record_capability(self, record, *, owner, capability, capability_id,
                              expires_at, bearer_generation):
            # The process dies here: the raw bearer was received and is gone.
            lost_bearer["capability"] = capability
            OBSERVED_SECRETS.add(capability)
            raise RuntimeError("SIMULATED_CRASH_BEFORE_BEARER_PERSISTENCE")

    conn = dict_conn(ctx["dsn"])
    try:
        ledger = LosesTheBearer(conn)
        services = pub.PublisherServices(
            ledger=ledger,
            client=sdc.SecureDeliveryClient(RecordingTransport(ctx["worker"].base_url),
                                            publisher_token=ctx["worker"].token),
            provider=provider,
            config=pub.PublisherConfig(dashboard_base_url=BASE_URL,
                                       message_id_domain=MESSAGE_DOMAIN))
        payload, digest = canonical_payload()
        try:
            pub.advance_delivery(services, identity=identity, payload_digest=digest,
                                 recipient_email=RECIPIENT, body=payload, owner="crasher")
        except RuntimeError as error:
            check("the simulated crash is the one we injected",
                  "SIMULATED_CRASH" in str(error))
        else:
            raise AssertionError("the injected crash did not happen")
    finally:
        conn.close()

    check("a bearer really was issued and lost", bool(lost_bearer.get("capability")))

    restarted = Harness(ctx["dsn"], ctx["worker"], provider)
    try:
        result = restarted.advance(identity)
        record = restarted.row(identity)
        check("the restart completed", result.state == DeliveryState.FINALIZED,
              f"{result.state}/{result.detail}")
        recoveries = [r for r in restarted.transport.requests
                      if r["path"] == "/api/publish/recover"]
        check("explicit recovery was used", len(recoveries) == 1)
        publishes = [r for r in restarted.transport.requests if r["path"] == "/api/publish"]
        check("exactly one publish attempt on the restart", len(publishes) == 1)

        key = derive_provider_idempotency_key(record.operation_id)
        sent = provider.messages[key]
        # THE predecessor-suppression assertion. Compared in memory.
        predecessor = lost_bearer["capability"]
        check("the revoked predecessor was never e-mailed",
              predecessor not in sent["text_body"] and predecessor not in sent["html_body"])
        check("the message carries the replacement",
              record.capability_digest != capability_digest(predecessor))
        for submission in provider.submissions:
            check("no constructed message ever contained the predecessor",
                  predecessor not in submission["text_body"]
                  and predecessor not in submission["html_body"])
    finally:
        restarted.close()
    PASSED.append("crash_before_bearer_persistence_uses_recovery")


# --- 4. provider semantics -------------------------------------------------------


def test_provider_accepted_but_response_lost(ctx) -> None:
    """Crash-matrix stage 9. One message, reconciled, no duplicate."""
    identity = fresh_identity()
    provider = ep.FakeEmailProvider()
    operation_id = derive_operation_id(identity)
    key = derive_provider_idempotency_key(operation_id)
    provider.script(key, "accept_then_lose_response")

    first = Harness(ctx["dsn"], ctx["worker"], provider)
    try:
        # Three durable steps: publish, delivery intent, submission. The third
        # one is where the process dies — after the provider took the message
        # and before any answer came back.
        result = first.advance(identity, max_steps=3)
        record = first.row(identity)
        check("the host knows a message may exist",
              record.state == DeliveryState.PROVIDER_SUBMISSION_PENDING,
              f"{record.state}/{result.detail}")
        check("the safe next action is reconciliation",
              record.next_action == NextAction.RECONCILE_PROVIDER)
        check("the message really does exist remotely", provider.accepted_count(key) == 1)
        check("the host recorded no acceptance", record.provider_message_id is None)
        check("the remote operation is NOT delivered",
              record.remote_delivered_at is None)
    finally:
        first.close()

    # The scripted behaviour is spent: a replay would now be answered normally,
    # which is precisely the situation in which a careless host sends twice.
    restarted = Harness(ctx["dsn"], ctx["worker"], provider)
    try:
        result = restarted.advance(identity)
        record = restarted.row(identity)
        check("the restart completed", result.state == DeliveryState.FINALIZED,
              f"{result.state}/{result.detail}")
        check("still exactly one message", provider.accepted_count(key) == 1)
        check("no duplicate message was created",
              len([m for m in provider.messages if m.startswith(key)]) == 1)
        check("acceptance was reconciled, not assumed",
              record.provider_message_id == provider.messages[key]["provider_message_id"])
        check("the reconciliation is recorded as such",
              (record.metadata_json or {}).get("acceptance_source") == "RECONCILED")
        check("the provider was asked about the key", key in provider.reconciliations)
    finally:
        restarted.close()
    PASSED.append("provider_accepted_but_response_lost")


def test_provider_rejection_never_becomes_delivered(ctx) -> None:
    identity = fresh_identity()
    provider = ep.FakeEmailProvider()
    key = derive_provider_idempotency_key(derive_operation_id(identity))
    provider.script(key, "reject")

    host = Harness(ctx["dsn"], ctx["worker"], provider, max_provider_attempts=2)
    try:
        result = host.advance(identity)
        record = host.row(identity)
        check("the delivery is explicitly rejected",
              record.state == DeliveryState.PROVIDER_REJECTED, record.state)
        check("a permanent rejection needs an operator",
              record.operator_action_required is True)
        check("the failure is named without a secret",
              record.failure_code == "RECIPIENT_REFUSED" and record.failure_phase == "PROVIDER")
        check("no message exists", provider.accepted_count(key) == 0)
        check("nothing was marked delivered locally", record.remote_delivered_at is None)
        check("nothing was marked delivered remotely",
              record.state != DeliveryState.REMOTE_DELIVERED)
        delivered = [r for r in host.transport.requests
                     if r["headers"].get("X-Publication-Phase") == "DELIVERED"]
        check("the remote DELIVERED transition was never attempted", not delivered)
        check("the invocation says a human is needed",
              result.invocation == pub.INVOCATION.OPERATOR_REQUIRED)

        # A retry does not quietly resume a terminal refusal.
        again = host.advance(identity)
        check("a rerun stays refused", again.state == DeliveryState.PROVIDER_REJECTED)
        check("no message appeared", provider.accepted_count(key) == 0)
    finally:
        host.close()

    # A TRANSIENT rejection stays retryable, and the retry uses the same key.
    identity2 = fresh_identity()
    provider2 = ep.FakeEmailProvider()
    key2 = derive_provider_idempotency_key(derive_operation_id(identity2))
    provider2.script(key2, "reject_transient")
    host2 = Harness(ctx["dsn"], ctx["worker"], provider2, max_provider_attempts=3)
    try:
        host2.advance(identity2)
        record = host2.row(identity2)
        check("a transient rejection is retryable",
              record.state == DeliveryState.PROVIDER_REJECTED
              and record.operator_action_required is False)
        provider2.script(key2, "accept")
        result = host2.advance(identity2)
        check("the retry completes", result.state == DeliveryState.FINALIZED, result.detail)
        check("the retry used the same idempotency identity",
              {s["idempotency_key"] for s in provider2.submissions} == {key2})
        check("exactly one message", provider2.accepted_count(key2) == 1)
    finally:
        host2.close()
    PASSED.append("provider_rejection_never_becomes_delivered")


def test_ambiguous_acceptance_never_resends(ctx) -> None:
    """A provider that cannot deduplicate and cannot be queried.

    The honest answer is an explicit state and a human, and the test proves the
    host takes it instead of resending — against a fake that WOULD have
    duplicated.
    """
    identity = fresh_identity()
    provider = ep.FakeEmailProvider(supports_idempotent_submit=False,
                                    supports_reconciliation=False)
    key = derive_provider_idempotency_key(derive_operation_id(identity))
    provider.script(key, "accept_then_lose_response")

    host = Harness(ctx["dsn"], ctx["worker"], provider)
    try:
        host.advance(identity, max_steps=3)
        record = host.row(identity)
        check("the submission is pending after the lost response",
              record.state == DeliveryState.PROVIDER_SUBMISSION_PENDING, record.state)
        check("one message exists remotely", provider.accepted_count(key) == 1)
    finally:
        host.close()

    restarted = Harness(ctx["dsn"], ctx["worker"], provider)
    try:
        result = restarted.advance(identity)
        record = restarted.row(identity)
        check("ambiguity is explicit",
              record.state == DeliveryState.PROVIDER_AMBIGUOUS, record.state)
        check("an operator is required", record.operator_action_required is True)
        check("the invocation says so",
              result.invocation == pub.INVOCATION.OPERATOR_REQUIRED)
        check("NO second message was created", len(provider.messages) == 1)
        check("the host did not submit again", len(provider.submissions) == 1)
        check("nothing was delivered", record.remote_delivered_at is None)
        # And it stays that way, however many times the scheduler fires.
        for _ in range(3):
            restarted.advance(identity)
        check("repeated invocations never resend", len(provider.submissions) == 1)
        check("still ambiguous",
              restarted.row(identity).state == DeliveryState.PROVIDER_AMBIGUOUS)
    finally:
        restarted.close()

    # The same provider shape, but the message genuinely never reached it: the
    # host must still refuse to guess.
    identity2 = fresh_identity()
    provider2 = ep.FakeEmailProvider(supports_idempotent_submit=False,
                                     supports_reconciliation=False)
    key2 = derive_provider_idempotency_key(derive_operation_id(identity2))
    provider2.script(key2, "timeout_before_acceptance")
    host2 = Harness(ctx["dsn"], ctx["worker"], provider2)
    try:
        host2.advance(identity2)
        host2.advance(identity2)
        record = host2.row(identity2)
        check("a timeout with no lookup is also ambiguous",
              record.state == DeliveryState.PROVIDER_AMBIGUOUS, record.state)
        check("no message was ever created", len(provider2.messages) == 0)
    finally:
        host2.close()
    PASSED.append("ambiguous_acceptance_never_resends")


# --- 5. recipient binding --------------------------------------------------------


def test_recipient_binding_cannot_silently_change(ctx) -> None:
    identity = fresh_identity()
    provider = ep.FakeEmailProvider()
    host = Harness(ctx["dsn"], ctx["worker"], provider)
    try:
        host.advance(identity, max_steps=1)
        before = host.row(identity)

        result = host.advance(identity, recipient=OTHER_RECIPIENT)
        check("a different recipient is a conflict",
              result.invocation == pub.INVOCATION.CONFLICT, result.invocation)
        check("the conflict is named", result.conflict_code == "RECIPIENT_CONFLICT")

        after = host.row(identity)
        check("the bound recipient is unchanged", after.recipient_email == RECIPIENT)
        check("nothing else moved either",
              after.state == before.state
              and after.recipient_identity == before.recipient_identity)
        check("no message was sent to the other address",
              not any(s["recipient_email"] == OTHER_RECIPIENT
                      for s in provider.submissions))

        # The same recipient — differing only in domain case, which RFC 5321
        # makes insignificant — reconciles the existing lifecycle rather than
        # conflicting with it.
        local, _, domain = RECIPIENT.rpartition("@")
        resumed = host.advance(identity, recipient=f"{local}@{domain.upper()}")
        check("the same recipient continues the same delivery",
              resumed.invocation != pub.INVOCATION.CONFLICT, resumed.invocation)
        check("the same recipient completes it",
              resumed.state == DeliveryState.FINALIZED, resumed.detail)

        # A local part differing only in case is a DIFFERENT mailbox and must
        # not be silently accepted as the bound recipient.
        shouted = host.advance(identity, recipient=f"{local.upper()}@{domain}")
        check("a case-different local part is a conflict, not a match",
              shouted.invocation == pub.INVOCATION.CONFLICT, shouted.invocation)
    finally:
        host.close()
    PASSED.append("recipient_binding_cannot_silently_change")


def test_recipient_email_never_reaches_the_delivery_boundary(ctx) -> None:
    identity = fresh_identity()
    provider = ep.FakeEmailProvider()
    host = Harness(ctx["dsn"], ctx["worker"], provider)
    try:
        host.advance(identity)
        transmitted = host.transport.transmitted()
        for needle in (RECIPIENT, identity.identity_key, CLIENT_ID, "example.invalid"):
            check("host-only data was never transmitted", needle not in transmitted, needle)

        # The needles are exactly the values that must exist ONLY on the host.
        # The period type and the period dates are legitimately part of the
        # published snapshot document and are deliberately not needles.
        needles = [RECIPIENT, identity.identity_key, CLIENT_ID,
                   RECIPIENT.split("@")[0], RECIPIENT.split("@")[1]]
        verdicts = ctx["worker"].leak_scan(needles)
        check("every needle was actually scanned",
              len(verdicts) == len(needles)
              and all(v["length"] >= 4 for v in verdicts), json.dumps(verdicts))
        check("nothing host-only is in Worker/D1/R2 state",
              not any(v["present"] for v in verdicts), json.dumps(verdicts))

        # The scan is a real oracle: a value that IS in the published payload
        # is found, so a clean result is evidence rather than a broken probe.
        control = ctx["worker"].leak_scan(["contract_id", "driver_eco_dashboard"])
        check("the leak scan can find something that is there",
              all(v["present"] for v in control), json.dumps(control))
    finally:
        host.close()
    PASSED.append("recipient_email_never_reaches_the_delivery_boundary")


# --- 6. the crash matrix ---------------------------------------------------------


def test_crash_matrix_has_one_safe_action_everywhere(ctx) -> None:
    """Twelve boundaries, each with exactly one safe next action, each verified.

    Boundaries are produced by stopping the state machine after a bounded
    number of DURABLE steps, or by injecting the specific loss that defines the
    window. Nothing is simulated by writing a state directly.
    """
    provider = ep.FakeEmailProvider()
    outcomes: dict[int, str] = {}

    # 1. before local persistence — nothing exists; a run creates exactly one.
    identity = fresh_identity()
    host = Harness(ctx["dsn"], ctx["worker"], provider)
    try:
        check("nothing is persisted yet", host.row(identity) is None)
        host.advance(identity, max_steps=0)
        check("a zero-step run still persists the operation first",
              host.row(identity) is not None)
        outcomes[1] = safe_next_action(host.row(identity).state)
    finally:
        host.close()

    # 2. persisted, before the publish request.
    identity = fresh_identity()
    host = Harness(ctx["dsn"], ctx["worker"], provider)
    try:
        _, digest = canonical_payload()
        pub.prepare_delivery(host.ledger, identity, payload_digest=digest,
                             recipient_email=RECIPIENT)
        outcomes[2] = safe_next_action(host.row(identity).state)
        check("boundary 2 publishes", outcomes[2] == NextAction.PUBLISH)
        check("boundary 2 converges", host.advance(identity).state == DeliveryState.FINALIZED)
    finally:
        host.close()

    # 3. publication committed, response not received.
    identity = fresh_identity()
    host = Harness(ctx["dsn"], ctx["worker"], provider)
    try:
        host.transport.lose_response_on("/api/publish")
        host.advance(identity)
        outcomes[3] = safe_next_action(host.row(identity).state)
        check("boundary 3 publishes again, idempotently", outcomes[3] == NextAction.PUBLISH)
    finally:
        host.close()
    host = Harness(ctx["dsn"], ctx["worker"], provider)
    try:
        check("boundary 3 converges", host.advance(identity).state == DeliveryState.FINALIZED)
        check("boundary 3 recovered rather than republished",
              host.row(identity).bearer_generation == 2)
    finally:
        host.close()

    # 4. response received, before raw bearer persistence -> recovery required.
    identity = fresh_identity()
    host = Harness(ctx["dsn"], ctx["worker"], provider)
    try:
        host.transport.lose_response_on("/api/publish")
        host.advance(identity)
        host.transport.clear_faults()
        host.advance(identity, max_steps=1)   # publish -> ALREADY_PUBLISHED
        record = host.row(identity)
        check("boundary 4 is bearer recovery",
              record.state == DeliveryState.BEARER_RECOVERY_REQUIRED, record.state)
        outcomes[4] = safe_next_action(record.state)
        check("boundary 4 recovers", outcomes[4] == NextAction.RECOVER_BEARER)
        check("boundary 4 converges", host.advance(identity).state == DeliveryState.FINALIZED)
    finally:
        host.close()

    # 5. capability persisted, before delivery intent.
    identity = fresh_identity()
    host = Harness(ctx["dsn"], ctx["worker"], provider)
    try:
        host.advance(identity, max_steps=1)
        record = host.row(identity)
        check("boundary 5 holds the bearer",
              record.state == DeliveryState.CAPABILITY_PERSISTED and record.has_bearer)
        outcomes[5] = safe_next_action(record.state)
        check("boundary 5 records intent", outcomes[5] == NextAction.RECORD_DELIVERY_INTENT)
        check("boundary 5 converges", host.advance(identity).state == DeliveryState.FINALIZED)
    finally:
        host.close()

    # 6. intent persisted, before the provider call.
    identity = fresh_identity()
    host = Harness(ctx["dsn"], ctx["worker"], provider)
    try:
        host.advance(identity, max_steps=2)
        record = host.row(identity)
        check("boundary 6 has intent", record.state == DeliveryState.DELIVERY_INTENT_RECORDED)
        check("boundary 6 made no provider attempt", record.provider_attempts == 0)
        outcomes[6] = safe_next_action(record.state)
        check("boundary 6 submits", outcomes[6] == NextAction.SUBMIT_TO_PROVIDER)
        check("boundary 6 converges", host.advance(identity).state == DeliveryState.FINALIZED)
    finally:
        host.close()

    # 7. provider call made, definitely rejected.
    identity = fresh_identity()
    rejecting = ep.FakeEmailProvider()
    key = derive_provider_idempotency_key(derive_operation_id(identity))
    rejecting.script(key, "reject")
    host = Harness(ctx["dsn"], ctx["worker"], rejecting)
    try:
        host.advance(identity)
        record = host.row(identity)
        check("boundary 7 is an explicit rejection",
              record.state == DeliveryState.PROVIDER_REJECTED)
        outcomes[7] = safe_next_action(record.state)
        check("boundary 7 needs a decision", outcomes[7] == NextAction.RETRY_OR_OPERATOR)
        check("boundary 7 created no message", rejecting.accepted_count(key) == 0)
    finally:
        host.close()

    # 8. provider accepted, response lost before the host recorded acceptance.
    identity = fresh_identity()
    losing = ep.FakeEmailProvider()
    key = derive_provider_idempotency_key(derive_operation_id(identity))
    losing.script(key, "accept_then_lose_response")
    host = Harness(ctx["dsn"], ctx["worker"], losing)
    try:
        host.advance(identity, max_steps=3)
        record = host.row(identity)
        check("boundary 8 is pending", record.state == DeliveryState.PROVIDER_SUBMISSION_PENDING)
        outcomes[8] = safe_next_action(record.state)
        check("boundary 8 reconciles", outcomes[8] == NextAction.RECONCILE_PROVIDER)
    finally:
        host.close()
    host = Harness(ctx["dsn"], ctx["worker"], losing)
    try:
        check("boundary 8 converges", host.advance(identity).state == DeliveryState.FINALIZED)
        check("boundary 8 produced one message", losing.accepted_count(key) == 1)
        check("boundary 8 submitted exactly once", len(losing.submissions) == 1)
    finally:
        host.close()

    # 9. acceptance recorded locally, before the remote DELIVERED transition.
    identity = fresh_identity()
    host = Harness(ctx["dsn"], ctx["worker"], provider)
    try:
        host.advance(identity, max_steps=3)
        record = host.row(identity)
        check("boundary 9 has acceptance",
              record.state == DeliveryState.PROVIDER_ACCEPTED
              and record.provider_message_id, record.state)
        check("boundary 9 has not delivered remotely", record.remote_delivered_at is None)
        outcomes[9] = safe_next_action(record.state)
        check("boundary 9 records DELIVERED", outcomes[9] == NextAction.RECORD_REMOTE_DELIVERED)
        check("boundary 9 converges", host.advance(identity).state == DeliveryState.FINALIZED)
    finally:
        host.close()

    # 10. remote DELIVERED committed, response lost.
    identity = fresh_identity()
    host = Harness(ctx["dsn"], ctx["worker"], provider)
    try:
        host.transport.lose_response_on("/api/publish/delivery", "DELIVERED")
        host.advance(identity)
        record = host.row(identity)
        check("boundary 10 stays at acceptance",
              record.state == DeliveryState.PROVIDER_ACCEPTED, record.state)
        outcomes[10] = safe_next_action(record.state)
    finally:
        host.close()
    host = Harness(ctx["dsn"], ctx["worker"], provider)
    try:
        result = host.advance(identity)
        check("boundary 10 converges idempotently",
              result.state == DeliveryState.FINALIZED, result.detail)
        key = derive_provider_idempotency_key(derive_operation_id(identity))
        check("boundary 10 sent no second message", provider.accepted_count(key) == 1)
    finally:
        host.close()

    # 11. remote DELIVERED confirmed, before bearer cleanup.
    identity = fresh_identity()
    host = Harness(ctx["dsn"], ctx["worker"], provider)
    try:
        host.advance(identity, max_steps=4)
        record = host.row(identity)
        check("boundary 11 is delivered remotely",
              record.state == DeliveryState.REMOTE_DELIVERED, record.state)
        check("boundary 11 still holds the bearer", record.has_bearer)
        outcomes[11] = safe_next_action(record.state)
        check("boundary 11 cleans up", outcomes[11] == NextAction.CLEANUP_BEARER)
        check("boundary 11 converges", host.advance(identity).state == DeliveryState.FINALIZED)
    finally:
        host.close()

    # 12. after final cleanup.
    host = Harness(ctx["dsn"], ctx["worker"], provider)
    try:
        record = host.row(identity)
        check("boundary 12 is terminal", record.state == DeliveryState.FINALIZED)
        outcomes[12] = safe_next_action(record.state)
        check("boundary 12 does nothing", outcomes[12] == NextAction.NONE)
        result = host.advance(identity)
        check("a rerun after cleanup changes nothing",
              result.state == DeliveryState.FINALIZED and result.steps == 0)
    finally:
        host.close()

    check("every boundary produced an action", len(outcomes) == 12, str(sorted(outcomes)))
    PASSED.append("crash_matrix_has_one_safe_action_everywhere")


# --- 7. concurrency --------------------------------------------------------------


def run_concurrent(ctx, identity, provider, workers: int) -> list:
    results: list = []
    errors: list = []
    barrier = threading.Barrier(workers)

    def worker(index: int) -> None:
        host = Harness(ctx["dsn"], ctx["worker"], provider)
        try:
            barrier.wait(timeout=30)
            results.append(host.advance(identity, owner=f"owner-{index}"))
        except BaseException as error:  # recorded, never swallowed
            errors.append(f"{type(error).__name__}: {error}")
        finally:
            host.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    check("no concurrent invocation raised", not errors, "; ".join(errors[:3]))
    return results


def test_concurrent_invocations_converge_to_one_lifecycle(ctx) -> None:
    for workers in (2, 8, 32):
        identity = fresh_identity()
        provider = ep.FakeEmailProvider()
        before = ctx["worker"].inspect()
        results = run_concurrent(ctx, identity, provider, workers)
        after = ctx["worker"].inspect()

        reader = Harness(ctx["dsn"], ctx["worker"], provider)
        try:
            record = reader.row(identity)
            with reader.conn.cursor() as cur:
                cur.execute("SELECT count(*) AS n FROM public.eco_dashboard_delivery_operation "
                            "WHERE client_id = %s AND identity_key = %s",
                            (identity.client_id, identity.identity_key))
                rows = cur.fetchone()["n"]
        finally:
            reader.close()

        key = derive_provider_idempotency_key(derive_operation_id(identity))
        label = f"{workers}-way"
        check(f"{label}: one host delivery record", rows == 1, str(rows))
        check(f"{label}: one publication operation",
              after["operations"] - before["operations"] == 1)
        check(f"{label}: one object", after["objects"] - before["objects"] == 1)
        check(f"{label}: one live capability",
              after["live_grants"] - before["live_grants"] == 1)
        check(f"{label}: one provider idempotency identity",
              {s["idempotency_key"] for s in provider.submissions} == {key},
              str({s["idempotency_key"] for s in provider.submissions}))
        check(f"{label}: at most one accepted message",
              provider.accepted_count(key) <= 1 and len(provider.messages) <= 1)
        check(f"{label}: exactly one final DELIVERED",
              after["operation_states"].count("DELIVERED")
              - before["operation_states"].count("DELIVERED") == 1)
        check(f"{label}: the delivery finalised",
              record.state == DeliveryState.FINALIZED, record.state)
        check(f"{label}: no work was duplicated",
              record.provider_attempts == 1, str(record.provider_attempts))
        # THE concurrency invariant, stated so it cannot pass by timing luck:
        # exactly one invocation performed any durable step. Every other one
        # either lost the lease or arrived after the delivery was already
        # terminal, and in both cases did nothing at all.
        working = [r for r in results if r.steps > 0]
        check(f"{label}: exactly one invocation did work",
              len(working) == 1, str([r.steps for r in results]))
        check(f"{label}: every other invocation was a no-op",
              all(r.steps == 0 for r in results if r is not working[0]))
        check(f"{label}: every invocation returned", len(results) == workers,
              str(len(results)))
    PASSED.append("concurrent_invocations_converge_to_one_lifecycle")


def test_an_expired_lease_can_be_reclaimed(ctx) -> None:
    """A crashed owner must not park a delivery forever."""
    identity = fresh_identity()
    provider = ep.FakeEmailProvider()
    dead = Harness(ctx["dsn"], ctx["worker"], provider, lease_seconds=0)
    try:
        _, digest = canonical_payload()
        record, _ = pub.prepare_delivery(dead.ledger, identity, payload_digest=digest,
                                         recipient_email=RECIPIENT)
        held = dead.ledger.claim(record.delivery_id, owner="dead-owner", lease_seconds=600)
        check("the dead owner holds the lease", held is not None)

        live = Harness(ctx["dsn"], ctx["worker"], provider)
        try:
            blocked = live.advance(identity)
            check("a live lease blocks another invocation",
                  blocked.invocation == pub.INVOCATION.NOT_OWNED, blocked.invocation)
            # Expire it the way time would.
            with live.conn.cursor() as cur:
                cur.execute("UPDATE public.eco_dashboard_delivery_operation "
                            "SET lease_expires_at = now() - interval '1 hour' "
                            "WHERE delivery_id = %s", (str(record.delivery_id),))
            resumed = live.advance(identity)
            check("an expired lease is reclaimed",
                  resumed.state == DeliveryState.FINALIZED, resumed.detail)
        finally:
            live.close()
    finally:
        dead.close()
    PASSED.append("an_expired_lease_can_be_reclaimed")


# --- 8. bearer retention and leakage ---------------------------------------------


def test_bearer_retention_is_minimal_and_deterministic(ctx) -> None:
    identity = fresh_identity()
    provider = ep.FakeEmailProvider()
    host = Harness(ctx["dsn"], ctx["worker"], provider)
    try:
        # It exists while an unresolved delivery still needs it...
        host.advance(identity, max_steps=1)
        check("held at CAPABILITY_PERSISTED", host.row(identity).has_bearer)
        host.advance(identity, max_steps=1)
        check("held at DELIVERY_INTENT_RECORDED", host.row(identity).has_bearer)
        host.advance(identity, max_steps=1)
        check("held at PROVIDER_ACCEPTED", host.row(identity).has_bearer)
        host.advance(identity, max_steps=1)
        check("held at REMOTE_DELIVERED", host.row(identity).has_bearer)
        capability = host.row(identity).capability_secret
        OBSERVED_SECRETS.add(capability)

        # ...and stops existing the moment it is no longer operationally needed.
        host.advance(identity)
        record = host.row(identity)
        check("finalised", record.state == DeliveryState.FINALIZED)
        check("the raw bearer is gone", record.capability_secret is None)
        check("cleanup is recorded", record.bearer_cleared_at is not None)
        check("the audit identity survives",
              record.capability_digest == capability_digest(capability))

        # No historical bearer is kept for audit convenience anywhere.
        dump = table_dump(ctx["dsn"])
        check("no raw bearer survives anywhere in the ledger", capability not in dump)
    finally:
        host.close()

    # An unresolved delivery keeps its bearer, because reconciliation may still
    # need to establish which link the message carried.
    identity2 = fresh_identity()
    ambiguous = ep.FakeEmailProvider(supports_idempotent_submit=False,
                                     supports_reconciliation=False)
    key = derive_provider_idempotency_key(derive_operation_id(identity2))
    ambiguous.script(key, "accept_then_lose_response")
    host2 = Harness(ctx["dsn"], ctx["worker"], ambiguous)
    try:
        host2.advance(identity2)
        record = host2.row(identity2)
        check("an ambiguous delivery keeps its bearer",
              record.state == DeliveryState.PROVIDER_AMBIGUOUS and record.has_bearer)
    finally:
        host2.close()
    PASSED.append("bearer_retention_is_minimal_and_deterministic")


#: Either DB-side control may be the one that fires — the BEFORE trigger runs
#: first, and the CHECK constraint stands behind it for the cases a NEW row can
#: answer on its own. The test asserts the INVARIANT, not which control caught it.
BEARER_DIAGNOSTIC_REFUSALS = ("no_bearer_in_diagnostics",
                              "ECO_DASHBOARD_BEARER_IN_DIAGNOSTICS")


def refused_bearer_in_diagnostics(error) -> bool:
    return any(marker in str(error) for marker in BEARER_DIAGNOSTIC_REFUSALS)


def test_the_database_refuses_a_bearer_in_diagnostics(ctx) -> None:
    """The schema refuses, not just the application.

    The application scrubs; the schema refuses. Two independent controls,
    because the interesting failure is the one where the application is wrong.
    """
    identity = fresh_identity()
    provider = ep.FakeEmailProvider()
    host = Harness(ctx["dsn"], ctx["worker"], provider)
    try:
        host.advance(identity, max_steps=1)
        record = host.row(identity)
        capability = record.capability_secret
        with host.conn.cursor() as cur:
            for column in ("failure_detail", "failure_code"):
                try:
                    cur.execute(
                        f"UPDATE public.eco_dashboard_delivery_operation "
                        f"SET {column} = %s WHERE delivery_id = %s",
                        (f"provider said: {capability}", str(record.delivery_id)))
                except Exception as error:
                    check("the refusal names a bearer-diagnostics control",
                          refused_bearer_in_diagnostics(error), column)
                else:
                    raise AssertionError(f"{column} accepted a raw bearer")
            try:
                cur.execute(
                    "UPDATE public.eco_dashboard_delivery_operation "
                    "SET metadata_json = %s::jsonb WHERE delivery_id = %s",
                    (json.dumps({"note": capability}), str(record.delivery_id)))
            except Exception as error:
                check("metadata is covered too",
                      refused_bearer_in_diagnostics(error))
            else:
                raise AssertionError("metadata accepted a raw bearer")

        # And a non-secret diagnostic is accepted normally.
        with host.conn.cursor() as cur:
            cur.execute("UPDATE public.eco_dashboard_delivery_operation "
                        "SET failure_detail = %s WHERE delivery_id = %s",
                        ("provider said: RATE_LIMITED", str(record.delivery_id)))
    finally:
        host.close()
    PASSED.append("the_database_refuses_a_bearer_in_diagnostics")


def test_no_secret_appears_on_any_printable_surface(ctx) -> None:
    """Logs, job summaries and durable diagnostics, against every observed secret."""
    identity = fresh_identity()
    provider = ep.FakeEmailProvider()
    host = Harness(ctx["dsn"], ctx["worker"], provider)
    try:
        result = host.advance(identity)
        record = host.row(identity)
        surfaces = {
            "events": json.dumps(host.services.events, ensure_ascii=False),
            "summary": json.dumps(result.summary(), ensure_ascii=False, default=str),
            "record_repr": repr(record),
            "ledger_text": table_dump(ctx["dsn"]),
            "worker_inspect": json.dumps(ctx["worker"].inspect()),
        }
        check("there are secrets to look for", len(OBSERVED_SECRETS) >= 3)
        for name, blob in surfaces.items():
            for secret in OBSERVED_SECRETS:
                # Compared in memory. Neither the secret nor the blob is printed.
                check(f"no secret on the {name} surface", secret not in blob, name)

        # `capability_secret` is the one column a raw bearer may occupy, and
        # only while a delivery is genuinely unresolved. Anything still holding
        # one must be in a state whose safe next action needs it.
        allowed = set(DeliveryState.__dict__.values()) & {
            DeliveryState.CAPABILITY_PERSISTED, DeliveryState.DELIVERY_INTENT_RECORDED,
            DeliveryState.PROVIDER_SUBMISSION_PENDING, DeliveryState.PROVIDER_ACCEPTED,
            DeliveryState.PROVIDER_AMBIGUOUS, DeliveryState.PROVIDER_REJECTED,
            DeliveryState.REMOTE_DELIVERED, DeliveryState.OPERATOR_REQUIRED,
        }
        held = retained_bearers(ctx["dsn"])
        check("only unresolved deliveries retain a bearer",
              all(row["state"] in allowed for row in held),
              json.dumps(sorted({row["state"] for row in held})))
        check("no finalised delivery retains one",
              all(row["state"] != DeliveryState.FINALIZED for row in held))
        # The publisher credential in particular travels only in the
        # Authorization header, never into a stored or reported value.
        check("the machine credential is not in the ledger",
              ctx["worker"].token not in surfaces["ledger_text"])
        check("the machine credential is not in the events",
              ctx["worker"].token not in surfaces["events"])
        # ...and the summary an operator reads still answers the operational
        # questions.
        delivery = result.summary()["delivery"]
        for field in ("operation_id", "state", "next_action", "period_start_date",
                      "recipient_identity", "provider_message_id", "capability_digest",
                      "operator_action_required"):
            check("the operator summary is complete", field in delivery, field)
        check("the summary reports the recipient by identity, not address",
              RECIPIENT not in json.dumps(delivery))
    finally:
        host.close()
    PASSED.append("no_secret_appears_on_any_printable_surface")


# --- 9. byte identity end to end -------------------------------------------------


def test_canonical_bytes_survive_the_whole_lifecycle(ctx) -> None:
    identity = fresh_identity()
    provider = ep.FakeEmailProvider()
    host = Harness(ctx["dsn"], ctx["worker"], provider)
    try:
        body, digest = canonical_payload()
        host.advance(identity)
        record = host.row(identity)
        published = [r for r in host.transport.requests if r["path"] == "/api/publish"][0]
        import hashlib

        sent = published["body"].encode("utf-8")
        check("the host sent the exact canonical octets", sent == body)
        check("the digest identifies those octets",
              hashlib.sha256(sent).hexdigest() == digest)
        check("the ledger stores the same digest", record.payload_digest == digest)
        check("the declared header digest is the same",
              published["headers"]["X-Publication-Payload-Digest"] == digest)
        check("a publication was accepted for those bytes",
              record.state == DeliveryState.FINALIZED)

        # A different snapshot for the SAME logical delivery is a conflict the
        # host refuses locally, before the Worker ever has to.
        result = pub.advance_delivery(
            host.services, identity=identity, payload_digest="0" * 64,
            recipient_email=RECIPIENT, body=body, owner="conflicting")
        check("different canonical bytes are refused",
              result.invocation == pub.INVOCATION.CONFLICT
              and result.conflict_code == "PAYLOAD_CONFLICT", result.conflict_code)
        check("the stored digest is unchanged", host.row(identity).payload_digest == digest)
    finally:
        host.close()
    PASSED.append("canonical_bytes_survive_the_whole_lifecycle")


# --- runner ---------------------------------------------------------------------


TESTS = [
    test_operation_is_durable_before_any_remote_call,
    test_normal_lifecycle_produces_exactly_one_message,
    test_message_carries_the_current_capability_only,
    test_publication_response_loss_never_publishes_twice,
    test_crash_before_bearer_persistence_uses_recovery,
    test_provider_accepted_but_response_lost,
    test_provider_rejection_never_becomes_delivered,
    test_ambiguous_acceptance_never_resends,
    test_recipient_binding_cannot_silently_change,
    test_recipient_email_never_reaches_the_delivery_boundary,
    test_crash_matrix_has_one_safe_action_everywhere,
    test_concurrent_invocations_converge_to_one_lifecycle,
    test_an_expired_lease_can_be_reclaimed,
    test_bearer_retention_is_minimal_and_deterministic,
    test_the_database_refuses_a_bearer_in_diagnostics,
    test_no_secret_appears_on_any_printable_surface,
    test_canonical_bytes_survive_the_whole_lifecycle,
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

    reset_schema(dsn)
    worker = WorkerServer()
    ctx = {"dsn": dsn, "worker": worker}
    started = time.time()
    try:
        for test in TESTS:
            test(ctx)
            print(f"PASS {test.__name__}")
    finally:
        worker.close()
    print(f"\n{len(PASSED)} lifecycle checks passed in {time.time() - started:.1f}s "
          f"— no real e-mail, no deployment, no Cloudflare mutation")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
