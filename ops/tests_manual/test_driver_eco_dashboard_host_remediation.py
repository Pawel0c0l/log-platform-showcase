#!/usr/bin/env python3
"""Driver Eco Dashboard V1 — host publisher remediation suite.

Run:
    ECO_DASHBOARD_PUBLISHER_TEST_DSN=postgresql://postgres:pw@127.0.0.1:5433/disposable \\
      python3 ops/tests_manual/test_driver_eco_dashboard_host_remediation.py

This suite exists because the previous one could not have caught what an
independent review found. It covers exactly the properties that a
single-process, single-configuration, single-backend test cannot express:

  1. the publisher URL is trusted by PARSING, so a loopback-lookalike hostname
     never receives the machine credential;
  2. a logical delivery is bound to ONE provider backend/account scope, so a
     restart onto a different backend cannot turn one accepted message into two;
  3. one provider idempotency key names ONE immutable message, so a changed
     dashboard URL, template or bearer generation is refused before any submit;
  4. an EXPIRED lease holder can neither mutate durable state nor initiate a
     remote effect, proved against genuinely concurrent PostgreSQL connections;
  5. missing provider configuration is discovered BEFORE any external effect,
     and a definite pre-submit failure never becomes PROVIDER_AMBIGUOUS.

Real here: the host publisher, the PostgreSQL ledger with its constraints and
row guard, and the actual Cloudflare Worker on in-memory bindings over real
HTTP. Synthetic: the provider, the driver data, the recipient and the
credentials.

NOT DONE ANYWHERE IN THIS FILE: a real e-mail, a live provider call, an SMTP
connection, a wrangler invocation, a Cloudflare resource, a deployment, a
schedule, or any mutation of a production database. The SMTP checks drive
`SmtpEmailProvider` with an INJECTED sender and an explicit config object, so
no socket is opened and the ambient AUTOMATION_SMTP_* environment is never
read.

DESTRUCTIVE. It drops and recreates `public.eco_dashboard_delivery_operation`
in the database the DSN names and refuses any DSN that is not loopback.
"""

from __future__ import annotations

import os
import smtplib
import sys
import threading
import time
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "ops" / "tests_manual") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "ops" / "tests_manual"))

from jobs.ecodriving_dashboard import dashboard_email as de  # noqa: E402
from jobs.ecodriving_dashboard import email_provider as ep  # noqa: E402
from jobs.ecodriving_dashboard import publisher as pub  # noqa: E402
from jobs.ecodriving_dashboard import secure_delivery_client as sdc  # noqa: E402
from jobs.ecodriving_dashboard.delivery_contract import (  # noqa: E402
    DeliveryState,
    derive_operation_id,
    derive_provider_idempotency_key,
)
from jobs.ecodriving_dashboard.delivery_ledger import LedgerConflict  # noqa: E402
from ops.tests_manual.postgres_dsn_safety import (  # noqa: E402
    require_loopback_dsn_or_exit,
)
from ops.tests_manual.test_driver_eco_dashboard_publisher_lifecycle import (  # noqa: E402
    BASE_URL,
    MESSAGE_DOMAIN,
    OBSERVED_SECRETS,
    RECIPIENT,
    Harness,
    WorkerServer,
    canonical_payload,
    check,
    fresh_identity,
    reset_schema,
)

ENV = "ECO_DASHBOARD_PUBLISHER_TEST_DSN"
PASSED: list[str] = []


# --- 1. the publisher URL / credential boundary ---------------------------------


ACCEPTED_ENDPOINTS = (
    "https://publisher.example.invalid",
    "https://publisher.example.invalid:8443/api",
    "http://127.0.0.1:8787",
    "http://localhost:8787/base",
    "http://[::1]:8787",
    #: The whole of 127.0.0.0/8 is loopback, and `ipaddress` knows that where a
    #: string comparison against "127.0.0.1" would not.
    "http://127.9.9.9:8787",
)

REFUSED_ENDPOINTS = (
    #: THE reproduced finding: both start with an accepted prefix and both name
    #: somebody else's server.
    ("http://localhost.attacker.invalid", "INSECURE_PUBLISHER_ENDPOINT"),
    ("http://127.0.0.1.attacker.invalid", "INSECURE_PUBLISHER_ENDPOINT"),
    ("http://localhost.attacker.invalid:8787/api", "INSECURE_PUBLISHER_ENDPOINT"),
    ("http://xlocalhost", "INSECURE_PUBLISHER_ENDPOINT"),
    ("http://127.0.0.1x", "INSECURE_PUBLISHER_ENDPOINT"),
    ("http://publisher.example.invalid", "INSECURE_PUBLISHER_ENDPOINT"),
    ("ftp://127.0.0.1", "INSECURE_PUBLISHER_ENDPOINT"),
    #: Userinfo: the browser-style trick where the host is the LAST @-part.
    ("http://127.0.0.1@attacker.invalid", "PUBLISHER_ENDPOINT_MALFORMED"),
    ("http://localhost@attacker.invalid/api", "PUBLISHER_ENDPOINT_MALFORMED"),
    ("http://user:pw@attacker.invalid", "PUBLISHER_ENDPOINT_MALFORMED"),
    ("http://127.0.0.1:notaport", "PUBLISHER_ENDPOINT_MALFORMED"),
    ("http://", "PUBLISHER_ENDPOINT_MALFORMED"),
    ("http://127.0.0.1?next=x", "PUBLISHER_ENDPOINT_MALFORMED"),
    ("http://127.0.0.1#f", "PUBLISHER_ENDPOINT_MALFORMED"),
    ("http://127.0.0.1 /api", "PUBLISHER_ENDPOINT_MALFORMED"),
    ("", "PUBLISHER_ENDPOINT_MISSING"),
    #: MALFORMED HOSTS. Each of these parsed to a NON-EMPTY hostname, which is
    #: all the previous validator asked for, so a request carrying the machine
    #: credential was built for a destination that can never name a host.
    ("https://-", "PUBLISHER_ENDPOINT_MALFORMED"),
    ("https://_", "PUBLISHER_ENDPOINT_MALFORMED"),
    ("https://%zz", "PUBLISHER_ENDPOINT_MALFORMED"),
    ("https://%", "PUBLISHER_ENDPOINT_MALFORMED"),
    ("https://ex%20ample.invalid", "PUBLISHER_ENDPOINT_MALFORMED"),
    ("https://", "PUBLISHER_ENDPOINT_MALFORMED"),
    ("https://.", "PUBLISHER_ENDPOINT_MALFORMED"),
    ("https://a..b", "PUBLISHER_ENDPOINT_MALFORMED"),
    ("https://-lead.example.invalid", "PUBLISHER_ENDPOINT_MALFORMED"),
    ("https://trail-.example.invalid", "PUBLISHER_ENDPOINT_MALFORMED"),
    ("https://under_score.example.invalid", "PUBLISHER_ENDPOINT_MALFORMED"),
    ("https://exam^ple.invalid", "PUBLISHER_ENDPOINT_MALFORMED"),
    #: Invalid IP literals. No label rule catches these; `ipaddress` does.
    ("https://999.999.999.999", "PUBLISHER_ENDPOINT_MALFORMED"),
    ("https://127.0.0.256", "PUBLISHER_ENDPOINT_MALFORMED"),
    ("https://[zz::1]", "PUBLISHER_ENDPOINT_MALFORMED"),
    ("https://[::1", "PUBLISHER_ENDPOINT_MALFORMED"),
    ("https://[fe80::1%25eth0]", "PUBLISHER_ENDPOINT_MALFORMED"),
    #: Userinfo, query and fragment under HTTPS as well as HTTP.
    ("https://user@example.invalid", "PUBLISHER_ENDPOINT_MALFORMED"),
    ("https://user:pw@example.invalid", "PUBLISHER_ENDPOINT_MALFORMED"),
    ("https://eco.example.invalid?next=x", "PUBLISHER_ENDPOINT_MALFORMED"),
    ("https://eco.example.invalid#f", "PUBLISHER_ENDPOINT_MALFORMED"),
    ("https://eco.example.invalid:notaport", "PUBLISHER_ENDPOINT_MALFORMED"),
)

#: HTTPS forms the product actually needs, including the shapes a Cloudflare
#: Workers or custom-domain endpoint takes. Overrejecting these would be the
#: same defect pointing the other way.
ACCEPTED_HTTPS_ENDPOINTS = (
    "https://eco-publisher.example.invalid",
    "https://eco-publisher.example.invalid/api/v1",
    "https://eco-publisher.example.invalid:8443",
    "https://eco-dashboard.workers.dev",
    "https://a.b.c.d.example.invalid",
    "https://xn--bcher-kva.example.invalid",
    "https://example.invalid.",
    "https://203.0.113.10",
    "https://[2001:db8::1]:8443",
)

#: A machine credential this repository invented for this assertion. It is not
#: a secret and names no real system; its only job is to be searched for.
CREDENTIAL_MARKER = "SYNTHETIC-PUBLISHER-CREDENTIAL-b3f1c9d2"


def test_only_real_loopback_may_receive_a_plaintext_credential(ctx) -> None:
    for url in ACCEPTED_ENDPOINTS:
        sdc.HttpSecureDeliveryTransport(url)
    for url, expected in REFUSED_ENDPOINTS:
        try:
            sdc.HttpSecureDeliveryTransport(url)
        except sdc.SecureDeliveryError as error:
            check(f"{url!r} is refused with the right code",
                  error.code == expected, f"{error.code} != {expected}")
        else:
            raise AssertionError(f"an untrusted publisher endpoint was accepted: {url!r}")
    PASSED.append("only_real_loopback_may_receive_a_plaintext_credential")


def test_a_malformed_https_host_is_refused_before_any_request(ctx) -> None:
    """THE second-pass finding: HTTPS was trusted on scheme alone.

    `https://-`, `https://_` and `https://%zz` reached the point where a
    credential-bearing request was constructed, because the validator asked
    only whether a hostname string was non-empty. "Is this a host at all" is a
    syntactic question, and it must be answered where no credential is in scope
    yet — the transport constructor, which runs before a client holding one
    exists.
    """
    malformed = [url for url, code in REFUSED_ENDPOINTS
                 if url.startswith("https://") and code == "PUBLISHER_ENDPOINT_MALFORMED"]
    check("the matrix actually covers the reported forms",
          {"https://-", "https://_", "https://%zz"} <= set(malformed))

    for url in ACCEPTED_HTTPS_ENDPOINTS:
        sdc.validate_publisher_endpoint(url)
        sdc.HttpSecureDeliveryTransport(url)
    # The permitted local-development loopback forms still work.
    for url in ACCEPTED_ENDPOINTS:
        sdc.HttpSecureDeliveryTransport(url)

    import urllib.request

    attempted: list = []
    original = urllib.request.urlopen

    def refusing_urlopen(*args, **kwargs):  # pragma: no cover - must not run
        attempted.append(args)
        raise AssertionError("a request was attempted to a malformed endpoint")

    captured: list[str] = []
    urllib.request.urlopen = refusing_urlopen
    try:
        for url in malformed:
            try:
                transport = sdc.HttpSecureDeliveryTransport(url)
            except sdc.SecureDeliveryError as error:
                # Everything the refusal can be observed through.
                captured.extend([str(error), repr(error), error.code, url])
                continue
            # If a transport ever existed, the client would be the next line —
            # so the assertion is made against the whole chain, not the parser.
            client = sdc.SecureDeliveryClient(transport, publisher_token=CREDENTIAL_MARKER)
            captured.append(repr(client))
            raise AssertionError(f"a malformed endpoint produced a usable client: {url!r}")
    finally:
        urllib.request.urlopen = original

    check("no transport call was made for any malformed endpoint", not attempted)
    check("the credential marker never appears in a refusal, log or repr",
          not any(CREDENTIAL_MARKER in text for text in captured))
    PASSED.append("a_malformed_https_host_is_refused_before_any_request")


def test_a_refused_endpoint_never_sees_the_credential(ctx) -> None:
    """The credential must not reach the transport, a mock, or a log line.

    Validation happens in the transport constructor, which runs before a
    `SecureDeliveryClient` holding the credential exists at all. This asserts
    the consequence directly rather than trusting the ordering: no outbound
    request is even attempted, so there is nothing for a credential to ride on.
    """
    import urllib.request

    opened: list = []
    original = urllib.request.urlopen

    def refusing_urlopen(*args, **kwargs):  # pragma: no cover - must not run
        opened.append(args)
        raise AssertionError("a request was attempted to a refused endpoint")

    urllib.request.urlopen = refusing_urlopen
    try:
        for url, _ in REFUSED_ENDPOINTS:
            try:
                transport = sdc.HttpSecureDeliveryTransport(url)
            except sdc.SecureDeliveryError:
                continue
            raise AssertionError(f"{url!r} produced a usable transport")
    finally:
        urllib.request.urlopen = original
    check("no request was attempted to any refused endpoint", not opened)

    # And the job-level guard refuses the same set before constructing anything.
    for url, _ in REFUSED_ENDPOINTS:
        try:
            sdc.validate_publisher_endpoint(url)
        except sdc.SecureDeliveryError:
            continue
        raise AssertionError(f"the job-level guard accepted {url!r}")
    PASSED.append("a_refused_endpoint_never_sees_the_credential")


# --- 2. provider backend binding ------------------------------------------------


def backend_pair():
    """Two adapters for the SAME backend, and one for a genuinely different one.

    `store` is what makes "the same backend seen by a restarted process" real:
    the accepted message survives the adapter object, exactly as it survives on
    a provider's servers.
    """
    store: dict = {}
    a1 = ep.FakeEmailProvider(account_scope="account-A", endpoint_scope="eu-1",
                              store=store)
    a2 = ep.FakeEmailProvider(account_scope="account-A", endpoint_scope="eu-1",
                              store=store)
    b = ep.FakeEmailProvider(account_scope="account-B", endpoint_scope="eu-1")
    return a1, a2, b


def test_a_backend_swap_after_a_lost_response_submits_nothing(ctx) -> None:
    """THE finding, end to end. A accepted a message; B must never hear of it."""
    identity = fresh_identity()
    provider_a, provider_a_restarted, provider_b = backend_pair()
    key = derive_provider_idempotency_key(derive_operation_id(identity))
    provider_a.script(key, "accept_then_lose_response")
    provider_a_restarted.script(key, "accept_then_lose_response")

    host = Harness(ctx["dsn"], ctx["worker"], provider_a)
    try:
        # Stop at the submission boundary: publish -> intent -> submit. The
        # response was lost, so this is the state a crashed process wakes in.
        result = host.advance(identity, max_steps=3)
        record = host.row(identity)
        check("the host knows a message may exist",
              record.state == DeliveryState.PROVIDER_SUBMISSION_PENDING, record.state)
        check("provider A holds exactly one message",
              provider_a.accepted_count(key) == 1)
        check("the backend scope is durably bound",
              record.provider_backend_id
              == provider_a.backend_identity().stable_id())
        check("the message identity is durably bound",
              bool(record.provider_message_fingerprint))
    finally:
        host.close()

    # RESTART ONTO A DIFFERENT BACKEND.
    swapped = Harness(ctx["dsn"], ctx["worker"], provider_b)
    try:
        result = swapped.advance(identity)
        record = swapped.row(identity)
        check("provider B received ZERO submissions",
              provider_b.submissions == [], str(len(provider_b.submissions)))
        check("provider B was not even asked to reconcile",
              provider_b.reconciliations == [])
        check("provider B holds no message", provider_b.messages == {})
        check("the invocation requires an operator",
              result.invocation == pub.INVOCATION.OPERATOR_REQUIRED, result.invocation)
        check("the refusal is an explicit backend conflict",
              record.failure_code == pub.ConflictCode.PROVIDER_BACKEND_CONFLICT,
              str(record.failure_code))
        check("the state stops automation",
              record.state == DeliveryState.OPERATOR_REQUIRED, record.state)
        check("the binding did not drift",
              record.provider_backend_id
              == provider_a.backend_identity().stable_id())
        check("provider A still holds exactly one message",
              provider_a.accepted_count(key) == 1)
    finally:
        swapped.close()
    PASSED.append("a_backend_swap_after_a_lost_response_submits_nothing")


def test_the_same_backend_after_a_lost_response_converges_to_one_message(ctx) -> None:
    """The other half: a restart onto the SAME backend must still resolve."""
    identity = fresh_identity()
    provider_a, provider_a_restarted, _ = backend_pair()
    key = derive_provider_idempotency_key(derive_operation_id(identity))
    provider_a.script(key, "accept_then_lose_response")

    host = Harness(ctx["dsn"], ctx["worker"], provider_a)
    try:
        host.advance(identity, max_steps=3)
        check("a message may exist",
              host.row(identity).state == DeliveryState.PROVIDER_SUBMISSION_PENDING,
              host.row(identity).state)
    finally:
        host.close()

    restarted = Harness(ctx["dsn"], ctx["worker"], provider_a_restarted)
    try:
        result = restarted.advance(identity)
        record = restarted.row(identity)
        check("the delivery finalised", record.state == DeliveryState.FINALIZED,
              f"{record.state} / {result.detail}")
        check("acceptance was RECONCILED, not resubmitted blindly",
              record.provider_message_id is not None)
        check("exactly one message exists on that backend",
              len(provider_a_restarted.messages) == 1
              and provider_a_restarted.accepted_count(key) == 1,
              str(len(provider_a_restarted.messages)))
        check("the restarted process made no new submission",
              provider_a_restarted.submissions == [],
              str(len(provider_a_restarted.submissions)))
    finally:
        restarted.close()
    PASSED.append("the_same_backend_after_a_lost_response_converges_to_one_message")


def test_the_same_provider_type_with_a_different_account_is_a_different_backend(ctx) -> None:
    identity = fresh_identity()
    same_type_other_account = ep.FakeEmailProvider(account_scope="account-Z",
                                                  endpoint_scope="eu-1")
    original = ep.FakeEmailProvider(account_scope="account-A", endpoint_scope="eu-1")
    other_endpoint = ep.FakeEmailProvider(account_scope="account-A",
                                          endpoint_scope="eu-2")
    check("a different account is a different backend",
          original.backend_identity().stable_id()
          != same_type_other_account.backend_identity().stable_id())
    check("a different endpoint/environment is a different backend",
          original.backend_identity().stable_id()
          != other_endpoint.backend_identity().stable_id())

    host = Harness(ctx["dsn"], ctx["worker"], original)
    try:
        host.advance(identity, max_steps=2)  # publish -> record intent
        check("the delivery is bound before any submission",
              host.row(identity).state == DeliveryState.DELIVERY_INTENT_RECORDED,
              host.row(identity).state)
    finally:
        host.close()

    swapped = Harness(ctx["dsn"], ctx["worker"], same_type_other_account)
    try:
        result = swapped.advance(identity)
        check("the other account received zero submissions",
              same_type_other_account.submissions == [])
        check("it is an explicit conflict",
              swapped.row(identity).failure_code
              == pub.ConflictCode.PROVIDER_BACKEND_CONFLICT,
              str(swapped.row(identity).failure_code))
    finally:
        swapped.close()
    PASSED.append("the_same_provider_type_with_a_different_account_is_a_different_backend")


def test_rotating_a_credential_is_not_a_backend_change(ctx) -> None:
    """A password rotation must not look like a different delivery backend.

    The identity is derived from provider type, account and endpoint. No secret
    is an input — which is both why a rotation is invisible here and why no
    credential can end up in a persisted identity.
    """
    class Config:
        host = "smtp.example.invalid"
        port = 587
        from_addr = "automation@example.invalid"
        username = "automation@example.invalid"

    class Rotated(Config):
        password = "the-new-one"

    class Original(Config):
        password = "the-old-one"

    before = ep.SmtpEmailProvider(config=Original()).backend_identity()
    after = ep.SmtpEmailProvider(config=Rotated()).backend_identity()
    check("a rotated credential is the same backend",
          before.stable_id() == after.stable_id())

    class OtherAccount(Config):
        username = "someone.else@example.invalid"
        password = "the-old-one"

    class OtherHost(Config):
        host = "smtp.elsewhere.invalid"
        password = "the-old-one"

    check("a different account IS a different backend",
          ep.SmtpEmailProvider(config=OtherAccount()).backend_identity().stable_id()
          != before.stable_id())
    check("a different endpoint IS a different backend",
          ep.SmtpEmailProvider(config=OtherHost()).backend_identity().stable_id()
          != before.stable_id())
    check("no credential appears in the identity material",
          "the-old-one" not in before.describe()
          and "the-old-one" not in before.stable_id())
    PASSED.append("rotating_a_credential_is_not_a_backend_change")


# --- 3. message identity --------------------------------------------------------


def bind_intent(ctx, identity, provider, *, base_url=BASE_URL,
                message_domain=MESSAGE_DOMAIN):
    """Drive one delivery to DELIVERY_INTENT_RECORDED and stop."""
    host = Harness(ctx["dsn"], ctx["worker"], provider)
    host.services.config = pub.PublisherConfig(
        dashboard_base_url=base_url, message_id_domain=message_domain,
        lease_seconds=300, max_provider_attempts=3)
    try:
        host.advance(identity, max_steps=2)
        record = host.row(identity)
        check("bound before any submission",
              record.state == DeliveryState.DELIVERY_INTENT_RECORDED, record.state)
        return record.provider_message_fingerprint
    finally:
        host.close()


def drifted(ctx, identity, provider, *, base_url=BASE_URL,
            message_domain=MESSAGE_DOMAIN, before=None):
    """Resume the SAME delivery under a drifted configuration."""
    host = Harness(ctx["dsn"], ctx["worker"], provider)
    host.services.config = pub.PublisherConfig(
        dashboard_base_url=base_url, message_id_domain=message_domain,
        lease_seconds=300, max_provider_attempts=3)
    try:
        if before is not None:
            before(host)
        result = host.advance(identity)
        return result, host.row(identity)
    finally:
        host.close()


def _assert_message_conflict(label, provider, result, record) -> None:
    check(f"{label}: the provider received ZERO submissions",
          provider.submissions == [], str(len(provider.submissions)))
    check(f"{label}: no message was created", provider.messages == {})
    check(f"{label}: an operator is required",
          result.invocation == pub.INVOCATION.OPERATOR_REQUIRED, result.invocation)
    check(f"{label}: the refusal is an explicit message conflict",
          record.failure_code == pub.ConflictCode.MESSAGE_CONFLICT,
          str(record.failure_code))
    check(f"{label}: the bound fingerprint did not drift",
          record.provider_message_fingerprint is not None)


def test_a_drifted_dashboard_base_url_is_refused_before_submitting(ctx) -> None:
    identity = fresh_identity()
    provider = ep.FakeEmailProvider()
    bind_intent(ctx, identity, provider)
    result, record = drifted(ctx, identity, provider,
                             base_url="https://eco-other.example.invalid/dashboard")
    _assert_message_conflict("base URL drift", provider, result, record)
    PASSED.append("a_drifted_dashboard_base_url_is_refused_before_submitting")


def test_a_drifted_message_id_domain_is_refused_before_submitting(ctx) -> None:
    identity = fresh_identity()
    provider = ep.FakeEmailProvider()
    bind_intent(ctx, identity, provider)
    result, record = drifted(ctx, identity, provider,
                             message_domain="other-domain.example.invalid")
    _assert_message_conflict("Message-ID domain drift", provider, result, record)
    PASSED.append("a_drifted_message_id_domain_is_refused_before_submitting")


def test_a_drifted_subject_template_is_refused_before_submitting(ctx) -> None:
    """A template change is a content change, and the key already named content."""
    identity = fresh_identity()
    provider = ep.FakeEmailProvider()
    bind_intent(ctx, identity, provider)
    original = de.WEEKLY_SUBJECT
    de.WEEKLY_SUBJECT = "Zupelnie inny temat ({start} - {end})"
    try:
        result, record = drifted(ctx, identity, provider)
    finally:
        de.WEEKLY_SUBJECT = original
    _assert_message_conflict("subject template drift", provider, result, record)
    PASSED.append("a_drifted_subject_template_is_refused_before_submitting")


def test_a_drifted_capability_generation_is_refused_before_submitting(ctx) -> None:
    """A replacement bearer after binding is a different message, not a retry.

    The state machine cannot reach recovery from a bound state, so this models
    the incident case directly: the stored capability is replaced underneath the
    binding. The fingerprint covers the message BODY, and the body carries the
    link, so a replacement generation is caught by the same single comparison.
    """
    identity = fresh_identity()
    provider = ep.FakeEmailProvider()
    bind_intent(ctx, identity, provider)

    def replace_bearer(host):
        with host.conn.cursor() as cur:
            cur.execute(
                "UPDATE public.eco_dashboard_delivery_operation "
                "SET capability_secret = %s, bearer_generation = bearer_generation + 1 "
                "WHERE operation_id = %s",
                ("Z" * 43, derive_operation_id(identity)))
        OBSERVED_SECRETS.add("Z" * 43)

    result, record = drifted(ctx, identity, provider, before=replace_bearer)
    _assert_message_conflict("bearer generation drift", provider, result, record)
    check("the binding still names the ORIGINAL capability generation",
          record.provider_bound_bearer_generation == 1,
          str(record.provider_bound_bearer_generation))
    PASSED.append("a_drifted_capability_generation_is_refused_before_submitting")


def test_a_drifted_recipient_is_refused_before_any_work(ctx) -> None:
    """Recipient drift is refused earlier still — the logical delivery is bound."""
    identity = fresh_identity()
    provider = ep.FakeEmailProvider()
    bind_intent(ctx, identity, provider)
    host = Harness(ctx["dsn"], ctx["worker"], provider)
    try:
        result = host.advance(identity, recipient="somebody.else@example.invalid")
        check("it is a recipient conflict",
              result.invocation == pub.INVOCATION.CONFLICT
              and result.conflict_code == "RECIPIENT_CONFLICT", str(result.conflict_code))
        check("the provider received zero submissions", provider.submissions == [])
        check("the stored recipient is unchanged",
              host.row(identity).recipient_email == RECIPIENT)
    finally:
        host.close()
    PASSED.append("a_drifted_recipient_is_refused_before_any_work")


def test_an_unchanged_configuration_still_submits_normally(ctx) -> None:
    """The conflict checks must not refuse the ordinary case."""
    identity = fresh_identity()
    provider = ep.FakeEmailProvider()
    fingerprint = bind_intent(ctx, identity, provider)
    result, record = drifted(ctx, identity, provider)
    key = derive_provider_idempotency_key(derive_operation_id(identity))
    check("the delivery finalised", record.state == DeliveryState.FINALIZED,
          f"{record.state} / {result.detail}")
    check("exactly one message was sent", provider.accepted_count(key) == 1
          and len(provider.submissions) == 1)
    check("the fingerprint is the one that was bound",
          record.provider_message_fingerprint == fingerprint)
    PASSED.append("an_unchanged_configuration_still_submits_normally")


# --- 3b. the RESPONSE-LOSS drift matrix -----------------------------------------
#
# The previous suite proved that drift is refused before a first SUBMIT. It did
# not prove the harder half: that drift is also refused before RECONCILIATION,
# where the provider has already accepted a message and the host is deciding
# whether to adopt that acceptance. Provider lookup succeeded and the operation
# finalised under content the current host could no longer prove it had sent.
#
# Every case below starts from the same baseline — accepted, response lost,
# `PROVIDER_SUBMISSION_PENDING` — and restarts with exactly one input changed.


def accepted_then_response_lost(ctx, identity, provider, key):
    """The baseline. One message exists remotely; the host does not know it."""
    provider.script(key, "accept_then_lose_response")
    host = Harness(ctx["dsn"], ctx["worker"], provider)
    try:
        host.advance(identity, max_steps=3)
        record = host.row(identity)
        check("the host knows a message MAY exist",
              record.state == DeliveryState.PROVIDER_SUBMISSION_PENDING, record.state)
        check("the provider really did accept exactly one",
              provider.accepted_count(key) == 1)
        check("the message identity is durably bound",
              bool(record.provider_message_fingerprint))
        return record.provider_message_fingerprint
    finally:
        host.close()


def _assert_conflicts_before_reconciling(label, provider, result, record, *,
                                         reconciles_before, submits_before) -> None:
    check(f"{label}: ZERO provider reconciliation calls",
          len(provider.reconciliations) == reconciles_before,
          str(len(provider.reconciliations) - reconciles_before))
    check(f"{label}: ZERO provider submissions after the restart",
          len(provider.submissions) == submits_before,
          str(len(provider.submissions) - submits_before))
    check(f"{label}: an operator is required",
          result.invocation == pub.INVOCATION.OPERATOR_REQUIRED, result.invocation)
    check(f"{label}: the refusal is an explicit message conflict",
          record.failure_code == pub.ConflictCode.MESSAGE_CONFLICT,
          str(record.failure_code))
    check(f"{label}: no acceptance was adopted",
          record.provider_message_id is None, str(record.provider_message_id))
    check(f"{label}: no remote DELIVERED transition",
          record.remote_delivered_at is None)
    check(f"{label}: the state stops automation, inspectably",
          record.state == DeliveryState.OPERATOR_REQUIRED, record.state)


def drift_case(ctx, label, *, base_url=BASE_URL, message_domain=MESSAGE_DOMAIN,
               patch=None, before=None):
    """One accepted-response-lost delivery, restarted with ONE input changed."""
    identity = fresh_identity()
    key = derive_provider_idempotency_key(derive_operation_id(identity))
    store: dict = {}
    first = ep.FakeEmailProvider(account_scope="drift-matrix", store=store)
    accepted_then_response_lost(ctx, identity, first, key)

    # The SAME backend, seen by a restarted process: the accepted message
    # survives the adapter object exactly as it survives on a provider.
    restarted = ep.FakeEmailProvider(account_scope="drift-matrix", store=store)
    restarted.script(key, "accept_then_lose_response")
    host = Harness(ctx["dsn"], ctx["worker"], restarted)
    host.services.config = pub.PublisherConfig(
        dashboard_base_url=base_url, message_id_domain=message_domain,
        lease_seconds=300, max_provider_attempts=3)
    undo = patch() if patch is not None else None
    try:
        if before is not None:
            before(host, identity)
        result = host.advance(identity)
        record = host.row(identity)
    finally:
        if undo is not None:
            undo()
        host.close()
    _assert_conflicts_before_reconciling(label, restarted, result, record,
                                         reconciles_before=0, submits_before=0)
    check(f"{label}: the original accepted message is still the only one",
          len(store) == 1 and first.accepted_count(key) == 1, str(len(store)))
    check(f"{label}: the bound fingerprint did not drift",
          record.provider_message_fingerprint is not None)


def _patch_attribute(module, name, value):
    def apply():
        original = getattr(module, name)
        setattr(module, name, value)
        return lambda: setattr(module, name, original)
    return apply


def _patch_bodies(transform):
    """Drift ONE body while leaving the other exactly as it was."""
    def apply():
        original = de.build_bodies

        def patched(context):
            return transform(*original(context))

        de.build_bodies = patched
        return lambda: setattr(de, "build_bodies", original)
    return apply


def test_response_loss_then_base_url_drift_conflicts_before_reconciling(ctx) -> None:
    drift_case(ctx, "base URL drift",
               base_url="https://eco-other.example.invalid/dashboard")
    PASSED.append("response_loss_then_base_url_drift_conflicts_before_reconciling")


def test_response_loss_then_message_id_domain_drift_conflicts(ctx) -> None:
    drift_case(ctx, "Message-ID domain drift",
               message_domain="other-domain.example.invalid")
    PASSED.append("response_loss_then_message_id_domain_drift_conflicts")


def test_response_loss_then_subject_drift_conflicts(ctx) -> None:
    drift_case(ctx, "subject drift",
               patch=_patch_attribute(de, "WEEKLY_SUBJECT",
                                      "Zupelnie inny temat ({start} - {end})"))
    PASSED.append("response_loss_then_subject_drift_conflicts")


def test_response_loss_then_text_body_drift_conflicts(ctx) -> None:
    drift_case(ctx, "text body drift",
               patch=_patch_bodies(lambda html_body, text_body:
                                   (html_body, text_body + "\nDopisek.")))
    PASSED.append("response_loss_then_text_body_drift_conflicts")


def test_response_loss_then_html_body_drift_conflicts(ctx) -> None:
    drift_case(ctx, "HTML body drift",
               patch=_patch_bodies(lambda html_body, text_body:
                                   (html_body.replace("</body>", "<p>x</p></body>"),
                                    text_body)))
    PASSED.append("response_loss_then_html_body_drift_conflicts")


def test_response_loss_then_bearer_generation_drift_conflicts(ctx) -> None:
    """THE capability-recovery boundary, on the reconciliation side.

    Before binding, a replacement capability may legitimately become the
    outbound message. After a submission may have happened it may not: the key
    already names a message, and a different link under the same key is a
    different message the provider would never dedupe against.
    """
    def replace_bearer(host, identity):
        with host.conn.cursor() as cur:
            cur.execute(
                "UPDATE public.eco_dashboard_delivery_operation "
                "SET capability_secret = %s, bearer_generation = bearer_generation + 1 "
                "WHERE operation_id = %s",
                ("Y" * 43, derive_operation_id(identity)))
        OBSERVED_SECRETS.add("Y" * 43)

    drift_case(ctx, "bearer generation drift", before=replace_bearer)
    PASSED.append("response_loss_then_bearer_generation_drift_conflicts")


def test_response_loss_then_recipient_drift_conflicts_before_any_provider_call(ctx) -> None:
    """Recipient drift is refused earlier still, and must stay that way."""
    identity = fresh_identity()
    key = derive_provider_idempotency_key(derive_operation_id(identity))
    store: dict = {}
    first = ep.FakeEmailProvider(account_scope="drift-recipient", store=store)
    accepted_then_response_lost(ctx, identity, first, key)

    restarted = ep.FakeEmailProvider(account_scope="drift-recipient", store=store)
    host = Harness(ctx["dsn"], ctx["worker"], restarted)
    try:
        result = host.advance(identity, recipient="somebody.else@example.invalid")
        check("it is a recipient conflict",
              result.invocation == pub.INVOCATION.CONFLICT
              and result.conflict_code == "RECIPIENT_CONFLICT", str(result.conflict_code))
        check("ZERO provider reconciliation calls", restarted.reconciliations == [])
        check("ZERO provider submissions", restarted.submissions == [])
        check("the stored recipient is unchanged",
              host.row(identity).recipient_email == RECIPIENT)
        check("still exactly one accepted message", len(store) == 1)
    finally:
        host.close()
    PASSED.append("response_loss_then_recipient_drift_conflicts_before_any_provider_call")


def test_response_loss_without_drift_reconciles_to_one_message(ctx) -> None:
    """The other half. Refusing everything is not a fix; this must still work."""
    identity = fresh_identity()
    key = derive_provider_idempotency_key(derive_operation_id(identity))
    store: dict = {}
    first = ep.FakeEmailProvider(account_scope="drift-none", store=store)
    fingerprint = accepted_then_response_lost(ctx, identity, first, key)

    restarted = ep.FakeEmailProvider(account_scope="drift-none", store=store)
    host = Harness(ctx["dsn"], ctx["worker"], restarted)
    try:
        result = host.advance(identity)
        record = host.row(identity)
        check("an unchanged restart finalises",
              record.state == DeliveryState.FINALIZED,
              f"{record.state} / {result.detail}")
        check("it reconciled rather than resubmitting",
              restarted.submissions == [], str(len(restarted.submissions)))
        check("the lookup did happen", len(restarted.reconciliations) >= 1)
        check("exactly ONE accepted message exists", len(store) == 1, str(len(store)))
        check("the acceptance is the bound one",
              record.provider_message_fingerprint == fingerprint)
    finally:
        host.close()
    PASSED.append("response_loss_without_drift_reconciles_to_one_message")


# --- 4. lease fencing -----------------------------------------------------------


def test_an_expired_holder_cannot_mutate_state(ctx) -> None:
    """The reproduced finding, at the exact boundary.

    Owner equality is not ownership: between expiry and takeover the row still
    names the stale holder, and that is precisely the window a crashed process
    wakes up in.
    """
    identity = fresh_identity()
    provider = ep.FakeEmailProvider()
    holder = Harness(ctx["dsn"], ctx["worker"], provider)
    try:
        payload, digest = canonical_payload()
        record, _ = pub.prepare_delivery(holder.ledger, identity,
                                         payload_digest=digest, recipient_email=RECIPIENT)
        # (a) BEFORE expiry: a valid owner may write.
        held = holder.ledger.claim(record.delivery_id, owner="alice", lease_seconds=60)
        check("the owner holds a live lease", held is not None)
        check("the owner may renew",
              holder.ledger.renew(record.delivery_id, owner="alice",
                                  lease_seconds=60) is not None)
        check("holds_lease agrees",
              holder.ledger.holds_lease(record.delivery_id, owner="alice"))

        # (b) AT/AFTER expiry, with NOBODY else holding it.
        with holder.conn.cursor() as cur:
            cur.execute("UPDATE public.eco_dashboard_delivery_operation "
                        "SET lease_expires_at = now() WHERE delivery_id = %s",
                        (str(record.delivery_id),))
        check("an expired lease is not held",
              not holder.ledger.holds_lease(record.delivery_id, owner="alice"))
        check("an expired lease cannot be renewed",
              holder.ledger.renew(record.delivery_id, owner="alice") is None)
        try:
            holder.ledger.mark_bearer_recovery_required(
                held, owner="alice", reason="stale write attempt")
        except LedgerConflict as conflict:
            check("the expired holder's write is refused",
                  conflict.code == "LOST_OWNERSHIP", conflict.code)
        else:
            raise AssertionError("an EXPIRED lease holder committed a state transition")
        after = holder.ledger.load(record.delivery_id)
        check("the state is untouched", after.state == DeliveryState.PREPARED,
              after.state)

        # (c) TAKEOVER, then the stale original owner resumes.
        taken = holder.ledger.claim(record.delivery_id, owner="bob", lease_seconds=60)
        check("a new owner may claim an expired lease", taken is not None)
        try:
            holder.ledger.mark_bearer_recovery_required(
                held, owner="alice", reason="stale write after takeover")
        except LedgerConflict as conflict:
            check("the stale holder is refused after takeover",
                  conflict.code == "LOST_OWNERSHIP", conflict.code)
        else:
            raise AssertionError("a stale holder wrote after a takeover")
        check("the new owner still holds the lease",
              holder.ledger.holds_lease(record.delivery_id, owner="bob"))
        check("the stale owner cannot renew its way back in",
              holder.ledger.renew(record.delivery_id, owner="alice") is None)
    finally:
        holder.close()
    PASSED.append("an_expired_holder_cannot_mutate_state")


def test_a_stale_holder_initiates_no_remote_effect(ctx) -> None:
    """Not just "the write is refused afterwards" — the effect is never started.

    Every remote boundary is checked: publication, lost-bearer recovery, the
    remote INTENT transition, the provider submission and remote DELIVERED.
    """
    provider = ep.FakeEmailProvider()

    # (a) PUBLICATION and (b) the remote INTENT transition.
    for label, steps, state in (("publication", 0, DeliveryState.PREPARED),
                                ("delivery intent", 1,
                                 DeliveryState.CAPABILITY_PERSISTED)):
        identity = fresh_identity()
        stale = Harness(ctx["dsn"], ctx["worker"], provider)
        try:
            if steps:
                stale.advance(identity, owner="rightful", max_steps=steps)
            else:
                payload, digest = canonical_payload()
                pub.prepare_delivery(stale.ledger, identity, payload_digest=digest,
                                     recipient_email=RECIPIENT)
            record = stale.row(identity)
            check(f"{label}: the delivery is where the test needs it",
                  record.state == state, record.state)
            # A stale holder: it owns the row, and its lease has expired.
            stale.ledger.claim(record.delivery_id, owner="ghost", lease_seconds=60)
            with stale.conn.cursor() as cur:
                cur.execute("UPDATE public.eco_dashboard_delivery_operation "
                            "SET lease_expires_at = now() - interval '1 second' "
                            "WHERE delivery_id = %s", (str(record.delivery_id),))
            record = stale.ledger.load(record.delivery_id)
            before = len(stale.transport.requests)
            try:
                if label == "publication":
                    pub._publish(stale.services, record, "ghost", canonical_payload()[0])
                else:
                    pub._record_intent(stale.services, record, "ghost")
            except LedgerConflict as conflict:
                check(f"{label}: refused before the effect",
                      conflict.code in ("LEASE_NOT_HELD", "STATE_MOVED"), conflict.code)
            else:
                raise AssertionError(f"a stale holder initiated the {label} effect")
            check(f"{label}: NO request was transmitted",
                  len(stale.transport.requests) == before,
                  str(len(stale.transport.requests) - before))
        finally:
            stale.close()

    # (c) BEARER RECOVERY.
    identity = fresh_identity()
    stale = Harness(ctx["dsn"], ctx["worker"], provider)
    try:
        payload, digest = canonical_payload()
        record, _ = pub.prepare_delivery(stale.ledger, identity, payload_digest=digest,
                                         recipient_email=RECIPIENT)
        held = stale.ledger.claim(record.delivery_id, owner="ghost", lease_seconds=60)
        record = stale.ledger.mark_bearer_recovery_required(
            held, owner="ghost", reason="synthetic")
        with stale.conn.cursor() as cur:
            cur.execute("UPDATE public.eco_dashboard_delivery_operation "
                        "SET lease_expires_at = now() - interval '1 second' "
                        "WHERE delivery_id = %s", (str(record.delivery_id),))
        record = stale.ledger.load(record.delivery_id)
        before = len(stale.transport.requests)
        try:
            pub._recover(stale.services, record, "ghost")
        except LedgerConflict as conflict:
            check("recovery: refused before the effect",
                  conflict.code in ("LEASE_NOT_HELD", "STATE_MOVED"), conflict.code)
        else:
            raise AssertionError("a stale holder initiated a bearer recovery")
        check("recovery: NO request was transmitted",
              len(stale.transport.requests) == before)
    finally:
        stale.close()

    # (d) PROVIDER SUBMISSION and (e) REMOTE DELIVERED.
    for label, steps, state in (("provider submit", 2,
                                 DeliveryState.DELIVERY_INTENT_RECORDED),
                                ("remote delivered", 3,
                                 DeliveryState.PROVIDER_ACCEPTED)):
        identity = fresh_identity()
        scoped = ep.FakeEmailProvider(account_scope=f"stale-{label}")
        stale = Harness(ctx["dsn"], ctx["worker"], scoped)
        try:
            stale.advance(identity, owner="rightful", max_steps=steps)
            record = stale.row(identity)
            check(f"{label}: the delivery is where the test needs it",
                  record.state == state, record.state)
            stale.ledger.claim(record.delivery_id, owner="ghost", lease_seconds=60)
            with stale.conn.cursor() as cur:
                cur.execute("UPDATE public.eco_dashboard_delivery_operation "
                            "SET lease_expires_at = now() - interval '1 second' "
                            "WHERE delivery_id = %s", (str(record.delivery_id),))
            record = stale.ledger.load(record.delivery_id)
            submissions_before = len(scoped.submissions)
            requests_before = len(stale.transport.requests)
            try:
                if label == "provider submit":
                    pub._submit(stale.services, record, "ghost")
                else:
                    pub._mark_remote_delivered(stale.services, record, "ghost")
            except LedgerConflict as conflict:
                check(f"{label}: refused before the effect",
                      conflict.code in ("LEASE_NOT_HELD", "STATE_MOVED", "LOST_OWNERSHIP"),
                      conflict.code)
            else:
                raise AssertionError(f"a stale holder initiated the {label} effect")
            check(f"{label}: the provider was not contacted",
                  len(scoped.submissions) == submissions_before)
            check(f"{label}: no request was transmitted",
                  len(stale.transport.requests) == requests_before)
        finally:
            stale.close()
    PASSED.append("a_stale_holder_initiates_no_remote_effect")


def test_a_lease_lost_during_an_in_flight_submission_cannot_duplicate(ctx) -> None:
    """The residual window, and why ORDERING rather than locking covers it.

    The lease expires and another owner takes over WHILE a submission is in
    flight. The taking-over owner must find `PROVIDER_SUBMISSION_PENDING` — the
    marker committed before the call — and reconcile under the bound identity
    rather than sending a second message.
    """
    identity = fresh_identity()
    key = derive_provider_idempotency_key(derive_operation_id(identity))
    store: dict = {}
    slow = ep.FakeEmailProvider(account_scope="inflight", store=store)
    taker_provider = ep.FakeEmailProvider(account_scope="inflight", store=store)

    started = threading.Event()
    release = threading.Event()
    original_submit = slow.submit

    def blocking_submit(*, message, idempotency_key):
        started.set()
        release.wait(20)
        return original_submit(message=message, idempotency_key=idempotency_key)

    slow.submit = blocking_submit  # type: ignore[method-assign]

    first = Harness(ctx["dsn"], ctx["worker"], slow, lease_seconds=2)
    outcome: dict = {}

    def run_first():
        try:
            outcome["result"] = first.advance(identity, owner="inflight-owner")
        except Exception as error:  # pragma: no cover - reported below
            outcome["error"] = error

    thread = threading.Thread(target=run_first)
    thread.start()
    try:
        check("the submission started", started.wait(20))
        # Expire the lease under the in-flight call and let a second owner in.
        taker = Harness(ctx["dsn"], ctx["worker"], taker_provider)
        try:
            with taker.conn.cursor() as cur:
                cur.execute("UPDATE public.eco_dashboard_delivery_operation "
                            "SET lease_expires_at = now() - interval '1 second' "
                            "WHERE operation_id = %s", (derive_operation_id(identity),))
                cur.execute("SELECT state FROM public.eco_dashboard_delivery_operation "
                            "WHERE operation_id = %s", (derive_operation_id(identity),))
                mid = cur.fetchone()["state"]
            check("the durable state already says a message may exist",
                  mid == DeliveryState.PROVIDER_SUBMISSION_PENDING, mid)
            taken = taker.advance(identity, owner="taking-over")
            record = taker.row(identity)
            # It may replay — but ONLY under the bound idempotency identity, to
            # the bound backend, and only because that backend deduplicates by
            # it. Anything else would be a blind resend.
            check("any replay used the bound idempotency identity",
                  {s["idempotency_key"] for s in taker_provider.submissions} <= {key},
                  str({s["idempotency_key"] for s in taker_provider.submissions}))
            check("the binding did not drift",
                  record.provider_backend_id
                  == taker_provider.backend_identity().stable_id())
            check("it reconciled to a resolved state",
                  record.state in (DeliveryState.FINALIZED,
                                   DeliveryState.PROVIDER_ACCEPTED,
                                   DeliveryState.REMOTE_DELIVERED),
                  f"{record.state} / {taken.detail}")
        finally:
            taker.close()
    finally:
        release.set()
        thread.join(20)
        first.close()

    check("exactly one message exists on that backend", len(store) == 1, str(len(store)))
    check("it is the bound idempotency identity", set(store) == {key}, str(set(store)))
    if "result" in outcome:
        check("the stale invocation reported that it lost ownership",
              outcome["result"].invocation in (pub.INVOCATION.NOT_OWNED,
                                               pub.INVOCATION.RETRY_LATER,
                                               pub.INVOCATION.COMPLETED),
              outcome["result"].invocation)
    PASSED.append("a_lease_lost_during_an_in_flight_submission_cannot_duplicate")


def test_a_takeover_never_replays_to_a_provider_that_cannot_deduplicate(ctx) -> None:
    """The same takeover, against an honest non-idempotent provider.

    A replay is safe only because the backend deduplicates by the key. Take
    that away — which is exactly what SMTP is — and the only honest answer is
    to stop and ask a human. The fake would gladly create a second message if
    asked, which is what makes this assertion meaningful.
    """
    identity = fresh_identity()
    key = derive_provider_idempotency_key(derive_operation_id(identity))
    store: dict = {}
    honest = ep.FakeEmailProvider(supports_idempotent_submit=False,
                                  supports_reconciliation=False,
                                  account_scope="no-dedupe", store=store)
    honest.script(key, "accept_then_lose_response")

    host = Harness(ctx["dsn"], ctx["worker"], honest)
    try:
        host.advance(identity, max_steps=3)
        record = host.row(identity)
        check("a message may exist", record.state
              == DeliveryState.PROVIDER_SUBMISSION_PENDING, record.state)
        check("the backend really did accept one", len(store) == 1, str(len(store)))
    finally:
        host.close()

    takeover = ep.FakeEmailProvider(supports_idempotent_submit=False,
                                    supports_reconciliation=False,
                                    account_scope="no-dedupe", store=store)
    taker = Harness(ctx["dsn"], ctx["worker"], takeover)
    try:
        result = taker.advance(identity, owner="taking-over")
        record = taker.row(identity)
        check("the takeover submitted nothing", takeover.submissions == [],
              str(len(takeover.submissions)))
        check("it stopped at ambiguity",
              record.state == DeliveryState.PROVIDER_AMBIGUOUS, record.state)
        check("an operator is required",
              result.invocation == pub.INVOCATION.OPERATOR_REQUIRED, result.invocation)
        check("still exactly one message on the backend", len(store) == 1,
              str(len(store)))
    finally:
        taker.close()
    PASSED.append("a_takeover_never_replays_to_a_provider_that_cannot_deduplicate")


def test_a_rolled_back_transaction_leaves_no_ownership(ctx) -> None:
    """An explicit rollback must not leave a phantom lease behind."""
    import psycopg
    from psycopg.rows import dict_row

    identity = fresh_identity()
    provider = ep.FakeEmailProvider()
    host = Harness(ctx["dsn"], ctx["worker"], provider)
    try:
        payload, digest = canonical_payload()
        record, _ = pub.prepare_delivery(host.ledger, identity, payload_digest=digest,
                                         recipient_email=RECIPIENT)
        other = psycopg.connect(ctx["dsn"], row_factory=dict_row)
        try:
            with other.cursor() as cur:
                cur.execute("UPDATE public.eco_dashboard_delivery_operation "
                            "SET lease_owner = %s, "
                            "lease_expires_at = now() + interval '1 hour' "
                            "WHERE delivery_id = %s",
                            ("rolled-back-owner", str(record.delivery_id)))
            other.rollback()
        finally:
            other.close()
        check("no lease survived the rollback",
              not host.ledger.holds_lease(record.delivery_id, owner="rolled-back-owner"))
        claimed = host.ledger.claim(record.delivery_id, owner="after-rollback")
        check("the row is claimable", claimed is not None)
    finally:
        host.close()
    PASSED.append("a_rolled_back_transaction_leaves_no_ownership")


def test_concurrent_stale_and_live_owners_converge(ctx) -> None:
    """Many invocations, one of them holding an expired lease. One lifecycle."""
    for workers in (2, 8, 32):
        identity = fresh_identity()
        provider = ep.FakeEmailProvider(account_scope=f"race-{workers}")
        seed = Harness(ctx["dsn"], ctx["worker"], provider)
        try:
            payload, digest = canonical_payload()
            record, _ = pub.prepare_delivery(seed.ledger, identity,
                                             payload_digest=digest,
                                             recipient_email=RECIPIENT)
            seed.ledger.claim(record.delivery_id, owner="ghost", lease_seconds=60)
            with seed.conn.cursor() as cur:
                cur.execute("UPDATE public.eco_dashboard_delivery_operation "
                            "SET lease_expires_at = now() - interval '1 second' "
                            "WHERE delivery_id = %s", (str(record.delivery_id),))
        finally:
            seed.close()

        results: list = []
        lock = threading.Lock()

        def one(index: int) -> None:
            worker_host = Harness(ctx["dsn"], ctx["worker"], provider)
            try:
                result = worker_host.advance(identity, owner=f"racer-{index}")
                with lock:
                    results.append(result)
            finally:
                worker_host.close()

        threads = [threading.Thread(target=one, args=(i,)) for i in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)

        reader = Harness(ctx["dsn"], ctx["worker"], provider)
        try:
            record = reader.row(identity)
        finally:
            reader.close()
        key = derive_provider_idempotency_key(derive_operation_id(identity))
        label = f"{workers}-way with a stale holder"
        check(f"{label}: every invocation returned", len(results) == workers)
        check(f"{label}: the delivery finalised",
              record.state == DeliveryState.FINALIZED, record.state)
        check(f"{label}: at most one accepted message",
              provider.accepted_count(key) <= 1)
        check(f"{label}: one provider attempt",
              record.provider_attempts == 1, str(record.provider_attempts))
        check(f"{label}: one submission for this key",
              len([s for s in provider.submissions if s["idempotency_key"] == key]) == 1)
    PASSED.append("concurrent_stale_and_live_owners_converge")


# --- 4b. the STALE RECONCILIATION matrix ----------------------------------------
#
# Reconciliation is an external call made against provider state, and the
# earlier remediation fenced every other remote boundary but this one. A stale
# holder could still ASK the provider; only its subsequent write was refused.
# That is not enough: the answer is what a stale process acts on, and the call
# itself is an interaction with a system that does not know the lease expired.


def pending_delivery(ctx, provider, *, behaviour="accept_then_lose_response",
                     account="stale-recon"):
    """Drive one delivery to PROVIDER_SUBMISSION_PENDING and return its row."""
    identity = fresh_identity()
    key = derive_provider_idempotency_key(derive_operation_id(identity))
    provider.script(key, behaviour)
    host = Harness(ctx["dsn"], ctx["worker"], provider)
    try:
        host.advance(identity, max_steps=3)
        record = host.row(identity)
        check("the delivery is awaiting reconciliation",
              record.state == DeliveryState.PROVIDER_SUBMISSION_PENDING, record.state)
        return identity, key, record
    finally:
        host.close()


def expire_lease(harness, delivery_id, *, seconds: int = 1) -> None:
    with harness.conn.cursor() as cur:
        cur.execute("UPDATE public.eco_dashboard_delivery_operation "
                    "SET lease_expires_at = now() - make_interval(secs => %s) "
                    "WHERE delivery_id = %s", (seconds, str(delivery_id)))


def test_provider_reconciliation_requires_a_live_lease(ctx) -> None:
    """A/B/C/D of the matrix: who may ask the provider about the key at all."""
    # (A) A VALID lease may reconcile. The control case — a fence that refuses
    #     everything would pass every other assertion here.
    store: dict = {}
    provider = ep.FakeEmailProvider(account_scope="recon-live", store=store)
    identity, key, record = pending_delivery(ctx, provider, account="recon-live")
    live = Harness(ctx["dsn"], ctx["worker"], provider)
    try:
        claimed = live.ledger.claim(record.delivery_id, owner="owner-a", lease_seconds=60)
        check("the owner holds a live lease", claimed is not None)
        step = pub._reconcile(live.services, claimed, "owner-a")
        check("a live owner reconciled", len(provider.reconciliations) == 1,
              str(len(provider.reconciliations)))
        check("and adopted the acceptance",
              step.record is not None
              and step.record.state == DeliveryState.PROVIDER_ACCEPTED,
              str(step.record.state if step.record else None))
    finally:
        live.close()

    # (B) EXPIRED before reconciliation, with nobody else holding it.
    # (C) EXPIRED, then owner B takes over; stale A resumes.
    # (D) A passed an earlier local step, then expired before `_reconcile`.
    for label, takeover, renew_first in (("expired holder", False, False),
                                         ("stale holder after a takeover", True, False),
                                         ("holder that expired mid-lifecycle", False, True)):
        scoped_store: dict = {}
        scoped = ep.FakeEmailProvider(account_scope=f"recon-{len(label)}",
                                      store=scoped_store)
        identity, key, record = pending_delivery(ctx, scoped)
        stale = Harness(ctx["dsn"], ctx["worker"], scoped)
        try:
            held = stale.ledger.claim(record.delivery_id, owner="owner-a",
                                      lease_seconds=60)
            if renew_first:
                # It was a legitimate owner a moment ago; the local work it did
                # is exactly what makes it believe it may continue.
                check("the earlier step was legitimate",
                      stale.ledger.renew(record.delivery_id, owner="owner-a",
                                         lease_seconds=60) is not None)
            expire_lease(stale, record.delivery_id)
            if takeover:
                taken = stale.ledger.claim(record.delivery_id, owner="owner-b",
                                           lease_seconds=60)
                check(f"{label}: owner B took over", taken is not None)
            before = len(scoped.reconciliations)
            fresh = stale.ledger.load(record.delivery_id)
            try:
                pub._reconcile(stale.services, held or fresh, "owner-a")
            except LedgerConflict as conflict:
                check(f"{label}: refused before the effect",
                      conflict.code in ("LEASE_NOT_HELD", "STATE_MOVED",
                                        "LOST_OWNERSHIP"), conflict.code)
            else:
                raise AssertionError(f"a {label} initiated a provider reconciliation")
            check(f"{label}: ZERO provider reconciliation calls",
                  len(scoped.reconciliations) == before,
                  str(len(scoped.reconciliations) - before))
            check(f"{label}: ZERO provider submissions after the attempt",
                  len(scoped.submissions) == 1, str(len(scoped.submissions)))
            after = stale.ledger.load(record.delivery_id)
            check(f"{label}: the durable state is untouched",
                  after.state == DeliveryState.PROVIDER_SUBMISSION_PENDING, after.state)
        finally:
            stale.close()
    PASSED.append("provider_reconciliation_requires_a_live_lease")


def test_a_reconciliation_answer_arriving_after_a_takeover_changes_nothing(ctx) -> None:
    """(E) The residual window: the lease expires DURING the external call.

    No database lease is atomic with a network call, so this cannot be
    prevented — only made harmless. The late answer must not mutate the ledger,
    and the new owner must converge on the SAME message rather than a second.
    """
    store: dict = {}
    slow = ep.FakeEmailProvider(account_scope="recon-inflight", store=store)
    identity, key, record = pending_delivery(ctx, slow, account="recon-inflight")

    started = threading.Event()
    release = threading.Event()
    original_reconcile = slow.reconcile

    def blocking_reconcile(*, idempotency_key):
        started.set()
        release.wait(20)
        return original_reconcile(idempotency_key=idempotency_key)

    slow.reconcile = blocking_reconcile  # type: ignore[method-assign]

    first = Harness(ctx["dsn"], ctx["worker"], slow, lease_seconds=2)
    outcome: dict = {}

    def run_first():
        try:
            held = first.ledger.claim(record.delivery_id, owner="owner-a",
                                      lease_seconds=2)
            outcome["step"] = pub._reconcile(first.services, held, "owner-a")
        except Exception as error:
            outcome["error"] = error

    thread = threading.Thread(target=run_first)
    thread.start()
    try:
        check("the reconciliation call started", started.wait(20))
        taker_provider = ep.FakeEmailProvider(account_scope="recon-inflight",
                                              store=store)
        taker = Harness(ctx["dsn"], ctx["worker"], taker_provider)
        try:
            expire_lease(taker, record.delivery_id)
            taken = taker.advance(identity, owner="owner-b")
            final = taker.row(identity)
            check("the new owner resolved the delivery",
                  final.state in (DeliveryState.FINALIZED,
                                  DeliveryState.PROVIDER_ACCEPTED,
                                  DeliveryState.REMOTE_DELIVERED),
                  f"{final.state} / {taken.detail}")
            check("it used the bound idempotency identity",
                  {s["idempotency_key"] for s in taker_provider.submissions} <= {key},
                  str({s["idempotency_key"] for s in taker_provider.submissions}))
        finally:
            taker.close()
    finally:
        release.set()
        thread.join(20)
        first.close()

    check("exactly one accepted message survives the whole race",
          len(store) == 1 and set(store) == {key}, str(sorted(store)))
    if "error" in outcome:
        check("the late writer was refused, not applied",
              isinstance(outcome["error"], LedgerConflict), repr(outcome["error"]))
    PASSED.append("a_reconciliation_answer_arriving_after_a_takeover_changes_nothing")


def test_concurrent_reconciliation_attempts_produce_one_lifecycle(ctx) -> None:
    """(F) Many invocations racing on the SAME unresolved delivery."""
    for workers in (2, 8, 32):
        store: dict = {}
        provider = ep.FakeEmailProvider(account_scope=f"recon-race-{workers}",
                                        store=store)
        identity, key, record = pending_delivery(ctx, provider)
        # A stale holder is in the mix, exactly as after a crash.
        seed = Harness(ctx["dsn"], ctx["worker"], provider)
        try:
            seed.ledger.claim(record.delivery_id, owner="ghost", lease_seconds=60)
            expire_lease(seed, record.delivery_id)
        finally:
            seed.close()

        results: list = []
        lock = threading.Lock()

        def one(index: int) -> None:
            worker_host = Harness(ctx["dsn"], ctx["worker"], provider)
            try:
                result = worker_host.advance(identity, owner=f"recon-racer-{index}")
                with lock:
                    results.append(result)
            finally:
                worker_host.close()

        threads = [threading.Thread(target=one, args=(i,)) for i in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)

        reader = Harness(ctx["dsn"], ctx["worker"], provider)
        try:
            final = reader.row(identity)
        finally:
            reader.close()
        label = f"{workers}-way reconciliation race"
        check(f"{label}: every invocation returned", len(results) == workers)
        check(f"{label}: the delivery finalised",
              final.state == DeliveryState.FINALIZED, final.state)
        check(f"{label}: exactly one accepted message",
              len(store) == 1 and set(store) == {key}, str(sorted(store)))
        check(f"{label}: one submission in total",
              len(provider.submissions) == 1, str(len(provider.submissions)))
        check(f"{label}: one remote DELIVERED transition",
              final.remote_delivered_at is not None)
    PASSED.append("concurrent_reconciliation_attempts_produce_one_lifecycle")


# --- 5. provider preflight and SMTP semantics -----------------------------------


class SmtpConfig:
    """An explicit config object. The ambient environment is never read."""

    def __init__(self, **overrides):
        self.host = "smtp.example.invalid"
        self.port = 587
        self.username = "automation@example.invalid"
        self.password = "unused-by-any-check"
        self.use_tls = True
        self.from_addr = "automation@example.invalid"
        self.timeout_s = 30
        for key, value in overrides.items():
            setattr(self, key, value)


def _refusing_sender(*args, **kwargs):  # pragma: no cover - must never run
    raise AssertionError("an SMTP send was attempted")


BROKEN_CONFIGS = (
    ("missing SMTP host", SmtpConfig(host=""), "SMTP_HOST_NOT_CONFIGURED"),
    ("missing envelope sender", SmtpConfig(from_addr=""), "SMTP_SENDER_NOT_CONFIGURED"),
    ("non-numeric port", SmtpConfig(port="not-a-port"), "SMTP_PORT_INVALID"),
    ("out-of-range port", SmtpConfig(port=99999), "SMTP_PORT_INVALID"),
)


def test_missing_provider_configuration_publishes_nothing(ctx) -> None:
    """The finding: configuration was discovered AFTER the remote publication."""
    for label, config, expected in BROKEN_CONFIGS:
        identity = fresh_identity()
        provider = ep.SmtpEmailProvider(config=config, sender=_refusing_sender)
        before = ctx["worker"].inspect()
        host = Harness(ctx["dsn"], ctx["worker"], provider)
        try:
            result = host.advance(identity)
            after = ctx["worker"].inspect()
            check(f"{label}: the invocation failed preflight",
                  result.invocation == pub.INVOCATION.PREFLIGHT_FAILED, result.invocation)
            check(f"{label}: it names the configuration problem",
                  result.conflict_code == expected, str(result.conflict_code))
            check(f"{label}: ZERO secure-delivery publication",
                  after["operations"] == before["operations"]
                  and after["objects"] == before["objects"], "a publication happened")
            check(f"{label}: ZERO capability was issued",
                  after["grants"] == before["grants"])
            check(f"{label}: nothing was transmitted",
                  host.transport.requests == [], str(len(host.transport.requests)))
            check(f"{label}: no durable delivery row was created",
                  host.row(identity) is None)
        finally:
            host.close()
    PASSED.append("missing_provider_configuration_publishes_nothing")


def test_a_provider_whose_preflight_raises_publishes_nothing(ctx) -> None:
    identity = fresh_identity()

    class Exploding(ep.FakeEmailProvider):
        def preflight(self):
            raise RuntimeError("the provider SDK blew up")

    provider = Exploding()
    before = ctx["worker"].inspect()
    host = Harness(ctx["dsn"], ctx["worker"], provider)
    try:
        result = host.advance(identity)
        after = ctx["worker"].inspect()
        check("preflight failed", result.invocation == pub.INVOCATION.PREFLIGHT_FAILED,
              result.invocation)
        check("it is reported as a preflight failure, not an ambiguity",
              result.conflict_code == "PROVIDER_PREFLIGHT_FAILED",
              str(result.conflict_code))
        check("nothing was published", after["operations"] == before["operations"])
        check("the provider was never asked to submit", provider.submissions == [])
    finally:
        host.close()

    # A fake provider that cannot name a backend scope fails the same way.
    identity = fresh_identity()
    unscoped = ep.FakeEmailProvider(configuration_error="FAKE_ACCOUNT_NOT_CONFIGURED")
    host = Harness(ctx["dsn"], ctx["worker"], unscoped)
    try:
        result = host.advance(identity)
        check("an unavailable backend scope fails preflight",
              result.invocation == pub.INVOCATION.PREFLIGHT_FAILED
              and result.conflict_code == "FAKE_ACCOUNT_NOT_CONFIGURED",
              str(result.conflict_code))
        check("nothing was submitted", unscoped.submissions == [])
    finally:
        host.close()
    PASSED.append("a_provider_whose_preflight_raises_publishes_nothing")


def test_a_pre_submit_provider_failure_never_becomes_ambiguous(ctx) -> None:
    """Config that breaks AFTER binding is still DEFINITE, at both boundaries.

    (a) It breaks between invocations: the next execution fails preflight and
        the delivery stays exactly where it was, having submitted nothing.
    (b) It breaks between the pre-submit check and the call itself: the marker
        is already committed, so the outcome is recorded — as a definite
        refusal, because the provider contract says a configuration error is
        raised before any network submission.
    """
    # (a) between invocations.
    identity = fresh_identity()
    config = SmtpConfig()
    provider = ep.SmtpEmailProvider(config=config, sender=_refusing_sender)
    host = Harness(ctx["dsn"], ctx["worker"], provider)
    try:
        host.advance(identity, max_steps=2)
        check("the delivery is bound and about to submit",
              host.row(identity).state == DeliveryState.DELIVERY_INTENT_RECORDED,
              host.row(identity).state)
        config.host = ""
        result = host.advance(identity)
        record = host.row(identity)
        check("(a) it is a preflight failure, not an ambiguity",
              result.invocation == pub.INVOCATION.PREFLIGHT_FAILED, result.invocation)
        check("(a) it names the configuration problem",
              result.conflict_code == "SMTP_HOST_NOT_CONFIGURED",
              str(result.conflict_code))
        check("(a) the state never became PROVIDER_AMBIGUOUS",
              record.state == DeliveryState.DELIVERY_INTENT_RECORDED, record.state)
        check("(a) nothing was submitted", record.provider_attempts == 0,
              str(record.provider_attempts))
    finally:
        host.close()
    # ...and repairing the configuration resumes the delivery normally.
    config.host = "smtp.example.invalid"
    repaired = Harness(ctx["dsn"], ctx["worker"],
                       ep.SmtpEmailProvider(config=config,
                                            sender=lambda **kw: _SentResult()))
    try:
        repaired.advance(identity)
        check("(a) a repaired configuration completes the delivery",
              repaired.row(identity).state == DeliveryState.FINALIZED,
              repaired.row(identity).state)
    finally:
        repaired.close()

    # (b) between the pre-submit check and the call.
    identity = fresh_identity()

    class BreaksAtTheLastMoment(ep.FakeEmailProvider):
        """Preflight and backend resolution succeed; the submission does not."""

        def submit(self, *, message, idempotency_key):
            raise ep.ProviderConfigurationError(
                "PROVIDER_ACCOUNT_DISABLED", "the account was disabled")

    late = BreaksAtTheLastMoment(account_scope="breaks-late")
    host = Harness(ctx["dsn"], ctx["worker"], late)
    try:
        result = host.advance(identity)
        record = host.row(identity)
        check("(b) the outcome is a DEFINITE refusal",
              record.state == DeliveryState.PROVIDER_REJECTED, record.state)
        check("(b) it is not ambiguity",
              record.state != DeliveryState.PROVIDER_AMBIGUOUS)
        check("(b) it names the configuration failure",
              record.failure_code == "PROVIDER_ACCOUNT_DISABLED",
              str(record.failure_code))
        check("(b) no acceptance was recorded", record.provider_message_id is None)
        check("(b) the remote operation was never marked DELIVERED",
              record.remote_delivered_at is None)
        check("(b) an operator is required",
              result.invocation == pub.INVOCATION.OPERATOR_REQUIRED, result.invocation)
    finally:
        host.close()
    PASSED.append("a_pre_submit_provider_failure_never_becomes_ambiguous")


class _SentResult:
    """What `send_html_email` returns. Nothing was actually sent."""

    message_id = "<synthetic@example.invalid>"
    recipients = ()


def test_smtp_declares_no_guarantee_it_does_not_have(ctx) -> None:
    provider = ep.SmtpEmailProvider(config=SmtpConfig(), sender=_refusing_sender)
    check("SMTP does not claim idempotent submission",
          provider.supports_idempotent_submit is False)
    check("SMTP does not claim reconciliation",
          provider.supports_reconciliation is False)
    check("asking it anyway is answered UNSUPPORTED",
          provider.reconcile(idempotency_key="eco-dash-x").outcome == ep.UNSUPPORTED)
    PASSED.append("smtp_declares_no_guarantee_it_does_not_have")


def test_a_definite_smtp_rejection_creates_no_message(ctx) -> None:
    identity = fresh_identity()

    def refusing(**kwargs):
        raise smtplib.SMTPResponseException(550, b"mailbox unavailable")

    provider = ep.SmtpEmailProvider(config=SmtpConfig(), sender=refusing)
    host = Harness(ctx["dsn"], ctx["worker"], provider)
    try:
        result = host.advance(identity)
        record = host.row(identity)
        check("a definite refusal is recorded as one",
              record.state == DeliveryState.PROVIDER_REJECTED, record.state)
        check("it is not ambiguous", record.state != DeliveryState.PROVIDER_AMBIGUOUS)
        check("no acceptance was recorded", record.provider_message_id is None)
        check("the remote operation was never marked DELIVERED",
              record.remote_delivered_at is None)
        check("an operator is required for a permanent refusal",
              result.invocation == pub.INVOCATION.OPERATOR_REQUIRED, result.invocation)
    finally:
        host.close()
    PASSED.append("a_definite_smtp_rejection_creates_no_message")


def test_a_genuinely_ambiguous_smtp_submission_is_never_auto_retried(ctx) -> None:
    """The submission may have happened. SMTP cannot say. A human decides."""
    identity = fresh_identity()
    attempts: list = []

    def disconnecting(**kwargs):
        attempts.append(kwargs.get("subject"))
        raise smtplib.SMTPServerDisconnected("the connection dropped mid-DATA")

    provider = ep.SmtpEmailProvider(config=SmtpConfig(), sender=disconnecting)
    host = Harness(ctx["dsn"], ctx["worker"], provider)
    try:
        result = host.advance(identity)
        record = host.row(identity)
        check("ambiguity is recorded as ambiguity",
              record.state == DeliveryState.PROVIDER_AMBIGUOUS, record.state)
        check("exactly one submission was attempted", len(attempts) == 1, str(len(attempts)))
        check("an operator is required",
              result.invocation == pub.INVOCATION.OPERATOR_REQUIRED)
        check("the bearer is retained for the operator's reconciliation",
              record.has_bearer)
        check("the provider identity is retained",
              record.provider_backend_id and record.provider_message_fingerprint)
    finally:
        host.close()

    # A RESTART must not resend. This is the whole point of the state.
    restarted = Harness(ctx["dsn"], ctx["worker"],
                        ep.SmtpEmailProvider(config=SmtpConfig(), sender=disconnecting))
    try:
        result = restarted.advance(identity)
        check("a restart made no further submission", len(attempts) == 1,
              str(len(attempts)))
        check("it still requires an operator",
              result.invocation == pub.INVOCATION.OPERATOR_REQUIRED, result.invocation)
        check("the state did not move",
              restarted.row(identity).state == DeliveryState.PROVIDER_AMBIGUOUS)
    finally:
        restarted.close()
    PASSED.append("a_genuinely_ambiguous_smtp_submission_is_never_auto_retried")


# --- 6. crash boundaries added by this remediation ------------------------------


def test_the_new_crash_boundaries_have_one_safe_action(ctx) -> None:
    """The boundaries the binding and preflight work introduced.

    Stage 6  — provider scope and message identity persisted, before submit.
    Stage 7  — definite pre-submit configuration failure.
    Stage 11 — restart with a DIFFERENT provider backend.
    Stage 12 — configuration drift after the intent binding.
    """
    from jobs.ecodriving_dashboard.delivery_contract import safe_next_action

    # Stage 6: bound, nothing submitted, one safe action.
    identity = fresh_identity()
    provider = ep.FakeEmailProvider(account_scope="crash-6")
    host = Harness(ctx["dsn"], ctx["worker"], provider)
    try:
        host.advance(identity, max_steps=2)
        record = host.row(identity)
        check("stage 6: bound before submit",
              record.state == DeliveryState.DELIVERY_INTENT_RECORDED, record.state)
        check("stage 6: the whole submission identity is durable",
              all([record.provider_idempotency_key, record.provider_backend_id,
                   record.provider_message_fingerprint,
                   record.provider_bound_capability_id,
                   record.provider_bound_bearer_generation]))
        check("stage 6: nothing was submitted", provider.submissions == [])
        check("stage 6: the one safe action is to submit",
              safe_next_action(record.state) == "SUBMIT_TO_PROVIDER")
    finally:
        host.close()
    # ...and a restart completes it.
    resumed = Harness(ctx["dsn"], ctx["worker"],
                      ep.FakeEmailProvider(account_scope="crash-6",
                                           store=provider.messages))
    try:
        resumed.advance(identity)
        check("stage 6: a restart completes the delivery",
              resumed.row(identity).state == DeliveryState.FINALIZED,
              resumed.row(identity).state)
    finally:
        resumed.close()
    PASSED.append("the_new_crash_boundaries_have_one_safe_action")


# --- runner ---------------------------------------------------------------------


TESTS = [
    test_only_real_loopback_may_receive_a_plaintext_credential,
    test_a_malformed_https_host_is_refused_before_any_request,
    test_a_refused_endpoint_never_sees_the_credential,
    test_a_backend_swap_after_a_lost_response_submits_nothing,
    test_the_same_backend_after_a_lost_response_converges_to_one_message,
    test_the_same_provider_type_with_a_different_account_is_a_different_backend,
    test_rotating_a_credential_is_not_a_backend_change,
    test_a_drifted_dashboard_base_url_is_refused_before_submitting,
    test_a_drifted_message_id_domain_is_refused_before_submitting,
    test_a_drifted_subject_template_is_refused_before_submitting,
    test_a_drifted_capability_generation_is_refused_before_submitting,
    test_a_drifted_recipient_is_refused_before_any_work,
    test_an_unchanged_configuration_still_submits_normally,
    test_response_loss_then_base_url_drift_conflicts_before_reconciling,
    test_response_loss_then_message_id_domain_drift_conflicts,
    test_response_loss_then_subject_drift_conflicts,
    test_response_loss_then_text_body_drift_conflicts,
    test_response_loss_then_html_body_drift_conflicts,
    test_response_loss_then_bearer_generation_drift_conflicts,
    test_response_loss_then_recipient_drift_conflicts_before_any_provider_call,
    test_response_loss_without_drift_reconciles_to_one_message,
    test_an_expired_holder_cannot_mutate_state,
    test_a_stale_holder_initiates_no_remote_effect,
    test_a_lease_lost_during_an_in_flight_submission_cannot_duplicate,
    test_a_takeover_never_replays_to_a_provider_that_cannot_deduplicate,
    test_a_rolled_back_transaction_leaves_no_ownership,
    test_concurrent_stale_and_live_owners_converge,
    test_provider_reconciliation_requires_a_live_lease,
    test_a_reconciliation_answer_arriving_after_a_takeover_changes_nothing,
    test_concurrent_reconciliation_attempts_produce_one_lifecycle,
    test_missing_provider_configuration_publishes_nothing,
    test_a_provider_whose_preflight_raises_publishes_nothing,
    test_a_pre_submit_provider_failure_never_becomes_ambiguous,
    test_smtp_declares_no_guarantee_it_does_not_have,
    test_a_definite_smtp_rejection_creates_no_message,
    test_a_genuinely_ambiguous_smtp_submission_is_never_auto_retried,
    test_the_new_crash_boundaries_have_one_safe_action,
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
    print(f"\n{len(PASSED)} remediation checks passed in {time.time() - started:.1f}s "
          f"— no real e-mail, no SMTP connection, no deployment, no Cloudflare mutation")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
