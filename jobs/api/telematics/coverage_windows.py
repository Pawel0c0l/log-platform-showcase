"""Pure derivation and gating of stabilized Telematics ``trips_sync`` windows.

Specification of record: ``docs/13_telematics_trips_stabilization_windows.md``
§16.1 (exact formulas), §2 (closed-interval coverage contract), §2.4 (overlap
duration versus duplicated representable timestamps), §4.2 (``D`` cancels from
inter-fire contiguity), §5.2/§5.2.1 (bounded coverage interval and the
fail-closed preconditions) and §11 (``D``/``O`` are absolute UTC durations
applied *after* the local→UTC fire conversion). Delivery scope: C4 (window
arithmetic) and C5 (coverage-state models and the pure gate) of
``docs/14_telematics_trips_compatibility_implementation_plan.md``.

This module is **pure**. It performs no clock access, no environment access, no
timezone lookup, no logging, no database access and no I/O of any kind at import
time or at call time. Every input is supplied explicitly by the caller and is
validated; nothing is defaulted from configuration on the caller's behalf.
``now_utc`` is a caller-supplied parameter precisely so that the "``W`` is in the
future" precondition can be evaluated without reading a clock here.

Scope boundaries that are deliberate and load-bearing:

* It does **not** advance ``covered_through_ts`` and does not expose a projected
  advancement value. ``W := max(W, E_end)`` belongs to C6.
* It never writes, mutates or infers ``coverage_start_ts``. A derived window may
  legitimately start before ``coverage_start_ts``; that is *request* arithmetic
  only and asserts nothing about how far back coverage is claimed
  (``docs/13_…`` §5.2).
* It performs no persistence of any kind.
  ``CoverageGateResult.requires_gap_persistence`` is a *signal only*: it records
  that a durable ``READY → GAP_DETECTED`` transition is owed. The dispatcher's
  separate C6 finalizer may act on that signal; this pure module never does.
* ``CoverageState`` carries plain immutable values only — never a connection, a
  cursor, a logger or a live row object.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from jobs.trips_stabilization_config import (
    CLOSED_INTERVAL_GRID_STEP_SECONDS,
    validate_trips_stabilization_config,
)

SECONDS_PER_DAY = 86_400
CLOSED_INTERVAL_GRID_STEP = timedelta(seconds=CLOSED_INTERVAL_GRID_STEP_SECONDS)

# `bootstrap_status` vocabulary of the persisted coverage row (migration 057 /
# docs/13 §5.2). Only READY may run a compatibility fire.
COVERAGE_STATUS_UNINITIALIZED = "UNINITIALIZED"
COVERAGE_STATUS_READY = "READY"
COVERAGE_STATUS_GAP_DETECTED = "GAP_DETECTED"
COVERAGE_STATUS_RESEED_REQUIRED = "RESEED_REQUIRED"
COVERAGE_BOOTSTRAP_STATUSES = frozenset({
    COVERAGE_STATUS_UNINITIALIZED,
    COVERAGE_STATUS_READY,
    COVERAGE_STATUS_GAP_DETECTED,
    COVERAGE_STATUS_RESEED_REQUIRED,
})

# Durable abort classifications (docs/13 §5.2.1, §5.3).
TRIPS_COVERAGE_BOOTSTRAP_REQUIRED = "TRIPS_COVERAGE_BOOTSTRAP_REQUIRED"
TRIPS_COVERAGE_GAP_DETECTED = "TRIPS_COVERAGE_GAP_DETECTED"

# Gate classifications. Exactly one applies to any evaluation.
COVERAGE_GATE_ALLOWED = "ALLOWED"
COVERAGE_GATE_BOOTSTRAP_REQUIRED = "BOOTSTRAP_REQUIRED"
COVERAGE_GATE_GAP_DETECTED = "GAP_DETECTED"

# Reasons are operator-facing text bound into `logs.context`, so they are
# bounded and carry only identifiers and instants — never payloads or refs.
COVERAGE_GATE_REASON_MAX_CHARS = 300


@dataclass(frozen=True)
class EffectiveWindow:
    """Immutable result of one stabilized window derivation.

    ``nominal_*`` is what the schedule itself says (``[F − L, F]``) and is
    independent of ``D``, ``O``, ``R`` and the coverage interval.
    ``effective_*`` is the closed interval that a compatibility-mode fire would
    request. Both bounds of the supplied coverage interval are echoed back
    unchanged so that a caller records what the derivation was evaluated
    against.

    ``is_connected`` is the §5.3 contiguity classification
    ``E_start <= W + 1 s`` on the one-second closed grid. It is reported, never
    enforced: this module raises nothing for a disconnected window and emits no
    operational classification.
    """

    scheduled_fire_ts: datetime
    nominal_window_start_ts: datetime
    nominal_window_end_ts: datetime
    effective_window_start_ts: datetime
    effective_window_end_ts: datetime
    coverage_start_ts: datetime
    covered_through_ts: datetime
    stabilization_delay_seconds: int
    overlap_seconds: int
    max_recovery_span_seconds: int
    is_connected: bool


def _validate_utc_second_instant(*, field_name: str, value: object) -> datetime:
    """Require a timezone-aware, zero-offset, whole-second instant.

    The scheduled path is second-precision end to end (``docs/13_…`` §1.2:
    ``prepare_run`` truncates ``now_utc`` to whole seconds, ``run_time`` carries
    whole seconds and ``_provider_dt_str`` truncates to seconds), so a sub-second
    input has no meaning here. It is rejected rather than rounded or truncated,
    because either choice would invent a precision contract this repository does
    not have.
    """
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a datetime; got {value!r}")
    offset = value.utcoffset()
    if offset is None:
        raise ValueError(
            f"{field_name} must be timezone-aware UTC; got a naive datetime"
        )
    if offset != timedelta(0):
        raise ValueError(
            f"{field_name} must be UTC (zero offset); got offset {offset!r}"
        )
    if value.microsecond != 0:
        raise ValueError(
            f"{field_name} must be a whole-second instant; got {value!r}"
        )
    return value.astimezone(timezone.utc)


def _validate_lookback_days(value: object) -> int:
    if type(value) is not int:
        raise ValueError(f"lookback_days must be an integer; got {value!r}")
    if value < 0:
        raise ValueError(f"lookback_days must be >= 0; got {value!r}")
    return value


def derive_effective_window(
    *,
    scheduled_fire_ts: object,
    lookback_days: object,
    stabilization_delay_seconds: object,
    overlap_seconds: object,
    max_recovery_span_seconds: object,
    coverage_start_ts: object,
    covered_through_ts: object,
) -> EffectiveWindow:
    """Derive the nominal and stabilized effective windows for one fire.

    ``docs/13_…`` §16.1, with every quantity an absolute UTC instant or an
    absolute second count::

        N_end           = F
        N_start         = F − L                       L = lookback_days × 86400
        E_end           = F − D
        base            = (F − L) − D − O
        candidate_start = min(base, W − O)
        E_start         = max(candidate_start, E_end − R)
        is_connected    = E_start <= W + 1 s

    ``D`` and ``O`` are subtracted from instants that are already UTC, so the
    arithmetic is elapsed-duration arithmetic only; no calendar-day or local
    wall-clock subtraction is performed anywhere (``docs/13_…`` §11, §16.8).
    ``E_start`` is never clamped to ``coverage_start_ts``.
    """
    fire = _validate_utc_second_instant(
        field_name="scheduled_fire_ts", value=scheduled_fire_ts
    )
    coverage_start = _validate_utc_second_instant(
        field_name="coverage_start_ts", value=coverage_start_ts
    )
    covered_through = _validate_utc_second_instant(
        field_name="covered_through_ts", value=covered_through_ts
    )
    if coverage_start > covered_through:
        raise ValueError(
            "coverage_start_ts must not be after covered_through_ts; got "
            f"{coverage_start!r} > {covered_through!r}"
        )

    lookback = _validate_lookback_days(lookback_days)
    delay, overlap, recovery = validate_trips_stabilization_config(
        stabilization_delay_seconds=stabilization_delay_seconds,
        overlap_seconds=overlap_seconds,
        max_recovery_span_seconds=max_recovery_span_seconds,
    )

    lookback_duration = timedelta(seconds=lookback * SECONDS_PER_DAY)
    delay_duration = timedelta(seconds=delay)
    overlap_duration = timedelta(seconds=overlap)
    recovery_duration = timedelta(seconds=recovery)

    nominal_end = fire
    nominal_start = fire - lookback_duration

    effective_end = fire - delay_duration
    base_start = nominal_start - delay_duration - overlap_duration
    candidate_start = min(base_start, covered_through - overlap_duration)
    effective_start = max(candidate_start, effective_end - recovery_duration)

    is_connected = effective_start <= covered_through + CLOSED_INTERVAL_GRID_STEP

    return EffectiveWindow(
        scheduled_fire_ts=fire,
        nominal_window_start_ts=nominal_start,
        nominal_window_end_ts=nominal_end,
        effective_window_start_ts=effective_start,
        effective_window_end_ts=effective_end,
        coverage_start_ts=coverage_start,
        covered_through_ts=covered_through,
        stabilization_delay_seconds=delay,
        overlap_seconds=overlap,
        max_recovery_span_seconds=recovery,
        is_connected=is_connected,
    )


# ---------------------------------------------------------------------------
# C5 — coverage state and the pure fail-closed gate (docs/13 §5.2.1)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CoverageState:
    """Immutable value view of one persisted coverage row.

    The twelve claim-time fields required by the C6 compare-and-swap contract
    are represented: identity, the bounded closed interval ``[A, W]``, status,
    reviewed-seed metadata, watermark provenance and the prior gap timestamp.
    The pure gate deliberately ignores the last two fields. ``updated_at`` is
    excluded by the approved contract.

    This is a plain immutable carrier. It never holds a connection, a cursor, a
    logger or a mutable row object, and it performs no validation of its own —
    validation is the gate's single, testable responsibility.

    **Identity since M5.** The row is owned by ``(client_id, dataset_name)`` and
    shared by every cadence over that dataset. ``schedule_id`` is retained
    because it is a real column and one of the eleven CAS fields, but it is the
    *originating* schedule — immutable provenance, written once by the bootstrap
    writer — and it is not the row's identity. For a reconciliation fire it will
    not equal the firing schedule, and that is correct rather than a mismatch.
    """

    #: Immutable originating-schedule provenance. NOT the coverage identity;
    #: see the class docstring. Kept first because it is the first column of the
    #: canonical fingerprint projection, whose field order is part of
    #: ``telematics-coverage-fingerprint/1`` and must not move.
    schedule_id: str
    client_id: str
    client_code: Optional[str]
    dataset_name: str
    coverage_start_ts: Optional[datetime]
    covered_through_ts: Optional[datetime]
    bootstrap_status: Optional[str]
    bootstrap_evidence_ref: Optional[str]
    seeded_at: Optional[datetime]
    seeded_by: Optional[str]
    covered_through_source: Optional[str]
    last_gap_detected_ts: Optional[datetime]


@dataclass(frozen=True)
class CoverageGateResult:
    """Immutable, deterministic decision for one compatibility-mode fire.

    ``effective_window`` is populated **only** when ``allowed`` is true. That is
    structural, not incidental: a rejected fire must claim its nominal window
    (``docs/14_…`` §7.1 step 3), so a rejected result cannot hand a caller an
    effective window to claim by accident.

    ``requires_gap_persistence`` reports that a durable
    ``bootstrap_status = 'GAP_DETECTED'`` transition is *owed* for this row. It
    is deliberately advisory: neither this module nor any C5 caller may perform
    that write. Owning it — including its transaction boundary, concurrency
    predicate, gap timestamp and explicit row-update timestamp — is C6 under
    the G-COV review gate. A row that is *already* ``GAP_DETECTED`` reports
    ``False``, because there is nothing left to record.

    ``coverage_start_ts`` / ``covered_through_ts`` echo the *original*
    immutable bounds that were evaluated, so a caller can bind its concurrency
    predicate to exactly the row it decided on. They are ``None`` when no usable
    bound was read.
    """

    allowed: bool
    classification: str
    abort_code: Optional[str]
    reason: str
    effective_window: Optional[EffectiveWindow]
    requires_gap_persistence: bool
    coverage_start_ts: Optional[datetime]
    covered_through_ts: Optional[datetime]
    bootstrap_status: Optional[str]


def _bounded_reason(text: str) -> str:
    reason = " ".join(str(text).split())
    if len(reason) > COVERAGE_GATE_REASON_MAX_CHARS:
        return reason[: COVERAGE_GATE_REASON_MAX_CHARS - 1] + "…"
    return reason


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _is_blank(value: object) -> bool:
    return not isinstance(value, str) or not value.strip()


def _aware_instant_or_none(value: object) -> Optional[datetime]:
    """Return a tz-aware datetime, or ``None`` for any malformed input."""
    if not isinstance(value, datetime):
        return None
    if value.utcoffset() is None:
        return None
    return value


def _coverage_bound_or_none(value: object) -> Optional[datetime]:
    """Normalize a stored coverage bound to a whole-second UTC instant.

    A `TIMESTAMPTZ` is an absolute instant; the offset psycopg renders it with
    is a property of the session's `TimeZone` (the platform sets the business
    timezone), not of the stored value. Any aware instant is therefore accepted
    and converted to UTC. A naive value or sub-second precision is malformed
    state and returns ``None`` so the caller can fail closed — the scheduled
    path is whole-second end to end and neither rounding nor truncating a bound
    would be an honest reading of a coverage claim.
    """
    instant = _aware_instant_or_none(value)
    if instant is None or instant.microsecond != 0:
        return None
    return instant.astimezone(timezone.utc)


def _validate_foundational_verified_state(
    *,
    state: CoverageState,
    now_utc: datetime,
) -> tuple[Optional[datetime], Optional[datetime], Optional[str]]:
    """Validate structure shared by statuses that claim a verified interval.

    ``READY`` and ``GAP_DETECTED`` both assert that ``[A, W]`` was previously
    verified. The status string is not evidence by itself, so neither status
    receives semantic treatment until the same bounds, evidence and seed
    metadata checks pass. The returned reason is bounded later by the single
    bootstrap-rejection constructor; this helper performs no I/O or mutation.

    ``seeded_at`` keeps the accepted C5 contract: it must be an aware datetime.
    Unlike coverage bounds, the existing contract does not require whole-second
    precision for seed metadata, so this helper does not invent that constraint.
    """
    if state.coverage_start_ts is None:
        return None, None, (
            "coverage_start_ts is NULL; there is no verified lower bound"
        )
    if state.covered_through_ts is None:
        return None, None, (
            "covered_through_ts is NULL; there is no verified upper bound"
        )

    coverage_start = _coverage_bound_or_none(state.coverage_start_ts)
    covered_through = _coverage_bound_or_none(state.covered_through_ts)
    if coverage_start is None or covered_through is None:
        return None, None, (
            "coverage bounds are not timezone-aware whole-second instants"
        )
    if coverage_start > covered_through:
        return coverage_start, covered_through, (
            "coverage_start_ts is later than covered_through_ts"
        )
    if covered_through > now_utc:
        return coverage_start, covered_through, (
            "covered_through_ts is in the future relative to now_utc"
        )
    if _is_blank(state.bootstrap_evidence_ref):
        return coverage_start, covered_through, (
            "bootstrap_evidence_ref is absent or blank for a verified coverage claim"
        )
    if _aware_instant_or_none(state.seeded_at) is None:
        return coverage_start, covered_through, (
            "seeded_at is absent or not a timezone-aware instant"
        )
    if _is_blank(state.seeded_by):
        return coverage_start, covered_through, (
            "seeded_by is absent or blank for a verified coverage claim"
        )
    return coverage_start, covered_through, None

def _bootstrap_required(
    *,
    reason: str,
    state: Optional[CoverageState],
    coverage_start_ts: Optional[datetime] = None,
    covered_through_ts: Optional[datetime] = None,
) -> CoverageGateResult:
    return CoverageGateResult(
        allowed=False,
        classification=COVERAGE_GATE_BOOTSTRAP_REQUIRED,
        abort_code=TRIPS_COVERAGE_BOOTSTRAP_REQUIRED,
        reason=_bounded_reason(reason),
        effective_window=None,
        requires_gap_persistence=False,
        coverage_start_ts=coverage_start_ts,
        covered_through_ts=covered_through_ts,
        bootstrap_status=(state.bootstrap_status if state is not None else None),
    )


def evaluate_coverage_gate(
    *,
    schedule_id: object,
    client_id: object,
    client_code: object,
    dataset_name: object,
    scheduled_fire_ts: object,
    lookback_days: object,
    stabilization_delay_seconds: object,
    overlap_seconds: object,
    max_recovery_span_seconds: object,
    coverage_state: object,
    now_utc: object,
) -> CoverageGateResult:
    """Decide whether one ``data_invariants_v1`` scheduled fire may run.

    Implements ``docs/13_…`` §5.2.1, and then — for a state that passes it —
    the §5.3 contiguity classification over the C4 arithmetic:

    * every §5.2.1 precondition failure yields
      ``TRIPS_COVERAGE_BOOTSTRAP_REQUIRED``: a missing row, a ``NULL`` or
      malformed bound, reversed bounds, a ``W`` in the future, missing evidence
      or seed metadata, an identity mismatch, an unknown status, and the
      ``UNINITIALIZED`` and ``RESEED_REQUIRED`` states — every state that has
      never constituted a verified ``READY`` interval;
    * a structurally valid existing ``GAP_DETECTED`` row is the **one narrow
      exception** to generic non-``READY`` handling. It re-emits
      ``TRIPS_COVERAGE_GAP_DETECTED`` on every later due fire because the
      condition is a known unclosed hole in a previously verified interval.
      A malformed ``GAP_DETECTED`` row fails the shared foundational validation
      above and remains bootstrap-required. ``requires_gap_persistence`` is
      ``False`` for the valid recorded gap: the transition is already recorded
      and C5 rewrites nothing;
    * a valid ``READY`` state whose derived window satisfies
      ``E_start <= W + 1 s`` is allowed;
    * a valid ``READY`` state whose derived window is genuinely disconnected —
      reachable only when the ``R`` cap binds across a hole wider than ``R`` —
      yields ``TRIPS_COVERAGE_GAP_DETECTED`` with
      ``requires_gap_persistence = True``. That flag is a signal for C6; C5
      refuses the fire and leaves the coverage row untouched.

    Nothing here is repaired, defaulted or inferred. A bound is never derived
    from the current fire, from ``E_start``/``E_end``, from schedule history,
    from client trips or from a synchronization timestamp.

    The schedule-scoped inputs (``scheduled_fire_ts``, ``lookback_days``,
    ``D``/``O``/``R``) are *configuration*, not coverage state: an invalid one
    raises ``ValueError`` rather than being mislabelled as a coverage problem.
    Coverage-row problems never raise — they are returned as a rejection so the
    caller can leave durable terminal evidence.
    """
    fire = _validate_utc_second_instant(
        field_name="scheduled_fire_ts", value=scheduled_fire_ts
    )
    now = _validate_utc_second_instant(field_name="now_utc", value=now_utc)
    lookback = _validate_lookback_days(lookback_days)
    delay, overlap, recovery = validate_trips_stabilization_config(
        stabilization_delay_seconds=stabilization_delay_seconds,
        overlap_seconds=overlap_seconds,
        max_recovery_span_seconds=max_recovery_span_seconds,
    )

    if coverage_state is None:
        return _bootstrap_required(
            reason="no coverage row exists for this client and dataset",
            state=None,
        )
    if not isinstance(coverage_state, CoverageState):
        raise ValueError(
            "coverage_state must be a CoverageState or None; got "
            f"{type(coverage_state).__name__}"
        )
    state = coverage_state

    # Identity. A row that does not describe this exact client/dataset is refused
    # rather than used; it is never "corrected" to match.
    #
    # `schedule_id` is deliberately NOT compared against the fired schedule (M5).
    # The coverage row is owned by `(client_id, dataset_name)` and shared by every
    # cadence over that dataset, so its `schedule_id` names the schedule that
    # seeded the watermark — which for a reconciliation fire is a different
    # schedule by design. Comparing them would refuse exactly the fires M5 exists
    # to make possible, and would refuse them as "bootstrap required", which is
    # the wrong diagnosis for a perfectly well-formed row.
    #
    # Nothing is weakened by dropping it: the caller loads the row BY this
    # client and dataset, so a mismatch on either is already impossible-by-
    # construction here and is still checked below in case a caller ever hands
    # over a row it did not load itself.
    if str(state.client_id) != str(client_id):
        return _bootstrap_required(
            reason=(
                "coverage row client_id does not match the schedule that fired "
                f"({schedule_id})"
            ),
            state=state,
        )
    if str(state.dataset_name) != str(dataset_name):
        return _bootstrap_required(
            reason=(
                "coverage row dataset_name does not match the schedule that "
                f"fired ({schedule_id})"
            ),
            state=state,
        )
    expected_code = str(client_code or "").strip()
    observed_code = str(state.client_code or "").strip()
    if expected_code and observed_code and expected_code != observed_code:
        return _bootstrap_required(
            reason="coverage row client_code contradicts the fired schedule",
            state=state,
        )

    status = state.bootstrap_status
    if not isinstance(status, str) or status not in COVERAGE_BOOTSTRAP_STATUSES:
        return _bootstrap_required(
            reason="coverage row bootstrap_status is outside the accepted vocabulary",
            state=state,
        )
    if status not in {COVERAGE_STATUS_READY, COVERAGE_STATUS_GAP_DETECTED}:
        return _bootstrap_required(
            reason=f"coverage bootstrap_status is {status}, expected READY",
            state=state,
        )

    coverage_start, covered_through, foundational_error = (
        _validate_foundational_verified_state(state=state, now_utc=now)
    )
    if foundational_error is not None:
        return _bootstrap_required(
            reason=foundational_error,
            state=state,
            coverage_start_ts=coverage_start,
            covered_through_ts=covered_through,
        )

    # The foundational validator returns both bounds on success.
    assert coverage_start is not None and covered_through is not None

    if status == COVERAGE_STATUS_GAP_DETECTED:
        # The narrow, deliberate exception to the generic non-READY rule. The
        # structurally verified hole is already recorded, so re-reporting it as
        # "bootstrap required" would understate a known data gap. The status
        # string alone never reaches this branch. C5 changes nothing.
        return CoverageGateResult(
            allowed=False,
            classification=COVERAGE_GATE_GAP_DETECTED,
            abort_code=TRIPS_COVERAGE_GAP_DETECTED,
            reason=_bounded_reason(
                "coverage row is already GAP_DETECTED; an unclosed hole stays "
                "loud until an authorized recovery or reviewed reseed"
            ),
            effective_window=None,
            requires_gap_persistence=False,
            coverage_start_ts=coverage_start,
            covered_through_ts=covered_through,
            bootstrap_status=status,
        )

    window = derive_effective_window(
        scheduled_fire_ts=fire,
        lookback_days=lookback,
        stabilization_delay_seconds=delay,
        overlap_seconds=overlap,
        max_recovery_span_seconds=recovery,
        coverage_start_ts=coverage_start,
        covered_through_ts=covered_through,
    )

    # Defensive and arithmetically unreachable: both `min(base, W − O)` and
    # `E_end − R` are <= E_end, so `E_start <= E_end` always holds. Kept as a
    # fail-closed rejection rather than an assertion so that a future formula
    # change cannot launch an inverted window. `E_start == E_end` is a valid
    # degenerate closed interval and is deliberately NOT rejected
    # (docs/14 §7.1 step 3).
    if window.effective_window_start_ts > window.effective_window_end_ts:
        return _bootstrap_required(
            reason="derived effective window is inverted",
            state=state,
            coverage_start_ts=coverage_start,
            covered_through_ts=covered_through,
        )

    if not window.is_connected:
        return CoverageGateResult(
            allowed=False,
            classification=COVERAGE_GATE_GAP_DETECTED,
            abort_code=TRIPS_COVERAGE_GAP_DETECTED,
            reason=_bounded_reason(
                "effective window start "
                f"{_iso_utc(window.effective_window_start_ts)} is later than "
                f"covered_through_ts {_iso_utc(covered_through)} + 1 s"
            ),
            effective_window=None,
            requires_gap_persistence=True,
            coverage_start_ts=coverage_start,
            covered_through_ts=covered_through,
            bootstrap_status=status,
        )

    return CoverageGateResult(
        allowed=True,
        classification=COVERAGE_GATE_ALLOWED,
        abort_code=None,
        reason=_bounded_reason(
            "READY coverage claim is connected to the derived effective window"
        ),
        effective_window=window,
        requires_gap_persistence=False,
        coverage_start_ts=coverage_start,
        covered_through_ts=covered_through,
        bootstrap_status=status,
    )
