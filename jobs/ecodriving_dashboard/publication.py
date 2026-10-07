"""Canonical publication interface — the only supported publisher data path.

TRUST SPLIT

    HOST (this module)      business validity + value-level privacy +
                            canonical deterministic serialisation
    WORKER (delivery)       authorization + subject/object binding +
                            strict structural schema allowlist

Value-level privacy lives here, not in the Worker. The delivery boundary can
only see structure: it knows `label` is a bounded string, but it cannot know
whether a particular string is a driver's name. That judgement belongs to the
side that has the source data, so the publisher must never hand the Worker
anything it did not build through this module.

WHY THE SHAPE CHANGED

The previous version exposed `canonical_bytes(mapping, banned_values=())` as
the publisher contract. Two defects followed directly from that signature, both
confirmed by the independent review:

  * ANY structurally valid mapping was publishable. A copied-and-mutated
    document — a person-like string inserted into an allowlisted `label`, an
    HTML fragment in a fixed-vocabulary field — passed the gate, because the
    gate only checked shape;
  * the value-level privacy sweep defaulted to EMPTY. Forgetting the argument
    silently turned the A12 value scan into a no-op, so the very check that
    exists to catch a leaked driver identifier was optional.

Both are now closed by construction rather than by discipline:

  * the publisher-facing type is `PublishableSnapshot`, which cannot be
    instantiated outside this module — `serialize_publishable_snapshot` accepts
    nothing else, so a mapping has no route to publishable bytes at all;
  * `PrivacyContext` is a REQUIRED argument of the one builder entry point, and
    the forbidden values are derived from it rather than passed alongside it,
    so they cannot be omitted or fall out of step with the identity actually
    being published.

The canonical gate itself (contract assertions A1–A16, the forbidden field-name
sweep, the fixed-vocabulary sweep, the value-level privacy sweep, the payload
budget) still runs over the finished document, so a bug in the builder cannot
publish either.

Nothing here writes to a database, a bucket or a network.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence

from jobs.ecodriving_dashboard.snapshot_builder import (
    DailyInput,
    PeriodInput,
    SeriesInput,
    build_driver_snapshot,
)
from jobs.ecodriving_dashboard.snapshot_contract import (
    MAX_BROWSER_PAYLOAD_BYTES,
    SCHEMA_VERSION,
    SNAPSHOT_CONTRACT_ID,
    SnapshotContractError,
    assert_snapshot_document,
    serialize_document,
)
from jobs.ecodriving_dashboard.subject_binding import subject_binding_digest


class PublicationRefused(RuntimeError):
    """Raised when a candidate document must not be published.

    Carries the failing assertion, never the offending value: a refusal message
    must not become the thing that leaks the identifier it caught.
    """

    def __init__(self, assertion: str, message: str) -> None:
        self.assertion = assertion
        super().__init__(f"{assertion}: {message}")


# --- mandatory privacy context -------------------------------------------------


@dataclass(frozen=True)
class PrivacyContext:
    """The source-derived values that must never appear in published output.

    This is the A12 value-level sweep's input, and it is a required argument
    rather than an optional one. Every field here is something the calculation
    host already has in hand when it builds a snapshot — the driver identity
    key, the client code, the person's name as stored upstream, the contact
    address the link will be e-mailed to — so there is no case in which the
    publisher legitimately has nothing to declare.

    None of these values is ever written into the document; they exist only to
    be searched for.
    """

    identity_key: str
    client_code: str
    person_names: Sequence[str] = ()
    email_addresses: Sequence[str] = ()
    vehicle_registrations: Sequence[str] = ()
    additional_identifiers: Sequence[str] = ()

    def forbidden_values(self) -> tuple[str, ...]:
        """Every declared value, de-duplicated, with blanks dropped."""

        collected: list[str] = [self.identity_key, self.client_code]
        for group in (self.person_names, self.email_addresses,
                      self.vehicle_registrations, self.additional_identifiers):
            collected.extend(group)
        seen: dict[str, None] = {}
        for value in collected:
            text = str(value).strip()
            if text:
                seen.setdefault(text, None)
        return tuple(seen)

    def as_internal(self) -> dict[str, Any]:
        """Host-side facts to carry alongside the snapshot. Never published."""

        return {"identity_key": self.identity_key, "client_code": self.client_code}

    def __post_init__(self) -> None:
        if not str(self.identity_key).strip():
            raise PublicationRefused("A12", "a privacy context requires an identity key")
        if not str(self.client_code).strip():
            raise PublicationRefused("A12", "a privacy context requires a client code")


# --- the publishable result ----------------------------------------------------

#: Module-private construction witness. A `PublishableSnapshot` can only be
#: created by code in this module that holds it, so an outside caller cannot
#: fabricate one around an arbitrary document and hand it to the serialiser.
_CONSTRUCTION_WITNESS = object()


@dataclass(frozen=True)
class PublishableSnapshot:
    """Exactly what the publisher may upload, and what it must persist.

    Instances are produced ONLY by `build_publishable_snapshot`. Constructing
    one directly raises: the type is the proof that the canonical builder,
    the contract assertions and the mandatory privacy sweep all ran.
    """

    body: bytes
    payload_digest: str
    document: Mapping[str, Any]
    internal: Mapping[str, Any]
    privacy_values_applied: int
    _witness: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._witness is not _CONSTRUCTION_WITNESS:
            raise PublicationRefused(
                "A0",
                "a publishable snapshot may only be produced by build_publishable_snapshot",
            )

    @property
    def size_bytes(self) -> int:
        return len(self.body)


def payload_digest(body: bytes) -> str:
    """SHA-256 of the exact canonical bytes, lowercase hex.

    The publisher sends this as `X-Publication-Payload-Digest`; the Worker
    recomputes it over the received body, so a retry that carries different
    bytes under the same operation id is a conflict rather than a silent
    overwrite. It is computed over exactly the bytes in
    `PublishableSnapshot.body` and over nothing else.
    """
    return hashlib.sha256(body).hexdigest()


def _canonical_bytes(document: Mapping[str, Any], *, banned_values: Sequence[str]) -> bytes:
    """PRIVATE. Run the full gate over a finished document and serialise it.

    Deliberately not the publisher contract. It takes a mapping, and a mapping
    is exactly what an ad-hoc, copied or hand-mutated document is — so exposing
    this would re-open the hole the review found. The supported entry point is
    `build_publishable_snapshot`; the supported serialiser is
    `serialize_publishable_snapshot`.
    """
    if not isinstance(document, Mapping):
        raise PublicationRefused("A0", "a snapshot document must be a mapping")
    if document.get("contract_id") != SNAPSHOT_CONTRACT_ID:
        raise PublicationRefused("A0", "unexpected contract_id")
    if document.get("schema_version") != SCHEMA_VERSION:
        raise PublicationRefused("A0", "unsupported schema_version")

    try:
        assert_snapshot_document(document, banned_values=banned_values)
    except SnapshotContractError as error:
        raise PublicationRefused(error.assertion, "snapshot failed the publication gate") from None
    except (KeyError, TypeError, AttributeError, ValueError):
        # A hand-assembled document that is not even shaped like the contract
        # fails here rather than escaping as a raw structural error.
        raise PublicationRefused("A0", "snapshot is not a valid v1 document") from None

    try:
        body = serialize_document(document)
    except SnapshotContractError as error:
        raise PublicationRefused(error.assertion, "snapshot could not be canonically serialised") from None

    if len(body) > MAX_BROWSER_PAYLOAD_BYTES:
        raise PublicationRefused("A14", "snapshot exceeded the browser payload budget")
    return body


def build_publishable_snapshot(
    *,
    privacy: PrivacyContext,
    generated_at_utc: datetime,
    period_type: str,
    current: PeriodInput,
    previous: PeriodInput | None = None,
    days: Sequence[DailyInput] = (),
    series: Sequence[SeriesInput] = (),
    series_reference: SeriesInput | None = None,
    comparable: bool = True,
    internal: Mapping[str, Any] | None = None,
    business_timezone: str = "Europe/Warsaw",
) -> PublishableSnapshot:
    """THE publisher entry point. Build one driver's snapshot and gate it.

    `privacy` is mandatory and is the sole source of the value-level privacy
    sweep — there is no `banned_values` argument to forget, and no way to run
    this with an empty one. The sweep runs twice by design: once inside the
    builder over the document it just produced, and once here over the finished
    document, so a future change to the builder cannot quietly drop it.
    """
    if not isinstance(privacy, PrivacyContext):
        raise PublicationRefused("A12", "publication requires a PrivacyContext")
    banned = privacy.forbidden_values()

    try:
        snapshot = build_driver_snapshot(
            generated_at_utc=generated_at_utc,
            period_type=period_type,
            current=current,
            previous=previous,
            days=days,
            series=series,
            series_reference=series_reference,
            comparable=comparable,
            internal={**privacy.as_internal(), **dict(internal or {})},
            banned_values=banned,
            business_timezone=business_timezone,
        )
    except SnapshotContractError as error:
        # The builder runs the same gate over the document it just produced.
        # Its failure is a publication refusal, not a raw contract error the
        # publisher has to know how to interpret.
        raise PublicationRefused(error.assertion, "snapshot failed the publication gate") from None
    body = _canonical_bytes(snapshot.document, banned_values=banned)
    return PublishableSnapshot(
        body=body,
        payload_digest=payload_digest(body),
        document=snapshot.document,
        internal=snapshot.internal,
        privacy_values_applied=len(banned),
        _witness=_CONSTRUCTION_WITNESS,
    )


def serialize_publishable_snapshot(snapshot: PublishableSnapshot) -> bytes:
    """The bytes the publisher may upload — and nothing else may reach.

    Accepts only a `PublishableSnapshot`, which only `build_publishable_snapshot`
    can produce. A dict, a `SimpleNamespace`, a subclass built around a mutated
    document or any other look-alike is refused: the argument type IS the
    assertion that the canonical builder and the privacy sweep ran.

    Python cannot make misuse literally impossible, so the boundary is drawn
    where it can be enforced — a construction witness the caller has no access
    to — and the tests assert this is the only supported interface.
    """
    if type(snapshot) is not PublishableSnapshot:
        raise PublicationRefused(
            "A0", "only a PublishableSnapshot from build_publishable_snapshot may be serialised"
        )
    if object.__getattribute__(snapshot, "_witness") is not _CONSTRUCTION_WITNESS:
        raise PublicationRefused("A0", "publishable snapshot failed its construction check")
    body = snapshot.body
    if not isinstance(body, bytes):
        raise PublicationRefused("A0", "publishable bytes must be bytes")
    if payload_digest(body) != snapshot.payload_digest:
        # The dataclass is frozen, but `object.__setattr__` can still reach it.
        # The digest is recomputed here so a tampered body cannot be uploaded
        # under the digest the Worker was told to expect.
        raise PublicationRefused("A0", "publishable bytes do not match their digest")
    return body


def expected_subject_binding(subject_ref: str, snapshot_object_key: str, pepper: str | None = None) -> str:
    """Recompute the binding the Worker will store for an object.

    The Worker mints the object key and derives the binding itself, so the
    publisher never chooses either. This exists so the host can verify, after a
    publication, that the object it caused to be written is bound to the subject
    it intended — see `delivery/driver_eco_dashboard/spec/subject_binding_v1.md`.
    """
    return subject_binding_digest(subject_ref, snapshot_object_key, pepper)


#: What the publisher must persist locally, per phase, before it can safely
#: retry. Documented here beside the code that produces each value, and aligned
#: with the Worker's publication state machine.
HOST_PERSISTENCE_CONTRACT = {
    "before_publish": (
        "operation_id",       # chosen by the host, stable across retries
        "subject_ref",        # opaque, never a driver identity
        "payload_digest",     # over the exact canonical bytes being sent
    ),
    "immediately_on_response": (
        "capability_id",      # identifies the grant; safe to store
        "capability",         # THE RAW BEARER — returned once, unrecoverable
        "expires_at",
    ),
    "before_delivery": (
        "delivery_intent_recorded",   # host wrote its own send intent durably
        "capability_id",              # the grant the host is about to deliver
    ),
    "after_delivery": (
        "delivered_confirmed",        # the message actually left
    ),
}
