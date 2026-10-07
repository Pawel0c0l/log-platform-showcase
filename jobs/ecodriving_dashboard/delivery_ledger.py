"""Driver Eco Dashboard V1 — durable host delivery ledger.

One row of `public.eco_dashboard_delivery_operation` per LOGICAL dashboard
delivery, in the client business database (migrations
`db/client_business/049_eco_dashboard_delivery_operation.sql` and its forward
delta `050_eco_dashboard_external_mailer_ownership.sql`, which adds the
external-mailer ownership contract this module's `external_mailer` column
depends on).

THE THREE PROPERTIES THIS MODULE EXISTS FOR

1. **Durability before every remote effect.** `ensure_operation()` commits the
   operation id, the subject reference, the exact canonical payload digest and
   the recipient binding BEFORE the publisher API is called. A process that
   dies immediately afterwards finds the same logical operation instead of
   minting a second, unrelated publication.

2. **Durable ownership, not an in-memory mutex.** `claim()` is a
   compare-and-set on the row itself. Two schedulers, two manual runs or
   thirty-two concurrent invocations of the same logical delivery produce one
   winner; every loser performs no work at all and mutates nothing. Every
   subsequent state mutation re-asserts `(state, lease_owner, lease still
   unexpired)` in the same statement that writes, so neither a decision made
   against a row that has moved on nor one made by a holder whose lease has
   since expired can be applied. `renew()` is the same predicate offered
   ahead of a remote effect.

2b. **One immutable provider submission identity.** `record_delivery_intent()`
   binds the idempotency key, the provider backend/account scope and a
   fingerprint of the exact intended message together, before anything may
   submit. A later invocation presenting a different backend or a different
   message is refused before a provider is contacted rather than producing a
   second delivery under the same key.

3. **Secrets stay out of diagnostics.** Every diagnostic string is scrubbed in
   Python, and the table's
   `chk_eco_dashboard_delivery_operation_no_bearer_in_diagnostics` refuses a
   stored raw bearer inside `failure_code`, `failure_detail` or
   `metadata_json` regardless of what the application believed. Two
   independent controls; neither is trusted alone.

Every method here is one self-contained committed unit of work — the ledger
requires an autocommit connection, because "persisted" must mean persisted the
moment the call returns and not at some later commit the caller controls.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

from jobs.ecodriving_dashboard.delivery_contract import (
    BEARER_REQUIRED_STATES,
    CLOSED_STATES,
    OPERATOR_RECOVERABLE_PUBLICATION_FAILURE_CODES,
    PROVIDER_BOUND_STATES,
    RETIRABLE_ON_EXPIRY_STATES,
    DeliveryContractError,
    DeliveryIdentity,
    DeliveryState,
    assert_legal_operator_recovery,
    assert_legal_transition,
    derive_provider_idempotency_key,
    normalise_email,
    safe_next_action,
    scrub_secrets,
)

DEFAULT_TABLE = "public.eco_dashboard_delivery_operation"
DEFAULT_LEASE_SECONDS = 300
#: Bounded so a pathological provider error cannot fill the column.
MAX_FAILURE_DETAIL = 500

_IDENT = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*(\.[a-zA-Z_][a-zA-Z0-9_]*)?$")

COLUMNS = (
    "delivery_id", "operation_id", "client_id", "identity_key", "period_type",
    "period_start_date", "period_end_date", "send_scope", "subject_ref",
    "payload_digest", "recipient_identity", "recipient_email", "state",
    "external_mailer",
    "capability_id", "capability_secret", "capability_digest",
    "capability_expires_at", "bearer_generation", "bearer_persisted_at",
    "bearer_cleared_at", "provider_name", "provider_idempotency_key",
    "provider_backend_id", "provider_message_fingerprint",
    "provider_bound_capability_id", "provider_bound_bearer_generation",
    "provider_message_id", "provider_attempts", "provider_submitted_at",
    "provider_accepted_at", "remote_delivered_at", "finalized_at",
    "failure_phase", "failure_code", "failure_detail",
    "operator_action_required", "lease_owner", "lease_expires_at",
    "attempt_count", "last_run_id", "created_at", "updated_at", "metadata_json",
)

#: Named explicitly, never `SELECT *`: a narrowed projection must behave in
#: tests exactly as in production, which is the same rule the Worker's
#: `store.js` follows for the same reason.
_PROJECTION = ", ".join(COLUMNS)


class LedgerConflict(RuntimeError):
    """A durable fact contradicts what this invocation was asked to do.

    Carries a machine-readable `code` and never the conflicting secret value.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def capability_digest(raw_capability: str) -> str:
    """Non-secret audit identity of a raw bearer.

    Stored so an operator can prove WHICH grant a message carried after the
    raw value has been destroyed. It is a one-way digest of a 256-bit CSPRNG
    value, so it is not a route back to the bearer.
    """
    return hashlib.sha256(str(raw_capability).encode("utf-8")).hexdigest()


@dataclass(frozen=True, repr=False)
class DeliveryRecord:
    """An immutable snapshot of one ledger row.

    `capability_secret` is present only while the lifecycle still needs it.
    `__repr__` is overridden so the bearer cannot reach a traceback, a log
    line or a test failure message by accident.
    """

    row: Mapping[str, Any]

    def __getattr__(self, name: str) -> Any:
        # `row` itself is a real field, so this only ever runs for column
        # names. The underscore guard keeps copy/pickle protocol lookups from
        # recursing through the mapping.
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return object.__getattribute__(self, "row")[name]
        except KeyError:
            raise AttributeError(name) from None

    def __repr__(self) -> str:  # pragma: no cover - defensive only
        return (
            "DeliveryRecord(operation_id=%r, state=%r, bearer_generation=%r, "
            "provider_message_id=%r)"
            % (self.row.get("operation_id"), self.row.get("state"),
               self.row.get("bearer_generation"), self.row.get("provider_message_id"))
        )

    @property
    def next_action(self) -> str:
        return safe_next_action(self.row["state"])

    @property
    def has_bearer(self) -> bool:
        return bool(self.row.get("capability_secret"))

    def public_summary(self) -> dict:
        """Everything an operator needs, and nothing that must not be printed.

        Deliberately excludes `capability_secret` and the recipient address:
        the address is an operational identifier only when a human asks for it
        explicitly, and a routine job result is not that moment.
        """
        return {
            "delivery_id": str(self.row["delivery_id"]),
            "operation_id": self.row["operation_id"],
            "state": self.row["state"],
            "next_action": self.next_action,
            "external_mailer": self.row.get("external_mailer"),
            "period_type": self.row["period_type"],
            "period_start_date": self.row["period_start_date"].isoformat(),
            "period_end_date": self.row["period_end_date"].isoformat(),
            "recipient_identity": self.row["recipient_identity"],
            "payload_digest": self.row["payload_digest"],
            "capability_id": self.row["capability_id"],
            "capability_digest": self.row["capability_digest"],
            "bearer_generation": self.row["bearer_generation"],
            "bearer_retained": self.has_bearer,
            "provider_name": self.row["provider_name"],
            "provider_idempotency_key": self.row["provider_idempotency_key"],
            "provider_backend_id": self.row["provider_backend_id"],
            "provider_message_fingerprint": self.row["provider_message_fingerprint"],
            "provider_bound_capability_id": self.row["provider_bound_capability_id"],
            "provider_bound_bearer_generation":
                self.row["provider_bound_bearer_generation"],
            "provider_message_id": self.row["provider_message_id"],
            "provider_attempts": self.row["provider_attempts"],
            "failure_phase": self.row["failure_phase"],
            "failure_code": self.row["failure_code"],
            "failure_detail": self.row["failure_detail"],
            "operator_action_required": bool(self.row["operator_action_required"]),
            "attempt_count": self.row["attempt_count"],
        }


# --- operator recovery ---------------------------------------------------------

#: The only failure phase this recovery accepts. A refusal in any later phase
#: happened after publication succeeded, which is a different situation with a
#: different safe action.
OPERATOR_RECOVERY_PHASE = "PUBLICATION"

#: Where the attestation is recorded. A manual state mutation with no durable
#: record of who made it and on what evidence is not an operational control.
OPERATOR_RECOVERY_METADATA_KEY = "operator_recovery"

#: The FULL eligibility contract for sending an `OPERATOR_REQUIRED` row back to
#: `PREPARED`, written once as `(name, SQL predicate, python predicate)`.
#:
#: WHY BOTH FORMS, AND WHY THEY ARE THE SAME LIST.
#:
#: The Python side answers the operator's question — "which specific fact makes
#: this row ineligible?" — which a bare "0 rows updated" cannot. The SQL side is
#: what actually decides, in the same statement that writes, so a row that
#: changed between the dry run and the execution is refused by the database
#: rather than by a stale snapshot. Keeping them adjacent is what makes it
#: checkable that the report and the guard agree; the recovery tests assert
#: exactly that, guard by guard.
#:
#: The list is fail-closed by construction: it names every column that could
#: carry evidence of a remote effect and requires each one to be absent. It does
#: NOT infer remote cleanup, and it never asks the Worker anything — it only
#: refuses to act unless the host's own durable facts prove that no remote
#: effect was ever created.
def _lease_is_live(row: Mapping[str, Any]) -> bool:
    """Does some invocation still durably own this row right now?"""
    expires = row.get("lease_expires_at")
    if not row.get("lease_owner") or expires is None:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires > datetime.now(timezone.utc)


OPERATOR_RECOVERY_GUARDS: tuple = (
    ("state_is_operator_required",
     "state = %(recovery_state)s",
     lambda r: r.get("state") == DeliveryState.OPERATOR_REQUIRED),
    ("operator_action_required",
     "operator_action_required IS TRUE",
     lambda r: bool(r.get("operator_action_required"))),
    ("failure_phase_is_publication",
     "failure_phase = %(recovery_phase)s",
     lambda r: r.get("failure_phase") == OPERATOR_RECOVERY_PHASE),
    ("failure_code_is_recoverable",
     "failure_code = ANY(%(recovery_codes)s)",
     lambda r: r.get("failure_code") in OPERATOR_RECOVERABLE_PUBLICATION_FAILURE_CODES),
    # No capability was ever minted for this operation.
    ("no_capability_id", "capability_id IS NULL", lambda r: r.get("capability_id") is None),
    ("no_bearer_held", "capability_secret IS NULL", lambda r: r.get("capability_secret") is None),
    ("no_capability_digest", "capability_digest IS NULL",
     lambda r: r.get("capability_digest") is None),
    ("no_capability_expiry", "capability_expires_at IS NULL",
     lambda r: r.get("capability_expires_at") is None),
    ("bearer_generation_is_zero", "bearer_generation = 0",
     lambda r: int(r.get("bearer_generation") or 0) == 0),
    ("bearer_never_persisted", "bearer_persisted_at IS NULL",
     lambda r: r.get("bearer_persisted_at") is None),
    ("bearer_never_cleared", "bearer_cleared_at IS NULL",
     lambda r: r.get("bearer_cleared_at") is None),
    # NOT A GUARD: `external_mailer`. Ownership is IMMUTABLE OWNERSHIP
    # METADATA, bound by the INSERT that created the row (migration 050) and
    # refused every later change by the row guard trigger in both directions.
    # It says WHICH lifecycle would send this delivery, never that any sending
    # lifecycle DID anything — so requiring it to be NULL made every
    # externally-owned dashboard delivery permanently unrecoverable for a fact
    # that carries no remote effect at all.
    #
    # The external branch's own activity is still proven absent, by the
    # capability guards immediately above rather than by the ownership column:
    # `EXTERNAL_MAILER_HANDOFF` is the ONLY state in which an external mailer
    # has been given anything, and migration 050's
    # `chk_..._bearer_present` makes it unreachable without
    # `capability_id`, `capability_secret`, `capability_digest`,
    # `bearer_persisted_at` and `bearer_generation >= 1` — each of which is
    # separately required here to be absent or zero, and a row parked from that
    # state would additionally carry `bearer_cleared_at`. A row that satisfies
    # this list has therefore never handed a link to its external mailer, and
    # the recovery leaves the ownership binding exactly as the INSERT decided
    # it: `external_mailer` appears in no SET list, and the trigger would
    # refuse it if it did.
    # No provider submission identity was ever bound, so no message can exist
    # under an idempotency key this reset would orphan.
    ("no_provider_binding", "provider_idempotency_key IS NULL",
     lambda r: r.get("provider_idempotency_key") is None),
    ("no_provider_name", "provider_name IS NULL", lambda r: r.get("provider_name") is None),
    ("no_provider_backend", "provider_backend_id IS NULL",
     lambda r: r.get("provider_backend_id") is None),
    ("no_message_fingerprint", "provider_message_fingerprint IS NULL",
     lambda r: r.get("provider_message_fingerprint") is None),
    ("no_bound_capability", "provider_bound_capability_id IS NULL",
     lambda r: r.get("provider_bound_capability_id") is None),
    ("no_bound_generation", "provider_bound_bearer_generation IS NULL",
     lambda r: r.get("provider_bound_bearer_generation") is None),
    ("no_provider_message", "provider_message_id IS NULL",
     lambda r: r.get("provider_message_id") is None),
    ("no_provider_attempt", "provider_attempts = 0",
     lambda r: int(r.get("provider_attempts") or 0) == 0),
    ("never_submitted", "provider_submitted_at IS NULL",
     lambda r: r.get("provider_submitted_at") is None),
    ("never_accepted", "provider_accepted_at IS NULL",
     lambda r: r.get("provider_accepted_at") is None),
    # No remote delivery phase was ever recorded, and the row is not terminal.
    ("never_remote_delivered", "remote_delivered_at IS NULL",
     lambda r: r.get("remote_delivered_at") is None),
    ("never_finalized", "finalized_at IS NULL", lambda r: r.get("finalized_at") is None),
    # Nobody else is durably working this row. An EXPIRED lease is not
    # ownership — `mark_operator_required` does not release one, so the real
    # failed row still names its last owner — but a LIVE one means an
    # invocation believes it owns the delivery, and two writers is exactly what
    # every other predicate in this table exists to prevent.
    ("no_live_lease",
     "(lease_owner IS NULL OR lease_expires_at IS NULL OR lease_expires_at < now())",
     lambda r: not _lease_is_live(r)),
)


@dataclass(frozen=True)
class RecoveryAssessment:
    """Why one row may or may not be re-armed. Carries no secret."""

    delivery_id: str
    operation_id: str
    payload_digest: str
    state: str
    recoverable: bool
    #: `(guard name, satisfied)` in declaration order.
    guards: tuple
    #: Only the guards that refused, for a short operator-facing message.
    blocked_by: tuple

    def as_dict(self) -> dict:
        return {
            "delivery_id": self.delivery_id,
            "operation_id": self.operation_id,
            "payload_digest": self.payload_digest,
            "state": self.state,
            "recoverable": self.recoverable,
            "guards": {name: satisfied for name, satisfied in self.guards},
            "blocked_by": list(self.blocked_by),
        }


def assess_operator_recovery(record: DeliveryRecord) -> RecoveryAssessment:
    """Evaluate every eligibility guard against one row. Reads nothing, writes nothing.

    This is the dry run. It is deliberately total — every guard is evaluated
    even after the first refusal — because an operator deciding what to do next
    needs the whole picture, not the first objection.
    """
    row = record.row
    guards = tuple((name, bool(predicate(row)))
                   for name, _sql, predicate in OPERATOR_RECOVERY_GUARDS)
    blocked = tuple(name for name, satisfied in guards if not satisfied)
    return RecoveryAssessment(
        delivery_id=str(row["delivery_id"]),
        operation_id=row["operation_id"],
        payload_digest=row["payload_digest"],
        state=row["state"],
        recoverable=not blocked,
        guards=guards,
        blocked_by=blocked,
    )


class DeliveryLedger:
    """Durable host state for the dashboard publication/delivery lifecycle."""

    def __init__(self, conn, *, table: str = DEFAULT_TABLE,
                 secrets: Iterable[str] = ()) -> None:
        if not _IDENT.match(table or ""):
            raise ValueError(f"Unsafe SQL identifier: {table!r}")
        self._table = table
        self._conn = conn
        #: Values that must never be persisted into a diagnostic column. The
        #: publisher adds the machine credential and every raw bearer it holds.
        self._secrets = [s for s in secrets if s]
        if not getattr(conn, "autocommit", False):
            # "Durably persisted before the remote call" is the whole contract.
            # A caller-controlled transaction would make that a promise about
            # some later commit instead of about this call.
            conn.autocommit = True

    # --- secrets ---------------------------------------------------------------

    @property
    def secrets(self) -> tuple:
        """Every secret this ledger has been told about, for scrubbing only."""
        return tuple(self._secrets)

    def register_secret(self, value: Optional[str]) -> None:
        if value and value not in self._secrets:
            self._secrets.append(value)

    def _scrub(self, text: object) -> Optional[str]:
        if text is None:
            return None
        cleaned = scrub_secrets(text, self._secrets)
        cleaned = "".join(ch for ch in cleaned if ch >= " " or ch == "\n")
        return cleaned[:MAX_FAILURE_DETAIL]

    # --- reads -----------------------------------------------------------------

    def _fetch_one(self, sql: str, params: tuple) -> Optional[DeliveryRecord]:
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        if row is None:
            return None
        if isinstance(row, Mapping):
            return DeliveryRecord(dict(row))
        return DeliveryRecord(dict(zip(COLUMNS, row)))

    def load(self, delivery_id) -> Optional[DeliveryRecord]:
        return self._fetch_one(
            f"SELECT {_PROJECTION} FROM {self._table} WHERE delivery_id = %s",
            (str(delivery_id),),
        )

    def find_by_operation(self, operation_id: str) -> Optional[DeliveryRecord]:
        return self._fetch_one(
            f"SELECT {_PROJECTION} FROM {self._table} WHERE operation_id = %s",
            (operation_id,),
        )

    def find_by_identity(self, identity: DeliveryIdentity) -> Optional[DeliveryRecord]:
        return self._fetch_one(
            f"""SELECT {_PROJECTION} FROM {self._table}
                 WHERE client_id = %s AND identity_key = %s AND period_type = %s
                   AND period_start_date = %s AND period_end_date = %s
                   AND send_scope = %s""",
            (identity.client_id, identity.identity_key, identity.period_type,
             identity.period_start_date, identity.period_end_date, identity.send_scope),
        )

    # --- creation --------------------------------------------------------------

    def ensure_operation(
        self,
        identity: DeliveryIdentity,
        *,
        operation_id: str,
        subject_ref: str,
        payload_digest: str,
        recipient_email: str,
        recipient_identity: str,
        external_mailer: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> tuple[DeliveryRecord, bool]:
        """Durably establish the logical operation, or find the existing one.

        THE PRE-PUBLICATION INVARIANT. Everything the secure-delivery contract
        needs to make a rerun idempotent — operation id, subject reference, the
        exact canonical payload digest, and the recipient binding — is
        committed here, before any network call exists.

        Returns `(record, created)`. A concurrent caller that loses the insert
        gets the winner's row, not an error: they are the same logical
        delivery.

        Refuses, without mutating anything:

        * `RECIPIENT_CONFLICT` — the same logical delivery bound to a different
          recipient. A retry must never silently redirect a driver's dashboard
          link to a different mailbox;
        * `PAYLOAD_CONFLICT` — different canonical bytes for a delivery that
          has already been published under a digest. The published bytes are
          the identity, and quietly adopting new ones would break the very
          equality the byte-integrity contract is built on;
        * `OPERATION_CONFLICT` / `SUBJECT_CONFLICT` — the derived identities
          disagree with what is stored, which can only mean the caller is not
          talking about the row it thinks it is;
        * `EXTERNAL_OWNERSHIP_CONFLICT` — the caller's send-accounting ownership
          is not the ownership the row was CREATED with. See below.

        OWNERSHIP IS BOUND HERE OR NOWHERE. `external_mailer` names the external
        mailing lifecycle that owns this delivery's send accounting, and it is
        written by this INSERT — the first durable statement about the delivery
        — rather than annotated onto the row later. The reason is a crash
        window, not tidiness: a row created without ownership and annotated
        afterwards is, for the whole interval between the two writes, a row the
        provider lifecycle is entitled to claim, lease, count an attempt
        against and drive towards a SECOND message for a driver the external
        mailer is already mailing. There is no such interval if the very first
        committed row already says who owns the send.

        The column is immutable in both directions (the row guard, installed
        by migration 049 and replaced with the reviewed body by migration
        050), so this is also the only place it is ever decided.
        """
        address = normalise_email(recipient_email)
        owner = str(external_mailer).strip() if external_mailer else None
        if external_mailer is not None and not owner:
            raise DeliveryContractError(
                "an external mailer name must not be blank")
        with self._conn.cursor() as cur:
            cur.execute(
                f"""INSERT INTO {self._table} (
                        operation_id, client_id, identity_key, period_type,
                        period_start_date, period_end_date, send_scope,
                        subject_ref, payload_digest, recipient_identity,
                        recipient_email, state, external_mailer, last_run_id
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    -- No conflict TARGET on purpose. The operation id is
                    -- derived from the same logical identity, so a concurrent
                    -- duplicate violates `uq_..._operation` and
                    -- `uq_..._identity` at the same time, and naming one
                    -- arbiter would let the other surface as an error instead
                    -- of the "somebody else already created it" that it is.
                    ON CONFLICT DO NOTHING
                    RETURNING {_PROJECTION}""",
                (operation_id, identity.client_id, identity.identity_key,
                 identity.period_type, identity.period_start_date,
                 identity.period_end_date, identity.send_scope, subject_ref,
                 payload_digest, recipient_identity, address,
                 DeliveryState.PREPARED, owner, run_id),
            )
            row = cur.fetchone()
        if row is not None:
            record = DeliveryRecord(dict(row) if isinstance(row, Mapping)
                                    else dict(zip(COLUMNS, row)))
            return record, True

        existing = self.find_by_identity(identity)
        if existing is None:  # pragma: no cover - only under concurrent deletion
            raise LedgerConflict("OPERATION_VANISHED",
                                 "the logical delivery could not be established")
        self._assert_compatible(existing, operation_id=operation_id,
                                subject_ref=subject_ref,
                                payload_digest=payload_digest,
                                recipient_identity=recipient_identity,
                                recipient_email=address,
                                external_mailer=owner)
        return existing, False

    def _assert_compatible(self, existing: DeliveryRecord, *, operation_id: str,
                           subject_ref: str, payload_digest: str,
                           recipient_identity: str, recipient_email: str,
                           external_mailer: Optional[str] = None) -> None:
        if existing.recipient_email != recipient_email or \
                existing.recipient_identity != recipient_identity:
            # The message names the recipient, so this is refused before any
            # work happens rather than reported afterwards. No column is
            # written: a conflict is a fact about the CALLER's request.
            raise LedgerConflict(
                "RECIPIENT_CONFLICT",
                "this logical delivery is already bound to a different recipient",
            )
        if existing.operation_id != operation_id:
            raise LedgerConflict("OPERATION_CONFLICT",
                                 "stored operation id differs from the derived one")
        if existing.subject_ref != subject_ref:
            raise LedgerConflict("SUBJECT_CONFLICT",
                                 "stored subject reference differs from the derived one")
        if existing.payload_digest != payload_digest:
            raise LedgerConflict(
                "PAYLOAD_CONFLICT",
                "this logical delivery is already bound to different canonical bytes",
            )
        if (existing.row.get("external_mailer") or None) != (external_mailer or None):
            # NEITHER DIRECTION IS ADOPTION. A provider-owned row is not taken
            # over by an external mailer, and an externally-owned row is not
            # handed to the provider lifecycle by a caller that simply forgot to
            # say who it was. Ownership was decided when the row was created and
            # a disagreement is a fact about the CALLER, so nothing is written.
            raise LedgerConflict(
                "EXTERNAL_OWNERSHIP_CONFLICT",
                "this logical delivery was created under different send-accounting "
                "ownership",
            )

    # --- ownership -------------------------------------------------------------

    def claim(self, delivery_id, *, owner: str,
              lease_seconds: int = DEFAULT_LEASE_SECONDS,
              run_id: Optional[str] = None) -> Optional[DeliveryRecord]:
        """Take durable ownership, or return `None` and do nothing.

        The predicate is the whole concurrency contract: a lease is free when
        nobody holds it, when it has expired, or when this same owner already
        holds it (so a retry inside one invocation is not a self-deadlock).
        """
        with self._conn.cursor() as cur:
            cur.execute(
                f"""UPDATE {self._table}
                       SET lease_owner = %s,
                           lease_expires_at = now() + make_interval(secs => %s),
                           attempt_count = attempt_count + 1,
                           last_run_id = COALESCE(%s, last_run_id),
                           updated_at = now()
                     WHERE delivery_id = %s
                       AND (lease_owner IS NULL
                            OR lease_owner = %s
                            OR lease_expires_at IS NULL
                            OR lease_expires_at < now())
                 RETURNING {_PROJECTION}""",
                (owner, float(lease_seconds), run_id, str(delivery_id), owner),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return DeliveryRecord(dict(row) if isinstance(row, Mapping)
                              else dict(zip(COLUMNS, row)))

    def renew(self, delivery_id, *, owner: str,
              lease_seconds: int = DEFAULT_LEASE_SECONDS) -> Optional[DeliveryRecord]:
        """Re-assert CURRENT ownership and extend it, in one statement.

        THE PRE-EFFECT FENCE. Two facts have to be true immediately before a
        worker initiates an external side effect: it still holds the lease, and
        the lease has not expired. Both are properties of the row right now,
        not of the snapshot the invocation started from, so they are re-read
        under the same predicate that would refuse the eventual write.

        `None` means ownership is gone — expired, released or taken over — and
        the caller must perform NO effect and write nothing. Extending at the
        same moment is what keeps a slow-but-legitimate remote call from losing
        the lease it is still actively using.

        What this deliberately does NOT claim: no database lease can be atomic
        with an HTTP request. A lease can still expire while a call is in
        flight. That case is covered by the ordering the lifecycle already
        guarantees — the "a submission may have happened" marker is committed
        before the call, so a taking-over owner reconciles under the bound
        idempotency identity instead of resending.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                f"""UPDATE {self._table}
                       SET lease_expires_at = now() + make_interval(secs => %s),
                           updated_at = now()
                     WHERE delivery_id = %s AND lease_owner = %s
                       AND lease_expires_at IS NOT NULL
                       AND lease_expires_at > now()
                 RETURNING {_PROJECTION}""",
                (float(lease_seconds), str(delivery_id), owner),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return DeliveryRecord(dict(row) if isinstance(row, Mapping)
                              else dict(zip(COLUMNS, row)))

    def holds_lease(self, delivery_id, *, owner: str) -> bool:
        """Read-only ownership question. Never extends anything."""
        with self._conn.cursor() as cur:
            cur.execute(
                f"""SELECT 1 FROM {self._table}
                     WHERE delivery_id = %s AND lease_owner = %s
                       AND lease_expires_at IS NOT NULL
                       AND lease_expires_at > now()""",
                (str(delivery_id), owner),
            )
            return cur.fetchone() is not None

    def release(self, delivery_id, *, owner: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                f"""UPDATE {self._table}
                       SET lease_owner = NULL, lease_expires_at = NULL,
                           updated_at = now()
                     WHERE delivery_id = %s AND lease_owner = %s""",
                (str(delivery_id), owner),
            )

    # --- transitions -----------------------------------------------------------

    def _transition(self, record: DeliveryRecord, *, owner: str, target: str,
                    assignments: str, params: tuple) -> DeliveryRecord:
        """One compare-and-set from an expected state under a LIVE lease.

        The predicate names three things, and all three are load-bearing:

        * the state the caller decided from — so a decision made against a row
          that has since moved on is refused rather than applied;
        * the lease owner — so a different owner's row is never written;
        * `lease_expires_at > now()` — so an owner whose lease has EXPIRED is
          refused too. Owner equality alone is not ownership: an expired holder
          still matches its own name in the row right up until somebody else
          claims it, and the window between expiry and takeover is exactly when
          a stale process wakes up and commits a decision the current state has
          already moved past.

        Every fenced write is evaluated by PostgreSQL in one statement against
        one row, so the check and the write cannot be separated by a takeover.
        """
        assert_legal_transition(record.state, target)
        sql = (
            f"UPDATE {self._table} SET state = %s, {assignments}, updated_at = now() "
            f"WHERE delivery_id = %s AND state = %s AND lease_owner = %s "
            f"  AND lease_expires_at IS NOT NULL AND lease_expires_at > now() "
            f"RETURNING {_PROJECTION}"
        )
        with self._conn.cursor() as cur:
            cur.execute(sql, (target, *params, str(record.delivery_id),
                              record.state, owner))
            row = cur.fetchone()
        if row is None:
            raise LedgerConflict(
                "LOST_OWNERSHIP",
                f"the delivery is no longer in {record.state} under a live lease "
                f"held by this invocation",
            )
        return DeliveryRecord(dict(row) if isinstance(row, Mapping)
                              else dict(zip(COLUMNS, row)))

    def record_capability(self, record: DeliveryRecord, *, owner: str,
                          capability: str, capability_id: str,
                          expires_at: datetime, bearer_generation: int) -> DeliveryRecord:
        """Persist the raw bearer. THE gate before any provider attempt.

        Nothing may treat an e-mail send as eligible until this has committed:
        the raw capability is returned exactly once by the publisher API, and a
        host that has not stored it cannot construct the driver's link at all.
        """
        if not capability:
            raise DeliveryContractError("a capability is required")
        self.register_secret(capability)
        return self._transition(
            record, owner=owner, target=DeliveryState.CAPABILITY_PERSISTED,
            assignments=(
                "capability_secret = %s, capability_id = %s, capability_digest = %s, "
                "capability_expires_at = %s, bearer_generation = %s, "
                "bearer_persisted_at = now(), bearer_cleared_at = NULL, "
                "failure_phase = NULL, failure_code = NULL, failure_detail = NULL, "
                "operator_action_required = FALSE"
            ),
            params=(capability, capability_id, capability_digest(capability),
                    expires_at, int(bearer_generation)),
        )

    def mark_bearer_recovery_required(self, record: DeliveryRecord, *, owner: str,
                                      reason: str,
                                      failure_code: str = "BEARER_NOT_PERSISTED",
                                      failure_phase: str = "PUBLICATION") -> DeliveryRecord:
        """The host cannot present a usable grant for an operation the Worker owns.

        Two situations reach this, and they are the same situation to a
        restart: the raw bearer was never persisted, or the persisted one has
        EXPIRED. In both the host holds nothing it can put in front of a
        driver, and in both the only legal move is the explicit recovery
        operation — never a second publish, and never handing over the material
        it still has. `failure_code` says which one it was, for an operator
        reading the row.

        Any bearer material still stored is destroyed in the same statement,
        which is also this ledger's bearer-minimisation rule: material that can
        no longer authorise anything is not kept because the row is terminal.
        After this point only recovery may produce a usable capability, and a
        stale predecessor must not survive to be selected by a later restart.
        """
        return self._transition(
            record, owner=owner, target=DeliveryState.BEARER_RECOVERY_REQUIRED,
            assignments=(
                "capability_secret = NULL, "
                # COALESCE, not a bare `now()`: this transition is now also
                # reachable from `CAPABILITY_RETIRED`, where the bearer was
                # already destroyed and stamped. Overwriting the stamp would
                # rewrite the audit answer to "when did this delivery stop
                # holding secret material?" every time the period is re-mailed.
                "bearer_cleared_at = CASE WHEN bearer_persisted_at IS NOT NULL "
                "                         THEN COALESCE(bearer_cleared_at, now()) "
                "                         ELSE NULL END, "
                "failure_phase = %s, failure_code = %s, failure_detail = %s"
            ),
            params=(str(failure_phase), str(failure_code), self._scrub(reason)),
        )

    def record_external_mailer_handoff(self, record: DeliveryRecord, *, owner: str,
                                      mailer: str,
                                      run_id: Optional[str] = None) -> DeliveryRecord:
        """The capability was handed to an external, authoritative mailing lifecycle.

        THE SMALLEST HONEST TERMINAL STATE. The existing Eco Driving
        weekly/monthly jobs own recipient resolution, send-log idempotency and
        the example.invalid SMTP send; this ledger owns the snapshot, the publication and
        the capability. Once the link has been handed over, this ledger has
        nothing left to decide — and saying so is not cosmetic. Leaving the row
        in `CAPABILITY_PERSISTED` would leave a durable instruction to record a
        delivery intent and submit a SECOND dashboard-specific message for a
        driver the Eco lifecycle has already mailed.

        WHAT IT DELIBERATELY DOES NOT CLAIM. Not that a message was sent, not
        that a provider accepted one, and not that the remote operation is
        `DELIVERED`. No provider identity is bound, no acceptance is recorded
        and no remote transition is made — `eco_*_email_send_log` is the sole
        authority for whether a driver was mailed. `mailer` names WHICH external
        lifecycle took the link, so an operator reading this row knows exactly
        where the send accounting for it lives.

        The raw bearer is retained (`BEARER_REQUIRED_STATES`) so a rerun of the
        same logical delivery hands over the identical link instead of rotating
        the capability.
        """
        if not str(mailer or "").strip():
            raise DeliveryContractError("an external mailer name is required")
        bound_mailer = str(record.row.get("external_mailer") or "").strip()
        if bound_mailer != str(mailer).strip():
            # The handoff RECORDS ownership; it never establishes it. A row
            # whose durable `external_mailer` does not already name this mailer
            # was not created for it, and the column cannot be corrected here
            # because migration 050's guard body makes it immutable.
            raise LedgerConflict(
                "EXTERNAL_OWNERSHIP_CONFLICT",
                "this delivery was not created under this external mailer's "
                "send-accounting ownership",
            )
        handoff = {
            "mailer": self._scrub(mailer),
            "run_id": self._scrub(run_id) if run_id else None,
            "send_accounting_authority": "eco_email_send_log",
        }
        return self._transition(
            record, owner=owner, target=DeliveryState.EXTERNAL_MAILER_HANDOFF,
            assignments=(
                "metadata_json = jsonb_set(metadata_json, "
                "  '{external_mailer_handoff}', %s::jsonb), "
                "failure_phase = NULL, failure_code = NULL, failure_detail = NULL, "
                "operator_action_required = FALSE"
            ),
            params=(json.dumps(handoff, ensure_ascii=False),),
        )

    def record_delivery_intent(self, record: DeliveryRecord, *, owner: str,
                               provider_name: str, provider_backend_id: str,
                               message_fingerprint: str,
                               bound_capability_id: str,
                               bound_bearer_generation: int) -> DeliveryRecord:
        """THE SUBMISSION IDENTITY BINDING. One statement, before any provider.

        Four facts become durable together and never change again:

        * `provider_idempotency_key` — derived from the publication operation
          id, so it is the same value on every attempt and after every restart;
        * `provider_backend_id` — WHICH backend/account that key is meaningful
          against. Without it the key is a promise made to nobody in
          particular: a restart reconfigured onto a second provider would ask a
          backend that has never seen the key, and get a second message;
        * `provider_message_fingerprint` — the exact message the key names, so
          the key cannot later be reused for different content;
        * the capability id and generation the message was built from, so the
          bearer a possibly-sent message carried stays identifiable.

        This is the last point at which any of them may be decided. Everything
        after it either matches the binding or refuses to contact a provider.
        """
        key = derive_provider_idempotency_key(record.operation_id)
        if not provider_backend_id:
            raise DeliveryContractError("a provider backend identity is required")
        if not message_fingerprint:
            raise DeliveryContractError("a message fingerprint is required")
        if not bound_capability_id:
            raise DeliveryContractError("a bound capability id is required")
        return self._transition(
            record, owner=owner, target=DeliveryState.DELIVERY_INTENT_RECORDED,
            assignments=(
                "provider_name = %s, provider_idempotency_key = %s, "
                "provider_backend_id = %s, provider_message_fingerprint = %s, "
                "provider_bound_capability_id = %s, "
                "provider_bound_bearer_generation = %s, "
                "failure_phase = NULL, failure_code = NULL, failure_detail = NULL"
            ),
            params=(provider_name, key, str(provider_backend_id),
                    str(message_fingerprint), str(bound_capability_id),
                    int(bound_bearer_generation)),
        )

    def record_submission_pending(self, record: DeliveryRecord, *,
                                  owner: str) -> DeliveryRecord:
        """Written BEFORE the provider call, and that ordering is the point.

        If the process dies during the call, the recovered state already says
        "a message may exist". The only safe action from here is reconciliation
        under the stable idempotency identity — never another submission
        decided from ignorance.
        """
        return self._transition(
            record, owner=owner, target=DeliveryState.PROVIDER_SUBMISSION_PENDING,
            assignments=(
                "provider_attempts = provider_attempts + 1, "
                "provider_submitted_at = now(), "
                "failure_phase = NULL, failure_code = NULL, failure_detail = NULL, "
                "operator_action_required = FALSE"
            ),
            params=(),
        )

    def record_provider_accepted(self, record: DeliveryRecord, *, owner: str,
                                 provider_message_id: str,
                                 reconciled: bool = False) -> DeliveryRecord:
        if not provider_message_id:
            raise DeliveryContractError("acceptance requires a provider message id")
        return self._transition(
            record, owner=owner, target=DeliveryState.PROVIDER_ACCEPTED,
            assignments=(
                "provider_message_id = %s, provider_accepted_at = now(), "
                "operator_action_required = FALSE, "
                "failure_phase = NULL, failure_code = NULL, failure_detail = NULL, "
                "metadata_json = jsonb_set(metadata_json, '{acceptance_source}', %s::jsonb)"
            ),
            params=(provider_message_id,
                    json.dumps("RECONCILED" if reconciled else "DIRECT")),
        )

    def record_provider_rejected(self, record: DeliveryRecord, *, owner: str,
                                 failure_code: str, detail: str,
                                 operator_required: bool) -> DeliveryRecord:
        """A DEFINITE refusal: the provider created no message.

        `PROVIDER_REJECTED` is never `DELIVERED` in any sense — the remote
        operation is not terminalised and no acceptance is recorded.
        """
        return self._transition(
            record, owner=owner, target=DeliveryState.PROVIDER_REJECTED,
            assignments=(
                "failure_phase = 'PROVIDER', failure_code = %s, failure_detail = %s, "
                "operator_action_required = %s"
            ),
            params=(self._scrub(failure_code), self._scrub(detail),
                    bool(operator_required)),
        )

    def record_provider_ambiguous(self, record: DeliveryRecord, *, owner: str,
                                  failure_code: str, detail: str) -> DeliveryRecord:
        """Acceptance is genuinely unknowable under this provider's abstraction.

        This is a first-class outcome, not an error path: it exists so the host
        never has to choose between "assume delivered" and "send again". The
        bearer is retained, because an operator reconciling this state may
        still need to establish which link was in the message.
        """
        return self._transition(
            record, owner=owner, target=DeliveryState.PROVIDER_AMBIGUOUS,
            assignments=(
                "failure_phase = 'PROVIDER', failure_code = %s, failure_detail = %s, "
                "operator_action_required = TRUE"
            ),
            params=(self._scrub(failure_code), self._scrub(detail)),
        )

    def record_remote_delivered(self, record: DeliveryRecord, *,
                                owner: str) -> DeliveryRecord:
        return self._transition(
            record, owner=owner, target=DeliveryState.REMOTE_DELIVERED,
            assignments="remote_delivered_at = now()",
            params=(),
        )

    def finalize(self, record: DeliveryRecord, *, owner: str) -> DeliveryRecord:
        """Terminal success, and the moment the raw bearer stops existing here.

        Retention is minimised, not audited: `capability_digest`,
        `capability_id` and `bearer_generation` survive, so an operator can
        still prove which grant was delivered, while the value that would let
        anyone open the driver's dashboard does not.
        """
        return self._transition(
            record, owner=owner, target=DeliveryState.FINALIZED,
            assignments=(
                "capability_secret = NULL, bearer_cleared_at = now(), "
                "finalized_at = now(), lease_owner = NULL, lease_expires_at = NULL, "
                "operator_action_required = FALSE"
            ),
            params=(),
        )

    def mark_operator_required(self, record: DeliveryRecord, *, owner: str,
                               phase: str, failure_code: str,
                               detail: str) -> DeliveryRecord:
        """Automation cannot make safe progress. Stop, visibly.

        The bearer is destroyed unless the state still legitimately needs it:
        an operation parked for investigation is not a place to keep live
        delivery material alive indefinitely.
        """
        keep_bearer = record.state in BEARER_REQUIRED_STATES
        assignments = (
            "failure_phase = %s, failure_code = %s, failure_detail = %s, "
            "operator_action_required = TRUE"
        )
        params: tuple = (self._scrub(phase), self._scrub(failure_code),
                         self._scrub(detail))
        if not keep_bearer:
            assignments += (
                ", capability_secret = NULL, "
                "bearer_cleared_at = CASE WHEN bearer_persisted_at IS NOT NULL "
                "                         THEN COALESCE(bearer_cleared_at, now()) "
                "                         ELSE NULL END"
            )
        return self._transition(
            record, owner=owner, target=DeliveryState.OPERATOR_REQUIRED,
            assignments=assignments, params=params,
        )

    # --- operator recovery -----------------------------------------------------

    def assess_operator_recovery(self, delivery_id) -> Optional[RecoveryAssessment]:
        """Read-only eligibility verdict for one delivery, or `None` if unknown."""
        record = self.load(delivery_id)
        if record is None:
            return None
        return assess_operator_recovery(record)

    def operator_recover_to_prepared(self, delivery_id, *, operator: str,
                                     reason: str,
                                     expected_operation_id: str,
                                     expected_payload_digest: str,
                                     now: Optional[datetime] = None
                                     ) -> Optional[DeliveryRecord]:
        """Re-arm ONE reviewed `OPERATOR_REQUIRED` delivery. The whole mutation.

        THE STATEMENT IS THE GUARD. Every eligibility fact in
        `OPERATOR_RECOVERY_GUARDS` is a predicate of the same UPDATE that
        writes, so a row that acquired remote state between the dry run and this
        call is refused by PostgreSQL rather than by a snapshot the operator
        read a minute ago. `None` means "not eligible now" and NOTHING was
        written — never "probably fine".

        WHAT IT DELIBERATELY DOES NOT TOUCH. The delivery identity, the
        operation id, the subject reference, the payload and snapshot digests,
        the recipient binding and the bearer generation are all absent from the
        SET list, and the operation id and payload digest the caller believes it
        is recovering are additionally asserted in the WHERE clause. The row
        guard trigger refuses a change to any of them regardless; naming them
        here means a caller that has the WRONG delivery in mind is refused
        instead of silently re-arming a different one.

        The reset itself is the smallest thing that makes a valid `PREPARED`
        row: the state, the operator flag the table requires to be false there,
        and the three failure columns that describe an attempt which is no
        longer the current situation. The lease is released so the next
        legitimate run can claim it.
        """
        assert_legal_operator_recovery(DeliveryState.OPERATOR_REQUIRED,
                                       DeliveryState.PREPARED)
        if not str(operator or "").strip():
            raise DeliveryContractError("an operator recovery requires an operator")
        if not str(reason or "").strip():
            raise DeliveryContractError("an operator recovery requires a reason")

        attestation = {
            "operator": self._scrub(operator),
            "reason": self._scrub(reason),
            "recovered_from": DeliveryState.OPERATOR_REQUIRED,
            "recovered_to": DeliveryState.PREPARED,
            "recorded_at": (now or datetime.now(timezone.utc)).isoformat(),
        }
        predicates = " AND ".join(sql for _name, sql, _check in OPERATOR_RECOVERY_GUARDS)
        statement = (
            f"UPDATE {self._table}"
            f"   SET state = %(target_state)s,"
            f"       operator_action_required = FALSE,"
            f"       failure_phase = NULL, failure_code = NULL, failure_detail = NULL,"
            f"       lease_owner = NULL, lease_expires_at = NULL,"
            f"       metadata_json = jsonb_set(metadata_json, %(metadata_path)s,"
            f"                                 %(attestation)s::jsonb, true),"
            f"       updated_at = now()"
            f" WHERE delivery_id = %(delivery_id)s"
            f"   AND operation_id = %(expected_operation_id)s"
            f"   AND payload_digest = %(expected_payload_digest)s"
            f"   AND {predicates}"
            f" RETURNING {_PROJECTION}"
        )
        params = {
            "target_state": DeliveryState.PREPARED,
            "metadata_path": [OPERATOR_RECOVERY_METADATA_KEY],
            "attestation": json.dumps(attestation, ensure_ascii=False),
            "delivery_id": str(delivery_id),
            "expected_operation_id": str(expected_operation_id),
            "expected_payload_digest": str(expected_payload_digest),
            "recovery_state": DeliveryState.OPERATOR_REQUIRED,
            "recovery_phase": OPERATOR_RECOVERY_PHASE,
            "recovery_codes": sorted(OPERATOR_RECOVERABLE_PUBLICATION_FAILURE_CODES),
        }
        with self._conn.cursor() as cur:
            cur.execute(statement, params)
            row = cur.fetchone()
        if row is None:
            return None
        return DeliveryRecord(dict(row) if isinstance(row, Mapping)
                              else dict(zip(COLUMNS, row)))

    # --- operational reporting -------------------------------------------------

    def open_operations(self, *, limit: int = 100) -> list[DeliveryRecord]:
        """Deliveries that still need something to happen.

        The exclusion is `CLOSED_STATES`, not `FINALIZED` alone. A delivery
        whose capability has expired and been retired has no next action, holds
        no secret and cannot be progressed by anyone — leaving it here would
        mean every historical report a client ever sent stayed permanently in
        the operational surface, and an "open work" list that only grows is a
        list nobody reads.

        `idx_..._open` (migration 051) carries the same predicate, so the query
        stays an indexed scan of the genuinely open set.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                f"""SELECT {_PROJECTION} FROM {self._table}
                     WHERE state <> ALL(%s) ORDER BY updated_at LIMIT %s""",
                (sorted(CLOSED_STATES), int(limit)),
            )
            rows = cur.fetchall()
        return [DeliveryRecord(dict(r) if isinstance(r, Mapping)
                               else dict(zip(COLUMNS, r))) for r in rows]

    # --- expired-capability retirement -----------------------------------------

    #: One sweep batch. Large enough that a weekly run clears a client's whole
    #: expiry backlog in a couple of statements, small enough that the statement
    #: stays a short indexed update rather than a long lock on a big ledger.
    RETIREMENT_BATCH = 200

    #: Where the non-secret retirement record is written.
    RETIREMENT_METADATA_KEY = "capability_retirement"

    def retire_expired_capabilities(self, *, limit: int = RETIREMENT_BATCH,
                                    run_id: Optional[str] = None,
                                    now: Optional[datetime] = None
                                    ) -> list[DeliveryRecord]:
        """Destroy the raw bearers of grants that have expired, in one statement.

        THE PROBLEM THIS SOLVES. A capability's host-side copy was only ever
        destroyed by something that happened TO that delivery: finalisation, an
        operator escalation, or a rerun of the same reporting period that found
        the grant expired and rotated it. A historical period nobody re-mails
        therefore kept live secret material, and stayed in the operational
        "open" surface, for as long as the row existed — which is forever. With
        period-scoped lifetimes (weekly 10 days, monthly 60) that is no longer a
        rare corner: it is the normal end of every link.

        Retirement is the lifecycle's own answer, and it needs nothing from
        anybody: no rerun, no operator, no remote call. The grant it retires is
        already dead at the Worker, so this makes no remote change of any kind —
        it destroys the host's now-useless copy of a secret and records that it
        did.

        WHAT IT WILL NEVER TOUCH, enforced by the statement's own predicate
        rather than by the caller:

        * a capability that has NOT expired. `capability_expires_at <= now()`
          is evaluated by PostgreSQL, in the statement that writes, against the
          database clock — not against a timestamp this process read earlier;
        * a row with an UNKNOWN expiry. `capability_expires_at IS NULL` is
          excluded: not knowing when a grant dies is not evidence that it has;
        * a row somebody else durably owns. A live lease is excluded, and
          `FOR UPDATE SKIP LOCKED` means a concurrent sweep or a concurrent
          `claim()` serialises rather than collides;
        * a state that has an owner. Only `RETIRABLE_ON_EXPIRY_STATES` — see
          that set for why each of the others is left alone, `PROVIDER_AMBIGUOUS`
          above all: an unresolved ambiguous submission is a human's to answer
          and nothing here weakens it;
        * a delivery that may have produced a message. `DELIVERY_INTENT_RECORDED`
          is retirable only with `provider_attempts = 0` and
          `provider_submitted_at IS NULL` PROVEN on the row, so "bound but never
          submitted" is a fact, not an assumption about a state name.

        IDEMPOTENT AND INTERRUPTIBLE. Retirement moves a row out of every
        retirable state, so a second sweep selects nothing and returns `[]`. The
        batch is one autocommitted statement, so an interruption either applied
        it or did not; there is no partially retired row. `bearer_cleared_at` is
        COALESCEd, so even a hand-run repeat cannot rewrite when the secret
        actually stopped existing.

        WHAT SURVIVES, AND WHY. `capability_id`, `capability_digest`,
        `capability_expires_at`, `bearer_generation` and any bound provider
        submission identity all remain, so an operator can still prove which
        grant this delivery held, which generation it was, and — where one was
        bound — which message it named. Migration 051 requires that audit
        identity of every retired row. What does not remain is the one value
        that could open a driver's dashboard.

        NOT A REPORT DELETION. The R2 snapshot this delivery published is
        untouched and is not deleted by bearer expiry; the two lifetimes are
        deliberately independent.

        Returns the rows it retired, in no promised order — `RETURNING` does
        not guarantee one and nothing here depends on it. A full batch means
        another call would do more work. Candidate SELECTION is ordered by
        expiry, so the longest-dead bearers are always the first to go.
        """
        moment = now or datetime.now(timezone.utc)
        record = {
            "retired_at": moment.isoformat(),
            "reason": "CAPABILITY_EXPIRED",
            "run_id": self._scrub(run_id) if run_id else None,
        }
        # The transition graph, not a second hand-written list: a state may be
        # swept only if the contract says it may reach `CAPABILITY_RETIRED`.
        # `delivery_contract` asserts the two agree at import time.
        for state in RETIRABLE_ON_EXPIRY_STATES:
            assert_legal_transition(state, DeliveryState.CAPABILITY_RETIRED)

        statement = f"""
            UPDATE {self._table} AS target
               SET state = %(target_state)s,
                   capability_secret = NULL,
                   bearer_cleared_at = CASE
                       WHEN bearer_persisted_at IS NOT NULL
                       THEN COALESCE(bearer_cleared_at, now())
                       ELSE NULL END,
                   operator_action_required = FALSE,
                   lease_owner = NULL,
                   lease_expires_at = NULL,
                   metadata_json = jsonb_set(metadata_json, %(metadata_path)s,
                                             %(retirement)s::jsonb, true),
                   updated_at = now()
             WHERE target.delivery_id IN (
                     SELECT candidate.delivery_id
                       FROM {self._table} AS candidate
                      WHERE candidate.state = ANY(%(states)s)
                        AND candidate.capability_expires_at IS NOT NULL
                        AND candidate.capability_expires_at <= now()
                        -- Never take a delivery an invocation durably owns.
                        AND (candidate.lease_owner IS NULL
                             OR candidate.lease_expires_at IS NULL
                             OR candidate.lease_expires_at <= now())
                        -- A bound submission identity is only retirable while
                        -- it is PROVEN that nothing was ever submitted under
                        -- it. `PROVIDER_REJECTED` is exempt: its whole meaning
                        -- is that the provider definitively created no message.
                        AND (candidate.state <> %(intent_state)s
                             OR (candidate.provider_attempts = 0
                                 AND candidate.provider_submitted_at IS NULL))
                      ORDER BY candidate.capability_expires_at
                      LIMIT %(limit)s
                        FOR UPDATE SKIP LOCKED
                   )
            RETURNING {_PROJECTION}
        """
        params = {
            "target_state": DeliveryState.CAPABILITY_RETIRED,
            "metadata_path": [self.RETIREMENT_METADATA_KEY],
            "retirement": json.dumps(record, ensure_ascii=False),
            "states": sorted(RETIRABLE_ON_EXPIRY_STATES),
            "intent_state": DeliveryState.DELIVERY_INTENT_RECORDED,
            "limit": max(1, int(limit)),
        }
        with self._conn.cursor() as cur:
            cur.execute(statement, params)
            rows = cur.fetchall()
        return [DeliveryRecord(dict(r) if isinstance(r, Mapping)
                               else dict(zip(COLUMNS, r))) for r in rows]
