"""Family-generic Eco Driving Explorer provider.

One implementation serves both production ranking families. Everything that
differs between them — source tables, identity column, roster join, stats
tables, trend views and, critically, the **aggregation-time inclusion
predicate** — is carried by a :class:`queries.FamilySources` descriptor chosen
by the registry, never by caller input.

The two families are genuinely different data contracts, not one contract with
a cosmetic rename:

* the **driver** family excludes a trip whose driver tag is marked private,
  because its aggregation job does;
* the **person** family includes an applicable trip even when that tag is
  marked private, because its aggregation job does.

Reading each family's own persisted ``aggregation_included`` decision back is
what keeps those semantics correct without this layer knowing either rule.

Reconstructed membership is always classified ``RECONSTRUCTED_CURRENT_STATE`` —
never immutable lineage.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from .eco_scoring import (
    MIN_QUALIFYING_DISTANCE_METERS,
    RATING_FALLBACK_LABEL,
    RATING_THRESHOLDS,
    METRIC_MAX_POINTS,
    REQUIRED_METRICS,
    SCORING_RULES,
    VALIDATION_LABELS,
    calculate_eco_score,
)

from .errors import (
    InvalidSortFieldError,
    InvalidRankingGroupError,
    RankingEntryNotFoundError,
    ReconstructionUnavailableError,
    UnsupportedPeriodTypeError,
)
from .models import (
    DriverMetadataSource,
    DynamicRankingBasis,
    LineageQuality,
    Page,
    PeriodProgressionRow,
    PeriodType,
    ProviderIdentity,
    RankingEntry,
    RankingGroup,
    RankingPeriod,
    RankingPeriodKey,
    RatingThreshold,
    ReconciliationField,
    ReconciliationResult,
    ReconciliationStatus,
    ScoreDefinition,
    ScoreDistribution,
    ScoreDistributionBin,
    ScoreMetricBucket,
    ScoreMetricDefinition,
    SortDirection,
    TrendPoint,
    TripFilters,
    build_metric_mapping,
)
from . import period_domain as PD
from .provider import EcoDrivingExplorerProvider
from .score_presentation import loss_from_points
from . import queries as q

if __package__ and __package__.startswith("api."):
    from ..timezone_utils import get_business_timezone_name as business_timezone_name
else:  # pragma: no cover - import-path parity with the rest of the package
    from timezone_utils import get_business_timezone_name as business_timezone_name



def _to_int(value: object) -> int:
    return int(value or 0)


def _opt_int(value: object) -> Optional[int]:
    return None if value is None else int(value)


def _opt_bool(value: object) -> Optional[bool]:
    return None if value is None else bool(value)


def _opt_text(value: object) -> Optional[str]:
    """Trim a nullable text column to a value or ``None``.

    A `client_trips` row can be absent (left join) or carry whitespace, and a
    blank plate is a missing plate, not the string ``" "``. Collapsing both to
    ``None`` here keeps the single missing-value rendering in the view.
    """

    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _opt_decimal(value: object) -> Optional[Decimal]:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _decimals_equal(left: Optional[Decimal], right: Optional[Decimal]) -> bool:
    if left is None and right is None:
        return True
    if left is None or right is None:
        return False
    return left == right


# --- dynamic arbitrary-week recomputation ------------------------------------


def _dec_map(values: dict) -> dict:
    return {
        metric: (None if values.get(metric) is None else Decimal(values[metric]))
        for metric in REQUIRED_METRICS
    }


class DynamicWeekSelectionMixin:
    """Recompute a ranking for an arbitrary union of a month's week buckets.

    The whole method group reads the selected family's assignments table and
    scores it through
    :mod:`api.eco_driving_explorer.period_domain` — the same module the
    aggregation job scores with. It contains no threshold, no weight, no
    maximum and no rating boundary of its own, so it cannot become a second
    scoring model.

    A persisted cumulative snapshot is never read here, and therefore never
    summed.
    """

    def recompute_basis(self, reader: q.RowReader, selection) -> DynamicRankingBasis:
        """Score the entire authorized population over the selected intervals.

        One statement for the whole client population, then pure Python for the
        ladder, the ranking and the group counts. There is deliberately no
        per-driver query: an N+1 shape here would scale with fleet size on every
        toggle of a week card.
        """

        intervals = selection.intervals
        if not intervals:
            return self._empty_basis(selection)

        params: dict[str, object] = {"client_id": self._client_id}
        params.update(q.build_interval_params(intervals))
        rows = reader.fetch_all(q.dynamic_population_sql(len(intervals), self.sources), params)

        scored_rows: list[dict] = []
        for row in rows:
            counters = {metric: _to_int(row.get(metric)) for metric in REQUIRED_METRICS}
            scored = PD.score_aggregate(counters, _to_int(row.get("total_distance_meters")))
            chart_exists = bool(row.get("roster_id"))
            chart_included = _opt_bool(row.get("ranking_included")) if chart_exists else None
            scored_rows.append(
                {
                    "assigned_id": str(row["assigned_id"]),
                    # The person pipeline resolves a canonical name while assigning;
                    # the driver pipeline reads it from the roster.
                    "driver_name": row.get("assignment_name") or row.get("roster_name"),
                    "chart_exists": chart_exists,
                    "ranking_included": chart_included,
                    "ranking_group": PD.ranking_group(
                        scored.qualification_status, chart_exists, chart_included
                    ),
                    "ranking_position": None,
                    "ranking_total_participants": None,
                    "qualification_status": scored.qualification_status,
                    "calculation_status": scored.calculation_status,
                    "ecodriving_rating_type": scored.ecodriving_rating_type,
                    "ecodriving_rating_type_share_percent": None,
                    "eco_driving_score_total": scored.eco_driving_score_total,
                    "total_kilometers": scored.total_kilometers,
                    "total_distance_meters": scored.total_distance_meters,
                    "trips_count": _to_int(row.get("trips_count")),
                    "event_counts": counters,
                    "scored": scored,
                }
            )

        # Ranking is recomputed per ranking population, exactly as the job does
        # it: one position sequence per group, never a filtered slice of an old
        # ranking and never a position carried over from another period.
        for group_value in (RankingGroup.INCLUDED.value, RankingGroup.EXCLUDED.value):
            members = [row for row in scored_rows if row["ranking_group"] == group_value]
            if members:
                PD.assign_ranking_positions(members)
        PD.apply_rating_type_share_percent(scored_rows)

        entries = tuple(
            self._entry_from_dynamic_row(row, selection) for row in scored_rows
        )
        counts = {
            value: sum(1 for row in scored_rows if row["ranking_group"] == value)
            for value in (
                RankingGroup.INCLUDED.value,
                RankingGroup.EXCLUDED.value,
                RankingGroup.UNKNOWN_DRIVER.value,
            )
        }
        not_ranked = sum(1 for row in scored_rows if row["ranking_group"] is None)
        # Permission granted, threshold not met: the INCLUDED tab's unranked
        # population, counted here with exactly the predicate the SQL path uses
        # so a recomputed week and a persisted month agree on who is on the tab.
        not_ranked_included = sum(
            1
            for row in scored_rows
            if row["ranking_group"] is None and row["ranking_included"] is True
        )
        return DynamicRankingBasis(
            client_code=self.CLIENT_CODE,
            month_start_date=selection.month_start_date,
            selected_sequences=tuple(selection.selected_sequences),
            entries=entries,
            counts_by_group=counts,
            not_ranked_count=not_ranked,
            not_ranked_included_count=not_ranked_included,
            total_trips_count=sum(row["trips_count"] for row in scored_rows),
            total_distance_meters=sum(row["total_distance_meters"] for row in scored_rows),
            qualified_count=sum(
                1 for row in scored_rows if row["qualification_status"] == "QUALIFIED"
            ),
            population_count=len(scored_rows),
        )

    def _empty_basis(self, selection) -> DynamicRankingBasis:
        return DynamicRankingBasis(
            client_code=self.CLIENT_CODE,
            month_start_date=selection.month_start_date,
            selected_sequences=(),
            entries=(),
            counts_by_group={
                RankingGroup.INCLUDED.value: 0,
                RankingGroup.EXCLUDED.value: 0,
                RankingGroup.UNKNOWN_DRIVER.value: 0,
            },
            not_ranked_count=0,
            not_ranked_included_count=0,
            total_trips_count=0,
            total_distance_meters=0,
            qualified_count=0,
            population_count=0,
        )

    def _entry_from_dynamic_row(self, row: dict, selection) -> RankingEntry:
        scored = row["scored"]
        chart_exists = bool(row["chart_exists"])
        return RankingEntry(
            provider_key=self.PROVIDER_KEY,
            client_code=self.CLIENT_CODE,
            ranking_family=self.RANKING_FAMILY,
            period_key=selection.synthetic_period_key(),
            assigned_id=row["assigned_id"],
            ranking_group=(
                RankingGroup(row["ranking_group"]) if row["ranking_group"] else None
            ),
            ranking_included=row["ranking_included"],
            ranking_position=row["ranking_position"],
            ranking_total_participants=row["ranking_total_participants"],
            qualification_status=row["qualification_status"],
            calculation_status=row["calculation_status"],
            trips_count=row["trips_count"],
            total_distance_meters=row["total_distance_meters"],
            total_kilometers=row["total_kilometers"],
            eco_driving_score_total=row["eco_driving_score_total"],
            ecodriving_rating_type=row["ecodriving_rating_type"],
            ecodriving_rating_type_share_percent=row["ecodriving_rating_type_share_percent"],
            period_label=f"{selection.month_token} · {selection.label}",
            is_partial_period=bool(selection.includes_partial_week),
            event_counts=dict(row["event_counts"]),
            metric_rates=build_metric_mapping(dict(scored.stored_rates)),
            metric_points=build_metric_mapping(_dec_map(dict(scored.metric_points))),
            metric_losses=build_metric_mapping(_dec_map(dict(scored.metric_losses))),
            current_driver_name=row.get("driver_name"),
            driver_metadata_source=(
                DriverMetadataSource.CURRENT_CHART if chart_exists else DriverMetadataSource.NONE
            ),
            current_chart_ranking_included=row["ranking_included"],
            lineage_quality=LineageQuality.RECONSTRUCTED_CURRENT_STATE,
        )

    def paginate_dynamic_entries(
        self,
        basis: DynamicRankingBasis,
        *,
        ranking_group: Optional[str] = None,
        search: Optional[str] = None,
        sort_field: Optional[str] = None,
        direction: SortDirection | str | None = None,
        page: int = 1,
        limit: int = q.DEFAULT_PAGE_SIZE,
    ) -> Page:
        """Filter, sort and page an already-ranked recomputed population.

        Positions were assigned over the **whole** population before any of this
        runs, so narrowing to a group, a search term or one page cannot change
        anybody's rank. The sort allowlist is the same one the persisted-period
        query uses, so a caller cannot reach a field here that SQL would refuse.
        """

        group_value = self._validate_ranking_group(ranking_group)
        page, limit, offset = q.normalize_pagination(page, limit)

        rows = [entry for entry in basis.entries if _in_group(entry, group_value)]
        if search:
            needle = str(search).strip().casefold()
            rows = [
                entry
                for entry in rows
                if needle in entry.assigned_id.casefold()
                or needle in str(entry.current_driver_name or "").casefold()
            ]

        rows = _sort_dynamic_entries(rows, sort_field, direction)
        total = len(rows)
        window = rows[offset:offset + limit]
        return Page(
            items=tuple(window),
            page=page,
            limit=limit,
            total_count=total,
            has_next=(offset + len(window)) < total,
        )

    def dynamic_score_distribution(
        self,
        basis: DynamicRankingBasis,
        *,
        ranking_group: Optional[str] = None,
    ) -> ScoreDistribution:
        """Histogram over the same recomputed population the ranking shows.

        It is built from ``basis.entries`` rather than from a second query, so
        a persisted monthly distribution can never leak into a ``W1 + W3`` view
        and the plot cannot describe a different period from the table beside
        it. With no group filter it still restricts to rows that belong to
        *some* ranking population, matching the persisted-period histogram.

        That is deliberately NARROWER than the `INCLUDED` tab, which also lists
        permitted below-threshold drivers: those rows carry no score the ranking
        will stand behind, so they are counted by the tab and plotted by
        neither histogram. The note under the plot says so.
        """

        group_value = self._validate_ranking_group(ranking_group)
        scores = [
            entry.eco_driving_score_total
            for entry in basis.entries
            if entry.ranking_group is not None
            and (group_value is None or entry.ranking_group.value == group_value)
            and entry.eco_driving_score_total is not None
        ]
        counts: dict[int, int] = {}
        for score in scores:
            index = q.score_bin_index(score)
            if index is not None:
                counts[index] = counts.get(index, 0) + 1
        bins = tuple(
            ScoreDistributionBin(
                index=index,
                lower_bound=q.SCORE_BIN_MIN + index * q.SCORE_BIN_WIDTH,
                upper_bound=q.SCORE_BIN_MIN + (index + 1) * q.SCORE_BIN_WIDTH,
                count=counts.get(index, 0),
            )
            for index in range(q.SCORE_BIN_COUNT)
        )
        ordered = sorted(scores)
        total = len(ordered)
        median = None
        mean = None
        if total:
            middle = total // 2
            median = (
                ordered[middle]
                if total % 2
                else ((ordered[middle - 1] + ordered[middle]) / Decimal(2))
            )
            mean = (sum(ordered, Decimal(0)) / Decimal(total)).quantize(Decimal("0.01"))
        return ScoreDistribution(
            client_code=self.CLIENT_CODE,
            period_key=basis_period_key(basis),
            ranking_group=RankingGroup(group_value) if group_value else None,
            bin_width=q.SCORE_BIN_WIDTH,
            bins=bins,
            total_count=total,
            median_score=median,
            mean_score=mean,
        )

    def list_dynamic_trips(
        self,
        reader: q.RowReader,
        selection,
        assigned_id: str,
        *,
        filters: Optional[TripFilters] = None,
        sort_field: Optional[str] = None,
        direction: SortDirection | str | None = None,
        page: int = 1,
        limit: int = q.DEFAULT_PAGE_SIZE,
    ) -> Page:
        """Trip evidence for one driver over the selected interval union.

        The permission boundary and the column set are unchanged; only the time
        predicate differs. A trip outside every selected week can therefore not
        appear as though it had contributed to the displayed score.
        """

        assigned_id = self._require_assigned_id(assigned_id)
        page, limit, offset = q.normalize_pagination(page, limit)
        selected_intervals = selection.intervals
        # An explicit trip-start filter narrows *inside* the basis; it can never
        # reach a trip the basis excluded. Clipping keeps the union honest for a
        # non-contiguous selection, where the span between the first and last
        # selected week is deliberately not the covered set.
        intervals = _clip_intervals(selected_intervals, filters)
        if not intervals:
            return Page(
                items=(), page=page, limit=limit, total_count=0,
                has_next=False,
                unfiltered_total_count=0 if not selected_intervals else None,
            )
        effective_sort = sort_field if sort_field is not None or direction is None else "trip_start_ts"
        order_by = q.resolve_order_by(
            effective_sort,
            direction,
            allowlist=q.TRIP_SORT_FIELDS,
            default_order=q.TRIP_DEFAULT_ORDER,
            tiebreaker=q.TRIP_TIEBREAKER,
        )
        params: dict[str, object] = {
            "client_id": self._client_id,
            "assigned_id": assigned_id,
        }
        params.update(q.build_interval_params(intervals))
        scope = dict(params)
        params["limit"] = limit
        params["offset"] = offset

        if filters is not None and filters.provider_trip_id is not None:
            params["provider_trip_id"] = filters.provider_trip_id
        if filters is not None and filters.min_distance_meters is not None:
            params["min_distance_meters"] = filters.min_distance_meters
        if filters is not None and filters.max_distance_meters is not None:
            params["max_distance_meters"] = filters.max_distance_meters
        where = q.dynamic_contributing_where(
            len(intervals),
            provider_trip_id=filters is not None and filters.provider_trip_id is not None,
            min_distance=filters is not None and filters.min_distance_meters is not None,
            max_distance=filters is not None and filters.max_distance_meters is not None,
            has_scoring_events=None if filters is None else filters.has_scoring_events,
            sources=self.sources,
        )
        rows = reader.fetch_all(q.contributing_trips_sql(order_by, where, self.sources), params)
        total_row = reader.fetch_one(q.contributing_trips_count_sql(where, self.sources), params)
        total = _to_int(total_row.get("total_count")) if total_row else None
        unfiltered_total = total
        if filters is not None and filters.active:
            unfiltered_row = reader.fetch_one(
                q.contributing_trips_count_sql(
                    q.dynamic_contributing_where(
                        len(selected_intervals), sources=self.sources
                    ),
                    self.sources,
                ),
                {**scope, **q.build_interval_params(selected_intervals)},
            )
            unfiltered_total = _to_int(unfiltered_row.get("total_count")) if unfiltered_row else None
        items = [self._trip_from_row(row) for row in rows]
        has_next = total is not None and (offset + len(items)) < total
        return Page(
            items=tuple(items), page=page, limit=limit, total_count=total,
            has_next=has_next, unfiltered_total_count=unfiltered_total,
        )

    def list_available_months(self, reader: q.RowReader) -> tuple[date, ...]:
        """Months with at least one assignment row, newest first, one client."""

        rows = reader.fetch_all(
            q.dynamic_month_bounds_sql(self.sources),
            {"client_id": self._client_id, "timezone": business_timezone_name()},
        )
        return tuple(row["month_start_date"] for row in rows if row.get("month_start_date"))


def basis_period_key(basis: DynamicRankingBasis) -> RankingPeriodKey:
    """Display-only period identity for a recomputed basis. Never persisted."""

    return RankingPeriodKey(
        period_type=PeriodType.WEEKLY,
        month_start_date=basis.month_start_date,
        period_start_date=basis.month_start_date,
        period_end_date=PD.next_month_start(basis.month_start_date),
        period_sequence_in_month=None,
    )


class FamilyEcoDrivingProvider(DynamicWeekSelectionMixin, EcoDrivingExplorerProvider):
    """Base provider. Concrete subclasses supply identity plus a source set."""

    PROVIDER_KEY: str = ""
    CLIENT_CODE: str = ""
    RANKING_FAMILY: str = ""
    DISPLAY_NAME: str = ""
    SOURCES: q.FamilySources = q.DRIVER_SOURCES

    def __init__(self, *, client_id: str, statement_timeout_ms: int = 15000) -> None:
        if not client_id or not str(client_id).strip():
            raise ValueError("client_id is required")
        self._client_id = str(client_id)
        self._statement_timeout_ms = int(statement_timeout_ms)

    @property
    def sources(self) -> q.FamilySources:
        """The repository-controlled source set for this family."""

        return self.SOURCES

    @property
    def identity(self) -> ProviderIdentity:
        return ProviderIdentity(
            provider_key=self.PROVIDER_KEY,
            client_code=self.CLIENT_CODE,
            ranking_family=self.RANKING_FAMILY,
            display_name=self.DISPLAY_NAME,
            supported_period_types=(PeriodType.WEEKLY, PeriodType.MONTHLY),
        )

    # -- periods --------------------------------------------------------------

    def list_periods(
        self,
        reader: q.RowReader,
        period_type: PeriodType,
        *,
        year: Optional[int] = None,
        month: Optional[int] = None,
    ) -> list[RankingPeriod]:
        if period_type not in self.supported_period_types:
            raise UnsupportedPeriodTypeError("unsupported period type")

        params: dict[str, object] = {"client_id": self._client_id}
        if year is not None:
            params["year"] = int(year)
        if month is not None:
            params["month"] = int(month)

        if period_type is PeriodType.WEEKLY:
            sql = q.weekly_periods_sql(
                with_year=year is not None, with_month=month is not None,
                sources=self.sources,
            )
            rows = reader.fetch_all(sql, params)
            return [self._weekly_period_from_row(row) for row in rows]

        sql = q.monthly_periods_sql(
            with_year=year is not None, with_month=month is not None,
            sources=self.sources,
        )
        rows = reader.fetch_all(sql, params)
        return [self._monthly_period_from_row(row) for row in rows]

    def _weekly_period_from_row(self, row: dict) -> RankingPeriod:
        key = RankingPeriodKey(
            period_type=PeriodType.WEEKLY,
            month_start_date=row["month_start_date"],
            period_start_date=row["period_start_date"],
            period_end_date=row["period_end_date"],
            period_sequence_in_month=_opt_int(row["period_sequence_in_month"]),
        )
        return RankingPeriod(
            key=key,
            period_type=PeriodType.WEEKLY,
            period_label=row["period_label"],
            month_start_date=row["month_start_date"],
            period_start_date=row["period_start_date"],
            period_end_date=row["period_end_date"],
            period_sequence_in_month=_opt_int(row["period_sequence_in_month"]),
            is_partial_period=bool(row["is_partial_period"]),
            entry_counts_by_group=self._counts(row),
            lineage_quality=LineageQuality.RECONSTRUCTED_CURRENT_STATE,
            source_calculated_at=row.get("source_calculated_at"),
            not_ranked_count=_to_int(row.get("not_ranked_count")),
            not_ranked_included_count=_to_int(row.get("not_ranked_included_count")),
        )

    def _monthly_period_from_row(self, row: dict) -> RankingPeriod:
        key = RankingPeriodKey(
            period_type=PeriodType.MONTHLY,
            month_start_date=row["month_start_date"],
            period_start_date=row["month_start_date"],
            period_end_date=row["month_end_date"],
            period_sequence_in_month=None,
        )
        return RankingPeriod(
            key=key,
            period_type=PeriodType.MONTHLY,
            period_label=row["month_start_date"].strftime("%Y-%m"),
            month_start_date=row["month_start_date"],
            period_start_date=row["month_start_date"],
            period_end_date=row["month_end_date"],
            period_sequence_in_month=None,
            is_partial_period=False,
            entry_counts_by_group=self._counts(row),
            lineage_quality=LineageQuality.RECONSTRUCTED_CURRENT_STATE,
            source_calculated_at=row.get("source_calculated_at"),
            not_ranked_count=_to_int(row.get("not_ranked_count")),
            not_ranked_included_count=_to_int(row.get("not_ranked_included_count")),
        )

    @staticmethod
    def _counts(row: dict) -> dict[str, int]:
        return {
            RankingGroup.INCLUDED.value: _to_int(row.get("included_count")),
            RankingGroup.EXCLUDED.value: _to_int(row.get("excluded_count")),
            RankingGroup.UNKNOWN_DRIVER.value: _to_int(row.get("unknown_count")),
        }

    # -- ranking entries ------------------------------------------------------

    def list_ranking_entries(
        self,
        reader: q.RowReader,
        period_key: RankingPeriodKey,
        *,
        ranking_group: Optional[str] = None,
        sort_field: Optional[str] = None,
        direction: SortDirection | str | None = None,
        page: int = 1,
        limit: int = q.DEFAULT_PAGE_SIZE,
        search: Optional[str] = None,
    ) -> Page:
        self._require_supported(period_key.period_type)
        group_value = self._validate_ranking_group(ranking_group)
        page, limit, offset = q.normalize_pagination(page, limit)
        # The sort key is semantic; the physical column it resolves to belongs to
        # this family's stats table. Migration 043 renamed the person family's
        # identity, so a driver-shaped ORDER BY would reference a column that
        # does not exist there.
        order_by = q.resolve_order_by(
            sort_field,
            direction,
            allowlist=q.entry_sort_fields(self.sources),
            default_order=q.entry_default_order(self.sources),
            tiebreaker=q.entry_tiebreaker(self.sources),
        )
        weekly = period_key.period_type is PeriodType.WEEKLY
        params = self._entry_scope_params(period_key)
        params["limit"] = limit
        params["offset"] = offset
        if group_value is not None:
            params["ranking_group"] = group_value
        with_search = bool(search)
        if with_search:
            params["search"] = q.like_pattern(str(search))

        rows = reader.fetch_all(
            q.entries_sql(
                weekly=weekly, ranking_group=group_value,
                order_by=order_by, with_search=with_search, sources=self.sources,
            ),
            params,
        )
        total_row = reader.fetch_one(
            q.entries_count_sql(
                weekly=weekly, ranking_group=group_value,
                with_search=with_search, sources=self.sources,
            ),
            params,
        )
        total = _to_int(total_row.get("total_count")) if total_row else None
        items = [self._entry_from_row(row, period_key) for row in rows]
        has_next = total is not None and (offset + len(items)) < total
        return Page(items=tuple(items), page=page, limit=limit, total_count=total, has_next=has_next)

    def get_ranking_entry(
        self,
        reader: q.RowReader,
        period_key: RankingPeriodKey,
        assigned_id: str,
    ) -> RankingEntry:
        self._require_supported(period_key.period_type)
        assigned_id = self._require_assigned_id(assigned_id)
        weekly = period_key.period_type is PeriodType.WEEKLY
        params = self._entry_scope_params(period_key)
        params["assigned_id"] = assigned_id
        row = reader.fetch_one(q.single_entry_sql(weekly=weekly, sources=self.sources), params)
        if row is None:
            raise RankingEntryNotFoundError("ranking entry not found")
        return self._entry_from_row(row, period_key)

    def _entry_from_row(self, row: dict, period_key: RankingPeriodKey) -> RankingEntry:
        current_present = bool(row.get("current_chart_present"))
        metadata_source = (
            DriverMetadataSource.CURRENT_CHART if current_present else DriverMetadataSource.NONE
        )
        rates = build_metric_mapping(
            {m: _opt_decimal(row.get(q.RATE_COLUMNS[m])) for m in REQUIRED_METRICS}
        )
        points = build_metric_mapping(
            {m: _opt_decimal(row.get(q.POINT_COLUMNS[m])) for m in REQUIRED_METRICS}
        )
        # Persisted loss first. Only when the column is absent for this row do we
        # fall back to the same `min(points - max, 0)` rule the job applies, so
        # the Explorer never presents a second, competing loss model.
        losses = build_metric_mapping(
            {
                m: (
                    _opt_decimal(row.get(q.SUBTRACT_COLUMNS[m]))
                    if row.get(q.SUBTRACT_COLUMNS[m]) is not None
                    else loss_from_points(m, _opt_decimal(row.get(q.POINT_COLUMNS[m])))
                )
                for m in REQUIRED_METRICS
            }
        )
        return RankingEntry(
            provider_key=self.PROVIDER_KEY,
            client_code=self.CLIENT_CODE,
            ranking_family=self.RANKING_FAMILY,
            period_key=period_key,
            assigned_id=str(row["assigned_id"]),
            ranking_group=(
                RankingGroup(row["ranking_group"])
                if row.get("ranking_group") is not None
                else None
            ),
            ranking_included=_opt_bool(row.get("ranking_included")),
            ranking_position=_opt_int(row.get("ranking_position")),
            ranking_total_participants=_opt_int(row.get("ranking_total_participants")),
            qualification_status=row["qualification_status"],
            calculation_status=row["calculation_status"],
            trips_count=_to_int(row.get("trips_count")),
            total_distance_meters=_to_int(row.get("total_distance_meters")),
            total_kilometers=_opt_decimal(row.get("total_kilometers")) or Decimal("0"),
            eco_driving_score_total=_opt_decimal(row.get("eco_driving_score_total")),
            ecodriving_rating_type=row.get("ecodriving_rating_type"),
            ecodriving_rating_type_share_percent=_opt_decimal(row.get("ecodriving_rating_type_share_percent")),
            period_label=str(row.get("period_label") or period_key.month_start_date.strftime("%Y-%m")),
            is_partial_period=bool(row.get("is_partial_period")),
            event_counts={m: _to_int(row.get(m)) for m in REQUIRED_METRICS},
            metric_rates=rates,
            metric_points=points,
            metric_losses=losses,
            current_driver_name=row.get("current_driver_name"),
            driver_metadata_source=metadata_source,
            current_chart_ranking_included=_opt_bool(row.get("current_chart_ranking_included")),
            lineage_quality=LineageQuality.RECONSTRUCTED_CURRENT_STATE,
        )

    # -- contributing trips ---------------------------------------------------

    def list_contributing_trips(
        self,
        reader: q.RowReader,
        period_key: RankingPeriodKey,
        assigned_id: str,
        *,
        filters: Optional[TripFilters] = None,
        sort_field: Optional[str] = None,
        direction: SortDirection | str | None = None,
        page: int = 1,
        limit: int = q.DEFAULT_PAGE_SIZE,
    ) -> Page:
        self._require_supported(period_key.period_type)
        assigned_id = self._require_assigned_id(assigned_id)
        page, limit, offset = q.normalize_pagination(page, limit)
        effective_sort = sort_field if sort_field is not None or direction is None else "trip_start_ts"
        order_by = q.resolve_order_by(
            effective_sort,
            direction,
            allowlist=q.TRIP_SORT_FIELDS,
            default_order=q.TRIP_DEFAULT_ORDER,
            tiebreaker=q.TRIP_TIEBREAKER,
        )
        filters = filters or TripFilters(
            trip_start_from=q.business_local_midnight(period_key.period_start_date),
            trip_start_to=q.business_local_midnight(period_key.period_end_date),
        )
        params = self._trip_scope_params(period_key, assigned_id)
        params["period_start_ts"] = filters.trip_start_from
        params["period_end_ts"] = filters.trip_start_to
        params["limit"] = limit
        params["offset"] = offset
        if filters.provider_trip_id is not None:
            params["provider_trip_id"] = filters.provider_trip_id
        if filters.min_distance_meters is not None:
            params["min_distance_meters"] = filters.min_distance_meters
        if filters.max_distance_meters is not None:
            params["max_distance_meters"] = filters.max_distance_meters
        where = q.contributing_where(
            provider_trip_id=filters.provider_trip_id is not None,
            min_distance=filters.min_distance_meters is not None,
            max_distance=filters.max_distance_meters is not None,
            has_scoring_events=filters.has_scoring_events,
            sources=self.sources,
        )

        rows = reader.fetch_all(q.contributing_trips_sql(order_by, where, self.sources), params)
        total_row = reader.fetch_one(q.contributing_trips_count_sql(where, self.sources), params)
        total = _to_int(total_row.get("total_count")) if total_row else None
        unfiltered_total = total
        if filters.active:
            scope = self._trip_scope_params(period_key, assigned_id)
            unfiltered_row = reader.fetch_one(q.contributing_trips_count_sql(sources=self.sources), scope)
            unfiltered_total = _to_int(unfiltered_row.get("total_count")) if unfiltered_row else None
        items = [self._trip_from_row(row) for row in rows]
        has_next = total is not None and (offset + len(items)) < total
        return Page(
            items=tuple(items), page=page, limit=limit, total_count=total,
            has_next=has_next, unfiltered_total_count=unfiltered_total,
        )

    def _trip_from_row(self, row: dict):
        # Every family's trip projection aliases its own identity column to
        # `assigned_id`, and `assigned_id` is a bound *parameter name*, not a
        # column name, so nothing below assumes the driver family's schema.
        from .models import TripRow

        counts = {m: _to_int(row.get(m)) for m in REQUIRED_METRICS}
        return TripRow(
            client_id=str(row["client_id"]),
            provider_trip_id=int(row["provider_trip_id"]),
            trip_start_ts=row["trip_start_ts"],
            trip_end_ts=row.get("trip_end_ts"),
            assigned_id=str(row["assigned_id"]),
            assignment_source=row["assignment_source"],
            trip_distance_meters=_opt_int(row.get("trip_distance_meters")),
            aggregation_included=bool(row["aggregation_included"]),
            is_private_trip=bool(row["is_private_trip"]),
            exclusion_reason=row.get("exclusion_reason"),
            event_counts=build_metric_mapping({m: counts[m] for m in REQUIRED_METRICS}),
            client_trip_present=bool(row.get("client_trip_present")),
            vehicle_registration=_opt_text(row.get("vehicle_registration")),
        )

    # -- reconciliation -------------------------------------------------------

    def reconcile_ranking_entry(
        self,
        reader: q.RowReader,
        period_key: RankingPeriodKey,
        assigned_id: str,
    ) -> ReconciliationResult:
        entry = self.get_ranking_entry(reader, period_key, assigned_id)
        params = self._trip_scope_params(period_key, entry.assigned_id)

        totals = reader.fetch_one(q.reconstruction_totals_sql(self.sources), params)
        if totals is None:
            raise ReconstructionUnavailableError("reconstruction totals unavailable")
        diagnostics_row = reader.fetch_one(q.window_diagnostics_sql(self.sources), params) or {}

        reconstructed_distance = _to_int(totals.get("total_distance_meters"))
        reconstructed_trips = _to_int(totals.get("trips_count"))
        reconstructed_counters = {m: _to_int(totals.get(m)) for m in REQUIRED_METRICS}

        total_km = q.total_kilometers(reconstructed_distance)
        reconstructed_rates = {
            m: q.stored_rate_per_100km(reconstructed_counters[m], total_km)
            for m in REQUIRED_METRICS
        }
        score_result = calculate_eco_score(reconstructed_rates)
        reconstructed_score = score_result["eco_driving_score_total"]
        reconstructed_score_dec = (
            None if reconstructed_score is None else Decimal(str(reconstructed_score))
        )
        reconstructed_points = {
            m: (None if score_result["metric_points"][m] is None else Decimal(str(score_result["metric_points"][m])))
            for m in REQUIRED_METRICS
        }

        fields: list[ReconciliationField] = []

        def add(name: str, persisted: object, reconstructed: object, matches: bool) -> None:
            fields.append(ReconciliationField(name, persisted, reconstructed, matches))

        add("trips_count", entry.trips_count, reconstructed_trips, entry.trips_count == reconstructed_trips)
        add(
            "total_distance_meters",
            entry.total_distance_meters,
            reconstructed_distance,
            entry.total_distance_meters == reconstructed_distance,
        )
        # per-metric counters, rates, points
        persisted_counters = self._persisted_counters(reader, period_key, entry.assigned_id)
        for m in REQUIRED_METRICS:
            pc = persisted_counters.get(m)
            add(f"counter:{m}", pc, reconstructed_counters[m], pc == reconstructed_counters[m])
        for m in REQUIRED_METRICS:
            pr = entry.metric_rates.get(m)
            rr = reconstructed_rates[m]
            add(f"rate:{m}", pr, rr, _decimals_equal(pr, rr))
        for m in REQUIRED_METRICS:
            pp = entry.metric_points.get(m)
            rp = reconstructed_points[m]
            add(f"points:{m}", pp, rp, _decimals_equal(pp, rp))
        add(
            "eco_driving_score_total",
            entry.eco_driving_score_total,
            reconstructed_score_dec,
            _decimals_equal(entry.eco_driving_score_total, reconstructed_score_dec),
        )

        mismatched = tuple(f.name for f in fields if not f.matches)
        status = ReconciliationStatus.MATCH if not mismatched else ReconciliationStatus.MISMATCH

        diagnostics = {
            "reconstructed_trips_count": reconstructed_trips,
            "missing_client_trip_count": _to_int(totals.get("missing_client_trip_count")),
            "window_trips": _to_int(diagnostics_row.get("window_trips")),
            "private_trips": _to_int(diagnostics_row.get("private_trips")),
            "not_aggregated_trips": _to_int(diagnostics_row.get("not_aggregated_trips")),
            "scoring_complete": bool(score_result["scoring_complete"]),
        }
        persisted_summary = {
            "trips_count": entry.trips_count,
            "total_distance_meters": entry.total_distance_meters,
            "eco_driving_score_total": entry.eco_driving_score_total,
            "ranking_group": entry.ranking_group.value if entry.ranking_group else None,
            "ranking_position": entry.ranking_position,
        }
        reconstructed_summary = {
            "trips_count": reconstructed_trips,
            "total_distance_meters": reconstructed_distance,
            "eco_driving_score_total": reconstructed_score_dec,
        }
        return ReconciliationResult(
            provider_key=self.PROVIDER_KEY,
            client_code=self.CLIENT_CODE,
            period_key=period_key,
            assigned_id=entry.assigned_id,
            reconciliation_status=status,
            lineage_quality=LineageQuality.RECONSTRUCTED_CURRENT_STATE,
            fields=tuple(fields),
            mismatched_fields=mismatched,
            persisted=persisted_summary,
            reconstructed=reconstructed_summary,
            diagnostics=diagnostics,
        )

    def _persisted_counters(self, reader, period_key, assigned_id) -> dict[str, int]:
        weekly = period_key.period_type is PeriodType.WEEKLY
        params = self._entry_scope_params(period_key)
        params["assigned_id"] = assigned_id
        row = reader.fetch_one(q.single_entry_sql(weekly=weekly, sources=self.sources), params)
        if row is None:
            return {m: 0 for m in REQUIRED_METRICS}
        return {m: _to_int(row.get(m)) for m in REQUIRED_METRICS}

    # -- fleet distribution / trend / progression (S12) -----------------------

    def get_score_distribution(
        self,
        reader: q.RowReader,
        period_key: RankingPeriodKey,
        *,
        ranking_group: Optional[str] = None,
    ) -> ScoreDistribution:
        """Score histogram for exactly one client, one period and one group.

        The query carries the same trusted ``client_id`` and the same period
        identity the page is already authorized for, so the histogram can never
        widen the disclosure surface of the ranking it sits above.

        Only rows inside a ranking group are binned. The `INCLUDED` tab is wider
        than that — it also lists permitted drivers below the distance threshold
        — and those rows are deliberately absent here, for the same reason their
        score is absent from the table.
        """

        self._require_supported(period_key.period_type)
        group_value = self._validate_ranking_group(ranking_group)
        weekly = period_key.period_type is PeriodType.WEEKLY
        params = self._entry_scope_params(period_key)
        if group_value is not None:
            params["ranking_group"] = group_value
        rows = reader.fetch_all(
            q.score_distribution_sql(
                weekly=weekly, with_group=group_value is not None, sources=self.sources
            ),
            params,
        )
        counts = {int(row["bin_index"]): _to_int(row.get("bin_count")) for row in rows}
        total = _to_int(rows[0].get("total_count")) if rows else 0
        mean = _opt_decimal(rows[0].get("mean_score")) if rows else None
        median = _opt_decimal(rows[0].get("median_score")) if rows else None
        bins = tuple(
            ScoreDistributionBin(
                index=index,
                lower_bound=q.SCORE_BIN_MIN + index * q.SCORE_BIN_WIDTH,
                upper_bound=q.SCORE_BIN_MIN + (index + 1) * q.SCORE_BIN_WIDTH,
                count=counts.get(index, 0),
            )
            for index in range(q.SCORE_BIN_COUNT)
        )
        return ScoreDistribution(
            client_code=self.CLIENT_CODE,
            period_key=period_key,
            ranking_group=RankingGroup(group_value) if group_value else None,
            bin_width=q.SCORE_BIN_WIDTH,
            bins=bins,
            total_count=total,
            median_score=median,
            mean_score=mean,
        )

    def get_driver_trend(
        self,
        reader: q.RowReader,
        period_key: RankingPeriodKey,
        assigned_id: str,
        *,
        limit: int = q.TREND_PERIOD_LIMIT,
    ) -> tuple[TrendPoint, ...]:
        """The driver's own last comparable periods, oldest first.

        Backed by the selected family's existing trend view. A
        period the driver has no persisted row for is simply absent from the
        result — this method never materialises a placeholder.
        """

        self._require_supported(period_key.period_type)
        assigned_id = self._require_assigned_id(assigned_id)
        weekly = period_key.period_type is PeriodType.WEEKLY
        params: dict[str, object] = {
            "client_id": self._client_id,
            "assigned_id": assigned_id,
            "month_start_date": period_key.month_start_date,
            "limit": int(limit),
        }
        if weekly:
            params["period_end_date"] = period_key.period_end_date
        rows = reader.fetch_all(q.driver_trend_sql(weekly=weekly, sources=self.sources), params)
        points: list[TrendPoint] = []
        for row in rows:
            key = RankingPeriodKey(
                period_type=period_key.period_type,
                month_start_date=row["month_start_date"],
                period_start_date=row["period_start_date"],
                period_end_date=row["period_end_date"],
                period_sequence_in_month=_opt_int(row.get("period_sequence_in_month")),
            )
            points.append(
                TrendPoint(
                    period_key=key,
                    period_label=str(row.get("period_label") or key.month_start_date.strftime("%Y-%m")),
                    period_start_date=row["period_start_date"],
                    period_end_date=row["period_end_date"],
                    is_partial_period=bool(row.get("is_partial_period")),
                    eco_driving_score_total=_opt_decimal(row.get("eco_driving_score_total")),
                    ranking_position=_opt_int(row.get("ranking_position")),
                    ranking_total_participants=_opt_int(row.get("ranking_total_participants")),
                    qualification_status=row.get("qualification_status"),
                    total_kilometers=_opt_decimal(row.get("total_kilometers")),
                    is_current=key == period_key,
                )
            )
        # The view is read newest-first so the LIMIT keeps the *latest* window;
        # the presentation contract is chronological, so reverse once here.
        points.reverse()
        return tuple(points)

    def get_period_progression(
        self,
        reader: q.RowReader,
        period_key: RankingPeriodKey,
        assigned_id: str,
    ) -> tuple[PeriodProgressionRow, ...]:
        """Cumulative month-to-date snapshots of one driver inside one month.

        Weekly periods are cumulative, so these rows are a running total, not
        week slices. They are returned for diagnosis only and must never be
        added together.
        """

        assigned_id = self._require_assigned_id(assigned_id)
        if period_key.period_type is not PeriodType.WEEKLY:
            # A monthly entry has no in-month snapshot progression of its own.
            return ()
        params = {
            "client_id": self._client_id,
            "assigned_id": assigned_id,
            "month_start_date": period_key.month_start_date,
            "period_end_date": period_key.period_end_date,
        }
        rows = reader.fetch_all(q.period_progression_sql(self.sources), params)
        return tuple(
            PeriodProgressionRow(
                period_label=str(row.get("period_label") or ""),
                period_start_date=row["period_start_date"],
                period_end_date=row["period_end_date"],
                period_sequence_in_month=_opt_int(row.get("period_sequence_in_month")),
                is_partial_period=bool(row.get("is_partial_period")),
                eco_driving_score_total=_opt_decimal(row.get("eco_driving_score_total")),
                qualification_status=row.get("qualification_status"),
                total_distance_meters=_to_int(row.get("total_distance_meters")),
                total_kilometers=_opt_decimal(row.get("total_kilometers")),
                trips_count=_to_int(row.get("trips_count")),
                event_counts={m: _to_int(row.get(m)) for m in REQUIRED_METRICS},
                metric_rates=build_metric_mapping(
                    {m: _opt_decimal(row.get(q.RATE_COLUMNS[m])) for m in REQUIRED_METRICS}
                ),
                is_current=row["period_end_date"] == period_key.period_end_date,
            )
            for row in rows
        )

    # -- score definition -----------------------------------------------------

    def get_score_definition(self) -> ScoreDefinition:
        metrics = []
        for metric in REQUIRED_METRICS:
            rules = SCORING_RULES[metric]
            metrics.append(
                ScoreMetricDefinition(
                    metric_key=metric,
                    label=VALIDATION_LABELS[metric],
                    max_points=METRIC_MAX_POINTS[metric],
                    final_points=rules.final_points,
                    buckets=tuple(
                        ScoreMetricBucket(upper_bound=b.upper_bound, points=b.points)
                        for b in rules.buckets
                    ),
                )
            )
        return ScoreDefinition(
            ranking_family=self.RANKING_FAMILY,
            max_possible_score=100,
            min_possible_score=-100,
            rate_basis="rounded_events_per_100km",
            rate_rounding="ROUND_HALF_UP to a whole event per 100 km before scoring",
            minimum_qualifying_distance_meters=MIN_QUALIFYING_DISTANCE_METERS,
            rating_thresholds=tuple(
                [RatingThreshold(minimum_score=minimum, label=label) for minimum, label in RATING_THRESHOLDS]
                + [RatingThreshold(minimum_score=None, label=RATING_FALLBACK_LABEL)]
            ),
            metrics=tuple(metrics),
        )

    # -- helpers --------------------------------------------------------------

    def _require_supported(self, period_type: PeriodType) -> None:
        if period_type not in self.supported_period_types:
            raise UnsupportedPeriodTypeError("unsupported period type")

    @staticmethod
    def _validate_ranking_group(ranking_group: Optional[str]) -> Optional[str]:
        if ranking_group is None:
            return None
        try:
            return RankingGroup(ranking_group).value
        except ValueError as exc:
            raise InvalidRankingGroupError("invalid ranking group") from exc

    @staticmethod
    def _require_assigned_id(assigned_id: str) -> str:
        if not isinstance(assigned_id, str) or assigned_id == "":
            raise RankingEntryNotFoundError("assigned_id must be a non-empty string")
        return assigned_id

    def _entry_scope_params(self, period_key: RankingPeriodKey) -> dict[str, object]:
        return {
            "client_id": self._client_id,
            "month_start_date": period_key.month_start_date,
            "period_start_date": period_key.period_start_date,
            "period_end_date": period_key.period_end_date,
        }

    def _trip_scope_params(self, period_key: RankingPeriodKey, assigned_id: str) -> dict[str, object]:
        params = {
            "client_id": self._client_id,
            "assigned_id": assigned_id,
        }
        params.update(
            q.build_period_boundary_params(
                period_key.period_start_date, period_key.period_end_date
            )
        )
        return params


def _in_group(entry: RankingEntry, group_value: Optional[str]) -> bool:
    """Tab membership for one recomputed entry — the SQL predicate, in Python.

    It is the direct counterpart of ``queries.group_predicate``: no filter keeps
    every row, ``INCLUDED`` additionally keeps a driver the roster permitted who
    fell under the distance threshold, and the other two tabs are plain group
    equality. The two implementations exist because one view is served from a
    persisted month and the other from a recomputed week set; they must answer
    the same question, so they are written to be read side by side.
    """

    if group_value is None:
        return True
    if entry.ranking_group is not None:
        return entry.ranking_group.value == group_value
    return (
        group_value == RankingGroup.INCLUDED.value
        and entry.ranking_included is True
    )


def _sort_dynamic_entries(entries, sort_field, direction):
    """Order recomputed entries with the persisted query's own semantics.

    The default order is the ranked order: position ascending with unranked
    rows last, then score descending, then distance descending, then the opaque
    id. An explicit sort field must be in ``ENTRY_SORT_FIELDS``; anything else
    is refused rather than silently ignored.
    """

    rows = list(entries)
    if sort_field is None:
        # Positions come first when they exist; where they do not — an
        # `UNKNOWN_DRIVER` population is never positioned — the canonical score
        # order decides. It is taken from the shared domain rather than
        # restated, so a numeric `0` outranks `-1` here exactly as it does in
        # the scheduled generators. Ordering happens before any page slicing.
        return sorted(
            rows,
            key=lambda e: (
                e.ranking_position is None,
                e.ranking_position or 0,
                PD.score_order_key(
                    e.eco_driving_score_total, e.total_kilometers, e.assigned_id
                ),
            ),
        )
    if sort_field not in q.ENTRY_SORT_FIELD_KEYS:
        raise InvalidSortFieldError("unsupported sort field")
    descending = str(direction or "ASC").strip().upper() == "DESC"
    if str(direction or "ASC").strip().upper() not in ("ASC", "DESC"):
        raise InvalidSortFieldError("sort direction must be ASC or DESC")

    def key(entry):
        value = getattr(entry, sort_field, None)
        # NULLS LAST in both directions, matching ``resolve_order_by``.
        if value is None:
            return (1, 0)
        if isinstance(value, str):
            return (0, value)
        return (0, Decimal(value))

    typed = [e for e in rows if getattr(e, sort_field, None) is not None]
    untyped = [e for e in rows if getattr(e, sort_field, None) is None]
    typed.sort(key=lambda e: e.assigned_id)
    typed.sort(key=lambda e: key(e)[1], reverse=descending)
    untyped.sort(key=lambda e: e.assigned_id)
    return typed + untyped


def _clip_intervals(intervals, filters):
    """Intersect the selected intervals with an explicit trip-start filter."""

    if filters is None:
        return tuple(intervals)
    low = filters.trip_start_from
    high = filters.trip_start_to
    clipped = []
    for start, end in intervals:
        lower = max(start, low) if low is not None else start
        upper = min(end, high) if high is not None else end
        if lower < upper:
            clipped.append((lower, upper))
    return tuple(clipped)
