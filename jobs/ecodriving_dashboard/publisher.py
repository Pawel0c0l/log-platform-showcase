"""Driver Eco Dashboard V1 — host publication/delivery state machine.

`advance_delivery()` takes one logical dashboard delivery as far as it can
safely go, one durable step at a time, and stops. It is deliberately
re-entrant: every invocation reads the durable state, performs THE one safe
next action for it, and returns. A scheduler that fires it again — after a
success, after a crash, or concurrently with another invocation — is a
supported input, not a hazard.

THE ORDERING THAT MATTERS

    provider/config preflight             (before ANY external effect)
      -> canonical snapshot bytes
      -> durable local operation          (before any remote effect)
      -> secure-delivery publication
      -> raw bearer durably persisted     (before any provider attempt)
      -> remote delivery intent
      -> immutable submission identity persisted: idempotency key + provider
         BACKEND scope + fingerprint of the exact message  (BEFORE any submit)
      -> durable "a submission may have happened"   (BEFORE the provider call)
      -> provider acceptance established or reconciled
      -> host records acceptance
      -> remote DELIVERED
      -> raw bearer destroyed

Four of those arrows are the whole design:

* preflight comes FIRST, so a missing provider configuration costs nothing —
  no publication, no capability, no submission marker. A failure established
  before any socket exists is definite, and definite failures must never be
  recorded as "a message may exist";
* the bearer is persisted BEFORE anything may attempt a send, so a host that
  cannot construct the driver's link never gets as far as trying;
* the submission identity is bound BEFORE the first submit. An idempotency key
  alone is not idempotency: it means "the same message" only relative to the
  backend that has heard of it and the content it named. Both are pinned here,
  so a restart onto a different provider account, or with a different dashboard
  URL or template, is refused before a provider is contacted rather than
  producing a second delivery under one key;
* `PROVIDER_SUBMISSION_PENDING` is committed BEFORE the provider call, so a
  process that dies during the call wakes up knowing a message may exist. From
  that state the only permitted moves are reconciliation and — strictly when
  the provider deduplicates by idempotency key — a replay under that same key.
  There is no path from "I do not know" to "send again anyway".

OWNERSHIP IS FENCED IN BOTH DIRECTIONS

Every ledger write re-asserts `(state, lease owner, lease unexpired)` in the
statement that writes, so a stale invocation cannot record an outcome; and
`_fence()` re-asserts the same thing immediately before each remote effect is
initiated, so a stale invocation does not cause one either. Neither claims a
database lease can be atomic with an HTTP call — the residual window is covered
by the ordering above, not by locking.

WHAT IS NEVER DONE HERE

* the remote operation is never marked `DELIVERED` because a request was
  attempted; only recorded provider acceptance unlocks that transition;
* a lost bearer is never answered with a re-publish — only with the explicit
  recovery operation;
* a revoked predecessor is never selected: `record_capability` overwrites the
  capability identity in one statement, so a restart reads the replacement or
  nothing;
* no raw bearer is logged, returned in a summary, or written to a diagnostic
  column.
"""

from __future__ import annotations

import os
import socket
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional

from jobs.ecodriving_dashboard import secure_delivery_client as sdc
from jobs.ecodriving_dashboard.dashboard_email import (
    DashboardEmailContext,
    EmailConstructionError,
    build_capability_url,
    build_message,
    build_message_id,
)
from jobs.ecodriving_dashboard.delivery_contract import (
    DeliveryContractError,
    DeliveryIdentity,
    DeliveryState,
    NextAction,
    derive_message_fingerprint,
    derive_operation_id,
    derive_provider_idempotency_key,
    derive_recipient_identity,
    derive_subject_ref,
    scrub_secrets,
)
from jobs.ecodriving_dashboard.delivery_ledger import (
    DeliveryLedger,
    DeliveryRecord,
    LedgerConflict,
)
from jobs.ecodriving_dashboard import email_provider as ep

#: A bound on durable steps per invocation. Each step is a committed state
#: change, so this is not a spin guard so much as a promise that one invocation
#: terminates.
MAX_STEPS = 12
DEFAULT_MAX_PROVIDER_ATTEMPTS = 3

#: Outcome vocabulary of one invocation. Distinct from the durable state: it
#: says what THIS call did, not where the delivery now is.
class INVOCATION:
    COMPLETED = "COMPLETED"
    IN_PROGRESS = "IN_PROGRESS"
    #: Another invocation holds the durable lease. Doing nothing is correct.
    NOT_OWNED = "NOT_OWNED"
    RETRY_LATER = "RETRY_LATER"
    OPERATOR_REQUIRED = "OPERATOR_REQUIRED"
    CONFLICT = "CONFLICT"
    #: A prerequisite of the whole execution is missing. Distinguished from
    #: every other outcome because it is established BEFORE any external
    #: effect: nothing was published, no capability was issued, no provider was
    #: contacted, and no durable state was touched.
    PREFLIGHT_FAILED = "PREFLIGHT_FAILED"


class ConflictCode:
    """Refusals this module raises before contacting a provider."""

    #: The configured provider backend/account scope is not the one this
    #: delivery was bound to. Submitting would ask a backend that has never
    #: heard of the idempotency key, which is how one logical delivery becomes
    #: two messages.
    PROVIDER_BACKEND_CONFLICT = "PROVIDER_BACKEND_CONFLICT"
    #: The message that would be sent is not the message the bound idempotency
    #: key names.
    MESSAGE_CONFLICT = "MESSAGE_CONFLICT"
    #: Provider configuration is missing or unusable. A DEFINITE local failure.
    PROVIDER_CONFIGURATION = "PROVIDER_CONFIGURATION_FAILURE"
    #: A prerequisite of execute mode is not configured.
    PREFLIGHT = "PREFLIGHT_FAILED"
    #: The delivery was handed to an EXTERNAL mailing lifecycle, which owns its
    #: send accounting. The provider path must not adopt it under any state.
    EXTERNAL_MAILER_OWNS_DELIVERY = "EXTERNAL_MAILER_OWNS_DELIVERY"
    #: The delivery's capability is expired (or too close to expiry to be worth
    #: e-mailing) and a rotation through the explicit recovery operation did not
    #: produce a usable replacement. No link is returned: an Eco e-mail carrying
    #: a link that answers 410 is worse than one this run refused to send.
    CAPABILITY_EXPIRED = "CAPABILITY_EXPIRED"


#: How much of a capability's lifetime must remain for it to be worth putting
#: in an e-mail. A grant that is merely "not expired yet" is not a usable link:
#: the message still has to be composed, queued, sent and — the part nobody
#: controls — opened. This margin is what makes "the returned URL is never
#: already expired" a statement about the driver's experience rather than about
#: the microsecond the row was read.
DEFAULT_CAPABILITY_MIN_REMAINING_SECONDS = 3600


@dataclass(frozen=True)
class PublisherConfig:
    dashboard_base_url: str
    message_id_domain: str
    lease_seconds: int = 300
    max_provider_attempts: int = DEFAULT_MAX_PROVIDER_ATTEMPTS
    capability_min_remaining_seconds: int = DEFAULT_CAPABILITY_MIN_REMAINING_SECONDS


@dataclass
class PublisherServices:
    ledger: DeliveryLedger
    client: sdc.SecureDeliveryClient
    config: PublisherConfig
    #: Optional ON PURPOSE. The publication-only path (`ensure_capability`)
    #: exists precisely so an EXTERNAL mailing lifecycle can obtain a dashboard
    #: link without a second e-mail sender existing at all, so wiring it with no
    #: provider is the supported configuration rather than an omission. Every
    #: provider-facing step goes through `preflight()`, which refuses to run
    #: without one.
    provider: Any = None
    #: `(level, event, context)`. Context values are scrubbed before emission.
    logger: Optional[Callable[[str, str, Mapping[str, Any]], None]] = None
    events: list = field(default_factory=list)

    def log(self, level: str, event: str, context: Optional[Mapping[str, Any]] = None) -> None:
        # Every secret the ledger knows about — the machine credential and
        # every raw bearer this process has held — is removed before a value
        # can reach a log sink or the captured event list.
        secrets = [self.client.credential, *self.ledger.secrets]
        payload = {}
        for key, value in dict(context or {}).items():
            payload[key] = scrub_secrets(value, secrets) if isinstance(value, str) else value
        self.events.append({"level": level, "event": event, "context": payload})
        if self.logger is not None:
            self.logger(level, event, payload)


@dataclass(frozen=True)
class DeliveryResult:
    invocation: str
    state: str
    next_action: str
    steps: int
    record: Optional[DeliveryRecord] = None
    detail: str = ""
    conflict_code: Optional[str] = None

    def summary(self) -> dict:
        base = {
            "invocation": self.invocation,
            "state": self.state,
            "next_action": self.next_action,
            "steps": self.steps,
            "detail": self.detail,
            "conflict_code": self.conflict_code,
        }
        if self.record is not None:
            base["delivery"] = self.record.public_summary()
        return base


def owner_token(run_id: Optional[str] = None) -> str:
    """Durable lease owner: host + process + this invocation.

    Deliberately unique per invocation, because the lease answers "is somebody
    else working on this right now", and a shared value would let two
    invocations believe they both held it.
    """
    return f"{socket.gethostname()}/{os.getpid()}/{run_id or uuid.uuid4().hex}"


def prepare_delivery(
    ledger: DeliveryLedger,
    identity: DeliveryIdentity,
    *,
    payload_digest: str,
    recipient_email: str,
    external_mailer: Optional[str] = None,
    run_id: Optional[str] = None,
) -> tuple[DeliveryRecord, bool]:
    """Satisfy the pre-publication persistence invariant, and nothing more.

    Separated from `advance_delivery` so the ordering is testable on its own:
    after this returns, the operation id, the subject reference, the exact
    canonical payload digest, the recipient binding AND the send-accounting
    ownership are durable, and no remote call has been made.

    `external_mailer` names the external mailing lifecycle that owns the send,
    or is `None` for a delivery this ledger's own provider lifecycle owns. It is
    part of the row from its first committed byte precisely so there is no
    reachable state in which an externally-owned delivery exists without saying
    so — see `DeliveryLedger.ensure_operation`.
    """
    operation_id = derive_operation_id(identity)
    subject_ref = derive_subject_ref(identity)
    recipient_identity = derive_recipient_identity(identity, recipient_email)
    return ledger.ensure_operation(
        identity,
        operation_id=operation_id,
        subject_ref=subject_ref,
        payload_digest=payload_digest,
        recipient_email=recipient_email,
        recipient_identity=recipient_identity,
        external_mailer=external_mailer,
        run_id=run_id,
    )


#: A capability-shaped placeholder, used only to prove that a base URL can
#: carry a link at all. It is not a capability and no grant is ever minted with
#: it; nothing built from it is transmitted anywhere.
_PREFLIGHT_CAPABILITY = "0" * 43


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    code: str = ""
    detail: str = ""
    backend: Optional[Any] = None


def publication_preflight(services: PublisherServices) -> PreflightResult:
    """What ANY external effect needs, provider or no provider.

    Split out of `preflight()` so the publication-only path can establish its
    own prerequisites without asserting things it will never use. It checks the
    two facts that decide whether a capability could be turned into a usable
    driver link at all — the machine credential exists, and the dashboard base
    URL can carry one — and it contacts nothing.

    A missing e-mail provider is deliberately NOT a failure here: on the
    integrated path the provider is the existing Eco Driving SMTP sender, which
    this module neither owns nor is allowed to speak to.
    """
    if not getattr(services.client, "credential", ""):  # pragma: no cover - ctor guards
        return PreflightResult(False, "PUBLISHER_CREDENTIAL_MISSING",
                               "no publisher machine credential is configured")
    try:
        build_capability_url(services.config.dashboard_base_url, _PREFLIGHT_CAPABILITY)
    except EmailConstructionError as error:
        return PreflightResult(False, str(error), "the dashboard base URL is unusable")
    return PreflightResult(True)


def preflight(services: PublisherServices) -> PreflightResult:
    """Everything execute mode must have, established before ANY remote effect.

    WHY THIS EXISTS AT ALL. Provider configuration used to be discovered inside
    `submit()` — which is to say, after the snapshot had been published, after a
    capability had been issued, and after `PROVIDER_SUBMISSION_PENDING` had been
    committed. A missing SMTP host therefore left a delivery parked in a state
    that means "a message may exist", for a provider that had never opened a
    socket. That is a definite local failure recorded as an unresolved one, and
    it is exactly backwards: the cheapest thing to establish was checked last,
    after the expensive irreversible things.

    WHAT IT CHECKS, IN ORDER OF WHAT IT WOULD OTHERWISE COST TO DISCOVER LATE:

    * the machine credential exists (the `SecureDeliveryClient` constructor
      refuses to exist without one, so reaching here already proves it);
    * the dashboard base URL can carry a capability link;
    * the `Message-ID` domain is usable;
    * the provider adapter is configured, and names a stable backend scope.

    WHAT IT DELIBERATELY DOES NOT DO. It contacts nothing — not the publisher
    API, and not the provider. SMTP offers no non-effecting validation call, and
    opening a connection to prove that one could be opened would be a remote
    effect performed by the check whose entire purpose is to precede remote
    effects. The publisher ENDPOINT is validated where it can be validated
    without a credential in scope at all: in the transport constructor, which
    runs before a client holding the credential exists.

    Provider CAPABILITIES are read, not required. A provider that can neither
    deduplicate nor be queried — SMTP — is a supported configuration; it simply
    means an ambiguous submission ends with a human rather than a retry.
    """
    base = publication_preflight(services)
    if not base.ok:
        return base
    try:
        build_message_id("eco-dash-preflight", domain=services.config.message_id_domain)
    except EmailConstructionError as error:
        return PreflightResult(False, str(error), "the Message-ID domain is unusable")

    provider = services.provider
    if provider is None:
        return PreflightResult(False, "PROVIDER_NOT_CONFIGURED",
                               "this execution has no e-mail provider adapter")
    try:
        backend = provider.preflight()
    except ep.ProviderConfigurationError as error:
        return PreflightResult(False, error.code,
                               "the provider configuration is missing or invalid")
    except Exception as error:
        # A provider whose preflight raises anything at all has not established
        # that it could accept a message. Before any submission that is a
        # DEFINITE local failure, never an ambiguity.
        return PreflightResult(False, "PROVIDER_PREFLIGHT_FAILED", type(error).__name__)
    try:
        backend_id = backend.stable_id()
    except Exception as error:
        return PreflightResult(False, "PROVIDER_BACKEND_SCOPE_UNAVAILABLE",
                               type(error).__name__)
    if not backend_id:  # pragma: no cover - defensive
        return PreflightResult(False, "PROVIDER_BACKEND_SCOPE_UNAVAILABLE",
                               "the provider named no backend scope")
    return PreflightResult(True, backend=backend)


def external_mailer_owns(record: DeliveryRecord) -> bool:
    """Does an external mailing lifecycle own this delivery's send accounting?

    THE ANSWER COMES FROM THE ROW'S CREATION, NOT FROM ITS PROGRESS. The
    authoritative evidence is `external_mailer`, which migration 050 binds at
    INSERT and makes immutable in both directions. Every state an
    externally-owned delivery can be in therefore answers this question the same
    way, including the ones it only passes through:

      * created but nothing published yet (`PREPARED`);
      * published, capability persisted, handoff not yet recorded
        (`CAPABILITY_PERSISTED`) — the crash window that used to be adoptable;
      * mid-rotation of an expired capability (`BEARER_RECOVERY_REQUIRED`),
        whose own safe next action would otherwise belong to the PROVIDER
        lifecycle;
      * recovered and re-persisted, handoff not yet re-recorded.

    The two state/metadata signals are kept as corroborating evidence rather
    than as the answer. They cannot contradict the column — the handoff refuses
    to write itself onto a row whose ownership does not already name the same
    mailer — so this stays true even for a row read through a projection that
    predates the column.
    """
    if record.row.get("external_mailer"):
        return True
    if record.state == DeliveryState.EXTERNAL_MAILER_HANDOFF:  # pragma: no cover
        return True
    metadata = record.metadata_json
    return isinstance(metadata, Mapping) and bool(metadata.get("external_mailer_handoff"))


def advance_delivery(
    services: PublisherServices,
    *,
    identity: DeliveryIdentity,
    payload_digest: str,
    recipient_email: str,
    body: Optional[bytes] = None,
    owner: str,
    run_id: Optional[str] = None,
    max_steps: int = MAX_STEPS,
) -> DeliveryResult:
    """Drive one logical delivery as far as it can safely go.

    `body` is the EXACT canonical snapshot octets from
    `serialize_publishable_snapshot`. It is required only while a publication
    may still have to be issued; once the operation holds a grant it is never
    needed again, because the published bytes are already the identity.
    """
    ledger = services.ledger
    ledger.register_secret(services.client.credential)

    # BEFORE ANYTHING. Not before the provider call, not before the submission
    # marker — before the local row, the publication and the capability, because
    # every one of those is work performed for a delivery that cannot happen.
    checked = preflight(services)
    if not checked.ok:
        services.log("ERROR", "eco_dashboard_delivery_preflight_failed",
                     {"operation_id": derive_operation_id(identity),
                      "code": checked.code, "detail": checked.detail})
        return DeliveryResult(INVOCATION.PREFLIGHT_FAILED, state="UNKNOWN",
                              next_action=NextAction.OPERATOR_INVESTIGATION,
                              steps=0, detail=checked.detail,
                              conflict_code=checked.code)

    try:
        record, created = prepare_delivery(
            ledger, identity, payload_digest=payload_digest,
            recipient_email=recipient_email,
            # THE PROVIDER LIFECYCLE CREATES PROVIDER-OWNED ROWS ONLY. It never
            # creates an externally-owned one, and — because ownership is part
            # of the compatibility check — it is refused before any mutation
            # when the row it finds was created for an external mailer.
            external_mailer=None,
            run_id=run_id)
    except LedgerConflict as conflict:
        # Nothing was mutated. A recipient conflict in particular must never be
        # "resolved" by redirecting the delivery. An ownership conflict is the
        # same shape of refusal: this delivery belongs to an external mailer,
        # and no lease, attempt count or provider field is touched saying so.
        services.log("ERROR", "eco_dashboard_delivery_conflict",
                     {"code": conflict.code, "operation_id": derive_operation_id(identity)})
        if conflict.code == "EXTERNAL_OWNERSHIP_CONFLICT":
            return DeliveryResult(
                INVOCATION.CONFLICT, state="UNKNOWN",
                next_action=NextAction.EXTERNAL_MAILER_OWNS_DELIVERY, steps=0,
                detail="an external mailing lifecycle owns this delivery",
                conflict_code=ConflictCode.EXTERNAL_MAILER_OWNS_DELIVERY)
        return DeliveryResult(INVOCATION.CONFLICT, state="UNKNOWN",
                              next_action=NextAction.OPERATOR_INVESTIGATION,
                              steps=0, detail=str(conflict), conflict_code=conflict.code)

    services.log("INFO", "eco_dashboard_delivery_prepared",
                 {"operation_id": record.operation_id, "created": created,
                  "state": record.state, "recipient_identity": record.recipient_identity})

    if external_mailer_owns(record):
        # NOT AN ERROR — A BOUNDARY. `eco_*_email_send_log` is authoritative for
        # this delivery's send, so nothing here may record an intent, submit,
        # reconcile or finalise it. Refused before the lease is even claimed, so
        # this invocation mutates nothing at all.
        services.log("ERROR", "eco_dashboard_delivery_external_mailer_owned",
                     {"operation_id": record.operation_id, "state": record.state})
        return DeliveryResult(INVOCATION.CONFLICT, state=record.state,
                              next_action=NextAction.EXTERNAL_MAILER_OWNS_DELIVERY,
                              steps=0, record=record,
                              detail="an external mailing lifecycle owns this delivery",
                              conflict_code=ConflictCode.EXTERNAL_MAILER_OWNS_DELIVERY)

    claimed = ledger.claim(record.delivery_id, owner=owner,
                           lease_seconds=services.config.lease_seconds, run_id=run_id)
    if claimed is None:
        # Another invocation owns this delivery. Doing nothing is the correct
        # and complete behaviour: the owner is already performing the one safe
        # next action.
        services.log("INFO", "eco_dashboard_delivery_not_owned",
                     {"operation_id": record.operation_id, "state": record.state})
        return DeliveryResult(INVOCATION.NOT_OWNED, state=record.state,
                              next_action=record.next_action, steps=0,
                              record=record, detail="another invocation owns this delivery")

    record = claimed
    if record.capability_secret:
        ledger.register_secret(record.capability_secret)

    steps = 0
    outcome = INVOCATION.IN_PROGRESS
    detail = ""
    try:
        while steps < max_steps:
            state = record.state
            if state in (DeliveryState.FINALIZED,):
                outcome = INVOCATION.COMPLETED
                break
            if state == DeliveryState.CAPABILITY_RETIRED:
                # Terminal, and terminal without a complaint. The capability
                # expired and its bearer was destroyed, so there is nothing to
                # submit, reconcile or finalise — and nothing for an operator
                # either, which is why this is not `OPERATOR_REQUIRED`. A
                # genuine resend of this period goes through
                # `ensure_capability`, which rotates via the explicit recovery
                # operation; the provider lifecycle must not resurrect it,
                # because the message identity it would submit under names a
                # capability that no longer exists.
                outcome = INVOCATION.COMPLETED
                detail = "the capability expired and was retired"
                break
            if state in (DeliveryState.OPERATOR_REQUIRED, DeliveryState.PROVIDER_AMBIGUOUS):
                outcome = INVOCATION.OPERATOR_REQUIRED
                break
            if state == DeliveryState.PROVIDER_REJECTED:
                if record.operator_action_required:
                    outcome = INVOCATION.OPERATOR_REQUIRED
                    break
                # A definite refusal created no message, so a fresh attempt
                # under the SAME idempotency identity is legal.
                step = _submit(services, record, owner)
            elif state == DeliveryState.PREPARED:
                step = _publish(services, record, owner, body)
            elif state == DeliveryState.BEARER_RECOVERY_REQUIRED:
                step = _recover(services, record, owner)
            elif state == DeliveryState.CAPABILITY_PERSISTED:
                step = _record_intent(services, record, owner)
            elif state == DeliveryState.DELIVERY_INTENT_RECORDED:
                step = _submit(services, record, owner)
            elif state == DeliveryState.PROVIDER_SUBMISSION_PENDING:
                step = _reconcile(services, record, owner)
            elif state == DeliveryState.PROVIDER_ACCEPTED:
                step = _mark_remote_delivered(services, record, owner)
            elif state == DeliveryState.REMOTE_DELIVERED:
                step = _finalize(services, record, owner)
            else:  # pragma: no cover - defensive
                raise AssertionError(f"unhandled delivery state {state!r}")

            steps += 1
            if step.record is not None:
                record = step.record
                if record.capability_secret:
                    ledger.register_secret(record.capability_secret)
            if step.stop:
                outcome = step.outcome or INVOCATION.RETRY_LATER
                detail = step.detail
                break
    except LedgerConflict as conflict:
        # The lease was lost mid-flight (it expired and another invocation took
        # over) or the row moved under the decision this step was made from.
        # Either way the write was REFUSED, not applied, so stopping here is
        # safe and the new owner performs the next action.
        services.log("WARNING", "eco_dashboard_delivery_ownership_lost",
                     {"operation_id": record.operation_id, "code": conflict.code})
        outcome = INVOCATION.NOT_OWNED
        detail = conflict.code
    finally:
        if record.state != DeliveryState.FINALIZED:
            ledger.release(record.delivery_id, owner=owner)

    fresh = ledger.load(record.delivery_id) or record
    if fresh.state == DeliveryState.FINALIZED:
        outcome = INVOCATION.COMPLETED
    elif fresh.state in (DeliveryState.OPERATOR_REQUIRED, DeliveryState.PROVIDER_AMBIGUOUS) or \
            (fresh.state == DeliveryState.PROVIDER_REJECTED and fresh.operator_action_required):
        outcome = INVOCATION.OPERATOR_REQUIRED
    return DeliveryResult(outcome, state=fresh.state, next_action=fresh.next_action,
                          steps=steps, record=fresh, detail=detail)


# --- publication-only: the integration seam for an external mailer -------------


#: A bound on durable steps for `ensure_capability`. The publication-only path
#: has at most three: publish (or recover) and hand off. The bound is a promise
#: that one invocation terminates, not a behavioural contract — nothing here may
#: depend on a particular step count.
MAX_PUBLICATION_STEPS = 4


@dataclass(frozen=True)
class CapabilityResult:
    """What one `ensure_capability` invocation established.

    `capability_url` is the ONLY place a bearer-bearing value is returned, it is
    returned to the caller and to nowhere else, and `summary()` deliberately
    does not contain it: a job summary, a log line and an exception message are
    all places a capability must never reach.
    """

    invocation: str
    state: str
    next_action: str
    steps: int
    capability_url: Optional[str] = None
    record: Optional[DeliveryRecord] = None
    detail: str = ""
    conflict_code: Optional[str] = None

    @property
    def has_link(self) -> bool:
        return bool(self.capability_url)

    def summary(self) -> dict:
        base = {
            "invocation": self.invocation,
            "state": self.state,
            "next_action": self.next_action,
            "steps": self.steps,
            "detail": self.detail,
            "conflict_code": self.conflict_code,
            "capability_url_present": self.has_link,
        }
        if self.record is not None:
            base["delivery"] = self.record.public_summary()
        return base


def ensure_capability(
    services: PublisherServices,
    *,
    identity: DeliveryIdentity,
    payload_digest: str,
    recipient_email: str,
    body: Optional[bytes],
    owner: str,
    mailer: str,
    run_id: Optional[str] = None,
    max_steps: int = MAX_PUBLICATION_STEPS,
) -> CapabilityResult:
    """Publish one driver's snapshot and return the CURRENT capability link.

    THE SUPPORTED SEAM FOR AN EXTERNAL MAILING LIFECYCLE. The existing Eco
    Driving weekly/monthly jobs already own client selection, driver
    enumeration, recipient resolution, the reporting period, send-log
    idempotency and the example.invalid SMTP send. What they do not have is a dashboard
    link. This gives them exactly that and nothing else:

        preflight -> durable local operation -> claim -> publish (or recover)
        -> capability persisted -> handoff recorded -> link returned

    WHAT IT WILL NEVER DO, BY CONSTRUCTION AND NOT BY CONVENTION.

    * it never touches `services.provider`. There is no `_record_intent`, no
      `_submit`, no `_reconcile` and no `_mark_remote_delivered` on this path,
      so no second SMTP lifecycle can exist around the same message. A
      publication-only wiring may legitimately carry no provider at all;
    * it never binds a provider submission identity, so the states that require
      one are unreachable from here;
    * it never marks the remote operation `DELIVERED`. Whether a driver was
      mailed is a fact `eco_*_email_send_log` owns.

    IT IS NOT `advance_delivery(max_steps=1)`. That call happens to stop after
    one step today, which is an implementation detail of a loop, not a contract:
    it would still record a delivery intent as its "one step" from
    `CAPABILITY_PERSISTED`, and it returns no link. This function is the
    designed contract.

    RERUN BEHAVIOUR. Identical inputs converge: the operation id, subject
    reference and payload digest are derived from the logical delivery, the
    ledger refuses a rerun that changes the recipient or the canonical bytes,
    and a delivery already in `EXTERNAL_MAILER_HANDOFF` returns the SAME link
    from the retained bearer rather than minting or rotating a capability.

    THE ONE EXCEPTION, AND IT IS NOT A CHOICE. A retained bearer whose grant
    has EXPIRED is not the same link — it is a URL that answers 410. Returning
    it would be the one outcome worse than returning nothing, so an expired
    handoff is rotated exactly once, through the explicit recovery operation,
    and the fresh capability is handed over under the unchanged delivery
    identity. That keeps a legitimate DELAYED rerun — a recovery weeks later, a
    period re-mailed after an incident — autonomous, instead of parking every
    normal expiry on an operator. See `_rotate_expired_capability`.
    """
    ledger = services.ledger
    ledger.register_secret(services.client.credential)

    if not str(mailer or "").strip():
        # OWNERSHIP HAS TO BE NAMEABLE BEFORE THE ROW EXISTS. A blank mailer
        # would create a row indistinguishable from a provider-owned one, which
        # is the exact adoption this path is built to make impossible.
        raise DeliveryContractError(
            "an external mailer name is required to obtain a capability")

    checked = publication_preflight(services)
    if not checked.ok:
        services.log("ERROR", "eco_dashboard_capability_preflight_failed",
                     {"operation_id": derive_operation_id(identity),
                      "code": checked.code, "detail": checked.detail})
        return CapabilityResult(INVOCATION.PREFLIGHT_FAILED, state="UNKNOWN",
                                next_action=NextAction.OPERATOR_INVESTIGATION,
                                steps=0, detail=checked.detail,
                                conflict_code=checked.code)

    try:
        record, created = prepare_delivery(
            ledger, identity, payload_digest=payload_digest,
            recipient_email=recipient_email,
            # OWNERSHIP IS ESTABLISHED BY THIS CALL, IN THE FIRST DURABLE ROW.
            # Not by `_handoff` further down, which only RECORDS it: a crash
            # anywhere between here and there must still leave a row the
            # provider lifecycle refuses to adopt.
            external_mailer=mailer,
            run_id=run_id)
    except LedgerConflict as conflict:
        services.log("ERROR", "eco_dashboard_capability_conflict",
                     {"code": conflict.code, "operation_id": derive_operation_id(identity)})
        return CapabilityResult(INVOCATION.CONFLICT, state="UNKNOWN",
                                next_action=NextAction.OPERATOR_INVESTIGATION,
                                steps=0, detail=str(conflict), conflict_code=conflict.code)

    services.log("INFO", "eco_dashboard_capability_prepared",
                 {"operation_id": record.operation_id, "created": created,
                  "state": record.state, "recipient_identity": record.recipient_identity})

    claimed = ledger.claim(record.delivery_id, owner=owner,
                           lease_seconds=services.config.lease_seconds, run_id=run_id)
    if claimed is None:
        # Another invocation owns this delivery. Returning no link is correct:
        # this invocation must not e-mail a driver on the strength of a
        # capability it does not own the lifecycle of.
        services.log("INFO", "eco_dashboard_capability_not_owned",
                     {"operation_id": record.operation_id, "state": record.state})
        return CapabilityResult(INVOCATION.NOT_OWNED, state=record.state,
                                next_action=record.next_action, steps=0, record=record,
                                detail="another invocation owns this delivery")

    record = claimed
    if record.capability_secret:
        ledger.register_secret(record.capability_secret)

    steps = 0
    outcome = INVOCATION.IN_PROGRESS
    detail = ""
    url: Optional[str] = None
    #: At most ONE expiry-driven rotation per invocation. A second would mean
    #: the replacement grant was born unusable, which is a configuration or
    #: Worker problem and not something a loop can fix.
    rotated = False
    try:
        while steps < max_steps:
            state = record.state
            if state in (DeliveryState.EXTERNAL_MAILER_HANDOFF,
                         # A DELIVERY WHOSE CAPABILITY THE SWEEP ALREADY
                         # RETIRED. Reaching it here means somebody is asking
                         # for this exact reporting period's link again after
                         # its grant expired — a re-mail, a recovery weeks
                         # later, a period resent after an incident.
                         #
                         # It is handled by the SAME branch as an expired
                         # handoff, deliberately: a retired row holds no bearer,
                         # so `capability_is_usable` is false, so the branch
                         # rotates once through the explicit recovery operation
                         # and hands the fresh capability over under the
                         # unchanged operation id, subject binding and payload
                         # digest. The link that comes back still shows THIS
                         # period's snapshot; nothing here can retarget it at a
                         # newer report.
                         DeliveryState.CAPABILITY_RETIRED):
                if capability_is_usable(services, record):
                    url = _capability_url(services, record)
                    outcome = INVOCATION.COMPLETED
                    break
                if rotated:
                    # The rotation ran and the replacement is still not usable.
                    # Refusing is the only honest answer left.
                    services.log("ERROR", "eco_dashboard_capability_expired",
                                 {"operation_id": record.operation_id,
                                  "capability_id": record.capability_id,
                                  "bearer_generation": record.bearer_generation})
                    outcome = INVOCATION.CONFLICT
                    detail = ConflictCode.CAPABILITY_EXPIRED
                    break
                rotated = True
                step = _rotate_expired_capability(services, record, owner)
            elif state == DeliveryState.PREPARED:
                step = _publish(services, record, owner, body)
            elif state == DeliveryState.BEARER_RECOVERY_REQUIRED:
                step = _recover(services, record, owner)
            elif state == DeliveryState.CAPABILITY_PERSISTED:
                step = _handoff(services, record, owner, mailer=mailer, run_id=run_id)
            else:
                # Every remaining state belongs to the provider lifecycle or is
                # terminal. This path must not progress any of them, and it must
                # not hand a link to a mailer for a delivery another lifecycle
                # already owns.
                outcome = (INVOCATION.OPERATOR_REQUIRED
                           if state in (DeliveryState.OPERATOR_REQUIRED,
                                        DeliveryState.PROVIDER_AMBIGUOUS)
                           else INVOCATION.CONFLICT)
                detail = f"delivery is in {state}; the publication-only path does not progress it"
                services.log("ERROR", "eco_dashboard_capability_unavailable",
                             {"operation_id": record.operation_id, "state": state})
                break

            steps += 1
            if step.record is not None:
                record = step.record
                if record.capability_secret:
                    ledger.register_secret(record.capability_secret)
            if step.stop:
                outcome = step.outcome or INVOCATION.RETRY_LATER
                detail = step.detail
                break
    except EmailConstructionError as error:
        # The bearer exists but no usable link can be built from it. Refusing is
        # the whole point: an Eco e-mail without its dashboard link must not go
        # out, and a half-built link must never be guessed at.
        services.log("ERROR", "eco_dashboard_capability_link_unusable",
                     {"operation_id": record.operation_id, "code": str(error)})
        outcome = INVOCATION.CONFLICT
        detail = str(error)
        url = None
    except LedgerConflict as conflict:
        services.log("WARNING", "eco_dashboard_capability_ownership_lost",
                     {"operation_id": record.operation_id, "code": conflict.code})
        outcome = INVOCATION.NOT_OWNED
        detail = conflict.code
        url = None
    finally:
        ledger.release(record.delivery_id, owner=owner)

    fresh = ledger.load(record.delivery_id) or record
    if url and fresh.state != DeliveryState.EXTERNAL_MAILER_HANDOFF:  # pragma: no cover
        # Defensive: a link is only ever returned for a delivery whose handoff
        # is durable, so the mailer can never be given a link the ledger has no
        # record of having handed over.
        url = None
        outcome = INVOCATION.CONFLICT
        detail = "handoff is not durable"
    return CapabilityResult(outcome, state=fresh.state, next_action=fresh.next_action,
                            steps=steps, capability_url=url, record=fresh, detail=detail,
                            # A bounded vocabulary, never prose: the invocation
                            # outcome IS the code a caller classifies on, and
                            # `detail` carries the human-readable half.
                            conflict_code=None if url else outcome)


def _capability_url(services: PublisherServices, record: DeliveryRecord) -> str:
    """The driver link, built from the CURRENT persisted bearer."""
    return build_capability_url(services.config.dashboard_base_url,
                                record.capability_secret)


def _as_utc(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if not isinstance(value, datetime):  # pragma: no cover - defensive
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def capability_is_usable(services: PublisherServices, record: DeliveryRecord,
                         *, now: Optional[datetime] = None) -> bool:
    """Is the persisted grant something this host may still hand to a driver?

    A retained bearer is only an answer to "what is this driver's link?" while
    the grant behind it is live. Past its expiry the same string is a URL that
    returns 410, so handing it over would put a dead link in a real e-mail and
    the ledger would record a handoff that delivered nothing.

    An UNKNOWN expiry is treated as unusable rather than as permission. The
    publication protocol always states one, so its absence means the host does
    not know what it is holding — and "I do not know whether this link works"
    is not a state from which to mail a driver.
    """
    if not record.capability_secret:
        return False
    expires_at = _as_utc(record.capability_expires_at)
    if expires_at is None:
        return False
    margin = timedelta(seconds=max(0, int(services.config.capability_min_remaining_seconds)))
    return expires_at > (now or datetime.now(timezone.utc)) + margin


def _rotate_expired_capability(services: PublisherServices, record: DeliveryRecord,
                               owner: str) -> _Step:
    """Send an expired handoff back through the EXPLICIT recovery operation.

    WHY THIS IS NOT A NEW MECHANISM. The Worker already has exactly one way to
    replace a grant this host cannot present: `POST /api/publish/recover`,
    which revokes the predecessor and mints a replacement inside one
    transaction, under the unchanged operation id, subject binding and payload
    digest, only for an operation whose stored object still verifies, and never
    for one that is already `DELIVERED`. Everything a rotation has to preserve
    is preserved by using that primitive rather than inventing a second one:
    ONE logical delivery identity, the same recipient binding (which never
    leaves this host at all), publication integrity, capability/session
    separation, and the lost-bearer safeguards.

    The transition itself destroys the expired bearer, so the row does not sit
    on secret material that can no longer authorise anything.

    WHAT IT STILL WILL NOT DO. It sends nothing, it binds no provider, and it
    makes no claim about SMTP: a rerun that rotates is a rerun that obtains a
    fresh LINK, while whether this driver was ever e-mailed remains a fact
    `eco_*_email_send_log` owns. If the recovery is refused — the operation is
    terminal remotely, its object no longer verifies, the grant was superseded
    — the delivery lands in `OPERATOR_REQUIRED` and no link is returned, which
    withholds one driver's e-mail instead of sending a broken one.
    """
    record = _fence(services, record, owner)
    services.log("WARNING", "eco_dashboard_capability_expired_rotating",
                 {"operation_id": record.operation_id,
                  "capability_id": record.capability_id,
                  "bearer_generation": record.bearer_generation})
    updated = services.ledger.mark_bearer_recovery_required(
        record, owner=owner,
        failure_code=ConflictCode.CAPABILITY_EXPIRED,
        reason="the retained capability expired; the external mailer link is reissued "
               "through the explicit recovery operation")
    return _Step(updated)


def _handoff(services: PublisherServices, record: DeliveryRecord, owner: str, *,
             mailer: str, run_id: Optional[str]) -> _Step:
    """Record that an external mailing lifecycle now owns this delivery.

    The link is constructed BEFORE the transition, so a bearer that cannot
    produce a usable link never becomes a recorded handoff; and the transition
    is committed BEFORE the link is returned, so the mailer is never handed a
    link the ledger has no record of.
    """
    record = _fence(services, record, owner)
    _capability_url(services, record)  # refuses here, before anything durable
    updated = services.ledger.record_external_mailer_handoff(
        record, owner=owner, mailer=mailer, run_id=run_id)
    services.log("INFO", "eco_dashboard_external_mailer_handoff",
                 {"operation_id": record.operation_id,
                  "capability_id": record.capability_id,
                  "bearer_generation": record.bearer_generation,
                  "mailer": mailer})
    return _Step(updated)


# --- one step ------------------------------------------------------------------


@dataclass(frozen=True)
class _Step:
    record: Optional[DeliveryRecord] = None
    stop: bool = False
    outcome: Optional[str] = None
    detail: str = ""


def _operator(services: PublisherServices, record: DeliveryRecord, owner: str, *,
              phase: str, code: str, detail: str) -> _Step:
    services.log("ERROR", "eco_dashboard_delivery_operator_required",
                 {"operation_id": record.operation_id, "phase": phase, "code": code})
    updated = services.ledger.mark_operator_required(
        record, owner=owner, phase=phase, failure_code=code, detail=detail)
    return _Step(updated, stop=True, outcome=INVOCATION.OPERATOR_REQUIRED, detail=code)


def _fence(services: PublisherServices, record: DeliveryRecord,
           owner: str) -> DeliveryRecord:
    """Re-assert LIVE ownership immediately before initiating a remote effect.

    Every ledger write is already fenced, so a stale holder can never record
    the OUTCOME of an effect. This closes the other half: a stale holder must
    not INITIATE one either, because some effects are not free to repeat and
    because "the write was refused afterwards" is no comfort to a driver who
    received a second e-mail.

    It renews as it checks, in one statement, so a legitimately slow call keeps
    the lease it is actively using instead of losing it mid-flight.

    THE LIMIT, STATED PLAINLY. No database lease is atomic with an HTTP request:
    a lease can still expire between this check and the effect completing. That
    residual window is covered by ordering rather than by locking — the
    "a submission may have happened" marker is committed before the provider
    call, so an owner that takes over reconciles under the bound idempotency
    identity instead of resending, and the publication/delivery routes are
    idempotent by operation id at the Worker.
    """
    fresh = services.ledger.renew(record.delivery_id, owner=owner,
                                  lease_seconds=services.config.lease_seconds)
    if fresh is None:
        raise LedgerConflict(
            "LEASE_NOT_HELD",
            "the lease is no longer held by this invocation; no effect was initiated")
    if fresh.state != record.state:  # pragma: no cover - defensive
        raise LedgerConflict(
            "STATE_MOVED",
            "the delivery moved under this invocation; no effect was initiated")
    return fresh


def _provider_backend(services: PublisherServices, record: DeliveryRecord,
                      owner: str) -> tuple[Optional[_Step], Optional[str]]:
    """Prove the configured backend is the one this delivery is bound to.

    A provider idempotency key is a statement to ONE backend. Presenting it to
    another is not a retry — the second backend has never heard of the key and
    will create a second message — so a mismatch stops here, before `submit`,
    and asks for a human rather than resolving itself.
    """
    try:
        backend = services.provider.backend_identity()
        configured = backend.stable_id()
    except ep.ProviderConfigurationError as error:
        return _operator(services, record, owner, phase="PROVIDER_CONFIGURATION",
                         code=error.code,
                         detail="the provider configuration is missing or invalid"), None
    bound = record.provider_backend_id
    if bound and configured != bound:
        return _operator(
            services, record, owner, phase="PROVIDER",
            code=ConflictCode.PROVIDER_BACKEND_CONFLICT,
            detail=("this delivery is bound to a different provider backend; "
                    f"the configured backend is {backend.describe()}")), None
    return None, configured


def _bound_message(services: PublisherServices, record: DeliveryRecord, owner: str):
    """Build the message and prove it is the one the bound key already names.

    Returns `(refusal_step, message, key, fingerprint)`; exactly one of the
    refusal and the message is not `None`.

    The fingerprint covers the whole rendered message, so this single
    comparison is what refuses a changed dashboard base URL, a changed subject
    or template, a changed recipient and a replacement bearer generation. All
    of them are the same event: the key would carry different content than the
    content it was issued for.
    """
    try:
        message, key = _build_message(services, record)
    except EmailConstructionError as error:
        return (_operator(services, record, owner, phase="MESSAGE", code=str(error),
                          detail="the message could not be constructed"),
                None, None, None)
    fingerprint = derive_message_fingerprint(
        recipient_email=message.recipient_email, subject=message.subject,
        message_id=message.message_id, html_body=message.html_body,
        text_body=message.text_body)
    bound = record.provider_message_fingerprint
    if bound and fingerprint != bound:
        return (_operator(
            services, record, owner, phase="PROVIDER",
            code=ConflictCode.MESSAGE_CONFLICT,
            detail=("the intended message differs from the one this provider "
                    "idempotency key was bound to")),
            None, None, None)
    return None, message, key, fingerprint


@dataclass(frozen=True)
class _ProviderGuard:
    """What a provider-facing step has PROVEN before it may touch a provider."""

    record: DeliveryRecord
    message: Any
    key: str
    fingerprint: str
    backend_id: str


def _provider_guard(services: PublisherServices, record: DeliveryRecord,
                    owner: str) -> tuple[Optional[_Step], Optional[_ProviderGuard]]:
    """The ONE contract every provider-facing step passes through.

    THE ORDER IS THE POINT: live ownership, then the bound backend, then the
    bound immutable message — all three established BEFORE the provider is
    spoken to at all.

    * `_fence` — a holder whose lease has expired must not INITIATE a provider
      interaction, not merely fail to record its outcome. A lookup is an
      external call made by an execution that no longer owns the delivery, and
      the answer it returns is what a stale process would otherwise act on.
    * `_provider_backend` — a key is a statement to ONE backend.
    * `_bound_message` — and it names ONE message. Verifying this only at the
      first submit was the gap: a lost response followed by a changed base URL,
      template or bearer generation would otherwise be "reconciled" into an
      acceptance of a message the current host can no longer prove it sent.

    Returns `(refusal_step, guard)`; exactly one of the two is not `None`. A
    refusal has already recorded the conflict and initiated NOTHING remote.
    """
    record = _fence(services, record, owner)
    refusal, backend_id = _provider_backend(services, record, owner)
    if refusal is not None:
        return refusal, None
    refusal, message, key, fingerprint = _bound_message(services, record, owner)
    if refusal is not None:
        return refusal, None
    return None, _ProviderGuard(record=record, message=message, key=key,
                                fingerprint=str(fingerprint),
                                backend_id=str(backend_id))


def _retry_later(services: PublisherServices, record: DeliveryRecord, *,
                 event: str, code: str) -> _Step:
    """Leave the durable state exactly where it is.

    Used for every genuinely unresolved outcome — a lost response, storage that
    could not be read, a transport that never answered. The state already
    encodes the one safe next action, so writing anything here could only make
    it less true.
    """
    services.log("WARNING", event, {"operation_id": record.operation_id, "code": code})
    return _Step(None, stop=True, outcome=INVOCATION.RETRY_LATER, detail=code)


def _publish(services: PublisherServices, record: DeliveryRecord, owner: str,
             body: Optional[bytes]) -> _Step:
    if body is None:
        return _retry_later(services, record, event="eco_dashboard_publish_needs_bytes",
                            code="CANONICAL_BYTES_NOT_AVAILABLE")
    record = _fence(services, record, owner)
    try:
        outcome = services.client.publish(
            operation_id=record.operation_id,
            subject_ref=record.subject_ref,
            payload_digest=record.payload_digest,
            body=body,
            # THE AUTHORITATIVE PERIOD, read from the durable delivery identity
            # and from nowhere else. It decides the capability's lifetime at the
            # Worker (weekly 10 days, monthly 60), and the ledger column it
            # comes from is constrained to those two values and is hashed into
            # this operation's id. No presentation text, template or URL is
            # consulted, and this host computes no lifetime of its own.
            period_type=record.period_type,
        )
    except sdc.TransportOutcomeUnknown:
        # The publication may have committed. `PREPARED` already means "issue
        # the idempotent publish", so the state needs no change at all.
        return _retry_later(services, record, event="eco_dashboard_publish_unresolved",
                            code="PUBLISH_OUTCOME_UNKNOWN")
    except sdc.SecureDeliveryError as error:
        return _operator(services, record, owner, phase="PUBLICATION",
                         code=error.code, detail=str(error))

    if outcome.status == sdc.PUBLISHED and outcome.capability:
        updated = services.ledger.record_capability(
            record, owner=owner, capability=outcome.capability,
            capability_id=outcome.capability_id,
            expires_at=outcome.expires_at or datetime.now(timezone.utc),
            bearer_generation=outcome.bearer_generation or 1)
        services.log("INFO", "eco_dashboard_capability_persisted",
                     {"operation_id": record.operation_id,
                      "capability_id": outcome.capability_id,
                      "bearer_generation": updated.bearer_generation})
        return _Step(updated)

    if outcome.status in (sdc.ALREADY_PUBLISHED,):
        # The Worker holds an authoritative grant and will never replay its
        # bearer. This host does not have one, so recovery — not another
        # publish — is the only legal move.
        updated = services.ledger.mark_bearer_recovery_required(
            record, owner=owner,
            reason="publication committed remotely; raw bearer not held locally")
        services.log("WARNING", "eco_dashboard_bearer_recovery_required",
                     {"operation_id": record.operation_id,
                      "remote_next_action": outcome.next_action})
        return _Step(updated)

    if outcome.status == sdc.IN_PROGRESS:
        # The operation exists without a grant; a plain retry converges.
        return _Step(None, stop=True, outcome=INVOCATION.RETRY_LATER,
                     detail="PUBLICATION_IN_PROGRESS")

    if outcome.status == sdc.OBJECT_UNREADABLE:
        return _retry_later(services, record, event="eco_dashboard_object_unreadable",
                            code=sdc.OBJECT_UNREADABLE)

    if outcome.status == sdc.ALREADY_DELIVERED:
        # The remote operation is terminal but this host has no record of a
        # message. Whether the driver received one is not knowable from here.
        return _operator(services, record, owner, phase="PUBLICATION",
                         code="REMOTE_ALREADY_DELIVERED",
                         detail="the remote operation is terminal; local delivery state is absent")

    return _operator(services, record, owner, phase="PUBLICATION",
                     code=str(outcome.error or outcome.status),
                     detail=f"publication refused: {outcome.status}")


def _recover(services: PublisherServices, record: DeliveryRecord, owner: str) -> _Step:
    record = _fence(services, record, owner)
    try:
        # Recovery mints a REPLACEMENT grant, so it writes a fresh expiry and
        # must use the same period-aware policy the original publication did.
        # Same source, same immutable row: a rotation cannot change which
        # period this delivery is for, and the recovered capability still
        # resolves to that period's snapshot.
        outcome = services.client.recover(operation_id=record.operation_id,
                                          period_type=record.period_type)
    except sdc.TransportOutcomeUnknown:
        # A recovery whose answer was lost may have replaced the grant. The
        # state stays `BEARER_RECOVERY_REQUIRED`, and a later recovery either
        # succeeds or reports the operation superseded — neither sends mail,
        # and neither re-publishes.
        return _retry_later(services, record, event="eco_dashboard_recover_unresolved",
                            code="RECOVER_OUTCOME_UNKNOWN")
    except sdc.SecureDeliveryError as error:
        return _operator(services, record, owner, phase="RECOVERY",
                         code=error.code, detail=str(error))

    if outcome.status == sdc.RECOVERED and outcome.capability:
        updated = services.ledger.record_capability(
            record, owner=owner, capability=outcome.capability,
            capability_id=outcome.capability_id,
            expires_at=outcome.expires_at or datetime.now(timezone.utc),
            bearer_generation=outcome.bearer_generation or (record.bearer_generation + 1))
        services.log("INFO", "eco_dashboard_bearer_recovered",
                     {"operation_id": record.operation_id,
                      "capability_id": outcome.capability_id,
                      "bearer_generation": updated.bearer_generation})
        return _Step(updated)

    if outcome.status == sdc.OBJECT_UNREADABLE:
        return _retry_later(services, record, event="eco_dashboard_object_unreadable",
                            code=sdc.OBJECT_UNREADABLE)

    return _operator(services, record, owner, phase="RECOVERY",
                     code=str(outcome.reason or outcome.error or outcome.status),
                     detail="the lost bearer could not be recovered")


def _record_intent(services: PublisherServices, record: DeliveryRecord, owner: str) -> _Step:
    """Record intent remotely, then BIND the whole submission identity locally.

    The message is constructed and the backend resolved BEFORE the remote call,
    so a configuration that cannot produce a message never causes a remote
    transition. The binding is committed after it, because it is the last
    durable step before anything may submit — and once written, none of it may
    move again.

    Nothing is bound yet at this point, so `_provider_guard` here proves live
    ownership and CONSTRUCTS the identity rather than comparing against one.
    """
    refusal, guard = _provider_guard(services, record, owner)
    if refusal is not None or guard is None:
        return refusal  # type: ignore[return-value]
    record, backend_id, fingerprint = guard.record, guard.backend_id, guard.fingerprint
    try:
        outcome = services.client.record_delivery(
            operation_id=record.operation_id,
            phase=sdc.DELIVERY_PHASE_INTENT,
            capability_id=record.capability_id)
    except sdc.TransportOutcomeUnknown:
        return _retry_later(services, record, event="eco_dashboard_intent_unresolved",
                            code="DELIVERY_INTENT_OUTCOME_UNKNOWN")
    except sdc.SecureDeliveryError as error:
        return _operator(services, record, owner, phase="DELIVERY_INTENT",
                         code=error.code, detail=str(error))

    if outcome.status in (sdc.RECORDED, sdc.ALREADY_RECORDED):
        updated = services.ledger.record_delivery_intent(
            record, owner=owner, provider_name=str(services.provider.name),
            provider_backend_id=backend_id, message_fingerprint=fingerprint,
            bound_capability_id=record.capability_id,
            bound_bearer_generation=int(record.bearer_generation))
        services.log("INFO", "eco_dashboard_provider_submission_identity_bound",
                     {"operation_id": record.operation_id,
                      "provider": services.provider.name,
                      "provider_backend_id": backend_id,
                      "message_fingerprint": fingerprint,
                      "bound_capability_id": record.capability_id,
                      "bound_bearer_generation": int(record.bearer_generation)})
        return _Step(updated)

    if outcome.status == sdc.CAPABILITY_SUPERSEDED:
        # The bearer this host holds is no longer the authoritative one. It
        # must never be e-mailed, and this host cannot mint a replacement.
        return _operator(services, record, owner, phase="DELIVERY_INTENT",
                         code="REMOTE_CAPABILITY_SUPERSEDED",
                         detail="the held capability is no longer authoritative")

    return _operator(services, record, owner, phase="DELIVERY_INTENT",
                     code=str(outcome.error or outcome.status),
                     detail="the remote delivery intent was refused")


def _build_message(services: PublisherServices, record: DeliveryRecord):
    """Construct the recipient-bound message from the CURRENT capability.

    The link is built from `record.capability_secret`, which
    `record_capability` overwrote in one statement, so a superseded predecessor
    cannot be the value read here.
    """
    url = build_capability_url(services.config.dashboard_base_url, record.capability_secret)
    key = record.provider_idempotency_key or derive_provider_idempotency_key(record.operation_id)
    message_id = build_message_id(key, domain=services.config.message_id_domain)
    context = DashboardEmailContext(
        recipient_email=record.recipient_email,
        period_type=record.period_type,
        period_start_date=record.period_start_date,
        period_end_date=record.period_end_date,
        capability_url=url,
        expires_at=record.capability_expires_at,
    )
    return build_message(context, message_id=message_id), key


def _submit(services: PublisherServices, record: DeliveryRecord, owner: str) -> _Step:
    if record.provider_attempts >= services.config.max_provider_attempts:
        return _operator(services, record, owner, phase="PROVIDER",
                         code="PROVIDER_ATTEMPTS_EXHAUSTED",
                         detail="the provider attempt budget is spent")

    # EVERY DEFINITE REFUSAL FIRST, WHILE NOTHING HAS BEEN ATTEMPTED. A stale
    # lease, a wrong backend, an unconstructable message and a message that is
    # not the bound one are all established before the pending marker exists,
    # so none of them burns an attempt or manufactures an unresolved submission.
    refusal, guard = _provider_guard(services, record, owner)
    if refusal is not None or guard is None:
        return refusal  # type: ignore[return-value]
    record, message, key = guard.record, guard.message, guard.key

    # DURABLE FIRST. After this commit the recovered state says "a message may
    # exist", which is the only honest thing to say once the call begins. It is
    # also the fence: the write carries the live-lease predicate, so a stale
    # invocation is refused HERE, before the provider is contacted.
    pending = services.ledger.record_submission_pending(record, owner=owner)
    services.log("INFO", "eco_dashboard_provider_submission_started",
                 {"operation_id": pending.operation_id,
                  "provider": services.provider.name,
                  "idempotency_key": key,
                  "attempt": pending.provider_attempts})

    try:
        submission = services.provider.submit(message=message, idempotency_key=key)
    except ep.ProviderConfigurationError as error:
        # A configuration that broke between the check above and this call. By
        # the provider contract this is raised BEFORE any network submission,
        # so it is a definite refusal that created no message — not the
        # ambiguity that "we were already PENDING" might otherwise suggest.
        return _definite_configuration_failure(services, pending, owner, error, key)
    except ep.ProviderTransportLost:
        # The message may already exist remotely. `PROVIDER_SUBMISSION_PENDING`
        # is already committed; reconciliation is the next step.
        services.log("WARNING", "eco_dashboard_provider_response_lost",
                     {"operation_id": pending.operation_id, "idempotency_key": key})
        return _Step(pending)

    return _apply_submission(services, pending, owner, submission, key)


def _definite_configuration_failure(services: PublisherServices, record: DeliveryRecord,
                                    owner: str, error, key: str) -> _Step:
    """Record a pre-submit configuration failure as DEFINITE, never ambiguous.

    `PROVIDER_REJECTED` is the state that means "no message was created", which
    is exactly what a provider that never opened a socket guarantees. Recording
    ambiguity instead would ask a human to rule out a message that provably
    does not exist.
    """
    services.log("ERROR", "eco_dashboard_provider_configuration_failure",
                 {"operation_id": record.operation_id, "idempotency_key": key,
                  "code": getattr(error, "code", ConflictCode.PROVIDER_CONFIGURATION)})
    updated = services.ledger.record_provider_rejected(
        record, owner=owner,
        failure_code=str(getattr(error, "code", ConflictCode.PROVIDER_CONFIGURATION)),
        detail="the provider configuration is missing or invalid; nothing was submitted",
        operator_required=True)
    return _Step(updated, stop=True, outcome=INVOCATION.OPERATOR_REQUIRED,
                 detail=ConflictCode.PROVIDER_CONFIGURATION)


def _apply_submission(services: PublisherServices, record: DeliveryRecord, owner: str,
                      submission, key: str) -> _Step:
    if submission.outcome == ep.ACCEPTED and submission.provider_message_id:
        updated = services.ledger.record_provider_accepted(
            record, owner=owner, provider_message_id=submission.provider_message_id,
            reconciled=bool(submission.deduplicated))
        services.log("INFO", "eco_dashboard_provider_accepted",
                     {"operation_id": record.operation_id, "idempotency_key": key,
                      "deduplicated": bool(submission.deduplicated)})
        return _Step(updated)

    if submission.outcome == ep.REJECTED:
        exhausted = (not submission.retryable
                     or record.provider_attempts >= services.config.max_provider_attempts)
        updated = services.ledger.record_provider_rejected(
            record, owner=owner, failure_code=str(submission.failure_code or "REJECTED"),
            detail=str(submission.detail), operator_required=exhausted)
        services.log("WARNING", "eco_dashboard_provider_rejected",
                     {"operation_id": record.operation_id,
                      "failure_code": str(submission.failure_code or "REJECTED"),
                      "retryable": bool(submission.retryable) and not exhausted})
        return _Step(updated, stop=True,
                     outcome=INVOCATION.OPERATOR_REQUIRED if exhausted else INVOCATION.RETRY_LATER,
                     detail=str(submission.failure_code or "REJECTED"))

    # AMBIGUOUS. The provider itself will not say. Ask it about the key if it
    # can be asked; otherwise this is exactly the state that must not resend.
    return _reconcile(services, record, owner,
                      ambiguity_code=str(submission.failure_code or "PROVIDER_UNSURE"))


def _reconcile(services: PublisherServices, record: DeliveryRecord, owner: str,
               ambiguity_code: str = "PROVIDER_RESPONSE_LOST") -> _Step:
    """Establish what happened to one idempotency identity. Never resend blindly.

    Order of preference:

    1. ask the provider about the key — the only source that can turn "unknown"
       into "accepted";
    2. replay under the same key, but ONLY when the provider deduplicates by
       it, because then a replay cannot produce a second message even if the
       reconciliation answer was wrong;
    3. otherwise record `PROVIDER_AMBIGUOUS` and stop. A human resolves it.

    Step 3 is not a failure of the design; it is the design. A provider with no
    idempotency and no lookup cannot be replayed safely, and pretending
    otherwise is what mails a driver twice.

    WHAT A LOOKUP MAY AND MAY NOT LEGITIMISE. "A message exists under key X" is
    only an answer about THIS delivery while the host can still prove that X
    names the message it currently intends. So the full guard runs first:

    * asking the WRONG backend about the key is not reconciliation — a backend
      that has never seen it answers NOT_FOUND, which looks exactly like "no
      message exists" and would licence a resend into a second provider;
    * adopting an acceptance for a message that has since DRIFTED is not
      reconciliation either — it records "the driver received this" about
      content the driver was never sent. A drift stops here, with the provider
      untouched and the ambiguity left intact for an operator;
    * and a holder whose lease has expired may not make the call at all.
    """
    provider = services.provider

    refusal, guard = _provider_guard(services, record, owner)
    if refusal is not None or guard is None:
        return refusal  # type: ignore[return-value]
    record, message, key = guard.record, guard.message, guard.key

    if getattr(provider, "supports_reconciliation", False):
        try:
            verdict = provider.reconcile(idempotency_key=key)
        except ep.ProviderConfigurationError as error:
            # A lookup that could not be made says nothing about the message.
            # The state already names reconciliation as the next action, so
            # leaving it exactly where it is remains the honest answer.
            return _retry_later(services, record,
                                event="eco_dashboard_provider_lookup_unconfigured",
                                code=str(getattr(error, "code",
                                                 ConflictCode.PROVIDER_CONFIGURATION)))
        services.log("INFO", "eco_dashboard_provider_reconciled",
                     {"operation_id": record.operation_id, "idempotency_key": key,
                      "outcome": verdict.outcome})
        if verdict.outcome == ep.FOUND and verdict.provider_message_id:
            updated = services.ledger.record_provider_accepted(
                record, owner=owner, provider_message_id=verdict.provider_message_id,
                reconciled=True)
            return _Step(updated)
        if verdict.outcome == ep.LOOKUP_FAILED:
            # A lookup that failed says nothing. Leave the state alone; the
            # next invocation asks again.
            return _retry_later(services, record,
                                event="eco_dashboard_provider_lookup_failed",
                                code="PROVIDER_LOOKUP_FAILED")

    if getattr(provider, "supports_idempotent_submit", False) and \
            record.provider_attempts < services.config.max_provider_attempts:
        # A replay is safe ONLY because the provider deduplicates by this key.
        # That guarantee is about the key naming one message, so a replay whose
        # content had drifted would defeat the very property it relies on — the
        # guard above already established that it has not.
        replay = services.ledger.record_submission_pending(record, owner=owner)
        services.log("INFO", "eco_dashboard_provider_replay",
                     {"operation_id": replay.operation_id, "idempotency_key": key,
                      "attempt": replay.provider_attempts})
        try:
            submission = provider.submit(message=message, idempotency_key=key)
        except ep.ProviderConfigurationError as error:
            return _definite_configuration_failure(services, replay, owner, error, key)
        except ep.ProviderTransportLost:
            return _Step(replay, stop=True, outcome=INVOCATION.RETRY_LATER,
                         detail="PROVIDER_RESPONSE_LOST")
        return _apply_submission(services, replay, owner, submission, key)

    updated = services.ledger.record_provider_ambiguous(
        record, owner=owner, failure_code=ambiguity_code,
        detail="provider acceptance is unknown and cannot be established automatically")
    services.log("ERROR", "eco_dashboard_provider_ambiguous",
                 {"operation_id": record.operation_id, "idempotency_key": key,
                  "code": ambiguity_code})
    return _Step(updated, stop=True, outcome=INVOCATION.OPERATOR_REQUIRED,
                 detail=ambiguity_code)


def _mark_remote_delivered(services: PublisherServices, record: DeliveryRecord,
                           owner: str) -> _Step:
    """The remote `DELIVERED` transition — and only now.

    Reached exclusively from `PROVIDER_ACCEPTED`, which requires a recorded
    provider message id. An attempted request is never sufficient.

    It is a progression made ON THE STRENGTH of provider acceptance, so it
    passes the same guard: terminalising the remote operation for a message the
    host can no longer prove it sent would publish that conclusion to the other
    half of the system.
    """
    refusal, guard = _provider_guard(services, record, owner)
    if refusal is not None or guard is None:
        return refusal  # type: ignore[return-value]
    record = guard.record
    try:
        outcome = services.client.record_delivery(
            operation_id=record.operation_id,
            phase=sdc.DELIVERY_PHASE_DELIVERED,
            capability_id=record.capability_id)
    except sdc.TransportOutcomeUnknown:
        # The remote transition is idempotent and names its capability, so a
        # retry answers `ALREADY_RECORDED` rather than changing anything.
        return _retry_later(services, record, event="eco_dashboard_delivered_unresolved",
                            code="REMOTE_DELIVERED_OUTCOME_UNKNOWN")
    except sdc.SecureDeliveryError as error:
        return _operator(services, record, owner, phase="REMOTE_DELIVERED",
                         code=error.code, detail=str(error))

    if outcome.status in (sdc.RECORDED, sdc.ALREADY_RECORDED):
        updated = services.ledger.record_remote_delivered(record, owner=owner)
        services.log("INFO", "eco_dashboard_remote_delivered",
                     {"operation_id": record.operation_id,
                      "remote_state": outcome.remote_state})
        return _Step(updated)

    if outcome.status == sdc.CAPABILITY_SUPERSEDED:
        return _operator(services, record, owner, phase="REMOTE_DELIVERED",
                         code="REMOTE_CAPABILITY_SUPERSEDED",
                         detail="the delivered capability is no longer authoritative")

    return _operator(services, record, owner, phase="REMOTE_DELIVERED",
                     code=str(outcome.error or outcome.status),
                     detail="the remote delivery transition was refused")


def _finalize(services: PublisherServices, record: DeliveryRecord, owner: str) -> _Step:
    updated = services.ledger.finalize(record, owner=owner)
    services.log("INFO", "eco_dashboard_delivery_finalized",
                 {"operation_id": record.operation_id,
                  "capability_digest": record.capability_digest,
                  "provider_message_id": record.provider_message_id})
    return _Step(updated, stop=True, outcome=INVOCATION.COMPLETED)
