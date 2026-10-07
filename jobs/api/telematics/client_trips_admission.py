"""Client Trips global ingestion admission rule.

This module is the **single admission boundary** for provider trips on their way
into `public.client_trips`.

`execute_client_trips_insert` below is the **only sanctioned way this repository
writes `client_trips`**. It applies the rule to the exact value each row binds to
`trip_distance_meters`, inside the call that issues the statement, so a caller
cannot build rows, skip the check and still reach the database through it. That
is what makes the invariant inherited rather than remembered: a future Client
Trips job does not have to know this rule exists — it only has to persist rows
the way the repository already persists them. `ops/tests_manual/
test_client_trips_distance_cap.py` audits that structurally, and importing this
module without calling the primitive is deliberately not enough to pass.

THE RULE (owner/business decision, not an inference)
----------------------------------------------------

    provider distance <= 2_000_000 m  ->  normal Client Trips persistence
    provider distance >  2_000_000 m  ->  discarded before persistence

The boundary is intentional and asymmetric:

    1_999_999 m  ACCEPT
    2_000_000 m  ACCEPT   (exactly 2,000 km is allowed)
    2_000_001 m  REJECT

WHAT THIS IS NOT
----------------

This is a **hard business cap on one field**, not an anomaly detector. It does
no GPS/geodesic validation, no average-speed check, no odometer reconstruction,
no statistical outlier detection, no per-client or per-vehicle threshold, no
correction, no confidence score and no quality tier. The provider-reported
distance stays authoritative in every other respect; the platform deliberately
does not try to decide whether a trip below the cap is correct.

MISSING / NULL DISTANCE
-----------------------

Unchanged. A provider trip whose distance is absent, `NULL`, empty or not
interpretable as a number carries **no known distance**, so the cap has nothing
to compare and the trip follows the repository's existing behaviour: it is
persisted with whatever the ingestion path already stored (`NULL` for a missing
value; a malformed value still fails at the column type exactly as before).
This module never invents a substitute distance and never rejects on absence.

HISTORICAL ROWS
---------------

This is an ingestion invariant only. It deletes and rewrites nothing. An
existing over-cap row stays untouched until an ordinary provider re-ingestion
would have re-upserted it — at which point the incoming provider record is
discarded here and the stored row is simply not refreshed.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Final, List, Mapping, Optional

# 2,000 km expressed in the unit `client_trips.trip_distance_meters` stores.
MAX_TRIP_DISTANCE_METERS: Final[int] = 2_000_000

# The provider payload field the two ingestion paths copy verbatim into
# `client_trips.trip_distance_meters` (audit docs/43 §3.1).
PROVIDER_DISTANCE_FIELD: Final[str] = "trip_distance"

# Stable rejection reason, used as a counter key in job diagnostics so a run's
# rejections are aggregated rather than logged per trip.
REASON_DISTANCE_OVER_CAP: Final[str] = "distance_over_2000km"

# Canonical observability key. Job summaries expose this name verbatim so an
# operator can grep one string across every Client Trips ingestion job.
REJECTED_COUNTER_KEY: Final[str] = "client_trips_rejected_distance_over_2000km"
REJECTED_MAX_DISTANCE_KEY: Final[str] = "client_trips_rejected_max_distance_meters"
REJECTED_SAMPLE_KEY: Final[str] = "client_trips_rejected_provider_trip_ids_sample"


@dataclass(frozen=True)
class AdmissionVerdict:
    """Outcome of the admission rule for one provider trip."""

    admitted: bool
    reason: Optional[str] = None
    distance_meters: Optional[Decimal] = None


def coerce_distance_meters(value: Any) -> Optional[Decimal]:
    """Return the provider distance as a number, or `None` when it is unknown.

    `None` means "no comparable distance" — absent, `NULL`, empty or not a
    number. It never means zero and never means "reject".
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        try:
            candidate = Decimal(str(value))
        except InvalidOperation:
            return None
        return candidate if candidate.is_finite() else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            candidate = Decimal(text)
        except InvalidOperation:
            return None
        return candidate if candidate.is_finite() else None
    return None


def distance_exceeds_cap(value: Any) -> bool:
    """`True` only for a known provider distance strictly above the cap."""
    distance = coerce_distance_meters(value)
    return distance is not None and distance > MAX_TRIP_DISTANCE_METERS


def evaluate_distance(value: Any) -> AdmissionVerdict:
    """Admission verdict for an already-extracted provider distance value."""
    distance = coerce_distance_meters(value)
    if distance is not None and distance > MAX_TRIP_DISTANCE_METERS:
        return AdmissionVerdict(
            admitted=False,
            reason=REASON_DISTANCE_OVER_CAP,
            distance_meters=distance,
        )
    return AdmissionVerdict(admitted=True, reason=None, distance_meters=distance)


def evaluate_provider_trip(trip: Any) -> AdmissionVerdict:
    """Admission verdict for a raw provider `/trips` row.

    A row that is not a mapping carries no distance this rule can read, so it is
    admitted here and left to the caller's own parse handling — this boundary
    owns exactly one decision and does not silently absorb malformed rows.
    """
    if not isinstance(trip, Mapping):
        return AdmissionVerdict(admitted=True, reason=None, distance_meters=None)
    return evaluate_distance(trip.get(PROVIDER_DISTANCE_FIELD))


@dataclass(frozen=True)
class ClientTripsInsertResult:
    """Outcome of one sanctioned `client_trips` INSERT/upsert statement."""

    rowcount: int
    rejected: int
    executed: bool


def execute_client_trips_insert(
    cur: Any,
    *,
    sql: str,
    rows: Any,
    distance_index: int,
    provider_trip_id_index: Optional[int] = None,
    gate: Optional["ClientTripsAdmission"] = None,
) -> ClientTripsInsertResult:
    """Execute a `client_trips` INSERT/upsert with the global cap enforced.

    **This is the only sanctioned way for repository code to write
    `client_trips`.** The rule is applied to the exact value each row binds to
    `trip_distance_meters`, inside the same call that issues the statement, so
    a caller cannot construct rows, skip the gate and still reach the database
    through this path. A row above the cap is dropped, never corrected; if that
    leaves nothing to write, no statement is issued at all.

    `distance_index` is the position of `trip_distance_meters` in the row tuple
    and must match the caller's own column list. `provider_trip_id_index` is
    optional and only feeds the bounded rejection sample.
    """
    admissible = []
    rejected = 0
    for row in rows:
        try:
            distance = row[distance_index]
        except (IndexError, KeyError, TypeError):
            # A row shape this boundary cannot read is not silently written.
            raise ValueError(
                "client_trips row does not expose trip_distance_meters at "
                f"index {distance_index}"
            )
        provider_trip_id = None
        if provider_trip_id_index is not None:
            try:
                provider_trip_id = row[provider_trip_id_index]
            except (IndexError, KeyError, TypeError):
                provider_trip_id = None
        admitted = (
            gate.admits_distance(distance, provider_trip_id=provider_trip_id)
            if gate is not None
            else not distance_exceeds_cap(distance)
        )
        if admitted:
            admissible.append(row)
        else:
            rejected += 1

    if not admissible:
        return ClientTripsInsertResult(rowcount=0, rejected=rejected, executed=False)

    cur.executemany(sql, admissible)
    return ClientTripsInsertResult(
        rowcount=max(0, int(cur.rowcount or 0)),
        rejected=rejected,
        executed=True,
    )


class ClientTripsAdmission:
    """Stateful admission gate that aggregates a run's rejections.

    Observability is deliberately a bounded counter plus a small sample of
    provider trip ids, never a per-trip warning log: an over-cap burst from one
    faulty odometer register must not be able to flood the platform log.
    """

    def __init__(self, *, sample_limit: int = 20) -> None:
        self._sample_limit = max(0, int(sample_limit))
        self._rejected_by_reason: Dict[str, int] = {}
        self._rejected_sample: List[Any] = []
        self._max_rejected_distance: Optional[Decimal] = None

    # -- decisions ---------------------------------------------------------

    def admits_provider_trip(self, trip: Any, *, provider_trip_id: Any = None) -> bool:
        """Return `True` when this raw provider trip may reach persistence."""
        return self._record(evaluate_provider_trip(trip), provider_trip_id)

    def admits_distance(self, value: Any, *, provider_trip_id: Any = None) -> bool:
        """Return `True` when this extracted distance may reach persistence."""
        return self._record(evaluate_distance(value), provider_trip_id)

    def _record(self, verdict: AdmissionVerdict, provider_trip_id: Any) -> bool:
        if verdict.admitted:
            return True
        reason = verdict.reason or REASON_DISTANCE_OVER_CAP
        self._rejected_by_reason[reason] = self._rejected_by_reason.get(reason, 0) + 1
        if provider_trip_id is not None and len(self._rejected_sample) < self._sample_limit:
            self._rejected_sample.append(provider_trip_id)
        if verdict.distance_meters is not None and (
            self._max_rejected_distance is None
            or verdict.distance_meters > self._max_rejected_distance
        ):
            self._max_rejected_distance = verdict.distance_meters
        return False

    # -- observability -----------------------------------------------------

    @property
    def rejected_total(self) -> int:
        return sum(self._rejected_by_reason.values())

    @property
    def rejected_distance_over_cap(self) -> int:
        return self._rejected_by_reason.get(REASON_DISTANCE_OVER_CAP, 0)

    @property
    def rejected_by_reason(self) -> Dict[str, int]:
        return dict(sorted(self._rejected_by_reason.items()))

    def summary(self) -> Dict[str, Any]:
        """Bounded, PII-free run summary for job diagnostics/log context."""
        max_distance = self._max_rejected_distance
        return {
            REJECTED_COUNTER_KEY: self.rejected_distance_over_cap,
            REJECTED_MAX_DISTANCE_KEY: (
                int(max_distance) if max_distance is not None and max_distance == max_distance.to_integral_value()
                else (float(max_distance) if max_distance is not None else None)
            ),
            REJECTED_SAMPLE_KEY: list(self._rejected_sample),
        }
