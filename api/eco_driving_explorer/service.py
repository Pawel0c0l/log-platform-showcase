"""Eco Driving Explorer read-API controller (framework-independent).

This layer contains all endpoint logic: authentication gating, effective-access
enforcement, trusted provider resolution, Stage 1 provider calls, JSON
serialization, sanitized audit, and domain-error -> HTTP mapping. It deliberately
does not import FastAPI so it can be unit-tested directly (the repo has no HTTP
test client available). ``api/eco_driving_explorer/http.py`` is a thin adapter
that extracts query parameters, resolves the current user, and turns
``ApiResult`` into a JSON response.

The controller depends only on an injected backend (``EcoDrivingBackend``
protocol) for all platform / client-database I/O and audit writes, so no
authentication, session, connection, or audit logic is duplicated here.
"""

from __future__ import annotations

import logging
import re

from datetime import date, datetime

from dataclasses import dataclass
from typing import Any, ContextManager, Mapping, Optional, Protocol

from .access import EcoDrivingClientAccess
from .backend import ResolvedEcoClient, assigned_id_digest
from .errors import (
    EnvironmentClientMismatchError,
    InvalidWeekSelectionError,
    InvalidPaginationError,
    InvalidRankingGroupError,
    InvalidSortFieldError,
    PeriodNotFoundError,
    ProviderNotFoundError,
    RankingEntryNotFoundError,
    ReconstructionUnavailableError,
    UnsupportedPeriodTypeError,
)
from .models import (
    LineageQuality,
    PeriodType,
    RankingGroup,
    RankingPeriodKey,
    TripFilters,
)
from .queries import DEFAULT_PAGE_SIZE, MAX_SEARCH_LENGTH, RowReader, business_local_midnight
if __package__.startswith("api."):
    from ..timezone_utils import get_business_timezone
else:
    from timezone_utils import get_business_timezone
from .registry import available_provider_identities, get_provider
from .week_selection import (
    BASIS_EMPTY,
    BASIS_MONTH_DYNAMIC,
    BASIS_MONTH_PERSISTED,
    BASIS_WEEKS_DYNAMIC,
    MODE_MONTH,
    parse_week_selection,
)
from .serialization import (
    error as envelope_error,
    ok as envelope_ok,
    serialize_page,
    serialize_period,
    serialize_period_progression_row,
    serialize_provider,
    serialize_ranking_entry,
    serialize_reconciliation,
    serialize_score_definition,
    serialize_dynamic_basis,
    serialize_score_distribution,
    serialize_trend_point,
    serialize_trip,
    serialize_week_selection,
)


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ApiResult:
    status_code: int
    body: dict[str, Any]


class EcoDrivingBackend(Protocol):
    def fetch_access(self, user_id: str, client_code: str) -> EcoDrivingClientAccess: ...

    def resolve_binding(self, client_code: str, ranking_family: str) -> ResolvedEcoClient: ...

    def open_reader(self, binding: ResolvedEcoClient) -> ContextManager[RowReader]: ...

    def record_audit(
        self,
        *,
        event_type: str,
        actor_user_id: str | None,
        client_code: str | None,
        request: Any,
        metadata: dict,
    ) -> None: ...


class _Unauthenticated(Exception):
    pass


class _Forbidden(Exception):
    pass


class _MalformedPeriodToken(Exception):
    pass


class _InvalidParameter(Exception):
    pass


_ERROR_MAP: tuple[tuple[type, int, str], ...] = (
    (_Unauthenticated, 401, "UNAUTHENTICATED"),
    (_Forbidden, 403, "FORBIDDEN"),
    (ProviderNotFoundError, 404, "PROVIDER_NOT_FOUND"),
    (PeriodNotFoundError, 404, "PERIOD_NOT_FOUND"),
    (RankingEntryNotFoundError, 404, "RANKING_ENTRY_NOT_FOUND"),
    (UnsupportedPeriodTypeError, 422, "UNSUPPORTED_PERIOD_TYPE"),
    (InvalidRankingGroupError, 422, "INVALID_RANKING_GROUP"),
    (InvalidSortFieldError, 422, "INVALID_SORT_FIELD"),
    (InvalidPaginationError, 422, "INVALID_PAGINATION"),
    (_MalformedPeriodToken, 422, "MALFORMED_PERIOD_TOKEN"),
    (_InvalidParameter, 422, "INVALID_PARAMETER"),
    (ReconstructionUnavailableError, 409, "RECONSTRUCTION_UNAVAILABLE"),
    (InvalidWeekSelectionError, 422, "INVALID_WEEK_SELECTION"),
    (EnvironmentClientMismatchError, 503, "ENVIRONMENT_CLIENT_MISMATCH"),
)

_SAFE_MESSAGES: Mapping[str, str] = {
    "UNAUTHENTICATED": "Authentication required.",
    "FORBIDDEN": "You do not have access to this Eco Driving resource.",
    "PROVIDER_NOT_FOUND": "Eco Driving provider not found.",
    "PERIOD_NOT_FOUND": "Ranking period not found.",
    "RANKING_ENTRY_NOT_FOUND": "Ranking entry not found.",
    "UNSUPPORTED_PERIOD_TYPE": "Unsupported period type.",
    "INVALID_RANKING_GROUP": "Invalid ranking group.",
    "INVALID_SORT_FIELD": "Invalid sort field or direction.",
    "INVALID_PAGINATION": "Invalid pagination parameters.",
    "MALFORMED_PERIOD_TOKEN": "Malformed period key.",
    "INVALID_PARAMETER": "Invalid request parameter.",
    "INVALID_WEEK_SELECTION": "Invalid month or week selection.",
    "RECONSTRUCTION_UNAVAILABLE": "Trip reconstruction is unavailable for this entry.",
    "ENVIRONMENT_CLIENT_MISMATCH": "Client environment is temporarily unavailable.",
    "INTERNAL_ERROR": "Internal error.",
}


class EcoDrivingApiService:
    def __init__(
        self,
        backend: EcoDrivingBackend,
        *,
        lineage_modes: tuple[LineageQuality, ...] = (LineageQuality.RECONSTRUCTED_CURRENT_STATE,),
    ) -> None:
        self._backend = backend
        self._lineage_modes = tuple(lineage_modes)

    # -- endpoints ------------------------------------------------------------

    def list_providers(self, *, user: Optional[dict], request: Any = None) -> ApiResult:
        try:
            u = self._require_user(user)
            providers: list[dict] = []
            for identity in available_provider_identities():
                access = self._backend.fetch_access(self._user_id(u), identity.client_code)
                if access.can_view_eco_ranking and access.client_is_active:
                    providers.append(
                        serialize_provider(identity, access, lineage_modes=self._lineage_modes)
                    )
            self._audit(
                u, request, "eco_driving_providers_viewed", None,
                {"result_count": len(providers)},
            )
            return ApiResult(200, envelope_ok(providers, {"count": len(providers)}))
        except Exception as exc:  # noqa: BLE001 - mapped to sanitized envelope
            return self._result_for_error(exc)

    def list_periods(
        self,
        *,
        user: Optional[dict],
        request: Any = None,
        client_code: Optional[str],
        ranking_family: Optional[str],
        period_type: Optional[str],
        year: Any = None,
        month: Any = None,
    ) -> ApiResult:
        try:
            u = self._require_user(user)
            self._require_ranking(u, client_code)
            pt = self._parse_period_type(period_type)
            year_i = self._opt_int(year, "year")
            month_i = self._opt_int(month, "month")
            binding = self._backend.resolve_binding(client_code or "", ranking_family or "")
            provider = self._provider_for(binding)
            with self._backend.open_reader(binding) as reader:
                periods = provider.list_periods(reader, pt, year=year_i, month=month_i)
            data = [serialize_period(p) for p in periods]
            self._audit(
                u, request, "eco_driving_periods_viewed", binding.client_code,
                {
                    "ranking_family": binding.ranking_family,
                    "period_type": pt.value,
                    "year": year_i,
                    "month": month_i,
                    "result_count": len(data),
                },
            )
            return ApiResult(200, envelope_ok(data, {"count": len(data), "period_type": pt.value}))
        except Exception as exc:  # noqa: BLE001
            return self._result_for_error(exc)

    def list_ranking_entries(
        self,
        *,
        user: Optional[dict],
        request: Any = None,
        client_code: Optional[str],
        ranking_family: Optional[str],
        period_key: Optional[str],
        ranking_group: Optional[str],
        page: Any = None,
        limit: Any = None,
        sort: Optional[str] = None,
        direction: Optional[str] = None,
        search: Optional[str] = None,
    ) -> ApiResult:
        try:
            u = self._require_user(user)
            self._require_ranking(u, client_code)
            key = self._parse_period_key(period_key)
            group = self._require_ranking_group(ranking_group)
            page_i = self._opt_int(page, "page", default=1)
            limit_i = self._opt_int(limit, "limit", default=DEFAULT_PAGE_SIZE)
            term = self._search_term(search)
            binding = self._backend.resolve_binding(client_code or "", ranking_family or "")
            provider = self._provider_for(binding)
            with self._backend.open_reader(binding) as reader:
                result = provider.list_ranking_entries(
                    reader,
                    key,
                    ranking_group=group,
                    sort_field=sort,
                    direction=direction,
                    page=page_i,
                    limit=limit_i,
                    search=term,
                )
            items, meta = serialize_page(result, serialize_ranking_entry)
            meta["ranking_group"] = group
            meta["search"] = term
            self._audit(
                u, request, "eco_driving_ranking_viewed", binding.client_code,
                {
                    "ranking_family": binding.ranking_family,
                    "period_type": key.period_type.value,
                    "period_start_date": key.period_start_date.isoformat(),
                    "period_end_date_exclusive": key.period_end_date.isoformat(),
                    "ranking_group": group,
                    "page": meta["page"],
                    "limit": meta["limit"],
                    "result_count": meta["count"],
                    "total_count": meta["total_count"],
                    # Whether a search narrowed the read, never the term itself.
                    "search_applied": term is not None,
                    "lineage_quality": LineageQuality.RECONSTRUCTED_CURRENT_STATE.value,
                },
            )
            return ApiResult(200, envelope_ok(items, meta))
        except Exception as exc:  # noqa: BLE001
            return self._result_for_error(exc)

    def get_ranking_entry(
        self,
        *,
        user: Optional[dict],
        request: Any = None,
        client_code: Optional[str],
        ranking_family: Optional[str],
        period_key: Optional[str],
        assigned_id: Optional[str],
    ) -> ApiResult:
        try:
            u = self._require_user(user)
            access = self._require_ranking(u, client_code)
            key = self._parse_period_key(period_key)
            aid = self._require_assigned_id(assigned_id)
            binding = self._backend.resolve_binding(client_code or "", ranking_family or "")
            provider = self._provider_for(binding)
            with self._backend.open_reader(binding) as reader:
                entry = provider.get_ranking_entry(reader, key, aid)
            data = serialize_ranking_entry(entry)
            data["provider_display_name"] = provider.identity.display_name
            data["score_definition"] = serialize_score_definition(provider.get_score_definition())
            data["capabilities"] = {
                "can_view_trip_details": bool(access.can_view_eco_trip_details),
                "can_view_trip_routes": bool(access.can_view_eco_trip_routes),
            }
            self._audit(
                u, request, "eco_driving_ranking_entry_viewed", binding.client_code,
                {
                    "ranking_family": binding.ranking_family,
                    "period_type": key.period_type.value,
                    "period_start_date": key.period_start_date.isoformat(),
                    "period_end_date_exclusive": key.period_end_date.isoformat(),
                    "assigned_id_digest": assigned_id_digest(aid),
                    # A row below the qualifying distance belongs to no ranking
                    # group at all; that is a reportable state, not a crash.
                    "ranking_group": entry.ranking_group.value if entry.ranking_group else None,
                    "lineage_quality": entry.lineage_quality.value,
                },
            )
            return ApiResult(200, envelope_ok(data))
        except Exception as exc:  # noqa: BLE001
            return self._result_for_error(exc)

    def list_contributing_trips(
        self,
        *,
        user: Optional[dict],
        request: Any = None,
        client_code: Optional[str],
        ranking_family: Optional[str],
        period_key: Optional[str],
        assigned_id: Optional[str],
        page: Any = None,
        limit: Any = None,
        sort: Optional[str] = None,
        direction: Optional[str] = None,
        trip_start_from: Optional[str] = None,
        trip_start_to: Optional[str] = None,
        provider_trip_id: Any = None,
        min_distance_meters: Any = None,
        max_distance_meters: Any = None,
        has_scoring_events: Any = None,
    ) -> ApiResult:
        try:
            u = self._require_user(user)
            self._require_trip_details(u, client_code)
            key = self._parse_period_key(period_key)
            aid = self._require_assigned_id(assigned_id)
            page_i = self._opt_int(page, "page", default=1)
            limit_i = self._opt_int(limit, "limit", default=DEFAULT_PAGE_SIZE)
            filters = self._trip_filters(
                key, trip_start_from=trip_start_from, trip_start_to=trip_start_to,
                provider_trip_id=provider_trip_id,
                min_distance_meters=min_distance_meters,
                max_distance_meters=max_distance_meters,
                has_scoring_events=has_scoring_events,
            )
            binding = self._backend.resolve_binding(client_code or "", ranking_family or "")
            provider = self._provider_for(binding)
            with self._backend.open_reader(binding) as reader:
                result = provider.list_contributing_trips(
                    reader, key, aid, filters=filters, sort_field=sort,
                    direction=direction, page=page_i, limit=limit_i,
                )
            items, meta = serialize_page(result, serialize_trip)
            meta.update({
                "lineage_quality": LineageQuality.RECONSTRUCTED_CURRENT_STATE.value,
                "period_start": key.period_start_date.isoformat(),
                "period_end_exclusive": key.period_end_date.isoformat(),
                "sort": sort or "trip_start_ts",
                "direction": str(direction or "ASC").upper(),
                "filters": self._serialize_trip_filters(filters),
                "filters_active": filters.active,
                "unfiltered_reconstructed_count": result.unfiltered_total_count,
            })
            audit_meta = {
                "ranking_family": binding.ranking_family,
                "period_type": key.period_type.value,
                "period_start_date": key.period_start_date.isoformat(),
                "period_end_date_exclusive": key.period_end_date.isoformat(),
                "assigned_id_digest": assigned_id_digest(aid),
                "page": meta["page"], "limit": meta["limit"],
                "sort": meta["sort"], "direction": meta["direction"],
                "result_count": meta["count"], "total_count": meta["total_count"],
                "lineage_quality": LineageQuality.RECONSTRUCTED_CURRENT_STATE.value,
                "filters_active": filters.active,
                "trip_start_from": filters.requested_trip_start_from,
                "trip_start_to": filters.requested_trip_start_to,
                "min_distance_meters": filters.min_distance_meters,
                "max_distance_meters": filters.max_distance_meters,
                "has_scoring_events": filters.has_scoring_events,
                "provider_trip_filter_used": filters.provider_trip_id is not None,
            }
            self._audit(u, request, "eco_driving_trip_details_viewed", binding.client_code, audit_meta)
            return ApiResult(200, envelope_ok(items, meta))
        except Exception as exc:  # noqa: BLE001
            return self._result_for_error(exc)

    def get_score_distribution(
        self,
        *,
        user: Optional[dict],
        request: Any = None,
        client_code: Optional[str],
        ranking_family: Optional[str],
        period_key: Optional[str],
        ranking_group: Optional[str] = None,
    ) -> ApiResult:
        """Fleet score histogram for the caller's already-authorized ranking.

        Guarded by exactly the same ``can_view_eco_ranking`` check and the same
        trusted client binding as the ranking itself, and scoped to one period
        and one ranking group, so it introduces no aggregate the caller could
        not already read row by row.
        """

        try:
            u = self._require_user(user)
            self._require_ranking(u, client_code)
            key = self._parse_period_key(period_key)
            group = self._require_ranking_group(ranking_group) if ranking_group else None
            binding = self._backend.resolve_binding(client_code or "", ranking_family or "")
            provider = self._provider_for(binding)
            with self._backend.open_reader(binding) as reader:
                distribution = provider.get_score_distribution(reader, key, ranking_group=group)
            data = serialize_score_distribution(distribution)
            self._audit(
                u, request, "eco_driving_ranking_viewed", binding.client_code,
                {
                    "ranking_family": binding.ranking_family,
                    "period_type": key.period_type.value,
                    "period_start_date": key.period_start_date.isoformat(),
                    "period_end_date_exclusive": key.period_end_date.isoformat(),
                    "ranking_group": group,
                    "surface": "score_distribution",
                    "result_count": data["total_count"],
                },
            )
            return ApiResult(200, envelope_ok(data))
        except Exception as exc:  # noqa: BLE001
            return self._result_for_error(exc)

    def get_driver_trend(
        self,
        *,
        user: Optional[dict],
        request: Any = None,
        client_code: Optional[str],
        ranking_family: Optional[str],
        period_key: Optional[str],
        assigned_id: Optional[str],
    ) -> ApiResult:
        """The requested driver's own comparable periods, chronological.

        Reads only rows for that one ``assigned_id`` inside the caller's
        authorized client, and never fills a period the driver has no persisted
        row for.
        """

        try:
            u = self._require_user(user)
            self._require_ranking(u, client_code)
            key = self._parse_period_key(period_key)
            aid = self._require_assigned_id(assigned_id)
            binding = self._backend.resolve_binding(client_code or "", ranking_family or "")
            provider = self._provider_for(binding)
            with self._backend.open_reader(binding) as reader:
                points = provider.get_driver_trend(reader, key, aid)
            data = [serialize_trend_point(point) for point in points]
            self._audit(
                u, request, "eco_driving_ranking_entry_viewed", binding.client_code,
                {
                    "ranking_family": binding.ranking_family,
                    "period_type": key.period_type.value,
                    "period_start_date": key.period_start_date.isoformat(),
                    "period_end_date_exclusive": key.period_end_date.isoformat(),
                    "assigned_id_digest": assigned_id_digest(aid),
                    "surface": "driver_trend",
                    "result_count": len(data),
                },
            )
            return ApiResult(200, envelope_ok(data, {"count": len(data)}))
        except Exception as exc:  # noqa: BLE001
            return self._result_for_error(exc)

    def get_period_progression(
        self,
        *,
        user: Optional[dict],
        request: Any = None,
        client_code: Optional[str],
        ranking_family: Optional[str],
        period_key: Optional[str],
        assigned_id: Optional[str],
    ) -> ApiResult:
        """Cumulative month-to-date snapshots for one driver, for diagnosis only.

        The rows are running totals of the same month; the envelope marks each
        one ``is_cumulative_snapshot`` so no consumer can treat them as addable
        week slices.
        """

        try:
            u = self._require_user(user)
            self._require_ranking(u, client_code)
            key = self._parse_period_key(period_key)
            aid = self._require_assigned_id(assigned_id)
            binding = self._backend.resolve_binding(client_code or "", ranking_family or "")
            provider = self._provider_for(binding)
            with self._backend.open_reader(binding) as reader:
                rows = provider.get_period_progression(reader, key, aid)
            data = [serialize_period_progression_row(row) for row in rows]
            self._audit(
                u, request, "eco_driving_ranking_entry_viewed", binding.client_code,
                {
                    "ranking_family": binding.ranking_family,
                    "period_type": key.period_type.value,
                    "period_start_date": key.period_start_date.isoformat(),
                    "period_end_date_exclusive": key.period_end_date.isoformat(),
                    "assigned_id_digest": assigned_id_digest(aid),
                    "surface": "period_progression",
                    "result_count": len(data),
                },
            )
            return ApiResult(
                200,
                envelope_ok(data, {"count": len(data), "cumulative_month_to_date": True}),
            )
        except Exception as exc:  # noqa: BLE001
            return self._result_for_error(exc)

    # -- month + arbitrary-week basis (dynamic recomputation) -----------------
    #
    # Whole month and week subset are two *different* data paths on purpose:
    #
    # * whole month reads the canonical persisted monthly snapshot, because that
    #   row is the official monthly reporting truth shared with the monthly
    #   e-mail and report. Replacing it with a fresh interpretation would make
    #   the portal and the report disagree about the same driver;
    # * any proper subset has no persisted row and never will, so it is
    #   recomputed from the underlying trip assignments over the union of the
    #   selected isolated intervals.
    #
    # The two are proved equivalent for a completed month by deterministic
    # parity tests rather than by substituting one for the other at runtime.
    # Persisted cumulative month-to-date snapshots are never summed on either
    # path.

    def list_basis_months(
        self,
        *,
        user: Optional[dict],
        request: Any = None,
        client_code: Optional[str],
        ranking_family: Optional[str],
    ) -> ApiResult:
        """Months this client has assignment data for, newest first."""

        try:
            u = self._require_user(user)
            self._require_ranking(u, client_code)
            binding = self._backend.resolve_binding(client_code or "", ranking_family or "")
            provider = self._provider_for(binding)
            with self._backend.open_reader(binding) as reader:
                months = provider.list_available_months(reader)
            data = [f"{month:%Y-%m}" for month in months]
            self._audit(
                u, request, "eco_driving_periods_viewed", binding.client_code,
                {
                    "ranking_family": binding.ranking_family,
                    "surface": "basis_months",
                    "result_count": len(data),
                },
            )
            return ApiResult(200, envelope_ok(data, {"count": len(data)}))
        except Exception as exc:  # noqa: BLE001
            return self._result_for_error(exc)

    def get_basis_ranking(
        self,
        *,
        user: Optional[dict],
        request: Any = None,
        client_code: Optional[str],
        ranking_family: Optional[str],
        month: Any,
        weeks: Any = None,
        ranking_group: Optional[str] = None,
        page: Any = None,
        limit: Any = None,
        sort: Optional[str] = None,
        direction: Optional[str] = None,
        search: Optional[str] = None,
    ) -> ApiResult:
        """One ranking for one client, one month and a canonical week basis."""

        try:
            u = self._require_user(user)
            self._require_ranking(u, client_code)
            selection = parse_week_selection(month, weeks)
            group = self._require_ranking_group(ranking_group)
            page_i = self._opt_int(page, "page", default=1)
            limit_i = self._opt_int(limit, "limit", default=DEFAULT_PAGE_SIZE)
            term = self._search_term(search)
            binding = self._backend.resolve_binding(client_code or "", ranking_family or "")
            provider = self._provider_for(binding)

            basis_meta = {"selection": serialize_week_selection(selection)}

            if selection.is_empty:
                # `EC-5`: zero weeks is a prompt, not an error and not a silent
                # widening back to the whole month. No query is issued.
                meta = {
                    **basis_meta,
                    "basis_source": BASIS_EMPTY,
                    "page": page_i, "limit": limit_i, "count": 0, "total_count": 0,
                    "has_next": False, "ranking_group": group, "search": term,
                    "basis": None, "distribution": None,
                }
                meta["selection"]["basis_source"] = BASIS_EMPTY
                self._audit_basis(
                    u, request, binding, selection, group, 0, term,
                    basis_source=BASIS_EMPTY,
                )
                return ApiResult(200, envelope_ok([], meta))

            if selection.mode == MODE_MONTH:
                key = selection.monthly_period_key()
                with self._backend.open_reader(binding) as reader:
                    period = self._persisted_month(provider, reader, selection)
                    if period is not None:
                        result = provider.list_ranking_entries(
                            reader, key, ranking_group=group, sort_field=sort,
                            direction=direction, page=page_i, limit=limit_i, search=term,
                        )
                        distribution = provider.get_score_distribution(
                            reader, key, ranking_group=group
                        )
                    else:
                        # No canonical monthly snapshot for this month yet. The
                        # month is still a real reporting range, so it is
                        # aggregated dynamically over its full canonical
                        # interval rather than reported as empty.
                        basis = provider.recompute_basis(reader, selection)

                if period is not None:
                    items, meta = serialize_page(result, serialize_ranking_entry)
                    counts = dict(period.entry_counts_by_group)
                    not_ranked = int(period.not_ranked_count)
                    not_ranked_included = int(period.not_ranked_included_count)
                    meta.update({
                        **basis_meta,
                        "basis_source": BASIS_MONTH_PERSISTED,
                        "ranking_group": group,
                        "search": term,
                        "period_key": key.token,
                        "basis": {
                            "counts_by_group": {k: int(v) for k, v in counts.items()},
                            "not_ranked_count": not_ranked,
                            "not_ranked_included_count": not_ranked_included,
                            "qualified_count": sum(int(v) for v in counts.values()),
                            "population_count": sum(int(v) for v in counts.values()) + not_ranked,
                            "total_trips_count": None,
                            "total_distance_meters": None,
                            "total_kilometers": None,
                        },
                        "distribution": serialize_score_distribution(distribution),
                    })
                    meta["selection"]["basis_source"] = BASIS_MONTH_PERSISTED
                    self._audit_basis(
                        u, request, binding, selection, group, meta.get("total_count"),
                        term, basis_source=BASIS_MONTH_PERSISTED,
                    )
                    return ApiResult(200, envelope_ok(items, meta))

                basis_source = BASIS_MONTH_DYNAMIC
            else:
                basis_source = BASIS_WEEKS_DYNAMIC
                with self._backend.open_reader(binding) as reader:
                    basis = provider.recompute_basis(reader, selection)
            result = provider.paginate_dynamic_entries(
                basis, ranking_group=group, search=term, sort_field=sort,
                direction=direction, page=page_i, limit=limit_i,
            )
            distribution = provider.dynamic_score_distribution(basis, ranking_group=group)
            items, meta = serialize_page(result, serialize_ranking_entry)
            meta.update({
                **basis_meta,
                "basis_source": basis_source,
                "ranking_group": group,
                "search": term,
                "period_key": None,
                "basis": serialize_dynamic_basis(basis),
                "distribution": serialize_score_distribution(distribution),
            })
            meta["selection"]["basis_source"] = basis_source
            self._audit_basis(
                u, request, binding, selection, group, meta.get("total_count"), term,
                basis_source=basis_source,
            )
            return ApiResult(200, envelope_ok(items, meta))
        except Exception as exc:  # noqa: BLE001
            return self._result_for_error(exc)

    def get_basis_ranking_entry(
        self,
        *,
        user: Optional[dict],
        request: Any = None,
        client_code: Optional[str],
        ranking_family: Optional[str],
        month: Any,
        weeks: Any = None,
        assigned_id: Optional[str] = None,
    ) -> ApiResult:
        """One driver, scored on the same basis the ranking was built from."""

        try:
            u = self._require_user(user)
            access = self._require_ranking(u, client_code)
            selection = parse_week_selection(month, weeks)
            if selection.is_empty:
                raise InvalidWeekSelectionError("a driver needs at least one selected week")
            aid = self._require_assigned_id(assigned_id)
            binding = self._backend.resolve_binding(client_code or "", ranking_family or "")
            provider = self._provider_for(binding)

            entry = None
            distribution = None
            period_key_token = None
            basis_source = BASIS_WEEKS_DYNAMIC
            basis = None

            if selection.mode == MODE_MONTH:
                key = selection.monthly_period_key()
                with self._backend.open_reader(binding) as reader:
                    period = self._persisted_month(provider, reader, selection)
                    if period is not None:
                        basis_source = BASIS_MONTH_PERSISTED
                        entry = provider.get_ranking_entry(reader, key, aid)
                        # `S12-C`: a non-qualified driver renders no distribution,
                        # so none is read and none is claimed in the audit.
                        distribution = (
                            provider.get_score_distribution(
                                reader,
                                key,
                                ranking_group=(
                                    entry.ranking_group.value if entry.ranking_group else None
                                ),
                            )
                            if entry.qualification_status == "QUALIFIED"
                            else None
                        )
                        period_key_token = key.token
                    else:
                        basis_source = BASIS_MONTH_DYNAMIC
                        basis = provider.recompute_basis(reader, selection)
            else:
                with self._backend.open_reader(binding) as reader:
                    basis = provider.recompute_basis(reader, selection)

            if basis is not None:
                # The detail page must be scored on exactly the basis the
                # ranking used, so it re-reads the same recomputation rather
                # than falling back to a persisted period.
                entry = next((e for e in basis.entries if e.assigned_id == aid), None)
                if entry is None:
                    raise RankingEntryNotFoundError("ranking entry not found")
                distribution = (
                    provider.dynamic_score_distribution(
                        basis,
                        ranking_group=(
                            entry.ranking_group.value if entry.ranking_group else None
                        ),
                    )
                    if entry.qualification_status == "QUALIFIED"
                    else None
                )

            data = serialize_ranking_entry(entry)
            data["provider_display_name"] = provider.identity.display_name
            data["score_definition"] = serialize_score_definition(provider.get_score_definition())
            data["capabilities"] = {
                "can_view_trip_details": bool(access.can_view_eco_trip_details),
                "can_view_trip_routes": bool(access.can_view_eco_trip_routes),
            }
            data["selection"] = serialize_week_selection(selection)
            data["selection"]["basis_source"] = basis_source
            data["basis_source"] = basis_source
            data["distribution"] = (
                serialize_score_distribution(distribution) if distribution else None
            )
            data["period_key"] = period_key_token
            self._audit(
                u, request, "eco_driving_ranking_entry_viewed", binding.client_code,
                {
                    "ranking_family": binding.ranking_family,
                    **self._basis_audit_fields(selection, basis_source=basis_source),
                    "assigned_id_digest": assigned_id_digest(aid),
                    "ranking_group": entry.ranking_group.value if entry.ranking_group else None,
                    "qualification_status": entry.qualification_status,
                    # Only claimed when the surface was actually rendered.
                    "distribution_rendered": distribution is not None,
                    "lineage_quality": entry.lineage_quality.value,
                },
            )
            return ApiResult(200, envelope_ok(data))
        except Exception as exc:  # noqa: BLE001
            return self._result_for_error(exc)

    def list_basis_contributing_trips(
        self,
        *,
        user: Optional[dict],
        request: Any = None,
        client_code: Optional[str],
        ranking_family: Optional[str],
        month: Any,
        weeks: Any = None,
        assigned_id: Optional[str] = None,
        page: Any = None,
        limit: Any = None,
        sort: Optional[str] = None,
        direction: Optional[str] = None,
        trip_start_from: Optional[str] = None,
        trip_start_to: Optional[str] = None,
        provider_trip_id: Any = None,
        min_distance_meters: Any = None,
        max_distance_meters: Any = None,
        has_scoring_events: Any = None,
    ) -> ApiResult:
        """Trip evidence restricted to the selected week union.

        Gated by ``can_view_eco_trip_details`` exactly as the persisted-period
        surface is; the basis changes which rows exist, never who may read them.
        """

        try:
            u = self._require_user(user)
            self._require_trip_details(u, client_code)
            selection = parse_week_selection(month, weeks)
            aid = self._require_assigned_id(assigned_id)
            page_i = self._opt_int(page, "page", default=1)
            limit_i = self._opt_int(limit, "limit", default=DEFAULT_PAGE_SIZE)
            binding = self._backend.resolve_binding(client_code or "", ranking_family or "")
            provider = self._provider_for(binding)

            if selection.is_empty:
                meta = {
                    "selection": serialize_week_selection(selection),
                    "page": page_i, "limit": limit_i, "count": 0, "total_count": 0,
                    "has_next": False, "filters_active": False,
                    "unfiltered_reconstructed_count": 0,
                }
                return ApiResult(200, envelope_ok([], meta))

            key = (
                selection.monthly_period_key()
                if selection.mode == MODE_MONTH
                else selection.synthetic_period_key()
            )
            filters = self._trip_filters(
                key, trip_start_from=trip_start_from, trip_start_to=trip_start_to,
                provider_trip_id=provider_trip_id,
                min_distance_meters=min_distance_meters,
                max_distance_meters=max_distance_meters,
                has_scoring_events=has_scoring_events,
            )
            with self._backend.open_reader(binding) as reader:
                if selection.mode == MODE_MONTH:
                    result = provider.list_contributing_trips(
                        reader, key, aid, filters=filters, sort_field=sort,
                        direction=direction, page=page_i, limit=limit_i,
                    )
                else:
                    result = provider.list_dynamic_trips(
                        reader, selection, aid, filters=filters, sort_field=sort,
                        direction=direction, page=page_i, limit=limit_i,
                    )
            items, meta = serialize_page(result, serialize_trip)
            meta.update({
                "selection": serialize_week_selection(selection),
                "lineage_quality": LineageQuality.RECONSTRUCTED_CURRENT_STATE.value,
                "sort": sort or "trip_start_ts",
                "direction": str(direction or "ASC").upper(),
                "filters": self._serialize_trip_filters(filters),
                "filters_active": filters.active,
                "unfiltered_reconstructed_count": result.unfiltered_total_count,
            })
            self._audit(
                u, request, "eco_driving_trip_details_viewed", binding.client_code,
                {
                    "ranking_family": binding.ranking_family,
                    **self._basis_audit_fields(selection),
                    "assigned_id_digest": assigned_id_digest(aid),
                    "page": meta["page"], "limit": meta["limit"],
                    "sort": meta["sort"], "direction": meta["direction"],
                    "result_count": meta["count"], "total_count": meta["total_count"],
                    "filters_active": filters.active,
                    "lineage_quality": LineageQuality.RECONSTRUCTED_CURRENT_STATE.value,
                },
            )
            return ApiResult(200, envelope_ok(items, meta))
        except Exception as exc:  # noqa: BLE001
            return self._result_for_error(exc)

    # -- basis audit ----------------------------------------------------------

    @staticmethod
    def _persisted_month(provider, reader, selection):
        """The canonical persisted monthly period for this month, or ``None``.

        Presence of a grouped row means at least one persisted monthly stats row
        exists, which is what makes the month official reporting truth. Absence
        means materialisation has not happened for a month that may still be
        fully backed by assignments.
        """

        periods = provider.list_periods(
            reader,
            PeriodType.MONTHLY,
            year=selection.month_start_date.year,
            month=selection.month_start_date.month,
        )
        return next(
            (p for p in periods if p.month_start_date == selection.month_start_date),
            None,
        )

    @staticmethod
    def _basis_audit_fields(selection, *, basis_source: Optional[str] = None) -> dict:
        """Safe basis facts for the audit trail.

        Records which weeks were selected and how the server resolved them.
        Never a trip row, a driver name, a search term or any SQL.
        """

        fields = {
            "basis_mode": selection.mode,
            "basis_month": selection.month_token,
            "basis_weeks": [int(value) for value in selection.selected_sequences],
            "basis_week_count": len(selection.selected_sequences),
            "basis_dynamically_recomputed": bool(
                selection.is_dynamic or basis_source == BASIS_MONTH_DYNAMIC
            ),
        }
        if basis_source is not None:
            # Which execution source served the logical selection. Safe to
            # record: it names a code path, never data.
            fields["basis_source"] = basis_source
        return fields

    def _audit_basis(
        self, u, request, binding, selection, group, total, term,
        *, basis_source: Optional[str] = None,
    ) -> None:
        self._audit(
            u, request, "eco_driving_ranking_viewed", binding.client_code,
            {
                "ranking_family": binding.ranking_family,
                **self._basis_audit_fields(selection, basis_source=basis_source),
                "ranking_group": group,
                "total_count": total,
                # Whether a search narrowed the read, never the term itself.
                "search_applied": term is not None,
                "lineage_quality": LineageQuality.RECONSTRUCTED_CURRENT_STATE.value,
            },
        )

    def reconcile_ranking_entry(
        self,
        *,
        user: Optional[dict],
        request: Any = None,
        client_code: Optional[str],
        ranking_family: Optional[str],
        period_key: Optional[str],
        assigned_id: Optional[str],
    ) -> ApiResult:
        try:
            u = self._require_user(user)
            self._require_trip_details(u, client_code)
            key = self._parse_period_key(period_key)
            aid = self._require_assigned_id(assigned_id)
            binding = self._backend.resolve_binding(client_code or "", ranking_family or "")
            provider = self._provider_for(binding)
            with self._backend.open_reader(binding) as reader:
                result = provider.reconcile_ranking_entry(reader, key, aid)
            data = serialize_reconciliation(result)
            data["score_definition"] = serialize_score_definition(provider.get_score_definition())
            self._audit(
                u, request, "eco_driving_reconciliation_viewed", binding.client_code,
                {
                    "ranking_family": binding.ranking_family,
                    "period_type": key.period_type.value,
                    "period_start_date": key.period_start_date.isoformat(),
                    "period_end_date_exclusive": key.period_end_date.isoformat(),
                    "assigned_id_digest": assigned_id_digest(aid),
                    "reconciliation_status": result.reconciliation_status.value,
                    "mismatch_field_count": len(result.mismatched_fields),
                    "lineage_quality": result.lineage_quality.value,
                },
            )
            return ApiResult(200, envelope_ok(data))
        except ReconstructionUnavailableError as exc:
            self._audit(
                u, request, "eco_driving_reconciliation_viewed", binding.client_code,
                {
                    "ranking_family": binding.ranking_family,
                    "period_type": key.period_type.value,
                    "period_start_date": key.period_start_date.isoformat(),
                    "period_end_date_exclusive": key.period_end_date.isoformat(),
                    "assigned_id_digest": assigned_id_digest(aid),
                    "reconciliation_status": "UNAVAILABLE",
                    "mismatch_field_count": 0,
                    "lineage_quality": LineageQuality.RECONSTRUCTED_CURRENT_STATE.value,
                },
            )
            return self._result_for_error(exc)
        except Exception as exc:  # noqa: BLE001
            try:
                LOGGER.error(
                    "eco driving reconciliation unavailable after unexpected error",
                    extra={
                        "client_code": binding.client_code,
                        "ranking_family": binding.ranking_family,
                        "assigned_id_digest": assigned_id_digest(aid),
                        "error_type": type(exc).__name__,
                    },
                )
                self._audit(
                    u, request, "eco_driving_reconciliation_viewed", binding.client_code,
                    {
                        "ranking_family": binding.ranking_family,
                        "period_type": key.period_type.value,
                        "period_start_date": key.period_start_date.isoformat(),
                        "period_end_date_exclusive": key.period_end_date.isoformat(),
                        "assigned_id_digest": assigned_id_digest(aid),
                        "reconciliation_status": "UNAVAILABLE",
                        "mismatch_field_count": 0,
                        "lineage_quality": LineageQuality.RECONSTRUCTED_CURRENT_STATE.value,
                    },
                )
            except Exception:
                pass
            return self._result_for_error(exc)

    # -- navigation helper ----------------------------------------------------

    def has_eco_ranking_access(self, user: Optional[dict]) -> bool:
        """True if the user has effective ``can_view_eco_ranking`` on any provider.

        Platform-side only (registry allowlist + portal-DB effective access); no
        client-business query is issued, so this is cheap enough for navigation.
        """

        if not user or not user.get("user_id"):
            return False
        uid = str(user.get("user_id"))
        for identity in available_provider_identities():
            try:
                access = self._backend.fetch_access(uid, identity.client_code)
            except Exception:
                continue
            if access.can_view_eco_ranking and access.client_is_active:
                return True
        return False

    # -- guards / helpers -----------------------------------------------------

    @staticmethod
    def _user_id(user: dict) -> str:
        return str(user.get("user_id") or "")

    def _require_user(self, user: Optional[dict]) -> dict:
        if not user or not user.get("user_id"):
            raise _Unauthenticated()
        return user

    def _require_ranking(self, user: dict, client_code: Optional[str]) -> EcoDrivingClientAccess:
        access = self._backend.fetch_access(self._user_id(user), str(client_code or ""))
        if not (access.can_view_eco_ranking and access.client_is_active):
            raise _Forbidden()
        return access

    def _require_trip_details(self, user: dict, client_code: Optional[str]) -> EcoDrivingClientAccess:
        access = self._backend.fetch_access(self._user_id(user), str(client_code or ""))
        if not (
            access.can_view_eco_ranking
            and access.can_view_eco_trip_details
            and access.client_is_active
        ):
            raise _Forbidden()
        return access

    def _provider_for(self, binding: ResolvedEcoClient):
        return get_provider(
            binding.client_code,
            binding.ranking_family,
            client_id=binding.client_id,
        )

    @staticmethod
    def _parse_period_type(value: Optional[str]) -> PeriodType:
        if not value:
            raise UnsupportedPeriodTypeError("period_type is required")
        try:
            return PeriodType(str(value).strip().lower())
        except ValueError as exc:
            raise UnsupportedPeriodTypeError("unsupported period type") from exc

    @staticmethod
    def _parse_period_key(token: Optional[str]) -> RankingPeriodKey:
        if not token:
            raise _MalformedPeriodToken("period_key is required")
        try:
            return RankingPeriodKey.from_token(str(token))
        except Exception as exc:  # noqa: BLE001 - any decode failure is malformed
            raise _MalformedPeriodToken("malformed period key") from exc

    @staticmethod
    def _require_ranking_group(value: Optional[str]) -> str:
        if not value:
            raise InvalidRankingGroupError("ranking_group is required")
        try:
            return RankingGroup(str(value).strip()).value
        except ValueError as exc:
            raise InvalidRankingGroupError("invalid ranking group") from exc

    @staticmethod
    def _search_term(value: Optional[str]) -> Optional[str]:
        """Validate the free-text driver search.

        The term reaches the database only as a bound, wildcard-escaped LIKE
        pattern, and is bounded in length so a pathological input is refused
        here rather than absorbed by the query planner. An empty term is simply
        no search.
        """

        if value is None:
            return None
        term = str(value).strip()
        if not term:
            return None
        if len(term) > MAX_SEARCH_LENGTH:
            raise _InvalidParameter("search term is too long")
        return term

    @staticmethod
    def _require_assigned_id(assigned_id: Optional[str]) -> str:
        # Opaque TEXT: never cast, normalize, lowercase, or strip leading zeroes.
        if not isinstance(assigned_id, str) or assigned_id == "":
            raise RankingEntryNotFoundError("assigned_id is required")
        return assigned_id

    @staticmethod
    def _opt_int(value: Any, name: str, *, default: Optional[int] = None) -> Optional[int]:
        if value is None or (isinstance(value, str) and value.strip() == ""):
            return default
        if isinstance(value, bool):
            raise _InvalidParameter(f"{name} must be an integer")
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise _InvalidParameter(f"{name} must be an integer") from exc

    @staticmethod
    def _parse_trip_boundary(value: Optional[str], name: str) -> Optional[datetime]:
        if value is None or value == "":
            return None
        raw = str(value)
        try:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
                return business_local_midnight(date.fromisoformat(raw))
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise _InvalidParameter(f"{name} must be an ISO date or datetime") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=get_business_timezone())
        return parsed.astimezone(get_business_timezone())

    @staticmethod
    def _strict_nonnegative_int(value: Any, name: str) -> Optional[int]:
        if value is None or value == "":
            return None
        raw = str(value)
        if not re.fullmatch(r"\d+", raw):
            raise _InvalidParameter(f"{name} must be a non-negative integer")
        return int(raw)

    @staticmethod
    def _optional_bool(value: Any, name: str) -> Optional[bool]:
        if value is None or value == "":
            return None
        if isinstance(value, bool):
            return value
        raw = str(value).lower()
        if raw == "true":
            return True
        if raw == "false":
            return False
        raise _InvalidParameter(f"{name} must be true or false")

    def _trip_filters(self, key: RankingPeriodKey, **raw: Any) -> TripFilters:
        period_start = business_local_midnight(key.period_start_date)
        period_end = business_local_midnight(key.period_end_date)
        requested_from = self._parse_trip_boundary(raw.get("trip_start_from"), "trip_start_from")
        requested_to = self._parse_trip_boundary(raw.get("trip_start_to"), "trip_start_to")
        effective_from = max(period_start, requested_from) if requested_from else period_start
        effective_to = min(period_end, requested_to) if requested_to else period_end
        if effective_from >= effective_to:
            raise _InvalidParameter("effective trip start range must be non-empty")
        provider_trip_id = self._strict_nonnegative_int(raw.get("provider_trip_id"), "provider_trip_id")
        minimum = self._strict_nonnegative_int(raw.get("min_distance_meters"), "min_distance_meters")
        maximum = self._strict_nonnegative_int(raw.get("max_distance_meters"), "max_distance_meters")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise _InvalidParameter("min_distance_meters cannot exceed max_distance_meters")
        return TripFilters(
            trip_start_from=effective_from,
            trip_start_to=effective_to,
            requested_trip_start_from=raw.get("trip_start_from") or None,
            requested_trip_start_to=raw.get("trip_start_to") or None,
            provider_trip_id=provider_trip_id,
            min_distance_meters=minimum,
            max_distance_meters=maximum,
            has_scoring_events=self._optional_bool(raw.get("has_scoring_events"), "has_scoring_events"),
        )

    @staticmethod
    def _serialize_trip_filters(filters: TripFilters) -> dict:
        return {
            "trip_start_from": filters.requested_trip_start_from,
            "trip_start_to": filters.requested_trip_start_to,
            "provider_trip_id": filters.provider_trip_id,
            "min_distance_meters": filters.min_distance_meters,
            "max_distance_meters": filters.max_distance_meters,
            "has_scoring_events": filters.has_scoring_events,
            "effective_trip_start_from": filters.trip_start_from.isoformat(),
            "effective_trip_start_to_exclusive": filters.trip_start_to.isoformat(),
        }

    def _audit(self, user: dict, request: Any, event_type: str, client_code: Optional[str], metadata: dict) -> None:
        try:
            self._backend.record_audit(
                event_type=event_type,
                actor_user_id=self._user_id(user) or None,
                client_code=client_code,
                request=request,
                metadata=metadata,
            )
        except Exception:
            # Audit must never break a successful read response.
            pass

    def _result_for_error(self, exc: Exception) -> ApiResult:
        for exc_type, status, code in _ERROR_MAP:
            if isinstance(exc, exc_type):
                return ApiResult(status, envelope_error(code, _SAFE_MESSAGES.get(code, code)))
        return ApiResult(500, envelope_error("INTERNAL_ERROR", _SAFE_MESSAGES["INTERNAL_ERROR"]))
