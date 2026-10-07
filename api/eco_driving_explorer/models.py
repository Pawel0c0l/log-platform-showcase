"""Typed, effectively immutable domain models for the Eco Driving Explorer.

This module is intentionally free of any I/O, SQL, or FastAPI dependency so it
can be reused by future server-rendered handlers, JSON endpoints, export
workers, and diagnostic tools.

Design notes:

* ``assigned_id`` is an opaque PostgreSQL ``TEXT`` identifier. It is represented
  as ``str`` everywhere; leading zeroes and exact equality are preserved and it
  is never cast to an integer.
* Weekly ranking periods are cumulative month-to-date snapshots, not isolated
  ISO weeks. ``period_end_date`` is exclusive. A month contains **four to six**
  periods (``W1``…``W6``): a month starting on a Sunday yields a one-day ``W1``
  plus a trailing ``W6``, and a 28-day February starting on a Monday yields
  only four. Week identity is month-relative and is never assumed to stop at
  ``W5``.
* Current driver-chart metadata is kept strictly separate from the persisted
  ranking semantics (``ranking_group`` / ``ranking_included``) so it can never
  be mistaken for historical snapshot state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Optional, Sequence

from .eco_scoring import REQUIRED_METRICS

# The eight scoring event counters, in canonical order, shared by
# ``eco_trip_assignments`` and the weekly/monthly stats tables.
EVENT_METRIC_COLUMNS: tuple[str, ...] = tuple(REQUIRED_METRICS)


class PeriodType(str, Enum):
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class RankingGroup(str, Enum):
    INCLUDED = "INCLUDED"
    EXCLUDED = "EXCLUDED"
    UNKNOWN_DRIVER = "UNKNOWN_DRIVER"


class LineageQuality(str, Enum):
    """Explainability quality of a reconstructed trip set.

    ``EXACT_SNAPSHOT`` is intentionally absent: no immutable historical lineage
    exists yet, so the read model must never claim it.
    """

    RECONSTRUCTED_CURRENT_STATE = "RECONSTRUCTED_CURRENT_STATE"
    UNAVAILABLE = "UNAVAILABLE"


class ReconciliationStatus(str, Enum):
    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    UNAVAILABLE = "UNAVAILABLE"


class DriverMetadataSource(str, Enum):
    CURRENT_CHART = "CURRENT_CHART"
    NONE = "NONE"


class SortDirection(str, Enum):
    ASC = "ASC"
    DESC = "DESC"


class TripContribution(str, Enum):
    """Diagnostic classification of a single assignment row for one entry."""

    CONTRIBUTING = "CONTRIBUTING"
    OUTSIDE_PERIOD = "OUTSIDE_PERIOD"
    ASSIGNED_ID_MISMATCH = "ASSIGNED_ID_MISMATCH"
    EXCLUDED_PRIVATE = "EXCLUDED_PRIVATE"
    EXCLUDED_NOT_AGGREGATED = "EXCLUDED_NOT_AGGREGATED"


def _readonly_mapping(values: Mapping[str, Optional[Decimal]]) -> Mapping[str, Optional[Decimal]]:
    return MappingProxyType(dict(values))


@dataclass(frozen=True)
class ProviderIdentity:
    provider_key: str
    client_code: str
    ranking_family: str
    display_name: str
    supported_period_types: tuple[PeriodType, ...]


@dataclass(frozen=True)
class RankingPeriodKey:
    """Stable internal identity of a ranking period.

    A weekly period is never identified by its ``W#`` label alone; the durable
    identity is the persisted period type plus month/start/end dates (and the
    in-month sequence for weekly periods).
    """

    period_type: PeriodType
    month_start_date: date
    period_start_date: date
    period_end_date: date  # exclusive
    period_sequence_in_month: Optional[int] = None

    @property
    def token(self) -> str:
        seq = "" if self.period_sequence_in_month is None else str(self.period_sequence_in_month)
        return ":".join(
            [
                self.period_type.value,
                self.month_start_date.isoformat(),
                self.period_start_date.isoformat(),
                self.period_end_date.isoformat(),
                seq,
            ]
        )

    @classmethod
    def from_token(cls, token: str) -> "RankingPeriodKey":
        parts = token.split(":")
        if len(parts) != 5:
            raise ValueError("invalid ranking period token")
        period_type = PeriodType(parts[0])
        seq = int(parts[4]) if parts[4] != "" else None
        return cls(
            period_type=period_type,
            month_start_date=date.fromisoformat(parts[1]),
            period_start_date=date.fromisoformat(parts[2]),
            period_end_date=date.fromisoformat(parts[3]),
            period_sequence_in_month=seq,
        )


@dataclass(frozen=True)
class RankingPeriod:
    key: RankingPeriodKey
    period_type: PeriodType
    period_label: str
    month_start_date: date
    period_start_date: date
    period_end_date: date  # exclusive
    period_sequence_in_month: Optional[int]
    is_partial_period: bool
    entry_counts_by_group: Mapping[str, int]
    lineage_quality: LineageQuality
    source_calculated_at: Optional[datetime]
    # Rows present in the period but outside every ranking population because
    # they are not QUALIFIED. Kept separate from `entry_counts_by_group` so a
    # non-ranked row can never be read as ranking-group membership.
    not_ranked_count: int = 0
    # The subset of `not_ranked_count` the roster still allowed into the
    # ranking: permission granted, distance threshold not met. These rows are
    # shown on the INCLUDED tab (unranked), so the tab's own count is
    # `entry_counts_by_group["INCLUDED"] + not_ranked_included_count`.
    not_ranked_included_count: int = 0


@dataclass(frozen=True)
class RankingEntry:
    provider_key: str
    client_code: str
    ranking_family: str
    period_key: RankingPeriodKey
    assigned_id: str
    # Persisted historical ranking semantics (never overwritten by the chart).
    # `None` means the row is outside every ranking population because it is not
    # QUALIFIED; it is reported but never ranked.
    ranking_group: Optional[RankingGroup]
    ranking_included: Optional[bool]
    ranking_position: Optional[int]
    ranking_total_participants: Optional[int]
    qualification_status: str
    calculation_status: str
    trips_count: int
    total_distance_meters: int
    total_kilometers: Decimal
    eco_driving_score_total: Optional[Decimal]
    ecodriving_rating_type: Optional[str]
    ecodriving_rating_type_share_percent: Optional[Decimal]
    period_label: str
    is_partial_period: bool
    event_counts: Mapping[str, int]
    metric_rates: Mapping[str, Optional[Decimal]]
    metric_points: Mapping[str, Optional[Decimal]]
    # Persisted `*_maxpoints_subtract`: points lost versus the metric maximum,
    # always <= 0. Never recomputed from a score difference when persisted.
    metric_losses: Mapping[str, Optional[Decimal]]
    # Current-chart display metadata, kept separate from persisted semantics.
    current_driver_name: Optional[str]
    driver_metadata_source: DriverMetadataSource
    current_chart_ranking_included: Optional[bool]
    lineage_quality: LineageQuality


@dataclass(frozen=True)
class TripRow:
    """Base trip DTO limited to fields required for score explainability.

    Route/location columns (coordinates, addresses) are intentionally excluded;
    they must later be added behind a separate permission, not here.

    ``vehicle_registration`` is the one deliberate exception, authorized on
    2026-08-20 (`UI-20260820-01`): operators identify a trip by plate, not by
    provider trip id. It is the plate already carried by the `client_trips`
    row this query left-joins, so it needs no new source, and it is ``None``
    whenever that row is absent. No other vehicle attribute is exposed.
    """

    client_id: str
    provider_trip_id: int
    trip_start_ts: datetime
    trip_end_ts: Optional[datetime]
    assigned_id: str
    assignment_source: str
    trip_distance_meters: Optional[int]
    aggregation_included: bool
    is_private_trip: bool
    exclusion_reason: Optional[str]
    event_counts: Mapping[str, int]
    client_trip_present: bool
    vehicle_registration: Optional[str] = None

    @property
    def total_scoring_events(self) -> int:
        return sum(int(value) for value in self.event_counts.values())


@dataclass(frozen=True)
class TripFilters:
    """Validated, provider-neutral filters for one bounded trip read."""

    trip_start_from: datetime
    trip_start_to: datetime
    requested_trip_start_from: Optional[str] = None
    requested_trip_start_to: Optional[str] = None
    provider_trip_id: Optional[object] = None
    min_distance_meters: Optional[int] = None
    max_distance_meters: Optional[int] = None
    has_scoring_events: Optional[bool] = None

    @property
    def active(self) -> bool:
        return any((
            self.requested_trip_start_from is not None,
            self.requested_trip_start_to is not None,
            self.provider_trip_id is not None,
            self.min_distance_meters is not None,
            self.max_distance_meters is not None,
            self.has_scoring_events is not None,
        ))


@dataclass(frozen=True)
class Page:
    items: Sequence[object]
    page: int
    limit: int
    total_count: Optional[int]
    has_next: bool
    unfiltered_total_count: Optional[int] = None


@dataclass(frozen=True)
class ReconciliationField:
    name: str
    persisted: object
    reconstructed: object
    matches: bool


@dataclass(frozen=True)
class ReconciliationResult:
    provider_key: str
    client_code: str
    period_key: RankingPeriodKey
    assigned_id: str
    reconciliation_status: ReconciliationStatus
    lineage_quality: LineageQuality
    fields: tuple[ReconciliationField, ...]
    mismatched_fields: tuple[str, ...]
    persisted: Mapping[str, object]
    reconstructed: Mapping[str, object]
    diagnostics: Mapping[str, object]


@dataclass(frozen=True)
class ScoreDistributionBin:
    """One histogram bucket over the repository's own score domain."""

    index: int
    lower_bound: int
    upper_bound: int  # inclusive on the top bin only
    count: int


@dataclass(frozen=True)
class ScoreDistribution:
    """Fleet score distribution for exactly one client, period and group."""

    client_code: str
    period_key: RankingPeriodKey
    ranking_group: Optional[RankingGroup]
    bin_width: int
    bins: tuple[ScoreDistributionBin, ...]
    total_count: int
    median_score: Optional[Decimal]
    mean_score: Optional[Decimal]


@dataclass(frozen=True)
class TrendPoint:
    """One persisted comparable period in a driver's trend.

    Only periods the driver actually has a persisted row for appear here; a
    missing period stays missing and is never zero-filled.
    """

    period_key: RankingPeriodKey
    period_label: str
    period_start_date: date
    period_end_date: date  # exclusive
    is_partial_period: bool
    eco_driving_score_total: Optional[Decimal]
    ranking_position: Optional[int]
    ranking_total_participants: Optional[int]
    qualification_status: Optional[str]
    total_kilometers: Optional[Decimal]
    is_current: bool = False


@dataclass(frozen=True)
class PeriodProgressionRow:
    """One cumulative month-to-date snapshot inside the selected month.

    These snapshots are cumulative and **must never be summed**; they are a
    diagnostic progression of the same running total.
    """

    period_label: str
    period_start_date: date
    period_end_date: date  # exclusive
    period_sequence_in_month: Optional[int]
    is_partial_period: bool
    eco_driving_score_total: Optional[Decimal]
    qualification_status: Optional[str]
    total_distance_meters: int
    total_kilometers: Optional[Decimal]
    trips_count: int
    event_counts: Mapping[str, int]
    metric_rates: Mapping[str, Optional[Decimal]]
    is_current: bool = False


@dataclass(frozen=True)
class ScoreMetricBucket:
    upper_bound: Decimal
    points: int


@dataclass(frozen=True)
class ScoreMetricDefinition:
    metric_key: str
    label: str
    max_points: int
    final_points: int
    buckets: tuple[ScoreMetricBucket, ...]


@dataclass(frozen=True)
class RatingThreshold:
    minimum_score: Optional[Decimal]
    label: str


@dataclass(frozen=True)
class ScoreDefinition:
    ranking_family: str
    max_possible_score: int
    min_possible_score: int
    rate_basis: str
    rate_rounding: str
    minimum_qualifying_distance_meters: int
    rating_thresholds: tuple[RatingThreshold, ...]
    metrics: tuple[ScoreMetricDefinition, ...]


def build_metric_mapping(values: Mapping[str, Optional[Decimal]]) -> Mapping[str, Optional[Decimal]]:
    """Return a read-only metric mapping keyed by the canonical metric order."""

    return _readonly_mapping({metric: values.get(metric) for metric in EVENT_METRIC_COLUMNS})


@dataclass(frozen=True)
class DynamicRankingBasis:
    """One recomputed ranking for one client, one month and a set of weeks.

    Every entry here was scored from the underlying trip assignments over the
    union of the selected isolated intervals, with the production ladder. No
    value is read from, derived from or reconciled against a persisted
    cumulative month-to-date snapshot: those snapshots overlap and are never
    summed to build this object.

    ``entries`` is the whole authorized population for the selection, already
    ordered and positioned. Group filtering, search and pagination are applied
    to it afterwards, so a filtered view can never change anybody's rank.
    """

    client_code: str
    month_start_date: date
    selected_sequences: tuple[int, ...]
    entries: tuple[RankingEntry, ...]
    counts_by_group: Mapping[str, int]
    not_ranked_count: int
    total_trips_count: int
    total_distance_meters: int
    qualified_count: int
    population_count: int
    # Permitted but below the distance threshold — the INCLUDED tab's unranked
    # population. A subset of `not_ranked_count`, never added into
    # `counts_by_group`. Last and defaulted so an existing construction stays
    # valid and simply reports none.
    not_ranked_included_count: int = 0

    @property
    def total_kilometers(self) -> Decimal:
        return (Decimal(int(self.total_distance_meters)) / Decimal(1000)).quantize(
            Decimal("0.001")
        )
