"""Provider abstraction for Eco Driving Explorer read models.

A provider encapsulates one client + ranking-family implementation. Different
providers may vary in source tables, stats tables, identifier semantics,
assignment rules, period rules, scoring metrics, ranking groups, trip detail
columns, and lineage quality. Nothing in this module builds SQL from
caller-supplied table names, schema names, or identifiers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

from .models import (
    Page,
    PeriodProgressionRow,
    ProviderIdentity,
    PeriodType,
    RankingEntry,
    RankingPeriod,
    RankingPeriodKey,
    TrendPoint,
    TripFilters,
    ReconciliationResult,
    ScoreDefinition,
    ScoreDistribution,
    SortDirection,
    TripContribution,
)
from .queries import DEFAULT_PAGE_SIZE, RowReader, TREND_PERIOD_LIMIT


def classify_trip_contribution(
    row: dict,
    *,
    expected_assigned_id: str,
    period_start_ts: datetime,
    period_end_ts: datetime,
) -> TripContribution:
    """Pure mirror of the contributing-trip SQL predicate (for diagnostics/tests).

    ``period_end_ts`` is exclusive: a trip exactly at the period end is not a
    contributing trip.
    """

    if row["assigned_id"] != expected_assigned_id:
        return TripContribution.ASSIGNED_ID_MISMATCH
    ts = row["trip_start_ts"]
    if ts < period_start_ts or ts >= period_end_ts:
        return TripContribution.OUTSIDE_PERIOD
    if bool(row["is_private_trip"]):
        return TripContribution.EXCLUDED_PRIVATE
    if not bool(row["aggregation_included"]):
        return TripContribution.EXCLUDED_NOT_AGGREGATED
    return TripContribution.CONTRIBUTING


class EcoDrivingExplorerProvider(ABC):
    """Read-only provider contract for one client + ranking family."""

    @property
    @abstractmethod
    def identity(self) -> ProviderIdentity:
        ...

    @property
    def provider_key(self) -> str:
        return self.identity.provider_key

    @property
    def client_code(self) -> str:
        return self.identity.client_code

    @property
    def ranking_family(self) -> str:
        return self.identity.ranking_family

    @property
    def supported_period_types(self) -> tuple[PeriodType, ...]:
        return self.identity.supported_period_types

    @abstractmethod
    def list_periods(
        self,
        reader: RowReader,
        period_type: PeriodType,
        *,
        year: Optional[int] = None,
        month: Optional[int] = None,
    ) -> list[RankingPeriod]:
        ...

    @abstractmethod
    def list_ranking_entries(
        self,
        reader: RowReader,
        period_key: RankingPeriodKey,
        *,
        ranking_group: Optional[str] = None,
        sort_field: Optional[str] = None,
        direction: SortDirection | str | None = None,
        page: int = 1,
        limit: int = DEFAULT_PAGE_SIZE,
        search: Optional[str] = None,
    ) -> Page:
        ...

    @abstractmethod
    def get_ranking_entry(
        self,
        reader: RowReader,
        period_key: RankingPeriodKey,
        assigned_id: str,
    ) -> RankingEntry:
        ...

    @abstractmethod
    def list_contributing_trips(
        self,
        reader: RowReader,
        period_key: RankingPeriodKey,
        assigned_id: str,
        *,
        filters: Optional[TripFilters] = None,
        sort_field: Optional[str] = None,
        direction: SortDirection | str | None = None,
        page: int = 1,
        limit: int = DEFAULT_PAGE_SIZE,
    ) -> Page:
        ...

    @abstractmethod
    def reconcile_ranking_entry(
        self,
        reader: RowReader,
        period_key: RankingPeriodKey,
        assigned_id: str,
    ) -> ReconciliationResult:
        ...

    @abstractmethod
    def get_score_definition(self) -> ScoreDefinition:
        ...

    @abstractmethod
    def get_score_distribution(
        self,
        reader: RowReader,
        period_key: RankingPeriodKey,
        *,
        ranking_group: Optional[str] = None,
    ) -> ScoreDistribution:
        """Fleet score histogram for one client + period + ranking group."""

    @abstractmethod
    def get_driver_trend(
        self,
        reader: RowReader,
        period_key: RankingPeriodKey,
        assigned_id: str,
        *,
        limit: int = TREND_PERIOD_LIMIT,
    ) -> tuple[TrendPoint, ...]:
        """The driver's own comparable periods, chronological, never zero-filled."""

    @abstractmethod
    def get_period_progression(
        self,
        reader: RowReader,
        period_key: RankingPeriodKey,
        assigned_id: str,
    ) -> tuple[PeriodProgressionRow, ...]:
        """Cumulative in-month snapshots for diagnosis. Never summable."""
