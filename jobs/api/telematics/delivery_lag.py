"""Pure derivation of the observed provider delivery lag and its daily slice.

Specification of record: ``docs/21_telematics_delivery_lag_trace.md`` §3 (the
metric and its exact edge semantics), §4 (NULL and negative values), §6 (why the
daily slice is recomputed rather than incremented), §7 (buckets, percentiles and
the weekly-guarantee boundary), §8 (discovery attribution).

This module is **pure**. No clock, no environment, no timezone lookup, no
database, no logging and no I/O at import time or at call time. Every input is
supplied by the caller. That is what lets the whole metric contract — including
the negative case, the empty case and the bucket partition — be proven without a
database.

THE ONE CALCULATION.
    ``observed_delivery_lag_seconds`` is computed here and nowhere else. The
    subtraction is trivial, which is exactly why it would otherwise be re-typed
    in a query, a report and a test until three of them disagreed about the
    NULL case or the sign.

WHAT THE METRIC IS, AND IS NOT.
    It is the interval between the instant our ingestion FIRST observed a trip
    through the provider API and that trip's CURRENT end timestamp::

        observed_delivery_lag_seconds
            = first_seen_response_received_at_utc - end_timestamp

    It is an **upper bound** on the provider's unknown publication lag, because
    our own polling cadence contributes to it: a trip published one minute after
    it ended but first requested three days later measures three days. It is
    therefore never called ``provider_publication_lag``, and no code here emits
    a provider publication instant. Narrowing the bound needs the last-absent
    interval, which is deferred (``docs/21`` §9).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# The weekly guaranteed capture horizon
# ---------------------------------------------------------------------------
#
# `docs/20` §3.7c: for a cadence of period `P` days at lookback `L`, with
# stabilization delay `D` and overlap `O`, the largest publication lag guaranteed
# to be captured for EVERY trip-start phase is
#
#     guaranteed_horizon_days = L + (D + O)/86400 - P
#
# The two guarantee buckets are computed against that value rather than a
# decimal literal, so a lookback or tuning change moves the boundary instead of
# leaving a stale magic number behind. `docs/20` §3.7c carried `0.208` for the
# constant at one point; it is `(D + O)/86400`, and deriving it is how that class
# of error stops being possible.

SECONDS_PER_DAY = 86_400


def guaranteed_horizon_seconds(
    *,
    lookback_days: int,
    cadence_period_days: int,
    stabilization_delay_seconds: int,
    overlap_seconds: int,
) -> int:
    """`L + (D + O)/86400 - P`, in whole seconds.

    Whole seconds because the value is a bucket boundary compared against
    integer second lags; carrying a float here would make boundary membership
    depend on binary rounding.
    """
    for name, value in (
        ("lookback_days", lookback_days),
        ("cadence_period_days", cadence_period_days),
        ("stabilization_delay_seconds", stabilization_delay_seconds),
        ("overlap_seconds", overlap_seconds),
    ):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be an integer; got {value!r}")
        if value < 0:
            raise ValueError(f"{name} must be >= 0; got {value!r}")
    if cadence_period_days < 1:
        raise ValueError("cadence_period_days must be >= 1")
    horizon = (
        lookback_days * SECONDS_PER_DAY
        + stabilization_delay_seconds
        + overlap_seconds
        - cadence_period_days * SECONDS_PER_DAY
    )
    if horizon <= 0:
        raise ValueError(
            "the cadence guarantees no horizon at all "
            f"({horizon} s); a bucket boundary cannot be derived from it"
        )
    return horizon


#: The M6 cadence, as committed. Kept here as the single place a caller can get
#: the in-force weekly boundary without restating the arithmetic. It is a
#: DEFAULT, not a constant: the aggregation runner derives the boundary from the
#: client's live schedule and only falls back to this when no reconciliation
#: cadence exists yet — which is the state today, since M6 is committed but no
#: production row exists.
M6_WEEKLY_LOOKBACK_DAYS = 16
M6_WEEKLY_CADENCE_PERIOD_DAYS = 7
DEFAULT_STABILIZATION_DELAY_SECONDS = 10_800
DEFAULT_OVERLAP_SECONDS = 3_600


def m6_weekly_guarantee_seconds() -> int:
    """The committed M6 weekly guarantee: 9.1667 d = 792 000 s."""
    return guaranteed_horizon_seconds(
        lookback_days=M6_WEEKLY_LOOKBACK_DAYS,
        cadence_period_days=M6_WEEKLY_CADENCE_PERIOD_DAYS,
        stabilization_delay_seconds=DEFAULT_STABILIZATION_DELAY_SECONDS,
        overlap_seconds=DEFAULT_OVERLAP_SECONDS,
    )


# ---------------------------------------------------------------------------
# The metric
# ---------------------------------------------------------------------------

#: A trip observed before its own end. Expected, not anomalous: the documented
#: `/trips` overlap rule returns trips extending past the requested boundary, so
#: a still-open trip can be returned and have its `end_timestamp` corrected by a
#: later fire. Reported as its own class rather than clamped to zero.
OBSERVED_BEFORE_TRIP_END = "OBSERVED_BEFORE_TRIP_END"

#: Provenance was never captured. Pre-M4 rows, the insert-only backfill path and
#: any `strict_meta` run. Excluded from every metric, never imputed.
PROVENANCE_ABSENT = "PROVENANCE_ABSENT"

#: An ordinary measured lag.
OBSERVED_AFTER_TRIP_END = "OBSERVED_AFTER_TRIP_END"

LAG_CLASSIFICATIONS = frozenset({
    OBSERVED_AFTER_TRIP_END,
    OBSERVED_BEFORE_TRIP_END,
    PROVENANCE_ABSENT,
})


# ---------------------------------------------------------------------------
# Provenance state — the EXPAND-window distinction
# ---------------------------------------------------------------------------
#
# During the expand-contract rollout a NULL `first_seen_response_received_at_utc`
# means one of TWO different things, and conflating them would make the aggregate
# lie about its own completeness:
#
#   NO_PROVENANCE                  both halves NULL. The observation was never
#                                  recorded and never can be. Permanent.
#   PROVENANCE_TIMESTAMP_PENDING   the identity is present, the instant is not.
#                                  The observation DID happen and is recoverable
#                                  from the exact platform request row, until
#                                  that row is pruned. Transitional.
#   PROVENANCE_COMPLETE            both halves present.
#
# The distinction is DERIVED from the two columns rather than stored: a third
# column would be a redundant state machine that could disagree with the columns
# it describes. It is surfaced in the daily slice as a count, which is the level
# at which it changes a reader's interpretation.

PROVENANCE_COMPLETE = "PROVENANCE_COMPLETE"
PROVENANCE_TIMESTAMP_PENDING = "PROVENANCE_TIMESTAMP_PENDING"
NO_PROVENANCE = "NO_PROVENANCE"

PROVENANCE_STATES = frozenset({
    PROVENANCE_COMPLETE,
    PROVENANCE_TIMESTAMP_PENDING,
    NO_PROVENANCE,
})


def provenance_state(
    *,
    first_seen_request_id: Optional[str],
    first_seen_response_received_at_utc: Optional[datetime],
) -> str:
    """Classify one row's first-seen provenance.

    The fourth combination — an instant with no identity — is refused by
    `ck_client_trips_first_seen_instant_needs_request` from the EXPAND migration
    onward, so it cannot be stored. It is still classified rather than raised,
    because a caller may be examining a row read from a database that predates
    the constraint, and a metric helper must not be the thing that crashes on it.
    """
    has_id = first_seen_request_id is not None and str(
        first_seen_request_id
    ).strip() != ""
    has_ts = first_seen_response_received_at_utc is not None
    if has_ts and has_id:
        return PROVENANCE_COMPLETE
    if has_ts and not has_id:
        # Structurally impossible from EXPAND onward. Reported as complete for
        # the lag (the instant is real) but it is a shape to investigate.
        return PROVENANCE_COMPLETE
    if has_id:
        return PROVENANCE_TIMESTAMP_PENDING
    return NO_PROVENANCE


def _aware_utc(value: object, *, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{field} must be a datetime; got {value!r}")
    if value.utcoffset() is None:
        raise ValueError(
            f"{field} must be timezone-aware; a naive value has no absolute "
            "meaning and the session timezone must never decide a lag"
        )
    return value.astimezone(timezone.utc)


def observed_delivery_lag_seconds(
    *,
    first_seen_response_received_at_utc: Optional[datetime],
    end_timestamp: Optional[datetime],
) -> Optional[int]:
    """The canonical metric. ``None`` when it is not defined.

    ``None`` for a missing first-seen instant (provenance was never captured)
    and for a missing trip end (nothing to measure against). Both are returned
    rather than raised: a fleet contains such rows legitimately, and a metric
    that raises on them would push every caller into inventing its own filter.

    Both operands are normalized to UTC before subtracting, so a caller that
    reads one from a session in a business timezone and one from a session in
    UTC still gets the same answer. A naive datetime is refused outright.
    """
    if first_seen_response_received_at_utc is None or end_timestamp is None:
        return None
    observed = _aware_utc(
        first_seen_response_received_at_utc,
        field="first_seen_response_received_at_utc",
    )
    ended = _aware_utc(end_timestamp, field="end_timestamp")
    return int((observed - ended).total_seconds())


def classify_lag(lag_seconds: Optional[int]) -> str:
    if lag_seconds is None:
        return PROVENANCE_ABSENT
    if lag_seconds < 0:
        return OBSERVED_BEFORE_TRIP_END
    return OBSERVED_AFTER_TRIP_END


# ---------------------------------------------------------------------------
# Buckets
# ---------------------------------------------------------------------------

#: Bucket names in slice order. `weekly_guarantee` is a placeholder resolved at
#: call time from the cadence in force, which is why the boundaries are a
#: function and not a module constant.
BUCKET_NAMES = (
    "bucket_negative",
    "bucket_under_6h",
    "bucket_6h_to_24h",
    "bucket_1d_to_3d",
    "bucket_3d_to_7d",
    "bucket_7d_to_weekly_guarantee",
    "bucket_weekly_guarantee_to_15d",
    "bucket_over_15d",
)


def bucket_for(lag_seconds: int, *, weekly_guarantee_seconds: int) -> str:
    """Half-open upward: a lag exactly on a boundary lands in the upper bucket.

    Stated explicitly because "3 days" is precisely the value an operator will
    construct a fixture from, and leaving it to `<` versus `<=` accident is how
    two consumers end up disagreeing about the same number.
    """
    if lag_seconds < 0:
        return "bucket_negative"
    if lag_seconds < 6 * 3600:
        return "bucket_under_6h"
    if lag_seconds < 24 * 3600:
        return "bucket_6h_to_24h"
    if lag_seconds < 3 * SECONDS_PER_DAY:
        return "bucket_1d_to_3d"
    if lag_seconds < 7 * SECONDS_PER_DAY:
        return "bucket_3d_to_7d"
    if lag_seconds < weekly_guarantee_seconds:
        return "bucket_7d_to_weekly_guarantee"
    if lag_seconds < 15 * SECONDS_PER_DAY:
        return "bucket_weekly_guarantee_to_15d"
    return "bucket_over_15d"


# ---------------------------------------------------------------------------
# Percentiles
# ---------------------------------------------------------------------------

def percentile_nearest_rank(values: Sequence[int], *, p: float) -> int:
    """Nearest-rank percentile over a sorted-ascending sequence.

    Nearest-rank, not linear interpolation: every value returned is an actually
    observed lag. An interpolated p90 is a number no trip ever had, which is the
    wrong thing to put in front of an operator deciding a reconciliation depth,
    and it would also break the stored monotonicity CHECK's meaning.
    """
    if not values:
        raise ValueError("an empty sample has no percentile")
    if not 0 < p <= 100:
        raise ValueError(f"p must be in (0, 100]; got {p!r}")
    ordered = sorted(values)
    # ceil(p/100 * n), 1-indexed, clamped into range.
    rank = -(-int(round(p * len(ordered))) // 100)
    rank = max(1, min(rank, len(ordered)))
    return ordered[rank - 1]


# ---------------------------------------------------------------------------
# Discovery attribution
# ---------------------------------------------------------------------------

#: Column each `run_type` contributes to. `None`/unknown never falls back to the
#: base role: attributing a trip to DAILY because its evidence expired would
#: invent the exact answer the metric exists to provide.
DISCOVERY_COLUMNS = {
    "DAILY": "discovered_daily",
    "WEEKLY_RECONCILIATION": "discovered_weekly_reconciliation",
    "MONTHLY_RECONCILIATION": "discovered_monthly_reconciliation",
}
DISCOVERY_UNATTRIBUTED = "discovered_unattributed"


def discovery_column_for(run_type: Optional[str]) -> str:
    return DISCOVERY_COLUMNS.get(
        str(run_type or "").strip(), DISCOVERY_UNATTRIBUTED
    )


# ---------------------------------------------------------------------------
# The slice
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TripObservation:
    """One trip, as the aggregator needs it. Immutable value only.

    ``end_timestamp`` is the trip's CURRENT end — the mutable one — and
    ``first_seen_response_received_at_utc`` is the immutable observation. That
    asymmetry is the whole design: the slice is recomputed so the mutable side
    can move.
    """

    provider_trip_id: int
    end_timestamp: Optional[datetime]
    first_seen_response_received_at_utc: Optional[datetime]
    #: Resolved `run_type` of the schedule whose fire first observed this trip,
    #: or ``None`` when the platform request row no longer resolves.
    first_seen_run_type: Optional[str] = None
    #: The identity half. Carried so the slice can tell an EXPAND-window
    #: PROVENANCE_TIMESTAMP_PENDING row apart from a genuine NO_PROVENANCE one —
    #: both have a NULL instant and both are excluded from the distribution, but
    #: only the first one will still change once enrichment runs.
    first_seen_request_id: Optional[str] = None


@dataclass(frozen=True)
class DailyLagSlice:
    """The complete projection of one ``(client, trip_end_date)``."""

    trip_end_date: date
    trips_total: int
    trips_with_provenance: int
    #: Rows in the EXPAND-window PROVENANCE_TIMESTAMP_PENDING state. While this
    #: is non-zero the slice is NOT historically complete: those trips have a
    #: real, recoverable observation that is not yet in the distribution. A
    #: consumer must read this before quoting a percentile as settled.
    trips_provenance_pending: int
    lag_p50_seconds: Optional[int]
    lag_p90_seconds: Optional[int]
    lag_p95_seconds: Optional[int]
    lag_max_seconds: Optional[int]
    lag_min_seconds: Optional[int]
    buckets: Dict[str, int]
    discovery: Dict[str, int]
    weekly_guarantee_seconds: int

    def as_row(self) -> Dict[str, object]:
        """Flat mapping matching migration 063's column names."""
        row: Dict[str, object] = {
            "trip_end_date": self.trip_end_date,
            "trips_total": self.trips_total,
            "trips_with_provenance": self.trips_with_provenance,
            "trips_provenance_pending": self.trips_provenance_pending,
            "lag_p50_seconds": self.lag_p50_seconds,
            "lag_p90_seconds": self.lag_p90_seconds,
            "lag_p95_seconds": self.lag_p95_seconds,
            "lag_max_seconds": self.lag_max_seconds,
            "lag_min_seconds": self.lag_min_seconds,
            "weekly_guarantee_seconds": self.weekly_guarantee_seconds,
        }
        row.update(self.buckets)
        row.update(self.discovery)
        return row


def build_daily_slice(
    *,
    trip_end_date: date,
    observations: Iterable[TripObservation],
    weekly_guarantee_seconds: int,
) -> DailyLagSlice:
    """Project one date's trips into its slice. Deterministic and total.

    Deterministic: the result depends only on the observations supplied, so
    recomputing a date yields byte-identical metrics and the upsert is
    idempotent. Total: every observation lands in exactly one bucket and exactly
    one discovery column, which is what makes migration 063's two partition
    CHECKs satisfiable rather than aspirational.
    """
    if weekly_guarantee_seconds <= 0:
        raise ValueError(
            f"weekly_guarantee_seconds must be positive; got "
            f"{weekly_guarantee_seconds!r}"
        )

    buckets = {name: 0 for name in BUCKET_NAMES}
    discovery = {name: 0 for name in DISCOVERY_COLUMNS.values()}
    discovery[DISCOVERY_UNATTRIBUTED] = 0

    lags: List[int] = []
    total = 0
    pending = 0
    seen_ids = set()
    for obs in observations:
        # A duplicate provider_trip_id inside one slice would double-count.
        # `(client_id, provider_trip_id)` already makes that impossible in the
        # database, so this is defence against a caller assembling the input
        # wrongly rather than against the data.
        if obs.provider_trip_id in seen_ids:
            continue
        seen_ids.add(obs.provider_trip_id)
        total += 1
        lag = observed_delivery_lag_seconds(
            first_seen_response_received_at_utc=(
                obs.first_seen_response_received_at_utc
            ),
            end_timestamp=obs.end_timestamp,
        )
        if lag is None:
            # Excluded from the distribution AND from the attribution, so both
            # partitions still sum to the same denominator. But WHY it is
            # excluded matters: a PROVENANCE_TIMESTAMP_PENDING row will re-enter
            # the distribution once enrichment runs, while a NO_PROVENANCE row
            # never will. Counting the former is what stops a transitional slice
            # from being read as settled.
            if provenance_state(
                first_seen_request_id=obs.first_seen_request_id,
                first_seen_response_received_at_utc=(
                    obs.first_seen_response_received_at_utc
                ),
            ) == PROVENANCE_TIMESTAMP_PENDING:
                pending += 1
            continue
        lags.append(lag)
        buckets[bucket_for(lag, weekly_guarantee_seconds=weekly_guarantee_seconds)] += 1
        discovery[discovery_column_for(obs.first_seen_run_type)] += 1

    if lags:
        p50 = percentile_nearest_rank(lags, p=50)
        p90 = percentile_nearest_rank(lags, p=90)
        p95 = percentile_nearest_rank(lags, p=95)
        lag_max: Optional[int] = max(lags)
        lag_min: Optional[int] = min(lags)
    else:
        p50 = p90 = p95 = None
        lag_max = lag_min = None

    return DailyLagSlice(
        trip_end_date=trip_end_date,
        trips_total=total,
        trips_with_provenance=len(lags),
        trips_provenance_pending=pending,
        lag_p50_seconds=p50,
        lag_p90_seconds=p90,
        lag_p95_seconds=p95,
        lag_max_seconds=lag_max,
        lag_min_seconds=lag_min,
        buckets=buckets,
        discovery=discovery,
        weekly_guarantee_seconds=weekly_guarantee_seconds,
    )


# ---------------------------------------------------------------------------
# The recompute horizon
# ---------------------------------------------------------------------------

def recompute_horizon_days(
    *,
    enabled_lookback_days: Sequence[int],
    margin_days: int = 2,
) -> int:
    """How far back a recompute pass must go for slices to be self-correcting.

    A trip's ``end_timestamp`` can only be corrected by a fire that re-requested
    it, and a fire can only re-request inside its effective window. The deepest
    enabled ``lookback_days`` over the client's schedules therefore bounds how
    far back a correction can originate; anything older cannot move again.

    ``margin_days`` covers the stabilization delay, the overlap and the fact
    that a window is expressed in absolute seconds while a slice is a calendar
    date in the business timezone — a trip within a few hours of midnight can
    legitimately fall either side. Two days is comfortably more than the ~4 h
    those effects total.

    An empty schedule list yields the margin alone: there is no cadence that
    could correct anything, so only the most recent days can still move.
    """
    if isinstance(margin_days, bool) or not isinstance(margin_days, int):
        raise ValueError(f"margin_days must be an integer; got {margin_days!r}")
    if margin_days < 0:
        raise ValueError("margin_days must be >= 0")
    deepest = 0
    for value in enabled_lookback_days:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(
                f"enabled_lookback_days must contain integers; got {value!r}"
            )
        if value < 0:
            raise ValueError("a lookback cannot be negative")
        deepest = max(deepest, value)
    return max(1, deepest + margin_days)


def weekly_guarantee_from_schedules(
    *,
    reconciliation_cadences: Sequence[Tuple[int, int]],
    stabilization_delay_seconds: int = DEFAULT_STABILIZATION_DELAY_SECONDS,
    overlap_seconds: int = DEFAULT_OVERLAP_SECONDS,
) -> int:
    """The best guaranteed horizon the live reconciliation cadences provide.

    ``reconciliation_cadences`` is ``(lookback_days, cadence_period_days)`` per
    enabled reconciliation schedule. The best guarantee wins, because a trip is
    captured if ANY cadence reaches it.

    With no reconciliation cadence enabled — the state today, since M6 is
    committed but no production row exists — this falls back to the committed M6
    boundary so the bucket edges are stable and comparable from the first slice
    onward, rather than shifting the day a schedule is enabled.
    """
    best: Optional[int] = None
    for lookback, period in reconciliation_cadences:
        try:
            candidate = guaranteed_horizon_seconds(
                lookback_days=lookback,
                cadence_period_days=period,
                stabilization_delay_seconds=stabilization_delay_seconds,
                overlap_seconds=overlap_seconds,
            )
        except ValueError:
            # A cadence that guarantees nothing contributes nothing; it is not
            # an error in the aggregation.
            continue
        if best is None or candidate > best:
            best = candidate
    if best is None:
        return m6_weekly_guarantee_seconds()
    return best
