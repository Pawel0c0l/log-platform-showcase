"""Provider-neutral e-mail boundary for the Driver Eco Dashboard link delivery.

WHY A BOUNDARY AT ALL, GIVEN THAT THIS REPOSITORY ALREADY SENDS E-MAIL

`jobs/common/emailer.py` and `jobs/ecodriving_person/email_delivery.py` are
SMTP senders, and SMTP is reused here (`SmtpEmailProvider` is a thin adapter
over the existing sender, not a second implementation). What SMTP does NOT
provide is the property crash-matrix stages 8–9 need:

    the host died between "the provider accepted the message" and
    "the host recorded that acceptance"

Resolving that safely requires the provider to (a) deduplicate a resubmission
carrying the same idempotency identity, or (b) let the host ask what happened
to that identity. `smtplib.send_message` offers neither: a second call is a
second message. So the abstraction declares both capabilities explicitly, and
the host state machine reads them rather than assuming them.

**No provider guarantee is invented here.** A provider that cannot deduplicate
and cannot be queried produces an explicit `PROVIDER_AMBIGUOUS` state that a
human resolves. That is the honest answer, and it is strictly better than the
alternative the review named: a blind resend that mails a driver twice.

WHAT A FUTURE LIVE PROVIDER ADAPTER MUST IMPLEMENT

  * `submit()` must transmit `idempotency_key` to the provider in whatever
    field that provider uses for request deduplication, and must return
    `ACCEPTED` only when the provider has taken responsibility for the message
    — never merely because an HTTP call was made;
  * `REJECTED` must mean the provider definitively created no message;
  * anything else — a timeout, a 5xx, a dropped connection — must be
    `AMBIGUOUS`, never `REJECTED`;
  * `supports_idempotent_submit` may be `True` only if resubmitting the same
    key provably yields the same single message;
  * `reconcile()` may return `FOUND` only from a provider-side lookup of that
    key, not from local state.

This milestone calls no live provider. `FakeEmailProvider` is the deterministic
one used everywhere in verification.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Protocol

from jobs.ecodriving_dashboard.delivery_contract import derive_provider_backend_id

# --- submission outcomes --------------------------------------------------------

ACCEPTED = "ACCEPTED"
REJECTED = "REJECTED"
AMBIGUOUS = "AMBIGUOUS"

# --- reconciliation outcomes ----------------------------------------------------

FOUND = "FOUND"
NOT_FOUND = "NOT_FOUND"
UNSUPPORTED = "UNSUPPORTED"
LOOKUP_FAILED = "LOOKUP_FAILED"


class ProviderTransportLost(RuntimeError):
    """The submission left the host and no answer came back.

    Modelled as an exception rather than a return value because it is exactly
    the case where the provider may already hold the message: there is no
    result to report, only an unresolved question.
    """


class ProviderConfigurationError(RuntimeError):
    """A DEFINITE local failure, established before any network submission.

    The distinction from `ProviderTransportLost` is the whole reason this type
    exists. A missing SMTP host is not an unresolved question: no socket was
    opened, no message can exist, and treating it as ambiguity would park a
    delivery in a state that needs a human to rule out a message that provably
    was never sent.

    Carries a stable `code` and never any credential material.
    """

    def __init__(self, code: str, message: str = "") -> None:
        self.code = str(code)
        super().__init__(f"{code}: {message}".strip(": "))


@dataclass(frozen=True)
class ProviderBackendIdentity:
    """The non-secret scope a provider submission is answerable within.

    Three fields, because three things independently decide whether an
    idempotency key can identify an already-accepted message:

    * `provider_type`   — a key means nothing across implementations;
    * `account_scope`   — the tenant/mailbox/account the message belongs to;
    * `endpoint_scope`  — the host/environment (staging and production of one
      provider are different message stores).

    NO CREDENTIAL IS AN INPUT. A rotated password is the same account and must
    stay the same backend; a changed account or endpoint is a different one.
    `stable_id()` is what gets persisted, and it is a digest of non-secret
    scope material only.
    """

    provider_type: str
    account_scope: str
    endpoint_scope: str

    def stable_id(self) -> str:
        return derive_provider_backend_id(
            self.provider_type, self.account_scope, self.endpoint_scope)

    def describe(self) -> str:
        """Operator-facing, non-secret. Safe to log."""
        return f"{self.provider_type}/{self.account_scope}@{self.endpoint_scope}"


@dataclass(frozen=True)
class OutboundMessage:
    """One recipient-bound message, ready to transmit.

    `html_body`/`text_body` contain the capability link in its fragment form.
    `__repr__` is suppressed so the bodies — and therefore the raw bearer —
    cannot reach a traceback or a test failure message.
    """

    recipient_email: str
    subject: str
    html_body: str
    text_body: str
    message_id: str
    headers: Mapping[str, str] = field(default_factory=dict)

    def __repr__(self) -> str:
        return f"OutboundMessage(message_id={self.message_id!r}, subject={self.subject!r})"


@dataclass(frozen=True)
class ProviderSubmission:
    outcome: str
    provider_message_id: Optional[str] = None
    failure_code: Optional[str] = None
    detail: str = ""
    #: Only meaningful for `REJECTED`: whether resubmitting the same message
    #: under the same idempotency key could plausibly succeed later.
    retryable: bool = False
    #: True when the provider recognised the idempotency key and returned the
    #: message it already holds instead of creating a second one.
    deduplicated: bool = False


@dataclass(frozen=True)
class ProviderReconciliation:
    outcome: str
    provider_message_id: Optional[str] = None
    detail: str = ""


class EmailProvider(Protocol):
    """What the delivery state machine is allowed to assume about a provider."""

    name: str
    #: Resubmitting the same idempotency key yields ONE message, not two.
    supports_idempotent_submit: bool
    #: The provider can be asked what happened to an idempotency key.
    supports_reconciliation: bool

    def backend_identity(self) -> ProviderBackendIdentity:
        """The scope this adapter would submit into, resolved from config.

        Raises `ProviderConfigurationError` when the configuration cannot name
        a backend at all — which is a definite pre-submit failure, never an
        ambiguous one.
        """
        ...

    def preflight(self) -> ProviderBackendIdentity:
        """Establish that a submission COULD be attempted. No remote effect.

        Contacts nothing. It exists so a missing or invalid configuration is
        discovered before the host performs any external mutation whose only
        purpose was an eventual delivery.
        """
        ...

    def submit(self, *, message: OutboundMessage,
               idempotency_key: str) -> ProviderSubmission:
        ...

    def reconcile(self, *, idempotency_key: str) -> ProviderReconciliation:
        ...


# --- SMTP adapter over the existing repository sender ---------------------------


class SmtpEmailProvider:
    """Adapter over `jobs/common/emailer.send_html_email`.

    It declares the truth about SMTP rather than a convenient fiction:

    * `supports_idempotent_submit = False` — a second `send_message` is a
      second message. The deterministic RFC 5322 `Message-ID` derived from the
      operation makes a duplicate IDENTIFIABLE afterwards; it does not prevent
      one, and the state machine must not treat it as if it did;
    * `supports_reconciliation = False` — nothing in SMTP answers "did you
      accept message X". A mailbox-search reconciliation adapter is a possible
      future addition (this repository already has IMAP search code for a
      different purpose) and is deliberately not wired in here on the strength
      of an untested assumption.

    Consequence, stated plainly: under this provider a lost response after
    submission ends in `PROVIDER_AMBIGUOUS` and needs a human. That is why a
    provider with real idempotency is a live-integration requirement rather
    than a nicety.
    """

    name = "smtp"
    supports_idempotent_submit = False
    supports_reconciliation = False

    def __init__(self, *, config=None, sender: Optional[Callable[..., Any]] = None) -> None:
        self._config = config
        self._sender = sender

    # -- configuration, resolved without contacting anything ----------------

    def _resolve_config(self):
        """The effective `SmtpConfig`, or a DEFINITE configuration failure.

        `load_smtp_config_from_env()` raises a bare `RuntimeError` for a
        missing host. Left alone that surfaces at SEND time, inside the call
        the host has already committed `PROVIDER_SUBMISSION_PENDING` for —
        which is precisely how a configuration mistake used to be recorded as
        "a message may exist". It is translated here into the definite
        pre-submit failure it actually is.
        """
        if self._config is not None:
            return self._config
        from jobs.common.emailer import load_smtp_config_from_env

        try:
            return load_smtp_config_from_env()
        except Exception as error:
            raise ProviderConfigurationError(
                "SMTP_CONFIGURATION_MISSING", type(error).__name__) from None

    def backend_identity(self) -> ProviderBackendIdentity:
        """`smtp` + the ACCOUNT and ENDPOINT the configuration names.

        The account scope is the authenticated username when there is one and
        the envelope sender otherwise, because that is what decides whose
        mailbox a message belongs to. The password is not an input: rotating it
        must not make an in-flight delivery look like a different backend.
        """
        config = self._resolve_config()
        host = str(getattr(config, "host", "") or "").strip().lower()
        port = getattr(config, "port", None)
        from_addr = str(getattr(config, "from_addr", "") or "").strip()
        username = str(getattr(config, "username", "") or "").strip()
        if not host:
            raise ProviderConfigurationError(
                "SMTP_HOST_NOT_CONFIGURED", "the SMTP host is not configured")
        try:
            port_number = int(port)
        except (TypeError, ValueError):
            raise ProviderConfigurationError(
                "SMTP_PORT_INVALID", "the SMTP port is not a number") from None
        if not (0 < port_number < 65536):
            raise ProviderConfigurationError(
                "SMTP_PORT_INVALID", "the SMTP port is out of range")
        if not from_addr:
            raise ProviderConfigurationError(
                "SMTP_SENDER_NOT_CONFIGURED", "no envelope sender is configured")
        endpoint = f"{host}:{port_number}"
        return ProviderBackendIdentity(
            provider_type=self.name,
            account_scope=f"{username or from_addr}",
            endpoint_scope=endpoint,
        )

    def preflight(self) -> ProviderBackendIdentity:
        """Resolve the configuration and stop. No socket is opened.

        SMTP offers no non-effecting validation call, and inventing one by
        connecting would be a remote effect performed during a check whose
        whole purpose is to happen before remote effects.
        """
        return self.backend_identity()

    def submit(self, *, message: OutboundMessage,
               idempotency_key: str) -> ProviderSubmission:
        import smtplib

        from jobs.common.emailer import send_html_email

        # Re-established immediately before the call, so a configuration that
        # became unusable between preflight and submission is still a definite
        # local failure rather than an attempted send.
        config = self._resolve_config()
        sender = self._sender or send_html_email
        try:
            result = sender(
                to_addrs=message.recipient_email,
                subject=message.subject,
                html_body=message.html_body,
                text_body=message.text_body,
                config=config,
                message_id=message.message_id,
            )
        except smtplib.SMTPResponseException as error:
            # A server response IS an answer: the code says whether the message
            # was refused. 4xx is transient, 5xx is permanent; neither created
            # a message.
            permanent = 500 <= int(getattr(error, "smtp_code", 500)) < 600
            return ProviderSubmission(
                outcome=REJECTED,
                failure_code=f"SMTP_{getattr(error, 'smtp_code', 'ERROR')}",
                detail="the SMTP server refused the message",
                retryable=not permanent,
            )
        except (smtplib.SMTPServerDisconnected, TimeoutError, OSError) as error:
            # No answer. The message may or may not have been queued, so this
            # is never reported as a rejection.
            raise ProviderTransportLost(type(error).__name__) from None
        return ProviderSubmission(outcome=ACCEPTED,
                                  provider_message_id=str(result.message_id))

    def reconcile(self, *, idempotency_key: str) -> ProviderReconciliation:
        return ProviderReconciliation(
            outcome=UNSUPPORTED,
            detail="SMTP cannot be asked whether it accepted a given message",
        )


# --- deterministic local provider -----------------------------------------------


class FakeEmailProvider:
    """A deterministic local provider. Never touches a network.

    It models a modern HTTP mail API: submissions are keyed by an idempotency
    identity, and a resubmission of a known key returns the SAME message rather
    than creating a second one. Behaviour is scripted per key so every
    scenario the crash matrix needs is reproducible rather than hoped for:

        accept                          plain acceptance
        reject / reject_transient       definite refusal, no message created
        timeout_before_acceptance       no message created, no answer
        accept_then_lose_response       message CREATED, then no answer
        ambiguous                       provider itself cannot say

    `accept_then_lose_response` is the crash-matrix stage 9 case, and it is the
    reason `messages` is keyed by idempotency identity: the test asserts that a
    replay produces exactly one message, which is only meaningful if the fake
    would happily have produced two.
    """

    name = "fake"

    def __init__(self, *, supports_idempotent_submit: bool = True,
                 supports_reconciliation: bool = True,
                 account_scope: str = "default-account",
                 endpoint_scope: str = "local-fake",
                 store: Optional[dict] = None,
                 configuration_error: Optional[str] = None) -> None:
        self.supports_idempotent_submit = bool(supports_idempotent_submit)
        self.supports_reconciliation = bool(supports_reconciliation)
        #: The backend/account scope this instance models. Two instances that
        #: share it are the SAME backend across a restart; two that differ are
        #: genuinely different destinations, which is the distinction the
        #: publisher must refuse to cross under one idempotency key.
        self.account_scope = str(account_scope)
        self.endpoint_scope = str(endpoint_scope)
        #: Set to make `preflight()`/`backend_identity()` fail definitely, the
        #: way a missing provider configuration does.
        self.configuration_error = configuration_error
        self.preflights = 0
        #: idempotency key -> accepted message. THE dedupe store. Passing an
        #: explicit `store` is how a test models "the same backend, seen by a
        #: restarted process": the message survives the adapter object.
        self.messages: dict[str, dict] = {} if store is None else store
        #: Every submission call, including the ones that created nothing.
        self.submissions: list[dict] = []
        self.reconciliations: list[str] = []
        self._behaviour: dict[str, str] = {}
        self._default = "accept"
        self._counter = 0
        self._lock = threading.Lock()

    # -- scripting ----------------------------------------------------------

    def script(self, idempotency_key: str, behaviour: str) -> "FakeEmailProvider":
        self._behaviour[idempotency_key] = behaviour
        return self

    def set_default(self, behaviour: str) -> "FakeEmailProvider":
        self._default = behaviour
        return self

    def behaviour_for(self, key: str) -> str:
        return self._behaviour.get(key, self._default)

    def accepted_count(self, idempotency_key: str) -> int:
        return 1 if idempotency_key in self.messages else 0

    # -- provider contract --------------------------------------------------

    def backend_identity(self) -> ProviderBackendIdentity:
        if self.configuration_error:
            raise ProviderConfigurationError(str(self.configuration_error),
                                             "the fake provider is misconfigured")
        return ProviderBackendIdentity(provider_type=self.name,
                                       account_scope=self.account_scope,
                                       endpoint_scope=self.endpoint_scope)

    def preflight(self) -> ProviderBackendIdentity:
        self.preflights += 1
        return self.backend_identity()

    def submit(self, *, message: OutboundMessage,
               idempotency_key: str) -> ProviderSubmission:
        with self._lock:
            self.submissions.append({
                "idempotency_key": idempotency_key,
                "recipient_email": message.recipient_email,
                "message_id": message.message_id,
                "subject": message.subject,
                # Kept so tests can prove WHICH capability generation a
                # constructed message carried. Never printed.
                "html_body": message.html_body,
                "text_body": message.text_body,
            })
            existing = self.messages.get(idempotency_key)
            if existing is not None:
                if not self.supports_idempotent_submit:
                    # An honest non-idempotent provider: the same key produces
                    # a SECOND message. The host must therefore never resubmit
                    # blindly to one of these, and the tests assert it does not
                    # by asserting this branch is unreachable in practice.
                    self._counter += 1
                    duplicate = dict(existing)
                    duplicate["provider_message_id"] = f"dup-{self._counter}"
                    self.messages[f"{idempotency_key}#dup{self._counter}"] = duplicate
                    return ProviderSubmission(outcome=ACCEPTED,
                                              provider_message_id=duplicate["provider_message_id"])
                return ProviderSubmission(outcome=ACCEPTED,
                                          provider_message_id=existing["provider_message_id"],
                                          deduplicated=True)

            behaviour = self.behaviour_for(idempotency_key)
            if behaviour == "reject":
                return ProviderSubmission(outcome=REJECTED, failure_code="RECIPIENT_REFUSED",
                                          detail="the provider refused the recipient",
                                          retryable=False)
            if behaviour == "reject_transient":
                return ProviderSubmission(outcome=REJECTED, failure_code="RATE_LIMITED",
                                          detail="the provider throttled the request",
                                          retryable=True)
            if behaviour == "timeout_before_acceptance":
                # Nothing is stored: the provider never took the message.
                raise ProviderTransportLost("TIMEOUT_BEFORE_ACCEPTANCE")
            if behaviour == "ambiguous":
                return ProviderSubmission(outcome=AMBIGUOUS, failure_code="PROVIDER_UNSURE",
                                          detail="the provider could not confirm acceptance")

            self._counter += 1
            record = {
                "provider_message_id": f"fake-msg-{self._counter:04d}",
                "idempotency_key": idempotency_key,
                "recipient_email": message.recipient_email,
                "message_id": message.message_id,
                "subject": message.subject,
                "html_body": message.html_body,
                "text_body": message.text_body,
            }
            self.messages[idempotency_key] = record
            if behaviour == "accept_then_lose_response":
                # THE stage-9 window: the message exists remotely and the host
                # will never learn it from this call.
                raise ProviderTransportLost("ACCEPTED_RESPONSE_LOST")
            return ProviderSubmission(outcome=ACCEPTED,
                                      provider_message_id=record["provider_message_id"])

    def reconcile(self, *, idempotency_key: str) -> ProviderReconciliation:
        with self._lock:
            self.reconciliations.append(idempotency_key)
            if not self.supports_reconciliation:
                return ProviderReconciliation(
                    outcome=UNSUPPORTED,
                    detail="this provider cannot be asked about an idempotency key")
            if self.behaviour_for(idempotency_key) == "lookup_failure":
                return ProviderReconciliation(outcome=LOOKUP_FAILED,
                                              detail="the provider lookup failed")
            record = self.messages.get(idempotency_key)
            if record is None:
                return ProviderReconciliation(outcome=NOT_FOUND)
            return ProviderReconciliation(outcome=FOUND,
                                          provider_message_id=record["provider_message_id"])
