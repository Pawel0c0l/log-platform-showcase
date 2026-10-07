#!/usr/bin/env python3
"""Driver Eco Dashboard V1 — host delivery contract, message and provider suite.

Run:
    python3 ops/tests_manual/test_driver_eco_dashboard_email_contract.py

No database, no network, no Node, no e-mail. This is the part of the host
publisher/e-mail lifecycle that is decidable from pure code:

  * the durable state model — every state has exactly ONE safe next action, and
    no illegal transition is expressible;
  * identity derivation — the logical delivery identity, the publication
    operation id, the opaque subject reference and the provider idempotency
    identity are deterministic, domain-separated and unambiguous;
  * the capability link — fragment only, never a query string, and redacted
    everywhere it can be printed;
  * message construction — carries the link and report context, and carries no
    internal identifier;
  * the provider abstraction — including the deliberately honest declaration
    that SMTP is neither idempotent nor queryable;
  * source-level guards — no schedule registration, `render_only` by default.

The full lifecycle (persistence, crash matrix, concurrency, the real Worker)
lives in `test_driver_eco_dashboard_publisher_lifecycle.py`, which needs a
disposable PostgreSQL instance.

Synthetic data only. No capability, credential or address printed.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.common.emailer import SmtpConfig  # noqa: E402
from jobs.ecodriving_dashboard import dashboard_email as de  # noqa: E402
from jobs.ecodriving_dashboard import email_provider as ep  # noqa: E402
from jobs.ecodriving_dashboard import delivery_contract as dc  # noqa: E402

PASSED: list[str] = []
SYNTHETIC_CAPABILITY = "A" * 43
SYNTHETIC_REPLACEMENT = "B" * 43
BASE_URL = "https://eco.example.invalid/dashboard"
RECIPIENT = "driver.one@example.invalid"


def identity(**overrides) -> dc.DeliveryIdentity:
    fields = dict(
        client_id="11111111-1111-1111-1111-111111111111",
        identity_key="SYNTHETIC-DRIVER-0001",
        period_type="weekly",
        period_start_date=date(2026, 7, 1),
        period_end_date=date(2026, 7, 20),
        send_scope="normal",
    )
    fields.update(overrides)
    return dc.DeliveryIdentity(**fields)


def check(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name}: {detail}" if detail else name)


# --- 1. the durable state model -------------------------------------------------


def test_every_state_has_exactly_one_safe_next_action() -> None:
    for state in dc.ALL_STATES:
        action = dc.safe_next_action(state)
        check("next action is defined", bool(action), state)
    # No two states that need DIFFERENT recovery behaviour may share an action,
    # because the action IS the recovery instruction.
    progressing = {s: dc.safe_next_action(s) for s in dc.ALL_STATES
                   if s not in dc.TERMINAL_STATES and s != dc.DeliveryState.PROVIDER_REJECTED}
    check("progressing states have distinct actions",
          len(set(progressing.values())) == len(progressing), str(progressing))
    check("finalized does nothing",
          dc.safe_next_action(dc.DeliveryState.FINALIZED) == dc.NextAction.NONE)
    check("pending submission reconciles, never resends",
          dc.safe_next_action(dc.DeliveryState.PROVIDER_SUBMISSION_PENDING)
          == dc.NextAction.RECONCILE_PROVIDER)
    check("ambiguity waits for a human",
          dc.safe_next_action(dc.DeliveryState.PROVIDER_AMBIGUOUS)
          == dc.NextAction.OPERATOR_RECONCILIATION)
    try:
        dc.safe_next_action("NOT_A_STATE")
    except dc.DeliveryContractError:
        pass
    else:
        raise AssertionError("an unknown state must not resolve to an action")
    PASSED.append("every_state_has_exactly_one_safe_next_action")


def test_illegal_transitions_are_refused() -> None:
    illegal = [
        # A lost bearer is never answered with a re-publish.
        (dc.DeliveryState.BEARER_RECOVERY_REQUIRED, dc.DeliveryState.PREPARED),
        # Acceptance is never reached without going through submission.
        (dc.DeliveryState.DELIVERY_INTENT_RECORDED, dc.DeliveryState.PROVIDER_ACCEPTED),
        # DELIVERED is never reached without recorded acceptance.
        (dc.DeliveryState.PROVIDER_SUBMISSION_PENDING, dc.DeliveryState.REMOTE_DELIVERED),
        (dc.DeliveryState.DELIVERY_INTENT_RECORDED, dc.DeliveryState.REMOTE_DELIVERED),
        (dc.DeliveryState.CAPABILITY_PERSISTED, dc.DeliveryState.FINALIZED),
        # Ambiguity is never resolved by automation into a resend.
        (dc.DeliveryState.PROVIDER_AMBIGUOUS, dc.DeliveryState.PROVIDER_SUBMISSION_PENDING),
        # Terminal is terminal.
        (dc.DeliveryState.FINALIZED, dc.DeliveryState.PROVIDER_SUBMISSION_PENDING),
        (dc.DeliveryState.OPERATOR_REQUIRED, dc.DeliveryState.PROVIDER_ACCEPTED),
        # A delivery handed to the EXTERNAL Eco mailing lifecycle may never
        # re-enter this ledger's provider lifecycle. That would be a second
        # message to a driver the existing send log already accounts for.
        (dc.DeliveryState.EXTERNAL_MAILER_HANDOFF,
         dc.DeliveryState.DELIVERY_INTENT_RECORDED),
        (dc.DeliveryState.EXTERNAL_MAILER_HANDOFF,
         dc.DeliveryState.PROVIDER_SUBMISSION_PENDING),
        (dc.DeliveryState.EXTERNAL_MAILER_HANDOFF, dc.DeliveryState.FINALIZED),
        # And a provider-bound delivery may never be reclassified as somebody
        # else's to send.
        (dc.DeliveryState.DELIVERY_INTENT_RECORDED,
         dc.DeliveryState.EXTERNAL_MAILER_HANDOFF),
        (dc.DeliveryState.PROVIDER_ACCEPTED, dc.DeliveryState.EXTERNAL_MAILER_HANDOFF),
        (dc.DeliveryState.PREPARED, dc.DeliveryState.EXTERNAL_MAILER_HANDOFF),
    ]
    for current, target in illegal:
        try:
            dc.assert_legal_transition(current, target)
        except dc.DeliveryContractError:
            continue
        raise AssertionError(f"{current} -> {target} must be refused")

    legal = [
        (dc.DeliveryState.PREPARED, dc.DeliveryState.CAPABILITY_PERSISTED),
        (dc.DeliveryState.PREPARED, dc.DeliveryState.BEARER_RECOVERY_REQUIRED),
        (dc.DeliveryState.BEARER_RECOVERY_REQUIRED, dc.DeliveryState.CAPABILITY_PERSISTED),
        (dc.DeliveryState.PROVIDER_SUBMISSION_PENDING, dc.DeliveryState.PROVIDER_ACCEPTED),
        (dc.DeliveryState.PROVIDER_ACCEPTED, dc.DeliveryState.REMOTE_DELIVERED),
        (dc.DeliveryState.REMOTE_DELIVERED, dc.DeliveryState.FINALIZED),
        # A definite rejection created no message, so a fresh attempt is legal.
        (dc.DeliveryState.PROVIDER_REJECTED, dc.DeliveryState.PROVIDER_SUBMISSION_PENDING),
        # The publication-only path: capability persisted, then handed to the
        # existing Eco mailer. A rerun re-records the same handoff.
        (dc.DeliveryState.CAPABILITY_PERSISTED, dc.DeliveryState.EXTERNAL_MAILER_HANDOFF),
        (dc.DeliveryState.EXTERNAL_MAILER_HANDOFF,
         dc.DeliveryState.EXTERNAL_MAILER_HANDOFF),
        (dc.DeliveryState.EXTERNAL_MAILER_HANDOFF, dc.DeliveryState.OPERATOR_REQUIRED),
    ]
    for current, target in legal:
        dc.assert_legal_transition(current, target)

    # Every state that may hold a bearer is a state that may still have to
    # construct or reconcile a message. Nothing else may hold one.
    # PROVIDER_AMBIGUOUS is in the set because its documented next action is a
    # HUMAN reconciliation, and that human has to be able to establish which
    # link a possibly-sent message carried.
    # EXTERNAL_MAILER_HANDOFF holds one for the same kind of reason: its whole
    # contract is to answer "what is this driver's link for this period?"
    # identically on every rerun of the external mailer, which a row that has
    # forgotten the bearer could only do by rotating the capability.
    check("bearer states are exactly the unresolved delivery states",
          dc.BEARER_REQUIRED_STATES == frozenset({
              dc.DeliveryState.CAPABILITY_PERSISTED,
              dc.DeliveryState.EXTERNAL_MAILER_HANDOFF,
              dc.DeliveryState.DELIVERY_INTENT_RECORDED,
              dc.DeliveryState.PROVIDER_SUBMISSION_PENDING,
              dc.DeliveryState.PROVIDER_ACCEPTED,
              dc.DeliveryState.PROVIDER_AMBIGUOUS,
          }))
    # Every state in which a submission may already have happened carries the
    # immutable submission identity, and binding happens strictly before the
    # first of them.
    check("provider-bound states begin at the intent binding",
          dc.PROVIDER_BOUND_STATES == frozenset({
              dc.DeliveryState.DELIVERY_INTENT_RECORDED,
              dc.DeliveryState.PROVIDER_SUBMISSION_PENDING,
              dc.DeliveryState.PROVIDER_ACCEPTED,
              dc.DeliveryState.PROVIDER_AMBIGUOUS,
              dc.DeliveryState.PROVIDER_REJECTED,
              dc.DeliveryState.REMOTE_DELIVERED,
              dc.DeliveryState.FINALIZED,
          }))
    check("no state may submit before it is bound",
          dc.DeliveryState.CAPABILITY_PERSISTED not in dc.PROVIDER_BOUND_STATES
          and dc.DeliveryState.PREPARED not in dc.PROVIDER_BOUND_STATES)
    # The handoff branch never contacts a provider at all, so it is not merely
    # not-yet-bound: it is unbindable.
    check("a handed-over delivery is never provider bound",
          dc.DeliveryState.EXTERNAL_MAILER_HANDOFF not in dc.PROVIDER_BOUND_STATES)
    check("a handed-over delivery is terminal for automation",
          dc.DeliveryState.EXTERNAL_MAILER_HANDOFF in dc.TERMINAL_STATES
          and dc.safe_next_action(dc.DeliveryState.EXTERNAL_MAILER_HANDOFF)
          == dc.NextAction.EXTERNAL_MAILER_OWNS_DELIVERY)
    PASSED.append("illegal_transitions_are_refused")


# --- 2. identity derivation -----------------------------------------------------


def test_identities_are_deterministic_and_unambiguous() -> None:
    base = identity()
    op = dc.derive_operation_id(base)
    check("operation id matches the Worker contract",
          bool(dc.OPERATION_ID_PATTERN.match(op)), op)
    check("derivation is deterministic", dc.derive_operation_id(identity()) == op)

    # The provider identity depends on the operation and NOTHING else — not on
    # an attempt counter, not on a clock, not on the bearer generation.
    key = dc.derive_provider_idempotency_key(op)
    check("provider identity is stable", dc.derive_provider_idempotency_key(op) == key)

    # Every field of the logical identity changes the operation.
    variants = [
        identity(client_id="22222222-2222-2222-2222-222222222222"),
        identity(identity_key="SYNTHETIC-DRIVER-0002"),
        identity(period_type="monthly", period_start_date=date(2026, 7, 1),
                 period_end_date=date(2026, 8, 1)),
        identity(period_end_date=date(2026, 7, 13)),
        identity(send_scope="test"),
    ]
    ids = {dc.derive_operation_id(v) for v in variants}
    check("each identity field changes the operation", op not in ids and len(ids) == 5)

    # Length prefixing: a value that looks like a boundary shift must not
    # produce the same material as a genuinely different tuple.
    a = identity(identity_key="AB", client_id="11111111-1111-1111-1111-11111111111")
    b = identity(identity_key="B", client_id="11111111-1111-1111-1111-111111111111A")
    check("length-prefixed material cannot collide",
          dc.derive_operation_id(a) != dc.derive_operation_id(b))

    # Domain separation: the same tuple yields different values per purpose.
    check("subject differs from operation", dc.derive_subject_ref(base) != op)

    # The subject reference is opaque: it carries no identity, code or period.
    subject = dc.derive_subject_ref(base)
    for secret in (base.client_id, base.identity_key, "weekly", "2026-07-01"):
        check("subject reference is opaque", secret not in subject, secret)

    # The recipient identity binds the address without containing it.
    rcpt = dc.derive_recipient_identity(base, RECIPIENT)
    check("recipient identity hides the address", RECIPIENT not in rcpt)
    check("recipient identity is case-normalised on the domain",
          dc.derive_recipient_identity(base, "driver.one@EXAMPLE.INVALID") == rcpt)
    check("a different address is a different identity",
          dc.derive_recipient_identity(base, "other@example.invalid") != rcpt)
    # The local part is case-SENSITIVE per RFC 5321; folding it would make two
    # genuinely different mailboxes compare equal.
    check("the local part is not folded",
          dc.derive_recipient_identity(base, "Driver.One@example.invalid") != rcpt)
    PASSED.append("identities_are_deterministic_and_unambiguous")


def test_identity_refuses_malformed_input() -> None:
    for kwargs, why in [
        (dict(period_type="daily"), "unsupported period type"),
        (dict(send_scope="forced"), "unsupported send scope"),
        (dict(period_end_date=date(2026, 7, 1)), "empty period"),
        (dict(identity_key="   "), "blank identity"),
        (dict(identity_key="A\u001FB"), "separator inside a value"),
    ]:
        try:
            dc.derive_operation_id(identity(**kwargs))
        except dc.DeliveryContractError:
            continue
        raise AssertionError(f"must refuse: {why}")

    for bad in ("", "not an address", "a@b", "a b@example.invalid",
                "a@example.invalid, b@example.invalid"):
        try:
            dc.normalise_email(bad)
        except dc.DeliveryContractError:
            continue
        raise AssertionError(f"must refuse recipient {bad!r}")
    PASSED.append("identity_refuses_malformed_input")


# --- 3. the capability link -----------------------------------------------------


def test_capability_link_is_fragment_only() -> None:
    url = de.build_capability_url(BASE_URL, SYNTHETIC_CAPABILITY)
    check("link is exactly base + fragment", url == f"{BASE_URL}#k={SYNTHETIC_CAPABILITY}")
    before_fragment, _, fragment = url.partition("#")
    check("no query string anywhere", "?" not in url)
    check("the capability is only after the fragment marker",
          SYNTHETIC_CAPABILITY not in before_fragment)
    check("the fragment names the documented parameter", fragment.startswith("k="))

    for base, why in [
        ("", "unconfigured base URL"),
        ("http://eco.example.invalid", "plaintext transport"),
        ("https://eco.example.invalid/#k=x", "a base that already has a fragment"),
        ("https://eco.example.invalid/?t=1", "a base with a query string"),
        ("ftp://eco.example.invalid", "a non-HTTP scheme"),
    ]:
        try:
            de.build_capability_url(base, SYNTHETIC_CAPABILITY)
        except de.EmailConstructionError:
            continue
        raise AssertionError(f"must refuse: {why}")

    # Loopback over plain HTTP is the single exception, for local verification.
    de.build_capability_url("http://127.0.0.1:8788", SYNTHETIC_CAPABILITY)

    for bad in ("", "short", SYNTHETIC_CAPABILITY + "=", "A" * 44):
        try:
            de.build_capability_url(BASE_URL, bad)
        except de.EmailConstructionError:
            continue
        raise AssertionError("a malformed capability must not become a link")

    redacted = de.redact_capability_url(url)
    check("redaction removes the value", SYNTHETIC_CAPABILITY not in redacted)
    check("redaction keeps the shape", redacted.startswith(f"{BASE_URL}#k="))
    PASSED.append("capability_link_is_fragment_only")


# --- 4. message construction ----------------------------------------------------


def message_for(period_type: str = "weekly", capability: str = SYNTHETIC_CAPABILITY):
    end = date(2026, 7, 20) if period_type == "weekly" else date(2026, 8, 1)
    context = de.DashboardEmailContext(
        recipient_email=RECIPIENT,
        period_type=period_type,
        period_start_date=date(2026, 7, 1),
        period_end_date=end,
        capability_url=de.build_capability_url(BASE_URL, capability),
        expires_at=datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc),
    )
    return de.build_message(context, message_id="<eco-dash-key@example.invalid>")


def test_message_carries_the_link_and_no_internal_identifier() -> None:
    for period_type in ("weekly", "monthly"):
        message = message_for(period_type)
        bodies = message.html_body + "\n" + message.text_body
        check("the link is present", f"#k={SYNTHETIC_CAPABILITY}" in bodies, period_type)
        check("no query string in the message", "?" not in message.html_body.split("href=")[1][:200]
              if "href=" in message.html_body else True)
        check("the recipient is bound", message.recipient_email == RECIPIENT)
        check("the subject names the programme",
              de.PROGRAMME_NAME.split()[0] in message.subject, message.subject)

        forbidden = [
            dc.derive_subject_ref(identity()),          # subject reference
            dc.derive_operation_id(identity()),         # publication operation id
            identity().client_id,                       # client internal id
            identity().identity_key,                    # driver identity key
            "snapshot_object_key", "capability_id", "payload_digest",
            "bearer", "Authorization", "subject_ref",
        ]
        for value in forbidden:
            check("no internal identifier in the message", value not in bodies, value)

    # No attachment surface exists at all: the message type has no attachment
    # field, so a snapshot copy cannot be added by accident.
    check("the outbound message has no attachment field",
          not hasattr(message_for(), "attachments"))
    PASSED.append("message_carries_the_link_and_no_internal_identifier")


def test_message_refuses_unsafe_construction() -> None:
    context = de.DashboardEmailContext(
        recipient_email=RECIPIENT, period_type="weekly",
        period_start_date=date(2026, 7, 1), period_end_date=date(2026, 7, 20),
        capability_url=f"{BASE_URL}?k={SYNTHETIC_CAPABILITY}")
    try:
        de.build_message(context, message_id="<x@example.invalid>")
    except de.EmailConstructionError:
        pass
    else:
        raise AssertionError("a capability in a query string must never be sent")

    bad_recipient = de.DashboardEmailContext(
        recipient_email="not-an-address", period_type="weekly",
        period_start_date=date(2026, 7, 1), period_end_date=date(2026, 7, 20),
        capability_url=de.build_capability_url(BASE_URL, SYNTHETIC_CAPABILITY))
    try:
        de.build_message(bad_recipient, message_id="<x@example.invalid>")
    except de.EmailConstructionError:
        pass
    else:
        raise AssertionError("a malformed recipient must not produce a message")

    # The Message-ID is derived, stable, and refuses anything that would let a
    # caller inject header structure.
    key = dc.derive_provider_idempotency_key(dc.derive_operation_id(identity()))
    message_id = de.build_message_id(key, domain="example.invalid")
    check("message id is stable",
          de.build_message_id(key, domain="example.invalid") == message_id)
    check("message id is well formed",
          bool(re.match(r"^<[A-Za-z0-9._-]+@[a-z0-9.\-]+>$", message_id)), message_id)
    for bad_key, bad_domain in [("a b", "example.invalid"), ("k", "exa mple"),
                                ("k@x", "example.invalid"), ("k", "")]:
        try:
            de.build_message_id(bad_key, domain=bad_domain)
        except de.EmailConstructionError:
            continue
        raise AssertionError("a malformed message id must be refused")
    PASSED.append("message_refuses_unsafe_construction")


def test_html_escaping_holds() -> None:
    # The capability alphabet is base64url so it cannot carry markup, but the
    # escape is asserted anyway: it is the control that stops a future field
    # from becoming an injection point.
    context = de.DashboardEmailContext(
        recipient_email=RECIPIENT, period_type="weekly",
        period_start_date=date(2026, 7, 1), period_end_date=date(2026, 7, 20),
        capability_url=de.build_capability_url(BASE_URL, SYNTHETIC_CAPABILITY),
        expires_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
        programme_name='Program "Eco" <b>')
    message = de.build_message(context, message_id="<x@example.invalid>")
    check("markup in a field is escaped", "<b>" not in message.html_body)
    check("the escape is visible", "&lt;b&gt;" in message.html_body)
    PASSED.append("html_escaping_holds")


# --- 5. the provider abstraction ------------------------------------------------


#: THE explicit synthetic SMTP configuration. Every SMTP check in this file
#: injects it, so nothing here reads `AUTOMATION_SMTP_*`, a dotenv file or any
#: other host configuration. The values are non-secret placeholders naming the
#: reserved `.invalid` TLD; the password is a marker asserted never to leave
#: the object rather than a credential.
SYNTHETIC_SMTP_PASSWORD = "SYNTHETIC-NOT-A-CREDENTIAL"


def synthetic_smtp_config():
    return SmtpConfig(
        host="smtp.example.invalid",
        port=587,
        username="automation@example.invalid",
        password=SYNTHETIC_SMTP_PASSWORD,
        use_tls=True,
        from_addr="automation@example.invalid",
        timeout_s=30,
    )


def test_smtp_declares_its_real_capabilities() -> None:
    provider = ep.SmtpEmailProvider(config=synthetic_smtp_config())
    check("SMTP is not idempotent", provider.supports_idempotent_submit is False)
    check("SMTP cannot be queried", provider.supports_reconciliation is False)
    verdict = provider.reconcile(idempotency_key="eco-dash-x")
    check("an SMTP lookup answers UNSUPPORTED, never NOT_FOUND",
          verdict.outcome == ep.UNSUPPORTED, verdict.outcome)

    # No live provider is contacted by this suite: the adapter is exercised
    # only through an injected sender AND an injected configuration.
    sent = []

    class _Result:
        message_id = "<smtp-1@example.invalid>"

    def fake_sender(**kwargs):
        sent.append(kwargs)
        return _Result()

    provider = ep.SmtpEmailProvider(config=synthetic_smtp_config(), sender=fake_sender)
    submission = provider.submit(message=message_for(), idempotency_key="eco-dash-x")
    check("acceptance carries the message id",
          submission.outcome == ep.ACCEPTED
          and submission.provider_message_id == "<smtp-1@example.invalid>")
    check("the deterministic message id is transmitted",
          sent[0]["message_id"] == message_for().message_id)
    check("the injected configuration was the one used",
          sent[0]["config"].host == "smtp.example.invalid", str(sent[0]["config"].host))
    PASSED.append("smtp_declares_its_real_capabilities")


def test_the_smtp_checks_never_read_the_ambient_environment() -> None:
    """The isolation this suite claims, asserted rather than assumed.

    The finding this closes: `SmtpEmailProvider()` with no config resolves it
    from `AUTOMATION_SMTP_*`, so a suite that called it was passing because the
    operator's machine happened to be configured and failing on a clean one.
    Infrastructure-independence is a property to prove, not a docstring.
    """
    seen: list[str] = []
    original_getenv = os.getenv

    def watching_getenv(name, *args, **kwargs):
        if str(name).startswith("AUTOMATION_SMTP"):
            seen.append(str(name))
        return original_getenv(name, *args, **kwargs)

    class _Result:
        message_id = "<smtp-env@example.invalid>"

    def refusing_sender(**kwargs):
        # An injected sender is what keeps this check socket-free: the real
        # `send_html_email` would open a connection to whatever host the
        # configuration named.
        check("the injected configuration reached the sender",
              kwargs["config"].host == "smtp.example.invalid")
        return _Result()

    os.environ.pop("AUTOMATION_SMTP_HOST", None)
    os.getenv = watching_getenv  # type: ignore[assignment]
    try:
        provider = ep.SmtpEmailProvider(config=synthetic_smtp_config(),
                                        sender=refusing_sender)
        identity = provider.backend_identity()
        check("the backend scope comes from the injected config",
              "smtp.example.invalid:587" in identity.describe(), identity.describe())
        provider.preflight()
        submission = provider.submit(message=message_for(),
                                     idempotency_key="eco-dash-env")
        check("the injected path produced the acceptance",
              submission.outcome == ep.ACCEPTED)
    finally:
        os.getenv = original_getenv  # type: ignore[assignment]
    check("no AUTOMATION_SMTP_* variable was read", not seen, ",".join(sorted(set(seen))))

    # No password, and nothing derived from one, may appear in the identity the
    # host persists or logs.
    identity = ep.SmtpEmailProvider(config=synthetic_smtp_config()).backend_identity()
    check("no credential material reaches the backend identity",
          SYNTHETIC_SMTP_PASSWORD not in identity.describe()
          and SYNTHETIC_SMTP_PASSWORD not in identity.stable_id())
    PASSED.append("the_smtp_checks_never_read_the_ambient_environment")


def test_fake_provider_models_every_required_outcome() -> None:
    provider = ep.FakeEmailProvider()
    message = message_for()

    accepted = provider.submit(message=message, idempotency_key="k-accept")
    check("accepted", accepted.outcome == ep.ACCEPTED and accepted.provider_message_id)

    # A duplicate under the same key returns the SAME message.
    again = provider.submit(message=message, idempotency_key="k-accept")
    check("duplicate is deduplicated",
          again.provider_message_id == accepted.provider_message_id and again.deduplicated)
    check("exactly one message exists", provider.accepted_count("k-accept") == 1)

    provider.script("k-reject", "reject")
    rejected = provider.submit(message=message, idempotency_key="k-reject")
    check("rejection creates no message",
          rejected.outcome == ep.REJECTED and provider.accepted_count("k-reject") == 0)

    provider.script("k-timeout", "timeout_before_acceptance")
    try:
        provider.submit(message=message, idempotency_key="k-timeout")
    except ep.ProviderTransportLost:
        pass
    else:
        raise AssertionError("a timeout before acceptance must not return a result")
    check("a timeout before acceptance created nothing",
          provider.accepted_count("k-timeout") == 0)
    check("a lookup confirms nothing was created",
          provider.reconcile(idempotency_key="k-timeout").outcome == ep.NOT_FOUND)

    provider.script("k-lost", "accept_then_lose_response")
    try:
        provider.submit(message=message, idempotency_key="k-lost")
    except ep.ProviderTransportLost:
        pass
    else:
        raise AssertionError("a lost response must not look like an answer")
    check("the message DOES exist remotely", provider.accepted_count("k-lost") == 1)
    verdict = provider.reconcile(idempotency_key="k-lost")
    check("reconciliation finds it", verdict.outcome == ep.FOUND and verdict.provider_message_id)

    provider.script("k-unsure", "ambiguous")
    unsure = provider.submit(message=message, idempotency_key="k-unsure")
    check("ambiguity is its own outcome", unsure.outcome == ep.AMBIGUOUS)

    provider.script("k-lookup", "lookup_failure")
    check("a failed lookup is not a NOT_FOUND",
          provider.reconcile(idempotency_key="k-lookup").outcome == ep.LOOKUP_FAILED)

    # A provider WITHOUT idempotency really does duplicate. The fake is honest
    # about it so the state machine's refusal to replay against such a provider
    # is a meaningful assertion rather than a tautology.
    naive = ep.FakeEmailProvider(supports_idempotent_submit=False,
                                 supports_reconciliation=False)
    naive.submit(message=message, idempotency_key="k")
    naive.submit(message=message, idempotency_key="k")
    check("a non-idempotent provider produces two messages", len(naive.messages) == 2)
    PASSED.append("fake_provider_models_every_required_outcome")


# --- 6. secret scrubbing --------------------------------------------------------


def test_scrubbing_removes_known_secrets() -> None:
    text = f"failed while sending {SYNTHETIC_CAPABILITY} to the provider"
    cleaned = dc.scrub_secrets(text, [SYNTHETIC_CAPABILITY])
    check("the secret is gone", SYNTHETIC_CAPABILITY not in cleaned)
    check("the context survives", "failed while sending" in cleaned)
    check("short values are not scrubbed away",
          dc.scrub_secrets("abc", ["ab"]) == "abc")
    PASSED.append("scrubbing_removes_known_secrets")


# --- 7. source-level guards -----------------------------------------------------


def test_job_is_callable_but_not_scheduled() -> None:
    job = (REPO_ROOT / "jobs" / "ecodriving_dashboard"
           / "job_eco_dashboard_publish.py").read_text(encoding="utf-8")
    check("the standard job contract is implemented", "def run(client, run_id" in job)
    check("dry run is the default", 'params.get("mode") or MODE_RENDER_ONLY' in job)
    check("execute mode is explicit", 'MODE_EXECUTE = "execute"' in job)
    # No schedule is registered, created or enabled anywhere in this milestone.
    for forbidden in ("CREATE TABLE schedules", "INSERT INTO schedules",
                      "schedule_enabled", "enable_schedule", "crontab"):
        check("no schedule registration", forbidden not in job, forbidden)

    # The publisher never reaches for the canonical bytes by any route other
    # than the supported host publication interface.
    for module_name in ("publisher.py", "job_eco_dashboard_publish.py"):
        text = (REPO_ROOT / "jobs" / "ecodriving_dashboard" / module_name).read_text("utf-8")
        check("no ad-hoc serialisation", "json.dumps" not in text, module_name)
        check("no direct document building", "build_driver_snapshot" not in text, module_name)

    publisher = (REPO_ROOT / "jobs" / "ecodriving_dashboard"
                 / "publisher.py").read_text(encoding="utf-8")
    # The one ordering that must be visible in the source: durable pending
    # state is written before the provider call, never after it.
    submit_index = publisher.index("services.provider.submit(")
    pending_index = publisher.index("record_submission_pending")
    check("the pending marker precedes the provider call", pending_index < submit_index)
    check("remote DELIVERED is reachable only from recorded acceptance",
          "DeliveryState.PROVIDER_ACCEPTED:\n                step = _mark_remote_delivered"
          in publisher.replace("\r\n", "\n") or
          "elif state == DeliveryState.PROVIDER_ACCEPTED:" in publisher)
    PASSED.append("job_is_callable_but_not_scheduled")


def test_render_only_mode_touches_nothing_remote() -> None:
    """The dry run proves construction, and proves it printed nothing secret."""
    from jobs.ecodriving_dashboard import job_eco_dashboard_publish as job

    logged: list[dict] = []

    class Client:
        def log(self, level, source, origin, message, run_id=None, context=None):
            logged.append({"message": message, "context": dict(context or {})})

    ident = identity()
    result = {
        "operation_id": dc.derive_operation_id(ident),
        "recipient_identity": dc.derive_recipient_identity(ident, RECIPIENT),
    }
    out = job._render_only(
        Client(), "run-1", result, ident,
        recipient_email=RECIPIENT, dashboard_base_url=BASE_URL,
        message_id_domain="example.invalid",
        expires_at=datetime(2026, 8, 3, tzinfo=timezone.utc))

    check("the message was constructed", out["message_prepared"] is True)
    check("the mode is explicit", out["invocation"] == "RENDER_ONLY")
    check("nothing has started", out["state"] == "NOT_STARTED"
          and out["next_action"] == "PUBLISH")
    check("the link is redacted", out["capability_url_redacted"].endswith("[REDACTED]"))
    check("the placeholder is not printed",
          job._PLACEHOLDER_CAPABILITY not in json_dump(out))
    check("the provider identity is the stable one",
          out["provider_idempotency_key"]
          == dc.derive_provider_idempotency_key(out["operation_id"]))
    check("something was logged", len(logged) == 1)
    check("the log carries no capability",
          job._PLACEHOLDER_CAPABILITY not in json_dump(logged))
    check("the log names the operation and the recipient identity",
          logged[0]["context"]["operation_id"] == out["operation_id"]
          and RECIPIENT not in json_dump(logged))
    PASSED.append("render_only_mode_touches_nothing_remote")


def json_dump(value) -> str:
    import json

    return json.dumps(value, default=str, ensure_ascii=False)


def test_no_live_provider_or_deployment_is_referenced() -> None:
    for module_name in ("publisher.py", "delivery_ledger.py", "delivery_contract.py",
                        "dashboard_email.py", "secure_delivery_client.py"):
        text = (REPO_ROOT / "jobs" / "ecodriving_dashboard" / module_name).read_text("utf-8")
        for forbidden in ("api.sendgrid.com", "api.postmarkapp.com", "email-smtp.",
                          "wrangler", "cloudflare.com/client/v4"):
            check("no live provider or deployment endpoint", forbidden not in text,
                  f"{module_name}: {forbidden}")
    PASSED.append("no_live_provider_or_deployment_is_referenced")


def main() -> int:
    tests = [
        test_every_state_has_exactly_one_safe_next_action,
        test_illegal_transitions_are_refused,
        test_identities_are_deterministic_and_unambiguous,
        test_identity_refuses_malformed_input,
        test_capability_link_is_fragment_only,
        test_message_carries_the_link_and_no_internal_identifier,
        test_message_refuses_unsafe_construction,
        test_html_escaping_holds,
        test_smtp_declares_its_real_capabilities,
        test_the_smtp_checks_never_read_the_ambient_environment,
        test_fake_provider_models_every_required_outcome,
        test_scrubbing_removes_known_secrets,
        test_job_is_callable_but_not_scheduled,
        test_render_only_mode_touches_nothing_remote,
        test_no_live_provider_or_deployment_is_referenced,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"\n{len(PASSED)} checks passed — host delivery contract, message and provider")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
