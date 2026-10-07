"""Driver Eco Dashboard V1 — host delivery state model and stable identities.

This module is pure: no database, no network, no clock beyond what a caller
passes in. It defines

  * the durable host state vocabulary and, for every state, THE one safe next
    action after a restart;
  * the derivation of the stable identities that make reruns idempotent —
    the logical delivery identity, the remote publication `operation_id`, the
    opaque `subject_ref`, and the provider/message idempotency identity;
  * the two identities that make that idempotency key MEAN something: the
    provider backend/account scope it was issued against, and a fingerprint of
    the one exact message it names;
  * the secret-scrubbing helper every diagnostic string passes through.

WHY THE IDENTITIES ARE DERIVED AND NOT MINTED

A minted (random) operation id is lost with the process that minted it. The
durable row is still the authority — it is written before any remote call —
but deriving the id from the logical delivery means that even a host that lost
its row entirely converges on the SAME remote publication operation instead of
opening an unrelated second one. Derivation is defence in depth behind the
durable record, never a replacement for it.

The subject_ref is deliberately opaque: it is a digest, so the Worker's
authorization state never contains a driver identity, a client code or a
period label. The recipient e-mail address is not an input to it — recipient
identity belongs exclusively to the host delivery layer.
"""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Mapping

# --- domain separation ---------------------------------------------------------

#: Every derived identity is domain-separated so that two different derivations
#: can never collide even when their inputs coincide.
OPERATION_DOMAIN = "driver_eco_dashboard.host_delivery.operation.v1"
SUBJECT_DOMAIN = "driver_eco_dashboard.host_delivery.subject.v1"
PROVIDER_IDEMPOTENCY_DOMAIN = "driver_eco_dashboard.host_delivery.provider.v1"
RECIPIENT_IDENTITY_DOMAIN = "driver_eco_dashboard.host_delivery.recipient.v1"
#: The provider BACKEND/ACCOUNT scope one logical delivery is bound to before
#: its first submission. Derived from non-secret scope material only.
PROVIDER_BACKEND_DOMAIN = "driver_eco_dashboard.host_delivery.provider_backend.v1"
#: The exact intended outbound message one provider idempotency key names.
MESSAGE_IDENTITY_DOMAIN = "driver_eco_dashboard.host_delivery.message.v1"

#: Field separator for the length-prefixed derivation material. Same choice as
#: `spec/subject_binding_v1.md`; a value containing it is refused, never
#: normalised.
UNIT_SEPARATOR = "\u001F"

#: The Worker's own contract: `^[A-Za-z0-9_-]{16,64}$`.
OPERATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,64}$")

PERIOD_TYPES = ("weekly", "monthly")
SEND_SCOPES = ("normal", "test")


class DeliveryContractError(ValueError):
    """An input cannot be encoded unambiguously, or a transition is illegal."""


# --- states --------------------------------------------------------------------


class DeliveryState:
    """Durable host states. Each answers exactly one question: after a crash,
    what is the single safe next action?"""

    #: The local operation is durable. Nothing has been published yet, or a
    #: publish request may have been in flight, or the response may have been
    #: lost, or the raw bearer may have been received and lost before it was
    #: persisted. All four are ONE state because they have ONE safe action:
    #: re-issue the idempotent publish and let the Worker say what happened.
    PREPARED = "PREPARED"

    #: The Worker holds an authoritative grant this host cannot present. Only
    #: the explicit recovery operation may resolve it; re-publishing is not a
    #: legal move.
    BEARER_RECOVERY_REQUIRED = "BEARER_RECOVERY_REQUIRED"

    #: The raw bearer is durably persisted. Delivery intent has not been
    #: recorded yet.
    CAPABILITY_PERSISTED = "CAPABILITY_PERSISTED"

    #: The capability was handed to an EXTERNAL mailing lifecycle that owns the
    #: send accounting from here on — today the existing Eco Driving
    #: weekly/monthly e-mail jobs and their `eco_*_email_send_log`.
    #:
    #: WHY THIS IS A STATE AND NOT A FLAG. Without it, a delivery whose link had
    #: already been handed over would sit in `CAPABILITY_PERSISTED` forever, and
    #: that state's one safe next action is "record delivery intent, then submit
    #: to the provider". That is not merely untidy: it is a durable instruction
    #: to send a second, dashboard-specific message to a driver the
    #: authoritative Eco lifecycle has already mailed. The handoff is therefore
    #: recorded explicitly, and it terminates this ledger's automation.
    #:
    #: The raw bearer is RETAINED here while it is still usable, for the same
    #: reason it is retained in `PROVIDER_AMBIGUOUS`: this state's own contract
    #: is to answer "what is the link for this driver and this period?"
    #: identically on every rerun. Destroying a live bearer would force a
    #: capability rotation on each rerun of the Eco job — a different link for
    #: one logical delivery — which is precisely the uncontrolled capability
    #: churn the delivery identity exists to prevent.
    #:
    #: RETENTION IS BOUNDED BY THE GRANT'S OWN LIFETIME. An EXPIRED bearer
    #: answers that question with a URL that returns 410, so it is neither
    #: handed over nor kept: the delivery re-enters `BEARER_RECOVERY_REQUIRED`,
    #: which destroys it and rotates through the explicit recovery operation.
    #: See `LEGAL_TRANSITIONS`.
    EXTERNAL_MAILER_HANDOFF = "EXTERNAL_MAILER_HANDOFF"

    #: Host delivery intent and the remote `INTENT` phase are both recorded.
    #: No provider call has been made.
    DELIVERY_INTENT_RECORDED = "DELIVERY_INTENT_RECORDED"

    #: A provider submission MAY have happened. Written BEFORE the call, so
    #: this state is reached even if the process dies during it. The only safe
    #: action is reconciliation under the stable idempotency identity — never a
    #: blind resend.
    PROVIDER_SUBMISSION_PENDING = "PROVIDER_SUBMISSION_PENDING"

    #: Provider acceptance is established and recorded, with a message id.
    PROVIDER_ACCEPTED = "PROVIDER_ACCEPTED"

    #: Acceptance is genuinely unknowable under this provider's abstraction.
    #: Terminal for automation; an operator must reconcile.
    PROVIDER_AMBIGUOUS = "PROVIDER_AMBIGUOUS"

    #: The provider definitively refused. No message exists.
    PROVIDER_REJECTED = "PROVIDER_REJECTED"

    #: The remote publication operation is `DELIVERED`.
    REMOTE_DELIVERED = "REMOTE_DELIVERED"

    #: Terminal success: bearer material minimised.
    FINALIZED = "FINALIZED"

    #: THE CAPABILITY'S VALIDITY WINDOW HAS PASSED, AND THIS LEDGER HAS
    #: NOTHING LEFT TO DO ABOUT IT.
    #:
    #: WHY A STATE HAD TO BE ADDED. The lifecycle already had a terminal
    #: success (`FINALIZED`), a terminal handoff (`EXTERNAL_MAILER_HANDOFF`)
    #: and a terminal failure (`OPERATOR_REQUIRED`). It had no way to say
    #: "mailing ownership completed; the capability expired; no operator action
    #: is required; the audit metadata is retained" — and every existing state
    #: says something else. `FINALIZED` asserts a completed provider delivery
    #: this ledger may never have made, and a row left in
    #: `EXTERNAL_MAILER_HANDOFF` or `CAPABILITY_PERSISTED` is a row the
    #: operational surface still counts as open work, forever, over a grant
    #: that can no longer authorise anything. Overloading either would have
    #: been recording something untrue to tidy up something dead.
    #:
    #: WHAT IT ASSERTS: only that the grant this delivery held has expired and
    #: its raw bearer no longer exists here. It asserts NOTHING about whether a
    #: message was sent — `eco_*_email_send_log` remains the sole authority for
    #: that — and NOTHING about the historical snapshot, which is retained
    #: independently of the bearer that pointed at it.
    #:
    #: WHAT IT RETAINS: `capability_id`, `capability_digest`,
    #: `capability_expires_at`, `bearer_generation`, `bearer_cleared_at` and a
    #: `capability_retirement` metadata record, so an operator can still prove
    #: which grant this delivery held and when it stopped being usable. What it
    #: does not retain is the one thing that could open a driver's dashboard.
    #:
    #: IT IS NOT A DEAD END FOR A RESEND. The same logical delivery may still
    #: be re-mailed: the rerun leaves for `BEARER_RECOVERY_REQUIRED` and rotates
    #: through the explicit recovery operation, exactly as an expired handoff
    #: does, under the unchanged operation id, subject binding and payload
    #: digest — so the recovered capability still shows THAT period's snapshot
    #: and never a newer one.
    CAPABILITY_RETIRED = "CAPABILITY_RETIRED"

    #: Terminal failure that automation must not try to progress.
    OPERATOR_REQUIRED = "OPERATOR_REQUIRED"


ALL_STATES = (
    DeliveryState.PREPARED,
    DeliveryState.BEARER_RECOVERY_REQUIRED,
    DeliveryState.CAPABILITY_PERSISTED,
    DeliveryState.EXTERNAL_MAILER_HANDOFF,
    DeliveryState.DELIVERY_INTENT_RECORDED,
    DeliveryState.PROVIDER_SUBMISSION_PENDING,
    DeliveryState.PROVIDER_ACCEPTED,
    DeliveryState.PROVIDER_AMBIGUOUS,
    DeliveryState.PROVIDER_REJECTED,
    DeliveryState.REMOTE_DELIVERED,
    DeliveryState.FINALIZED,
    DeliveryState.CAPABILITY_RETIRED,
    DeliveryState.OPERATOR_REQUIRED,
)


class NextAction:
    """THE one safe next action for a persisted state."""

    PUBLISH = "PUBLISH"
    RECOVER_BEARER = "RECOVER_BEARER"
    RECORD_DELIVERY_INTENT = "RECORD_DELIVERY_INTENT"
    #: Terminal for this ledger. An external mailing lifecycle — the existing
    #: Eco weekly/monthly jobs and their send log — is authoritative for whether
    #: a message was sent, so nothing here may submit, reconcile or resend.
    EXTERNAL_MAILER_OWNS_DELIVERY = "EXTERNAL_MAILER_OWNS_DELIVERY"
    SUBMIT_TO_PROVIDER = "SUBMIT_TO_PROVIDER"
    RECONCILE_PROVIDER = "RECONCILE_PROVIDER"
    RECORD_REMOTE_DELIVERED = "RECORD_REMOTE_DELIVERED"
    CLEANUP_BEARER = "CLEANUP_BEARER"
    NONE = "NONE"
    #: Terminal, and terminal WITHOUT a complaint. The capability expired, its
    #: bearer no longer exists here, and nothing — automation or human — is
    #: expected to act. Distinct from `NONE` (terminal success) so a reader can
    #: tell "this delivery completed" from "this delivery's link simply ran
    #: out"; both mean the same thing operationally, which is: do nothing.
    CAPABILITY_EXPIRED_NO_ACTION = "CAPABILITY_EXPIRED_NO_ACTION"
    OPERATOR_RECONCILIATION = "OPERATOR_RECONCILIATION"
    OPERATOR_INVESTIGATION = "OPERATOR_INVESTIGATION"
    #: A definite rejection: no message exists, so a bounded retry under the
    #: SAME idempotency identity is legal. Beyond the bound it is an operator's
    #: decision, which is why the state itself never advances on its own.
    RETRY_OR_OPERATOR = "RETRY_OR_OPERATOR"


SAFE_NEXT_ACTION: Mapping[str, str] = {
    DeliveryState.PREPARED: NextAction.PUBLISH,
    DeliveryState.BEARER_RECOVERY_REQUIRED: NextAction.RECOVER_BEARER,
    DeliveryState.CAPABILITY_PERSISTED: NextAction.RECORD_DELIVERY_INTENT,
    DeliveryState.EXTERNAL_MAILER_HANDOFF: NextAction.EXTERNAL_MAILER_OWNS_DELIVERY,
    DeliveryState.DELIVERY_INTENT_RECORDED: NextAction.SUBMIT_TO_PROVIDER,
    DeliveryState.PROVIDER_SUBMISSION_PENDING: NextAction.RECONCILE_PROVIDER,
    DeliveryState.PROVIDER_ACCEPTED: NextAction.RECORD_REMOTE_DELIVERED,
    DeliveryState.PROVIDER_AMBIGUOUS: NextAction.OPERATOR_RECONCILIATION,
    DeliveryState.PROVIDER_REJECTED: NextAction.RETRY_OR_OPERATOR,
    DeliveryState.REMOTE_DELIVERED: NextAction.CLEANUP_BEARER,
    DeliveryState.FINALIZED: NextAction.NONE,
    DeliveryState.CAPABILITY_RETIRED: NextAction.CAPABILITY_EXPIRED_NO_ACTION,
    DeliveryState.OPERATOR_REQUIRED: NextAction.OPERATOR_INVESTIGATION,
}

#: STATES THAT ARE NOT OPERATIONAL WORK. `open_operations()` and the partial
#: index that backs it both exclude exactly this set, and nothing else.
#:
#: `FINALIZED` is a completed delivery. `CAPABILITY_RETIRED` is a delivery whose
#: link ran out; it holds no secret, needs no decision and has no next action.
#: Everything else — including a delivery an external mailer owns while its
#: capability is still live, which a rerun may legitimately ask for again — is
#: still something the system may have to answer for, and stays visible.
CLOSED_STATES = frozenset({
    DeliveryState.FINALIZED,
    DeliveryState.CAPABILITY_RETIRED,
})

#: States in which the raw bearer must still exist locally, because an
#: unresolved delivery may still have to construct or reconcile its message.
#:
#: `PROVIDER_AMBIGUOUS` is in this set deliberately. It is the one state whose
#: documented next action is a HUMAN reconciliation, and that human has to be
#: able to establish which link a possibly-sent message carried. Dropping the
#: bearer there would leave the operator with a question the row can no longer
#: answer, which is the opposite of what the state exists for.
BEARER_REQUIRED_STATES = frozenset({
    DeliveryState.CAPABILITY_PERSISTED,
    DeliveryState.EXTERNAL_MAILER_HANDOFF,
    DeliveryState.DELIVERY_INTENT_RECORDED,
    DeliveryState.PROVIDER_SUBMISSION_PENDING,
    DeliveryState.PROVIDER_ACCEPTED,
    DeliveryState.PROVIDER_AMBIGUOUS,
})

#: States in which the immutable provider submission identity — backend/account
#: scope, idempotency key and exact intended message — is already durable.
#: `DELIVERY_INTENT_RECORDED` is the FIRST of them, because binding happens
#: strictly before anything may submit, and every state reachable from it
#: therefore inherits the same binding.
PROVIDER_BOUND_STATES = frozenset({
    DeliveryState.DELIVERY_INTENT_RECORDED,
    DeliveryState.PROVIDER_SUBMISSION_PENDING,
    DeliveryState.PROVIDER_ACCEPTED,
    DeliveryState.PROVIDER_AMBIGUOUS,
    DeliveryState.PROVIDER_REJECTED,
    DeliveryState.REMOTE_DELIVERED,
    DeliveryState.FINALIZED,
})

TERMINAL_STATES = frozenset({
    DeliveryState.FINALIZED,
    DeliveryState.CAPABILITY_RETIRED,
    DeliveryState.EXTERNAL_MAILER_HANDOFF,
    DeliveryState.OPERATOR_REQUIRED,
    DeliveryState.PROVIDER_AMBIGUOUS,
})

#: THE STATES AN EXPIRED CAPABILITY MAY BE RETIRED FROM, and the reasoning for
#: every state that is NOT here.
#:
#: The rule is one question: with the grant expired, can this state's own next
#: action still be performed, and can a provider message still be awaiting an
#: answer? If neither, the row is holding dead secret material and dead
#: operational work, and `DeliveryLedger.retire_expired_capabilities` compacts
#: it. If either, the row has a real owner and is left alone.
#:
#:   CAPABILITY_PERSISTED       nothing is bound and nothing was sent. An
#:                              expired grant cannot become a message.
#:   EXTERNAL_MAILER_HANDOFF    the external lifecycle owns the send; this
#:                              ledger's only remaining service was answering
#:                              "what is the link?", which an expired grant
#:                              cannot do.
#:   DELIVERY_INTENT_RECORDED   the submission identity is bound but nothing was
#:                              ever submitted, which the sweep additionally
#:                              PROVES per row (`provider_attempts = 0` and
#:                              `provider_submitted_at IS NULL`) rather than
#:                              inferring from the state name. No message can
#:                              exist, and submitting now would mail a link that
#:                              answers 410.
#:   PROVIDER_REJECTED          a DEFINITE refusal: no message exists. The
#:                              retry this state allows would mail a dead link.
#:
#: DELIBERATELY ABSENT, each because it still has an owner:
#:
#:   PROVIDER_SUBMISSION_PENDING / PROVIDER_ACCEPTED  a message may exist and
#:       automation still owes it reconciliation or terminalisation; both clear
#:       the bearer as part of their own next action. Retiring them would
#:       discard the state that makes a resend safe.
#:   PROVIDER_AMBIGUOUS  acceptance is unknowable and a HUMAN owes the answer.
#:       It is already flagged `operator_action_required`, so it is visible
#:       work rather than silent retention, and nothing here may weaken the
#:       ambiguous-SMTP contract.
#:   OPERATOR_REQUIRED  likewise a human's, and it already destroyed the bearer
#:       unless the state it failed from legitimately needs one.
#:   REMOTE_DELIVERED  the correct terminal condition for a delivered message is
#:       `FINALIZED`, which is one legal automatic transition away and is what
#:       destroys its bearer. Retiring it would replace a true statement
#:       ("delivered") with a weaker one.
#:   PREPARED / BEARER_RECOVERY_REQUIRED  hold no bearer at all, and their next
#:       action (publish, recover) is still exactly right for a resend.
RETIRABLE_ON_EXPIRY_STATES = frozenset({
    DeliveryState.CAPABILITY_PERSISTED,
    DeliveryState.EXTERNAL_MAILER_HANDOFF,
    DeliveryState.DELIVERY_INTENT_RECORDED,
    DeliveryState.PROVIDER_REJECTED,
})

#: Legal successors. A transition outside this map is a programming error and
#: is refused before it reaches the database.
LEGAL_TRANSITIONS: Mapping[str, frozenset] = {
    DeliveryState.PREPARED: frozenset({
        DeliveryState.PREPARED,
        DeliveryState.CAPABILITY_PERSISTED,
        DeliveryState.BEARER_RECOVERY_REQUIRED,
        DeliveryState.OPERATOR_REQUIRED,
    }),
    DeliveryState.BEARER_RECOVERY_REQUIRED: frozenset({
        DeliveryState.BEARER_RECOVERY_REQUIRED,
        DeliveryState.CAPABILITY_PERSISTED,
        DeliveryState.OPERATOR_REQUIRED,
    }),
    DeliveryState.CAPABILITY_PERSISTED: frozenset({
        DeliveryState.CAPABILITY_PERSISTED,
        DeliveryState.DELIVERY_INTENT_RECORDED,
        DeliveryState.EXTERNAL_MAILER_HANDOFF,
        # The grant expired before anything was ever done with it — an SMTP
        # failure, a run that never came back, a client that was disabled. The
        # bearer is dead material and there is nothing to decide.
        DeliveryState.CAPABILITY_RETIRED,
        DeliveryState.OPERATOR_REQUIRED,
    }),
    # A handoff is terminal for the SEND. A rerun of the same logical delivery
    # may re-record it (the link handed over is the same one) and it may be
    # escalated, but it may never become a provider submission: the external
    # lifecycle already owns the send.
    #
    # THE ONE NON-TERMINAL MOVE, AND WHY IT IS NOT AN ESCAPE HATCH. A retained
    # capability EXPIRES. Once it has, the state's own contract — "answer 'what
    # is this driver's link?' identically on every rerun" — can no longer be
    # honoured by returning the retained bearer, because that answer is a URL
    # the Worker now refuses. The row is then holding unusable secret material
    # and owes the mailer a link it cannot give.
    #
    # `BEARER_RECOVERY_REQUIRED` is exactly the state for "this host cannot
    # present a usable grant for an operation the Worker still owns", and its
    # one safe next action — the explicit recovery operation — is the SAME
    # primitive that already rotates a lost bearer: one transaction at the
    # Worker that revokes the predecessor and mints a replacement under the
    # unchanged operation id, subject binding and payload digest. So an expired
    # handoff re-enters recovery, rotates once, and hands the fresh capability
    # over again under ONE logical delivery identity. The transition into it
    # destroys the expired bearer in the same statement (see
    # `DeliveryLedger.mark_bearer_recovery_required`), which is also why a
    # terminal row never keeps dead bearer material indefinitely.
    #
    # It is still not a path to a provider: nothing here can reach
    # `DELIVERY_INTENT_RECORDED`, so no second message can be sent, and the
    # send accounting stays with `eco_*_email_send_log`.
    DeliveryState.EXTERNAL_MAILER_HANDOFF: frozenset({
        DeliveryState.EXTERNAL_MAILER_HANDOFF,
        DeliveryState.BEARER_RECOVERY_REQUIRED,
        # ...AND THE MOVE THAT DOES NOT NEED A RERUN. The transition above
        # happens only when somebody asks for this link again; `CAPABILITY_RETIRED`
        # is what happens when nobody ever does. Without it a handed-off
        # delivery whose grant expired years ago would keep a live bearer and
        # keep counting as open work purely because that historical period was
        # never re-mailed.
        DeliveryState.CAPABILITY_RETIRED,
        DeliveryState.OPERATOR_REQUIRED,
    }),
    DeliveryState.DELIVERY_INTENT_RECORDED: frozenset({
        DeliveryState.DELIVERY_INTENT_RECORDED,
        DeliveryState.PROVIDER_SUBMISSION_PENDING,
        # Bound but never submitted, and the grant has since expired. The sweep
        # proves "never submitted" from the row rather than from the state name
        # before it may use this edge.
        DeliveryState.CAPABILITY_RETIRED,
        DeliveryState.OPERATOR_REQUIRED,
    }),
    DeliveryState.PROVIDER_SUBMISSION_PENDING: frozenset({
        DeliveryState.PROVIDER_SUBMISSION_PENDING,
        DeliveryState.PROVIDER_ACCEPTED,
        DeliveryState.PROVIDER_REJECTED,
        DeliveryState.PROVIDER_AMBIGUOUS,
        DeliveryState.OPERATOR_REQUIRED,
    }),
    DeliveryState.PROVIDER_ACCEPTED: frozenset({
        DeliveryState.PROVIDER_ACCEPTED,
        DeliveryState.REMOTE_DELIVERED,
        DeliveryState.OPERATOR_REQUIRED,
    }),
    # A definite rejection created no message, so a retry re-enters submission
    # under the same idempotency identity.
    DeliveryState.PROVIDER_REJECTED: frozenset({
        DeliveryState.PROVIDER_REJECTED,
        DeliveryState.PROVIDER_SUBMISSION_PENDING,
        # Once the grant has expired the retry above would mail a link that
        # answers 410, so the definite refusal has nothing left to retry with.
        DeliveryState.CAPABILITY_RETIRED,
        DeliveryState.OPERATOR_REQUIRED,
    }),
    # Ambiguity is never resolved by automation. Only an operator-driven
    # reconciliation may move it, and only onto a known answer.
    DeliveryState.PROVIDER_AMBIGUOUS: frozenset({
        DeliveryState.PROVIDER_AMBIGUOUS,
        DeliveryState.PROVIDER_ACCEPTED,
        DeliveryState.OPERATOR_REQUIRED,
    }),
    DeliveryState.REMOTE_DELIVERED: frozenset({
        DeliveryState.REMOTE_DELIVERED,
        DeliveryState.FINALIZED,
        DeliveryState.OPERATOR_REQUIRED,
    }),
    DeliveryState.FINALIZED: frozenset({DeliveryState.FINALIZED}),
    # TERMINAL FOR HOUSEKEEPING, NOT FOR THE DELIVERY IDENTITY.
    #
    # Re-running the sweep must be a no-op, so the state is its own successor.
    # A genuine resend of THIS reporting period is still legal and takes the
    # same route an expired handoff takes: `BEARER_RECOVERY_REQUIRED`, then the
    # explicit recovery operation, which rotates the grant under the UNCHANGED
    # operation id, subject binding and payload digest. The replacement
    # capability therefore still resolves to this period's snapshot — a resend
    # can never be retargeted at a newer report by going through here.
    #
    # It cannot reach a provider state. Nothing here binds a submission
    # identity, so an expired delivery can never become a second message.
    DeliveryState.CAPABILITY_RETIRED: frozenset({
        DeliveryState.CAPABILITY_RETIRED,
        DeliveryState.BEARER_RECOVERY_REQUIRED,
        DeliveryState.OPERATOR_REQUIRED,
    }),
    DeliveryState.OPERATOR_REQUIRED: frozenset({DeliveryState.OPERATOR_REQUIRED}),
}

#: The retirable set and the transition map must agree, or the sweep would
#: either refuse a state it is supposed to compact or attempt an edge the
#: contract does not authorise. Checked at import, where a drift is a startup
#: failure rather than a runtime surprise on one client's ledger.
def _assert_retirement_edges_exist() -> None:  # pragma: no cover - import-time
    for state in RETIRABLE_ON_EXPIRY_STATES:
        if DeliveryState.CAPABILITY_RETIRED not in LEGAL_TRANSITIONS[state]:
            raise AssertionError(
                f"{state} is retirable but cannot legally reach CAPABILITY_RETIRED")


_assert_retirement_edges_exist()


# --- operator recovery ---------------------------------------------------------

#: THE ONE TRANSITION AUTOMATION MAY NEVER MAKE, AND A REVIEWED OPERATOR MAY.
#:
#: `OPERATOR_REQUIRED` is terminal in `LEGAL_TRANSITIONS` on purpose: it means
#: automation established that it cannot make safe progress, and a state
#: machine that could walk back out of it by itself would simply be a retry
#: loop with extra steps. That stays exactly as it was — this map is separate,
#: it is not consulted by `assert_legal_transition`, and nothing in the
#: publisher lifecycle reads it.
#:
#: What it authorises is the narrow case a human has actually adjudicated: an
#: operation that failed BEFORE any remote effect existed, whose cause was a
#: defect outside the delivery itself, and which is therefore still exactly the
#: `PREPARED` operation it was before the failed attempt. Sending it back to
#: `PREPARED` re-arms the SAME publish of the SAME bytes under the SAME
#: operation id; it invents no identity and reverses no remote fact.
OPERATOR_RECOVERY_TRANSITIONS: Mapping[str, frozenset] = {
    DeliveryState.OPERATOR_REQUIRED: frozenset({DeliveryState.PREPARED}),
}

#: The PUBLICATION-phase refusals a reviewed operator may re-arm, and the whole
#: reason the code is part of the eligibility test rather than a comment.
#:
#: `PAYLOAD_NOT_CANONICAL` is the Worker's 422 for a body it will not accept.
#: The route computes and compares the digest, decodes, validates and checks
#: canonical form — and every one of those refusals happens BEFORE
#: `publishSnapshot`, so the operation, the R2 object and the grant provably do
#: not exist. That is what makes re-arming safe: there is no remote state to
#: reconcile, only a host row that says a publish was refused.
#:
#: It is deliberately not a general "retryable publication failure" list. A
#: refusal whose remote effect is unknown, or one that names a terminal remote
#: operation, is not in it and must not be added without the same reasoning.
OPERATOR_RECOVERABLE_PUBLICATION_FAILURE_CODES = frozenset({
    "PAYLOAD_NOT_CANONICAL",
})


def safe_next_action(state: str) -> str:
    try:
        return SAFE_NEXT_ACTION[state]
    except KeyError:
        raise DeliveryContractError(f"unknown delivery state: {state!r}") from None


def assert_legal_transition(current: str, target: str) -> None:
    allowed = LEGAL_TRANSITIONS.get(current)
    if allowed is None:
        raise DeliveryContractError(f"unknown delivery state: {current!r}")
    if target not in allowed:
        raise DeliveryContractError(f"illegal transition {current} -> {target}")


def assert_legal_operator_recovery(current: str, target: str) -> None:
    """Refuse anything the operator recovery surface is not authorised to do.

    Deliberately a SECOND function rather than a widening of
    `assert_legal_transition`: the automation state machine must keep refusing
    this move, so the authorisation lives where only the operator path can
    reach it.
    """
    allowed = OPERATOR_RECOVERY_TRANSITIONS.get(current)
    if allowed is None:
        raise DeliveryContractError(
            f"no operator recovery is defined from state {current!r}")
    if target not in allowed:
        raise DeliveryContractError(
            f"illegal operator recovery {current} -> {target}")


# --- logical delivery identity -------------------------------------------------


@dataclass(frozen=True)
class DeliveryIdentity:
    """The tuple that makes one logical dashboard delivery.

    Two invocations that agree on this tuple are the SAME logical delivery and
    must converge on one publication and at most one recipient message. The
    recipient address is deliberately not part of it: a recipient change is a
    conflict to refuse, not a new delivery to create.
    """

    client_id: str
    identity_key: str
    period_type: str
    period_start_date: date
    period_end_date: date
    send_scope: str = "normal"

    def __post_init__(self) -> None:
        if not str(self.client_id).strip():
            raise DeliveryContractError("delivery identity requires a client_id")
        if not str(self.identity_key).strip():
            raise DeliveryContractError("delivery identity requires an identity_key")
        if self.period_type not in PERIOD_TYPES:
            raise DeliveryContractError("period_type must be 'weekly' or 'monthly'")
        if self.send_scope not in SEND_SCOPES:
            raise DeliveryContractError("send_scope must be 'normal' or 'test'")
        if not isinstance(self.period_start_date, date) or not isinstance(self.period_end_date, date):
            raise DeliveryContractError("delivery identity requires real dates")
        if self.period_end_date <= self.period_start_date:
            raise DeliveryContractError("period_end_date must be after period_start_date")

    def material(self, domain: str) -> bytes:
        """Length-prefixed, domain-separated encoding.

        The length prefixes are what make it unambiguous: a separator inside a
        value cannot imitate a different tuple, because each field's UTF-8 byte
        length is part of the material. Same construction as the Worker's
        subject binding (`spec/subject_binding_v1.md`).
        """
        fields = (
            str(self.client_id),
            str(self.identity_key),
            self.period_type,
            self.period_start_date.isoformat(),
            self.period_end_date.isoformat(),
            self.send_scope,
        )
        parts = [domain]
        for value in fields:
            if UNIT_SEPARATOR in value:
                raise DeliveryContractError("DELIVERY_IDENTITY_INVALID_INPUT")
            parts.append(str(len(value.encode("utf-8"))))
            parts.append(value)
        return UNIT_SEPARATOR.join(parts).encode("utf-8")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def derive_operation_id(identity: DeliveryIdentity) -> str:
    """The remote publication operation id for one logical delivery.

    43 base64url characters of one SHA-256 — inside the Worker's
    `^[A-Za-z0-9_-]{16,64}$` contract, opaque, and useless as a credential
    (the Worker treats operation ids as identifiers, never as authorization).
    """
    value = _b64url(hashlib.sha256(identity.material(OPERATION_DOMAIN)).digest())
    if not OPERATION_ID_PATTERN.match(value):  # pragma: no cover - defensive
        raise DeliveryContractError("derived operation id violates the Worker contract")
    return value


def derive_subject_ref(identity: DeliveryIdentity) -> str:
    """The opaque subject reference the Worker binds the object to.

    A digest, so no driver identity, client code or period label ever enters
    Worker/D1 authorization state.
    """
    return _b64url(hashlib.sha256(identity.material(SUBJECT_DOMAIN)).digest())


def derive_provider_idempotency_key(operation_id: str) -> str:
    """The stable provider/message idempotency identity for one delivery.

    Derived from the publication operation id and NOTHING else — not from the
    attempt number, not from a clock, not from the bearer generation. One
    logical dashboard delivery therefore has exactly one provider identity for
    its whole life, which is what makes "accepted but response lost" safe.
    """
    if not isinstance(operation_id, str) or not OPERATION_ID_PATTERN.match(operation_id):
        raise DeliveryContractError("provider idempotency requires a valid operation id")
    material = UNIT_SEPARATOR.join(
        (PROVIDER_IDEMPOTENCY_DOMAIN, str(len(operation_id)), operation_id)
    )
    return "eco-dash-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:40]


def _length_prefixed(domain: str, fields: Iterable[str]) -> bytes:
    """The one unambiguous encoding used by every derivation in this module."""
    parts = [domain]
    for value in fields:
        text = str(value)
        if UNIT_SEPARATOR in text:
            raise DeliveryContractError("DERIVATION_INPUT_INVALID")
        parts.append(str(len(text.encode("utf-8"))))
        parts.append(text)
    return UNIT_SEPARATOR.join(parts).encode("utf-8")


def derive_provider_backend_id(provider_type: str, account_scope: str,
                               endpoint_scope: str) -> str:
    """The stable, NON-SECRET identity of one provider backend/account scope.

    THE POINT. A logical delivery is bound to this value before its first
    submission, and a later invocation that presents a different one is refused
    before any provider is contacted. Without it, "the same idempotency key"
    means nothing across a restart that reconfigured the backend: provider A
    holds the message and provider B has never heard of the key, so B would
    happily create a second one.

    WHAT IT MUST AND MUST NOT DEPEND ON. It is derived from provider TYPE, the
    ACCOUNT/tenant scope and the ENDPOINT/environment scope — the three things
    that determine whether a key can identify an already-accepted message. It
    is never derived from a credential: rotating a password does not move the
    account, so a rotation must not look like a different delivery backend,
    and a secret must never end up in a persisted identity in the first place.
    """
    for name, value in (("provider_type", provider_type),
                        ("account_scope", account_scope),
                        ("endpoint_scope", endpoint_scope)):
        if not str(value or "").strip():
            raise DeliveryContractError(f"provider backend scope requires {name}")
    material = _length_prefixed(
        PROVIDER_BACKEND_DOMAIN,
        (str(provider_type).strip(), str(account_scope).strip(),
         str(endpoint_scope).strip()),
    )
    return "pbk_" + hashlib.sha256(material).hexdigest()[:40]


def derive_message_fingerprint(*, recipient_email: str, subject: str,
                               message_id: str, html_body: str,
                               text_body: str) -> str:
    """The identity of ONE exact intended outbound message.

    A provider idempotency key promises "this is the same message". That
    promise is only true if the host can prove it, so the whole rendered
    message — recipient, subject, `Message-ID` and both bodies — is folded into
    one SHA-256 before the first submission and compared before every later
    one. Because the capability link lives in the bodies, a changed dashboard
    base URL, a changed template and a replacement bearer generation are all
    the same kind of event here: a different message, refused under an
    already-bound key.

    It is a one-way digest, so persisting it persists no bearer — exactly the
    same reasoning as `capability_digest`.
    """
    material = _length_prefixed(
        MESSAGE_IDENTITY_DOMAIN,
        (normalise_email(recipient_email), str(subject), str(message_id),
         str(html_body), str(text_body)),
    )
    return hashlib.sha256(material).hexdigest()


def derive_recipient_identity(identity: DeliveryIdentity, recipient_email: str) -> str:
    """A non-secret, stable reference to the intended recipient.

    Logs and operational reports name THIS, never the address. It binds the
    address so that a silent recipient change is detectable even if the stored
    address were edited.
    """
    address = normalise_email(recipient_email)
    material = UNIT_SEPARATOR.join(
        (RECIPIENT_IDENTITY_DOMAIN, str(len(address.encode("utf-8"))), address)
    ).encode("utf-8")
    return "rcpt_" + hashlib.sha256(
        identity.material(RECIPIENT_IDENTITY_DOMAIN) + material
    ).hexdigest()[:32]


_EMAIL_PATTERN = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")


def normalise_email(value: str) -> str:
    """One canonical form, so a recipient conflict cannot hide behind case.

    Only the domain is case-folded: the local part is case-sensitive per
    RFC 5321, and folding it would let two genuinely different mailboxes
    compare equal.
    """
    text = str(value or "").strip()
    if not _EMAIL_PATTERN.match(text):
        raise DeliveryContractError("RECIPIENT_EMAIL_INVALID")
    local, _, domain = text.rpartition("@")
    return f"{local}@{domain.lower()}"


# --- secret scrubbing ----------------------------------------------------------

REDACTED = "[REDACTED]"


def scrub_secrets(text: object, secrets: Iterable[str]) -> str:
    """Remove known secret material from any string that may be persisted.

    Applied to every diagnostic value the ledger writes and to every log line
    the publisher emits. The database additionally refuses a stored bearer in
    the diagnostic columns, so this is the first of two independent controls,
    not the only one.
    """
    value = "" if text is None else str(text)
    for secret in secrets:
        if secret and isinstance(secret, str) and len(secret) >= 8 and secret in value:
            value = value.replace(secret, REDACTED)
    return value
