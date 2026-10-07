"""Canonical Eco Driving period, aggregation and ranking domain.

This module is **pure**: no SQL, no I/O, no FastAPI, no psycopg. It holds the
business rules that must be identical in the scheduled aggregation job and in
the portal's dynamic week-selection recomputation:

* how a calendar month is cut into week buckets;
* how metre totals become kilometres and ``/ 100 km`` coefficients;
* how a distance total becomes a qualification status;
* how a qualification status plus driver-chart membership becomes a ranking
  group;
* how event totals become metric points, losses, a total score and a rating;
* how a set of scored rows becomes an ordered ranking with positions.

It exists so those rules have exactly **one** implementation. ``jobs`` already
depends on ``api`` (``api.timezone_utils``), so the shared home is here and the
job imports it rather than the other way round.

Nothing in this module knows about persisted snapshots. A cumulative
month-to-date snapshot and an isolated week interval are both just a set of
event totals to it, which is precisely why the dynamic path cannot accidentally
inherit snapshot semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable, Mapping, Optional, Sequence

if __package__ and __package__.startswith("api."):
    from ..timezone_utils import get_business_timezone
else:  # pragma: no cover - import-path parity with the rest of the package
    from timezone_utils import get_business_timezone

from .eco_scoring import (
    MIN_QUALIFYING_DISTANCE_METERS,
    REQUIRED_METRICS,
    calculate_eco_score,
    calculate_maxpoints_subtractions,
    calculate_top_validations,
    classify_ecodriving_rating_type,
)

__all__ = [
    "KILOMETERS_QUANTUM",
    "RAW_RATE_QUANTUM",
    "STORED_RATE_QUANTUM",
    "SHARE_PERCENT_QUANTUM",
    "WeekBucket",
    "AggregateScore",
    "local_midnight",
    "next_month_start",
    "previous_month_start",
    "month_week_buckets",
    "week_bucket_by_sequence",
    "merge_intervals",
    "selection_intervals",
    "selection_day_count",
    "total_kilometers",
    "raw_rate_per_100km",
    "round_stored_per_100km",
    "rate_per_100km",
    "qualification_and_calculation",
    "ranking_group",
    "score_aggregate",
    "DEFAULT_IDENTITY_KEY",
    "score_order_key",
    "sort_rank_rows",
    "assign_ranking_positions",
    "apply_rating_type_share_percent",
]


# --- quantization (the persisted arithmetic, stated once) --------------------

KILOMETERS_QUANTUM = Decimal("0.001")
RAW_RATE_QUANTUM = Decimal("0.0001")
STORED_RATE_QUANTUM = Decimal("1")
SHARE_PERCENT_QUANTUM = Decimal("0.01")


def _quantize(value: Decimal, quantum: Decimal) -> Decimal:
    return value.quantize(quantum, rounding=ROUND_HALF_UP)


def total_kilometers(total_distance_meters: int | None) -> Decimal:
    return _quantize(Decimal(int(total_distance_meters or 0)) / Decimal("1000"), KILOMETERS_QUANTUM)


def raw_rate_per_100km(event_count: int | None, total_km: Decimal) -> Optional[Decimal]:
    if total_km <= 0:
        return None
    return _quantize((Decimal(int(event_count or 0)) / total_km) * Decimal("100"), RAW_RATE_QUANTUM)


def round_stored_per_100km(value: Optional[Decimal]) -> Optional[Decimal]:
    if value is None:
        return None
    return _quantize(value, STORED_RATE_QUANTUM)


def rate_per_100km(event_count: int | None, total_km: Decimal) -> Optional[Decimal]:
    return round_stored_per_100km(raw_rate_per_100km(event_count, total_km))


# --- period geometry ---------------------------------------------------------


def local_midnight(day: date) -> datetime:
    """Business-timezone midnight for a date — the canonical period boundary."""

    return datetime.combine(day, time.min, tzinfo=get_business_timezone())


def next_month_start(month_start: date) -> date:
    if month_start.month == 12:
        return month_start.replace(year=month_start.year + 1, month=1)
    return month_start.replace(month=month_start.month + 1)


def previous_month_start(month_start: date) -> date:
    if month_start.month == 1:
        return month_start.replace(year=month_start.year - 1, month=12)
    return month_start.replace(month=month_start.month - 1)


@dataclass(frozen=True)
class WeekBucket:
    """One **isolated** in-month reporting segment.

    The persisted weekly stats rows are cumulative month-to-date snapshots whose
    ``period_start_date`` is always the month start. A ``WeekBucket`` is the
    *incremental* segment that ends at the same boundary — ``[start_date,
    end_date_exclusive)`` — and is the only thing a dynamic week selection may
    be built from. The two share a boundary and a label and nothing else.
    """

    sequence: int
    label: str
    month_start_date: date
    start_date: date
    end_date_exclusive: date
    is_partial: bool

    @property
    def day_count(self) -> int:
        return (self.end_date_exclusive - self.start_date).days

    @property
    def start_ts(self) -> datetime:
        return local_midnight(self.start_date)

    @property
    def end_ts(self) -> datetime:
        """Exclusive upper boundary: a trip starting here belongs to the next bucket."""

        return local_midnight(self.end_date_exclusive)

    @property
    def cumulative_end_date(self) -> date:
        """The boundary the persisted cumulative snapshot for this bucket ends at."""

        return self.end_date_exclusive


def month_week_buckets(month_start: date) -> tuple[WeekBucket, ...]:
    """Cut one calendar month into its canonical week buckets.

    The boundaries are exactly those of
    ``job_eco_driving_aggregate._month_bounded_weekly_periods``: the first
    segment runs from the first of the month to the next Monday, every full
    segment is Monday-to-Monday, and the last segment is truncated at the month
    end. This is **not** ISO-week segmentation and must not be replaced by one.

    The result holds **four to six** buckets, ``W1``…``W6``. A month starting on
    a Sunday yields a one-day ``W1`` and a trailing ``W6``; a 28-day February
    starting on a Monday yields exactly four full ones. No caller may assume a
    fixed count or a ``W5`` ceiling — week identity is month-relative.
    """

    month_start = month_start.replace(day=1)
    month_end = next_month_start(month_start)
    buckets: list[WeekBucket] = []
    boundary_start = month_start
    sequence = 1

    while boundary_start < month_end:
        days_until_next_monday = 8 - boundary_start.isoweekday()
        candidate_end = boundary_start + timedelta(days=days_until_next_monday)
        segment_end = min(candidate_end, month_end)
        is_partial = (
            boundary_start.isoweekday() != 1 or (segment_end - boundary_start).days != 7
        )
        buckets.append(
            WeekBucket(
                sequence=sequence,
                label=f"W{sequence}",
                month_start_date=month_start,
                start_date=boundary_start,
                end_date_exclusive=segment_end,
                is_partial=is_partial,
            )
        )
        boundary_start = segment_end
        sequence += 1

    return tuple(buckets)


def week_bucket_by_sequence(month_start: date, sequence: int) -> Optional[WeekBucket]:
    for bucket in month_week_buckets(month_start):
        if bucket.sequence == int(sequence):
            return bucket
    return None


def merge_intervals(
    intervals: Iterable[tuple[datetime, datetime]]
) -> tuple[tuple[datetime, datetime], ...]:
    """Merge touching/overlapping half-open intervals, ordered by start.

    Adjacent week buckets share a boundary instant, so merging them keeps the
    covered set identical while guaranteeing that no trip can be matched by two
    predicates and counted twice.
    """

    ordered = sorted((start, end) for start, end in intervals if start < end)
    merged: list[list[datetime]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1][1] = end
            continue
        merged.append([start, end])
    return tuple((start, end) for start, end in merged)


def selection_intervals(buckets: Sequence[WeekBucket]) -> tuple[tuple[datetime, datetime], ...]:
    """The merged half-open timestamp intervals covered by a set of buckets."""

    return merge_intervals((bucket.start_ts, bucket.end_ts) for bucket in buckets)


def selection_day_count(buckets: Sequence[WeekBucket]) -> int:
    """True number of covered days — never the span between first and last."""

    return sum(bucket.day_count for bucket in buckets)


# --- qualification and ranking membership ------------------------------------


def qualification_and_calculation(total_distance_meters: int | None) -> tuple[str, str]:
    """The 100 km reporting-period gate, evaluated once for a whole selection.

    There is deliberately no per-day, per-week and per-trip variant: the total
    qualifying distance of the **entire** selected range is what is compared
    against ``MIN_QUALIFYING_DISTANCE_METERS``.
    """

    meters = int(total_distance_meters or 0)
    if meters <= 0:
        return "NO_DISTANCE", "NO_DISTANCE"
    if meters < MIN_QUALIFYING_DISTANCE_METERS:
        return "LOW_DISTANCE", "OK"
    return "QUALIFIED", "OK"


def ranking_group(
    qualification_status: str,
    chart_row_exists: bool,
    ranking_included: Optional[bool],
) -> Optional[str]:
    """Ranking eligibility gate: only QUALIFIED rows belong to a ranking group.

    ``None`` means "outside every ranking population". It stays distinct from
    ``UNKNOWN_DRIVER``, which is reserved for QUALIFIED rows with no chart
    mapping. Qualification takes precedence over chart membership.
    """

    if qualification_status != "QUALIFIED":
        return None
    if not chart_row_exists:
        return "UNKNOWN_DRIVER"
    return "INCLUDED" if ranking_included is True else "EXCLUDED"


# --- scoring -----------------------------------------------------------------


@dataclass(frozen=True)
class AggregateScore:
    """Everything the scoring ladder derives from one set of period totals."""

    total_distance_meters: int
    total_kilometers: Decimal
    raw_rates: Mapping[str, Optional[Decimal]]
    stored_rates: Mapping[str, Optional[Decimal]]
    metric_points: Mapping[str, Optional[int]]
    metric_losses: Mapping[str, Optional[int]]
    eco_driving_score_total: Optional[Decimal]
    ecodriving_rating_type: Optional[str]
    top_1_validation: Optional[str]
    top_2_validation: Optional[str]
    qualification_status: str
    calculation_status: str


def score_aggregate(
    event_totals: Mapping[str, Any],
    total_distance_meters: int | None,
) -> AggregateScore:
    """Score one driver's period totals with the production ladder.

    This is the single scoring entry point shared by the aggregation job and by
    the portal's dynamic recomputation. It restates no threshold, weight or
    maximum: every one of them is read from ``eco_scoring``.
    """

    meters = int(total_distance_meters or 0)
    total_km = total_kilometers(meters)
    raw_rates = {
        metric: raw_rate_per_100km(event_totals.get(metric), total_km)
        for metric in REQUIRED_METRICS
    }
    stored_rates = {
        metric: round_stored_per_100km(raw_rate) for metric, raw_rate in raw_rates.items()
    }
    score = calculate_eco_score(stored_rates)
    losses = calculate_maxpoints_subtractions(score)
    top_1, top_2 = calculate_top_validations(losses)
    qualification_status, calculation_status = qualification_and_calculation(meters)
    return AggregateScore(
        total_distance_meters=meters,
        total_kilometers=total_km,
        raw_rates=raw_rates,
        stored_rates=stored_rates,
        metric_points=dict(score["metric_points"]),
        metric_losses=dict(losses),
        eco_driving_score_total=score["eco_driving_score_total"],
        ecodriving_rating_type=classify_ecodriving_rating_type(score["eco_driving_score_total"]),
        top_1_validation=top_1,
        top_2_validation=top_2,
        qualification_status=qualification_status,
        calculation_status=calculation_status,
    )


# --- ranking order -----------------------------------------------------------


# The identity column that breaks a full tie. It differs per ranking family —
# the driver family keys on the opaque ``assigned_id``, the person family on
# ``person_name_group_key`` — but the ordering rule itself does not.
DEFAULT_IDENTITY_KEY = "assigned_id"


def score_order_key(
    score: Optional[Decimal],
    total_kilometers: Any,
    identity: Any,
) -> tuple:
    """The canonical ranking sort key, for callers that are not row mappings.

    Exposed so the portal's dynamic pagination orders recomputed entries with
    the *same* rule as the scheduled generators instead of restating it. Score
    is tested with an explicit ``is None``: a score of exactly ``0`` is a real
    score and outranks every negative one.
    """

    return (
        score is None,
        Decimal(0) if score is None else -Decimal(score),
        -Decimal(total_kilometers or 0),
        identity,
    )


def _order_key(row: Mapping[str, Any], identity_key: str) -> tuple:
    """Sort key for one scored row.

    ``eco_driving_score_total`` is tested with an explicit ``is None``. A score
    of exactly ``0`` is a **real score** produced by the scoring ladder and must
    rank above every negative score; it is not a missing value, not an
    unqualified row and not NULL. Treating it as falsy — which an
    ``or``-based fallback does — sorted a legitimate ``0`` below ``-1`` and
    inverted the ranking for those drivers.
    """

    return score_order_key(
        row["eco_driving_score_total"],
        row["total_kilometers"],
        row[identity_key],
    )


def sort_rank_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    identity_key: str = DEFAULT_IDENTITY_KEY,
) -> list:
    """Canonical ranking order and tie behaviour.

    Score descending as ordinary numeric ordering — ``1 > 0 > -1`` — then total
    kilometres descending, then the family's identity column ascending so the
    order is total and reproducible. Rows with **no** score sort last, and stay
    distinct from a row that scored zero.
    """

    return sorted(rows, key=lambda row: _order_key(row, identity_key))


def assign_ranking_positions(
    rows: list,
    *,
    identity_key: str = DEFAULT_IDENTITY_KEY,
) -> None:
    """Write 1-based ``ranking_position``/``ranking_total_participants`` in place.

    The caller supplies exactly one ranking population; this function never
    decides membership.
    """

    ranked = sort_rank_rows(rows, identity_key=identity_key)
    total = len(ranked)
    for position, row in enumerate(ranked, start=1):
        row["ranking_position"] = position
        row["ranking_total_participants"] = total


def apply_rating_type_share_percent(rows: Iterable[Mapping[str, Any]]) -> None:
    """Share of the rating band inside one period's primary ranked population.

    The denominator is the ``ranking_included`` + ``QUALIFIED`` population of the
    same period, matching the persisted column.
    """

    rows = list(rows)
    for row in rows:
        row["ecodriving_rating_type_share_percent"] = None

    qualified_ranked = [
        row
        for row in rows
        if row.get("ranking_included") is True
        and row.get("qualification_status") == "QUALIFIED"
        and row.get("ecodriving_rating_type")
    ]
    denominator = len(qualified_ranked)
    if denominator == 0:
        return

    counts: dict[str, int] = {}
    for row in qualified_ranked:
        rating_type = row["ecodriving_rating_type"]
        counts[rating_type] = counts.get(rating_type, 0) + 1

    for row in qualified_ranked:
        numerator = counts[row["ecodriving_rating_type"]]
        row["ecodriving_rating_type_share_percent"] = _quantize(
            (Decimal(numerator) * Decimal("100")) / Decimal(denominator),
            SHARE_PERCENT_QUANTUM,
        )
