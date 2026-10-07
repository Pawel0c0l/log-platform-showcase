"""Server-rendered Eco Driving pages (`ECO-001` / `ECO-002` / `ECO-003`).

These controllers build page *bodies* plus the context-bar values the shared
shell needs. They do not import FastAPI and issue no HTTP calls to the app's own
JSON endpoints: all data, RBAC, client/provider resolution, period parsing,
query construction, sorting/pagination validation, serialization and audit are
reused from the :class:`EcoDrivingApiService` envelopes. ``api/main.py`` resolves
the portal user, calls a controller method and wraps the result in the shared
shell chrome.

What S12 changed, and why it is not a regression:

* the per-row ``Lineage`` column is gone — the qualifier is constant for a
  period, so it is stated **once** in the context bar (`EC-29`);
* the reconciliation panel and the "how the score was calculated" panel are
  gone by owner decision (`D-003` / `EC-30`). The reconciliation *service* and
  the score-definition model are untouched and still serve the JSON API and the
  jobs that depend on them; only the two UI panels were removed;
* ranking-group tabs became filter chips over one ranking (`D-004` / `EC-12`).
  The three groups keep their distinct persisted meaning — an ``EXCLUDED``
  driver is still a real driver with a detail page and a normal
  ``bezpieczny`` / ``akceptowalny`` / ``niebezpieczny`` classification.

The period model now has two resolved modes, and `month` selects between them
(`docs/36`). A persisted period token (`period_key`) keeps serving the snapshot
views S12 built. A `month` — optionally narrowed to a set of week buckets —
selects the analytical basis: the whole month is served from the canonical
persisted monthly snapshot, and any proper subset of the month's weeks is
recomputed from the underlying trip assignments over the union of the selected
isolated intervals. Persisted cumulative month-to-date snapshots are still never
summed; two consecutive ones share a prefix. When `month` is present
`period_key` is ignored entirely, so one page resolves exactly one period
model.

Audit policy is unchanged: the pages emit no events of their own and rely on the
service-level event for each logical read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional

from . import basis_view as B
from . import detail_view_models as D
from . import eco_view as V
from . import html as H
from . import trip_export as X
from . import ranking_view_models as R
from . import trip_view_models as T


class _ExportRefused(Exception):
    """A non-200 from the service, carried out of the paging loop unchanged."""

    def __init__(self, result):
        super().__init__(getattr(result, "status_code", "refused"))
        self.result = result


class _ExportFile:
    """Bytes, the two headers a download needs, and WHICH SCOPE PRODUCED THEM.

    `scope` is the identity the controller actually resolved, in name order, and
    it exists so the filename cannot disagree with the contents.

    It used to be absent, and the filename was rebuilt downstream by reading the
    raw query string. That worked until a request carried both a `month` and a
    `period_key`: the contents correctly resolved to the month, while the name --
    which had no way to learn that -- listed both, leading with a period key from
    a month the file contained nothing from. Re-deriving an identity from data
    you can only guess at is the same defect `ExportScope` closed one layer up;
    this is the same repair applied to the layer below it.
    """

    __slots__ = ("body", "content_type", "extension", "scope")

    def __init__(self, body: bytes, content_type: str, extension: str,
                 scope: tuple = ()):
        self.body = body
        self.content_type = content_type
        self.extension = extension
        self.scope = scope
from .eco_scoring import MIN_QUALIFYING_DISTANCE_METERS
from .models import PeriodType, RankingPeriodKey
from .service import EcoDrivingApiService

if __package__ and __package__.startswith("api."):
    from ..portal_ui.i18n import t
    from ..portal_ui import formats as F
else:  # pragma: no cover - import-path parity with the rest of the package
    from portal_ui.i18n import t
    from portal_ui import formats as F

PORTAL_LABEL = "Eco Driving"
ACTIVE_KEY = "eco-driving"
DEFAULT_GROUP = "INCLUDED"
DEFAULT_LIMIT = 50
# Rows of trip evidence embedded in section 6 before the user is sent to the
# full, filterable trip surface.
DETAIL_TRIP_PREVIEW_LIMIT = 25

GROUP_ORDER = ("INCLUDED", "EXCLUDED", "UNKNOWN_DRIVER")
GROUP_LABEL_KEYS = {
    "INCLUDED": "eco.group.included",
    "EXCLUDED": "eco.group.excluded",
    "UNKNOWN_DRIVER": "eco.group.unknown",
}


def group_label(group: Optional[str]) -> str:
    key = GROUP_LABEL_KEYS.get(str(group or ""))
    return t(key) if key else str(group or "")


_ERROR_TITLES = {
    "FORBIDDEN": "Brak dostępu",
    "PROVIDER_NOT_FOUND": "Dostawca niedostępny",
    "PERIOD_NOT_FOUND": "Nie znaleziono okresu rankingowego",
    "RANKING_ENTRY_NOT_FOUND": "Nie znaleziono wpisu rankingowego",
    "UNSUPPORTED_PERIOD_TYPE": "Nieprawidłowe żądanie",
    "INVALID_RANKING_GROUP": "Nieprawidłowe żądanie",
    "INVALID_SORT_FIELD": "Nieprawidłowe żądanie",
    "INVALID_PAGINATION": "Nieprawidłowe żądanie",
    "MALFORMED_PERIOD_TOKEN": "Nieprawidłowy lub nieaktualny odnośnik",
    "INVALID_PARAMETER": "Nieprawidłowe żądanie",
    "RECONSTRUCTION_UNAVAILABLE": "Chwilowo niedostępne",
    "ENVIRONMENT_CLIENT_MISMATCH": "Chwilowo niedostępne",
    "UNAUTHENTICATED": "Wymagane logowanie",
    "INTERNAL_ERROR": "Coś poszło nie tak",
}

@dataclass(frozen=True)
class PageResult:
    status_code: int
    title: str
    body_html: str
    description: str = ""
    active_key: str = ACTIVE_KEY
    # Context-bar values for the shared shell. The client is never hidden.
    context_client_name: str = ""
    context_client_code: str = ""
    context_module_name: str = ""
    context_meta_html: str = ""
    header_actions: str = ""
    page_assets: tuple[str, ...] = field(default=H.ECO_DRIVING_PAGE_ASSETS)


def _period_columns() -> list:
    """The period table's columns, for the file.

    Same columns as the screen, minus the trailing `Ranking` link — a control
    is not a value. The two count columns the screen renders as a range
    (`start → end`) become two dated columns here, because a spreadsheet cannot
    filter on an arrow.
    """

    from .trip_view_models import Column

    def col(key, label):
        return Column(key, label, lambda row: "", lambda row, k=key: row.get(k))

    return [
        col("period_label", t("eco.period.label")),
        col("period_start_date", "Początek zakresu"),
        col("period_end_date_exclusive", t("eco.period.end_exclusive")),
        col("included", t("eco.group.included")),
        col("excluded", t("eco.group.excluded")),
        col("unknown_driver", t("eco.group.unknown")),
        col("not_ranked_count", "Poza rankingiem"),
    ]


def _period_export_row(period: dict) -> dict:
    counts = period.get("entry_counts_by_group") or {}
    return {
        "period_label": period.get("period_label"),
        "period_start_date": period.get("period_start_date"),
        "period_end_date_exclusive": period.get("period_end_date_exclusive"),
        "included": counts.get("INCLUDED", 0),
        "excluded": counts.get("EXCLUDED", 0),
        "unknown_driver": counts.get("UNKNOWN_DRIVER", 0),
        "not_ranked_count": period.get("not_ranked_count", 0),
    }


class EcoDrivingPages:
    def __init__(self, service: EcoDrivingApiService) -> None:
        self._service = service

    # -- landing --------------------------------------------------------------

    def landing(
        self,
        *,
        user: Optional[dict],
        request: Any = None,
        client_code: Optional[str] = None,
        ranking_family: Optional[str] = None,
        period_type: Optional[str] = None,
        year: Any = None,
        month: Any = None,
        unit: Any = None,
    ) -> PageResult:
        unit_value = V.normalize_unit(unit)
        prov = self._service.list_providers(user=user, request=request)
        if prov.status_code != 200:
            return self._error_page(prov)
        providers = prov.body.get("data") or []
        if not providers:
            body = H.empty_state(t("eco.state.no_access_title"), t("eco.state.no_access"))
            return PageResult(
                200,
                t("eco.state.no_access_title"),
                body,
                context_module_name=PORTAL_LABEL,
            )

        selected = self._pick_provider(providers, client_code, ranking_family)
        cc = selected["client_code"]
        fam = selected["ranking_family"]
        pt_raw = period_type or PeriodType.WEEKLY.value

        per = self._service.list_periods(
            user=user, request=request, client_code=cc, ranking_family=fam,
            period_type=pt_raw, year=year, month=month,
        )
        header = self._provider_chips(providers, selected, unit_value) + self._period_type_chips(
            cc, fam, pt_raw, unit_value
        )
        context = self._context_for(selected)
        if per.status_code != 200:
            return PageResult(
                per.status_code,
                t("eco.ranking.title"),
                header + self._error_body(per),
                **context,
            )

        periods = per.body.get("data") or []
        pt_norm = (per.body.get("meta") or {}).get("period_type") or pt_raw
        # Derived from the periods already read, so the landing page issues no
        # extra query and emits no second audit event just to list months.
        months = sorted(
            {
                str(period.get("month_start_date") or "")[:7]
                for period in periods
                if period.get("month_start_date")
            },
            reverse=True,
        )
        body = (
            header
            + self._basis_entry_section(cc, fam, months, unit_value)
            + self._period_section(cc, fam, pt_norm, periods, unit_value)
        )
        return PageResult(200, t("eco.ranking.title"), body, **context)

    def _basis_entry_section(self, cc: str, fam: str, months: list, unit: str) -> str:
        """Entry into the month + week basis, one link per month with data.

        The persisted-period table below stays exactly as it was: it lists the
        snapshots the aggregation job actually wrote. This section is the
        *analytical* basis, where a month can be narrowed to a set of weeks.
        """

        if not months:
            return H.empty_state(t("eco.basis.no_months_title"), t("eco.basis.no_months"))
        links = "".join(
            H.link(
                H.url(H.RANKINGS_PATH, {
                    "client_code": cc, "ranking_family": fam, "month": month,
                    "ranking_group": DEFAULT_GROUP, "unit": unit,
                }),
                B.month_label(str(month)),
                cls="eco-week-shortcut",
            )
            for month in sorted((str(value) for value in months), reverse=True)
        )
        return (
            '<section class="eco-basis-entry">'
            f'<span class="eco-eyebrow">{H.esc(t("eco.basis.label"))}</span>'
            f'<div class="eco-week-shortcuts">{links}</div>'
            f'<p class="eco-note">{H.esc(t("eco.basis.dynamic_source"))}</p>'
            "</section>"
        )

    # -- ranking (`ECO-001` / `ECO-002`) --------------------------------------

    def rankings(
        self,
        *,
        user: Optional[dict],
        request: Any = None,
        client_code: Optional[str] = None,
        ranking_family: Optional[str] = None,
        period_key: Optional[str] = None,
        ranking_group: Optional[str] = None,
        page: Any = None,
        limit: Any = None,
        sort: Optional[str] = None,
        direction: Optional[str] = None,
        unit: Any = None,
        search: Optional[str] = None,
        notice: Optional[str] = None,
        month: Optional[str] = None,
        weeks: Optional[str] = None,
    ) -> PageResult:
        group = ranking_group or DEFAULT_GROUP
        unit_value = V.normalize_unit(unit)
        if month not in (None, ""):
            # `month` selects the basis model. `period_key` is deliberately not
            # consulted here: one page must resolve exactly one period mode.
            return self._basis_rankings(
                user=user, request=request, client_code=client_code,
                ranking_family=ranking_family, month=month, weeks=weeks,
                ranking_group=group, page=page, limit=limit, sort=sort,
                direction=direction, unit=unit_value, search=search, notice=notice,
            )
        limit_val = limit if (limit is not None and str(limit) != "") else str(DEFAULT_LIMIT)

        res = self._service.list_ranking_entries(
            user=user, request=request, client_code=client_code, ranking_family=ranking_family,
            period_key=period_key, ranking_group=group, page=page, limit=limit_val,
            sort=sort, direction=direction, search=search,
        )
        if res.status_code != 200:
            back = H.link(
                H.url(H.LANDING_PATH, {"client_code": client_code, "ranking_family": ranking_family}),
                "Wróć do okresów",
                cls="portal-button secondary",
            )
            body = self._error_body(res) + f'<div class="portal-actions">{back}</div>'
            return PageResult(res.status_code, t("eco.ranking.title"), body,
                              context_module_name=PORTAL_LABEL)

        items = res.body.get("data") or []
        meta = res.body.get("meta") or {}
        key = self._decode_key(period_key)

        periods = self._periods_for(user, request, client_code, ranking_family, key)
        current_period = self._match_period(periods, period_key)
        distribution = self._distribution(
            user, request, client_code, ranking_family, period_key, group
        )

        base = {
            "client_code": client_code, "ranking_family": ranking_family, "period_key": period_key,
            "sort": sort, "direction": direction, "limit": limit_val,
            "page": meta.get("page") or 1, "unit": unit_value,
            "search": meta.get("search"),
        }

        notice_html = ""
        if notice:
            notice_html = (
                '<div class="portal-callout" role="status">' + H.esc(notice) + "</div>"
            )

        table = self._ranking_table(items, group, base, sort, direction, unit_value)
        # The page states the scope rather than letting the renderer guess it
        # back off a row: the same rule the trips export follows, and the same
        # defect it exists to prevent.
        export_scope = (
            T.ExportScope(
                {"client_code": client_code, "ranking_family": ranking_family,
                 "period_key": period_key, "ranking_group": group},
                ("client_code", "period_key", "ranking_group"),
            )
            if period_key
            else T.ExportScope.refused(
                "eksport nie zna zakresu tego widoku (brak okresu)")
        )
        body = (
            notice_html
            + self._period_bar(base, periods, current_period, distribution, unit_value)
            + self._toolbar(base, group, current_period, unit_value, meta.get("search"))
            + R.export_bar(export_scope, sort=sort, direction=direction,
                           unit=unit_value, search=meta.get("search"),
                           total=meta.get("total_count"))
            + table.html
            + self._ranking_pagination(meta, base, group)
            + self._ranking_footer(current_period)
        )
        return PageResult(
            200,
            t("eco.ranking.title"),
            body,
            page_assets=table.page_assets,
            **self._context_for(
                {"client_code": client_code, "ranking_family": ranking_family}
            ),
        )

    # -- driver detail (`ECO-003`) -------------------------------------------

    def ranking_entry(
        self,
        *,
        user: Optional[dict],
        request: Any = None,
        client_code: Optional[str] = None,
        ranking_family: Optional[str] = None,
        period_key: Optional[str] = None,
        assigned_id: Optional[str] = None,
        ranking_group: Optional[str] = None,
        page: Any = None,
        limit: Any = None,
        sort: Optional[str] = None,
        direction: Optional[str] = None,
        unit: Any = None,
        month: Optional[str] = None,
        weeks: Optional[str] = None,
    ) -> PageResult:
        unit_value = V.normalize_unit(unit)
        if month not in (None, ""):
            return self._basis_ranking_entry(
                user=user, request=request, client_code=client_code,
                ranking_family=ranking_family, month=month, weeks=weeks,
                assigned_id=assigned_id, ranking_group=ranking_group,
                page=page, limit=limit, sort=sort, direction=direction,
                unit=unit_value,
            )
        entry_res = self._service.get_ranking_entry(
            user=user, request=request, client_code=client_code,
            ranking_family=ranking_family, period_key=period_key, assigned_id=assigned_id,
        )
        if entry_res.status_code != 200:
            code = (entry_res.body.get("error") or {}).get("code")
            if code == "RANKING_ENTRY_NOT_FOUND":
                # `D-009`: a context switch that leaves the driver behind must
                # land on the ranking **for the new context** and say who was
                # dropped and why — not on a dead-end error page.
                return self._driver_absent_fallback(
                    user=user, request=request, client_code=client_code,
                    ranking_family=ranking_family, period_key=period_key,
                    assigned_id=assigned_id, ranking_group=ranking_group,
                    page=page, limit=limit, sort=sort, direction=direction,
                    unit=unit_value,
                )
            return self._error_page(entry_res)

        entry = entry_res.body.get("data") or {}
        context = D.safe_return_context(
            ranking_group=ranking_group or entry.get("ranking_group"),
            page=page, limit=limit, sort=sort, direction=direction,
        )
        back_href = H.url(H.RANKINGS_PATH, {
            "client_code": client_code, "ranking_family": ranking_family,
            "period_key": period_key, "unit": unit_value, **context,
        })
        evidence_allowed = bool((entry.get("capabilities") or {}).get("can_view_trip_details"))

        periods = self._periods_for(
            user, request, client_code, ranking_family, self._decode_key(period_key)
        )
        # The distribution is only rendered for a qualified period. Reading it
        # for a driver whose page cannot show it would issue a query, emit an
        # audit event naming a surface that was never displayed, and then throw
        # the rows away.
        distribution = (
            self._distribution(
                user, request, client_code, ranking_family, period_key,
                entry.get("ranking_group"),
            )
            if D.is_period_qualified(entry)
            else None
        )
        trend = self._trend(user, request, client_code, ranking_family, period_key, assigned_id)
        progression = self._progression(
            user, request, client_code, ranking_family, period_key, assigned_id
        )

        trips: list[dict] = []
        trips_meta: dict = {}
        trips_href = None
        if evidence_allowed:
            trips_href = H.url(H.RANKING_ENTRY_TRIPS_PATH, {
                "client_code": client_code, "ranking_family": ranking_family,
                "period_key": period_key, "assigned_id": assigned_id,
                "unit": unit_value,
                "ranking_group": context.get("ranking_group"),
                "ranking_page": context.get("page"), "ranking_limit": context.get("limit"),
                "ranking_sort": context.get("sort"), "ranking_direction": context.get("direction"),
            })
            trips_res = self._service.list_contributing_trips(
                user=user, request=request, client_code=client_code,
                ranking_family=ranking_family, period_key=period_key, assigned_id=assigned_id,
                page=1, limit=DETAIL_TRIP_PREVIEW_LIMIT,
            )
            if trips_res.status_code == 200:
                trips = trips_res.body.get("data") or []
                trips_meta = trips_res.body.get("meta") or {}

        body = D.render_detail(
            entry,
            back_href=back_href,
            evidence_allowed=evidence_allowed,
            trips=trips,
            trips_meta=trips_meta,
            trips_href=trips_href,
            distribution=distribution,
            trend=trend,
            progression=progression,
            unit=unit_value,
            unit_href_fn=lambda target: H.url(H.RANKING_ENTRY_PATH, {
                "client_code": client_code, "ranking_family": ranking_family,
                "period_key": period_key, "assigned_id": assigned_id,
                "unit": target, **context,
            }),
            export_href_fn=lambda table, fmt: H.url(H.RANKING_ENTRY_EXPORT_PATH, {
                "client_code": client_code, "ranking_family": ranking_family,
                "period_key": period_key, "assigned_id": assigned_id,
                "table": table, "format": fmt,
            }),
            trips_export_href_fn=lambda fmt: H.url(H.RANKING_ENTRY_TRIPS_EXPORT_PATH, {
                "client_code": client_code, "ranking_family": ranking_family,
                "period_key": period_key, "assigned_id": assigned_id, "format": fmt,
            }) if evidence_allowed else None,
            period_switch_hrefs=self._period_switch_hrefs(
                periods, period_key,
                lambda token: H.url(H.RANKING_ENTRY_PATH, {
                    "client_code": client_code, "ranking_family": ranking_family,
                    "period_key": token, "assigned_id": assigned_id,
                    "unit": unit_value, **context,
                }),
            ),
        )
        driver = (entry.get("current_chart") or {}).get("current_driver_name")
        return PageResult(
            200,
            str(driver or entry.get("assigned_id") or ""),
            body,
            **self._context_for(
                {"client_code": client_code, "ranking_family": ranking_family}
            ),
        )

    # -- contributing-trip evidence (full surface) ---------------------------

    def ranking_entry_trips(
        self, *, user: Optional[dict], request: Any = None,
        client_code: Optional[str] = None, ranking_family: Optional[str] = None,
        period_key: Optional[str] = None, assigned_id: Optional[str] = None,
        page: Any = None, limit: Any = None, sort: Optional[str] = None,
        direction: Optional[str] = None, trip_start_from: Optional[str] = None,
        trip_start_to: Optional[str] = None, provider_trip_id: Any = None,
        min_distance_meters: Any = None, max_distance_meters: Any = None,
        has_scoring_events: Any = None, ranking_group: Any = None,
        ranking_page: Any = None, ranking_limit: Any = None,
        ranking_sort: Any = None, ranking_direction: Any = None,
        unit: Any = None, month: Optional[str] = None, weeks: Optional[str] = None,
    ) -> PageResult:
        unit_value = V.normalize_unit(unit)
        if month not in (None, ""):
            return self._basis_ranking_entry_trips(
                user=user, request=request, client_code=client_code,
                ranking_family=ranking_family, month=month, weeks=weeks,
                assigned_id=assigned_id, page=page, limit=limit, sort=sort,
                direction=direction, trip_start_from=trip_start_from,
                trip_start_to=trip_start_to, provider_trip_id=provider_trip_id,
                min_distance_meters=min_distance_meters,
                max_distance_meters=max_distance_meters,
                has_scoring_events=has_scoring_events, ranking_group=ranking_group,
                ranking_page=ranking_page, ranking_limit=ranking_limit,
                ranking_sort=ranking_sort, ranking_direction=ranking_direction,
                unit=unit_value,
            )
        limit_value = limit if limit not in (None, "") else 50
        trips = self._service.list_contributing_trips(
            user=user, request=request, client_code=client_code,
            ranking_family=ranking_family, period_key=period_key, assigned_id=assigned_id,
            page=page, limit=limit_value, sort=sort, direction=direction,
            trip_start_from=trip_start_from, trip_start_to=trip_start_to,
            provider_trip_id=provider_trip_id, min_distance_meters=min_distance_meters,
            max_distance_meters=max_distance_meters, has_scoring_events=has_scoring_events,
        )
        if trips.status_code != 200:
            return self._error_page(trips)
        entry_res = self._service.get_ranking_entry(
            user=user, request=request, client_code=client_code,
            ranking_family=ranking_family, period_key=period_key, assigned_id=assigned_id,
        )
        if entry_res.status_code != 200:
            return self._error_page(entry_res)
        entry = entry_res.body.get("data") or {}
        context = D.safe_return_context(
            ranking_group=ranking_group or entry.get("ranking_group"),
            page=ranking_page, limit=ranking_limit,
            sort=ranking_sort, direction=ranking_direction,
        )
        ranking_context = {
            "ranking_group": context.get("ranking_group"),
            "ranking_page": context.get("page"), "ranking_limit": context.get("limit"),
            "ranking_sort": context.get("sort"), "ranking_direction": context.get("direction"),
            "unit": unit_value,
        }
        filter_values = {
            "trip_start_from": trip_start_from, "trip_start_to": trip_start_to,
            "provider_trip_id": provider_trip_id,
            "min_distance_meters": min_distance_meters,
            "max_distance_meters": max_distance_meters,
            "has_scoring_events": has_scoring_events,
        }
        rendered = T.render(
            entry, trips.body.get("data") or [], trips.body.get("meta") or {},
            ranking_context=ranking_context, filter_values=filter_values,
            # This page fetched by period key, so that is what reproduces it.
            export_scope=T.ExportScope.for_period_key({
                "client_code": client_code, "ranking_family": ranking_family,
                "period_key": entry.get("period_key") or period_key,
                "assigned_id": entry.get("assigned_id") or assigned_id,
            }),
        )
        return PageResult(
            200,
            t("eco.detail.trips"),
            rendered.html,
            page_assets=rendered.page_assets,
            **self._context_for(
                {"client_code": client_code, "ranking_family": ranking_family}
            ),
        )

    # -- export of the periods table -------------------------------------------

    def periods_export(
        self, *, user, request=None, export_format: str = "xlsx",
        client_code=None, ranking_family=None, period_type=None,
        year=None, month=None,
    ):
        """The landing page's period table as a spreadsheet.

        It is an index of the snapshots the aggregation job actually wrote, and
        it is the one Eco table that needs no scope beyond the provider and the
        period type it was rendered with: the whole list IS the view. The
        provider is resolved through the same `_pick_provider` the page uses, so
        a request naming a client the account cannot reach gets that account's
        provider — not a refusal that reveals the client exists.
        """

        prov = self._service.list_providers(user=user, request=request)
        if prov.status_code != 200:
            return self._error_page(prov)
        providers = prov.body.get("data") or []
        if not providers:
            return PageResult(403, t("eco.state.no_access_title"), H.empty_state(
                t("eco.state.no_access_title"), t("eco.state.no_access")))
        selected = self._pick_provider(providers, client_code, ranking_family)
        cc, fam = selected["client_code"], selected["ranking_family"]
        pt_raw = period_type or PeriodType.WEEKLY.value
        res = self._service.list_periods(
            user=user, request=request, client_code=cc, ranking_family=fam,
            period_type=pt_raw, year=year, month=month,
        )
        if res.status_code != 200:
            return self._error_page(res)
        periods = res.body.get("data") or []
        pt_norm = (res.body.get("meta") or {}).get("period_type") or pt_raw

        cols = _period_columns()
        rows = [_period_export_row(period) for period in periods]
        scope_parts = (("client_code", cc), ("period_type", pt_norm))
        if str(export_format).lower() == "csv":
            return _ExportFile(X.to_csv(cols, rows), "text/csv; charset=utf-8",
                               "csv", scope_parts)
        return _ExportFile(
            X.to_xlsx(cols, rows, sheet_title="Okresy"),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "xlsx", scope_parts)

    # -- export of the driver-detail tables ------------------------------------

    #: The detail tables an export can name. A `table` outside this set is
    #: refused rather than defaulted: guessing which table someone meant is how
    #: a person ends up with a file they did not ask for and does not check.
    DETAIL_EXPORT_TABLES = ("composition", "progression")

    def ranking_entry_export(
        self, *, user, request=None, export_format: str = "xlsx", table: str = "",
        client_code=None, ranking_family=None, period_key=None, assigned_id=None,
        month=None, weeks=None,
    ):
        """One driver-detail table as a spreadsheet.

        The detail page holds two tables and one route serves both, named by
        `table`, because they share a scope exactly: the same driver, in the same
        period, resolved the same way. Two routes would be two places to keep
        that resolution correct.

        AUTHORIZATION AND SCOPE come from the same service methods the page
        calls — `get_ranking_entry` for a persisted period and
        `get_basis_ranking_entry` for a month/week basis — so the export cannot
        reach a driver, a client or a period the page would refuse.

        A NON-QUALIFIED PERIOD HAS NO COMPOSITION TABLE. The panel shows an
        insufficient-distance state instead of one, and this refuses rather than
        producing a file of the metrics that state exists to withhold.
        """

        table_name = str(table or "").strip().lower()
        if table_name not in self.DETAIL_EXPORT_TABLES:
            return PageResult(400, t("eco.ranking.title"), H.empty_state(
                "Nieznana tabela",
                "Eksport dotyczy jednej z tabel strony kierowcy: "
                f"{', '.join(self.DETAIL_EXPORT_TABLES)}."))

        if month:
            scope = T.ExportScope.for_basis(
                {"client_code": client_code, "ranking_family": ranking_family,
                 "assigned_id": assigned_id},
                month=month, weeks=weeks,
            )
            if scope.unresolvable:
                return PageResult(422, t("eco.ranking.title"), H.empty_state(
                    _ERROR_TITLES["MALFORMED_PERIOD_TOKEN"], scope.unresolvable))
            res = self._service.get_basis_ranking_entry(
                user=user, request=request, client_code=client_code,
                ranking_family=ranking_family, month=month, weeks=weeks,
                assigned_id=assigned_id,
            )
        elif not period_key:
            return PageResult(422, t("eco.ranking.title"), H.empty_state(
                _ERROR_TITLES["MALFORMED_PERIOD_TOKEN"],
                "Eksport nie zna zakresu tego widoku: brak okresu (period_key) "
                "i brak miesiąca (month). Wróć na stronę kierowcy i użyj "
                "przycisku pobierania na niej."))
        else:
            scope = T.ExportScope(
                {"client_code": client_code, "ranking_family": ranking_family,
                 "period_key": period_key, "assigned_id": assigned_id},
                ("assigned_id", "period_key"),
            )
            res = self._service.get_ranking_entry(
                user=user, request=request, client_code=client_code,
                ranking_family=ranking_family, period_key=period_key,
                assigned_id=assigned_id,
            )
        if res.status_code != 200:
            return self._error_page(res)
        entry = res.body.get("data") or {}

        if table_name == "composition":
            if not D.is_period_qualified(entry):
                return PageResult(409, t("eco.ranking.title"), H.empty_state(
                    t("eco.detail.composition_plain"),
                    "Ten okres nie kwalifikuje się do punktacji, więc tabela "
                    "składowych nie istnieje — nie ma czego wyeksportować."))
            cols = D.composition_columns()
            rows = D.composition_rows(entry)
            sheet, part = "Skladowe", "skladowe"
        else:
            selection = entry.get("selection") or {}
            progression = self._detail_progression(
                user=user, request=request, client_code=client_code,
                ranking_family=ranking_family, period_key=period_key,
                assigned_id=assigned_id, selection=selection if month else None,
            )
            cols = D.progression_columns()
            rows = D.progression_rows(progression)
            sheet, part = "Postep", "postep"

        scope_parts = ((("table", part),)
                       + tuple((name, scope.params.get(name)) for name in scope.filename_parts))
        if str(export_format).lower() == "csv":
            return _ExportFile(X.to_csv(cols, rows), "text/csv; charset=utf-8",
                               "csv", scope_parts)
        return _ExportFile(
            X.to_xlsx(cols, rows, sheet_title=sheet),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "xlsx", scope_parts)

    def _detail_progression(self, *, user, request, client_code, ranking_family,
                            period_key, assigned_id, selection):
        """The progression rows the detail page shows, for either period mode.

        `selection` present means the ranking-basis page, which reads the
        PERSISTED history of the month it names and strips the current-period
        marker for a dynamic week selection — none of those persisted periods is
        the selection on screen. Factored out so the export inherits that rule
        rather than restating it.
        """

        if selection is None:
            return self._progression(
                user, request, client_code, ranking_family, period_key, assigned_id
            )
        progression = self._progression(
            user, request, client_code, ranking_family,
            self._month_progression_key(selection), assigned_id,
        )
        if selection.get("mode") == "WEEKS":
            progression = [{**row, "is_current": False} for row in progression]
        return progression

    # -- export of the ranking table ------------------------------------------

    def rankings_export(
        self, *, user, request=None, export_format: str = "xlsx",
        client_code=None, ranking_family=None, period_key=None,
        ranking_group=None, sort=None, direction=None, unit=None,
        search=None, month=None, weeks=None,
    ):
        """The ranking table as a spreadsheet, at the current view.

        AUTHORIZATION IS NOT RE-IMPLEMENTED HERE, for the same reason the trips
        export does not re-implement it: this calls the same service method the
        page called, so the same permission pair, client scoping and audit event
        apply. A second gate would be a second thing to keep in step.

        THE UNIT IS PART OF THE ANSWER. `/ 100 km` and `Σ suma` are two readings
        of the same persisted row, and a metric column means a different number
        under each. The export takes the unit the screen was showing and puts
        the caption in the column heading, because in a file there is no toggle
        to explain which reading landed there.

        TWO PAGES, TWO SERVICE METHODS, exactly as on screen: a `month` means the
        ranking-basis page and is answered by `get_basis_ranking`; otherwise the
        persisted period is answered by `list_ranking_entries`. `month` wins over
        `period_key` because the page's own rule is that one page resolves one
        period mode.
        """

        group = ranking_group or DEFAULT_GROUP
        unit_value = V.normalize_unit(unit)

        if month:
            scope = T.ExportScope.for_basis(
                {"client_code": client_code, "ranking_family": ranking_family,
                 "ranking_group": group},
                month=month, weeks=weeks,
            )

            def fetch_page(page: int, limit: int):
                res = self._service.get_basis_ranking(
                    user=user, request=request, client_code=client_code,
                    ranking_family=ranking_family, month=month, weeks=weeks,
                    ranking_group=group, page=page, limit=limit,
                    sort=sort, direction=direction, search=search,
                )
                if res.status_code != 200:
                    raise _ExportRefused(res)
                body = res.body or {}
                return body.get("data") or [], body.get("meta") or {}
        elif not period_key:
            # Nothing identifies which rows are meant, and the only alternative
            # to refusing is inventing a scope.
            return PageResult(422, t("eco.ranking.title"), H.empty_state(
                _ERROR_TITLES["MALFORMED_PERIOD_TOKEN"],
                "Eksport nie zna zakresu tego widoku: brak okresu (period_key) "
                "i brak miesiąca (month). Wróć do rankingu i użyj przycisku "
                "pobierania na nim."))
        else:
            scope = T.ExportScope(
                {"client_code": client_code, "ranking_family": ranking_family,
                 "period_key": period_key, "ranking_group": group},
                ("client_code", "period_key", "ranking_group"),
            )

            def fetch_page(page: int, limit: int):
                res = self._service.list_ranking_entries(
                    user=user, request=request, client_code=client_code,
                    ranking_family=ranking_family, period_key=period_key,
                    ranking_group=group, page=page, limit=limit,
                    sort=sort, direction=direction, search=search,
                )
                if res.status_code != 200:
                    raise _ExportRefused(res)
                body = res.body or {}
                return body.get("data") or [], body.get("meta") or {}

        if scope.unresolvable:
            return PageResult(422, t("eco.ranking.title"), H.empty_state(
                _ERROR_TITLES["MALFORMED_PERIOD_TOKEN"], scope.unresolvable))

        try:
            rows = X.collect_rows(fetch_page)
        except _ExportRefused as refused:
            return self._error_page(refused.result)
        except X.ExportTruncated as exc:
            return PageResult(413, t("eco.ranking.title"), H.empty_state(
                "Zbyt wiele wierszy",
                f"Widok zawiera {exc.total} wierszy, a limit eksportu to "
                f"{X.MAX_EXPORT_ROWS}. Zawęź filtry i spróbuj ponownie."))

        cols = R.columns(unit_value)
        scope_parts = tuple(
            (name, scope.params.get(name)) for name in scope.filename_parts
        )
        if str(export_format).lower() == "csv":
            return _ExportFile(X.to_csv(cols, rows), "text/csv; charset=utf-8",
                               "csv", scope_parts)
        return _ExportFile(
            X.to_xlsx(cols, rows, sheet_title="Ranking"),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "xlsx", scope_parts)

    # -- export of the contributing-trip table --------------------------------

    def ranking_entry_trips_export(
        self, *, user, request=None, export_format: str = "xlsx",
        month=None, weeks=None, **filters
    ):
        """The current filtered view as a spreadsheet.

        AUTHORIZATION IS NOT RE-IMPLEMENTED HERE. This calls the same service
        method the page calls, so the same permission pair, the same client
        scoping and the same audit event apply. An export that authorized itself
        would be a second gate to keep in step with the first.

        SCOPE: the active filters and sort, every matching row rather than the
        page's 50 — a person who filtered a table and clicked export means what
        they filtered, and pagination is a property of the screen. It pages
        through `MAX_PAGE_SIZE` rather than raising the cap.

        TWO PAGES, TWO SERVICE METHODS. A `month` means the caller is the
        ranking-basis page, which is answered by `list_basis_contributing_trips`
        — the same method that produced its table. Routing a basis request
        through the period-key method is what broke this surface: the basis page
        has no period key to send in two of its three states, so the link went
        out without one and the endpoint refused it.

        FAILS CLOSED ON SCOPE. With neither a month nor a period key there is
        nothing that identifies which rows are meant, and the only alternative
        to refusing is inventing a scope. A file that quietly contains a wider
        set than the screen showed is worse than an error, because someone is
        about to do arithmetic on it.
        """

        period_key = filters.get("period_key")
        if month:
            # `month` wins over `period_key`, mirroring the page's own rule
            # (`ranking_entry_trips`): when a month is present it IS the basis
            # and the period key is ignored entirely. A hand-built URL carrying
            # both therefore exports what that same URL would display.
            filters.pop("period_key", None)
            # The resolved identity, decided HERE, in the branch that decided
            # the contents. The filename is built from this and never from the
            # query, so the two cannot describe different scopes.
            scope = (("assigned_id", filters.get("assigned_id")),
                     ("month", month), ("weeks", weeks))

            def fetch_page(page: int, limit: int):
                res = self._service.list_basis_contributing_trips(
                    user=user, request=request, month=month, weeks=weeks,
                    page=page, limit=limit, **filters)
                if res.status_code != 200:
                    raise _ExportRefused(res)
                body = res.body or {}
                return body.get("data") or [], body.get("meta") or {}
        elif not period_key:
            return PageResult(422, t("eco.detail.trips"), H.empty_state(
                _ERROR_TITLES["MALFORMED_PERIOD_TOKEN"],
                "Eksport nie zna zakresu tego widoku: brak okresu (period_key) "
                "i brak miesiąca (month). Wróć na stronę przejazdów i użyj "
                "przycisku pobierania na niej."))
        else:
            scope = (("assigned_id", filters.get("assigned_id")),
                     ("period_key", period_key))

            def fetch_page(page: int, limit: int):
                res = self._service.list_contributing_trips(
                    user=user, request=request, page=page, limit=limit, **filters)
                if res.status_code != 200:
                    raise _ExportRefused(res)
                body = res.body or {}
                return body.get("data") or [], body.get("meta") or {}

        try:
            rows = X.collect_rows(fetch_page)
        except _ExportRefused as refused:
            return self._error_page(refused.result)
        except X.ExportTruncated as exc:
            return PageResult(413, t("eco.detail.trips"), H.empty_state(
                "Zbyt wiele wierszy",
                f"Widok zawiera {exc.total} wierszy, a limit eksportu to "
                f"{X.MAX_EXPORT_ROWS}. Zawęź filtry i spróbuj ponownie."))

        cols = T.columns()
        if str(export_format).lower() == "csv":
            return _ExportFile(X.to_csv(cols, rows), "text/csv; charset=utf-8",
                               "csv", scope)
        return _ExportFile(
            X.to_xlsx(cols, rows),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "xlsx", scope)

    # -- month + arbitrary-week basis (`EC-1`-`EC-7`) -------------------------
    #
    # One page, one resolved period mode. When ``month`` is present it is the
    # basis, and ``period_key`` is ignored entirely — two period controls that
    # could both claim the page would be exactly the ambiguous state the
    # interaction contract forbids.

    def _basis_months(self, user, request, client_code, ranking_family) -> list:
        res = self._service.list_basis_months(
            user=user, request=request, client_code=client_code,
            ranking_family=ranking_family,
        )
        return (res.body.get("data") or []) if res.status_code == 200 else []

    def _basis_base(self, *, client_code, ranking_family, selection, unit, sort,
                    direction, limit, page, search) -> dict:
        """Canonical link state. ``weeks`` is whatever the server canonicalized."""

        return {
            "client_code": client_code,
            "ranking_family": ranking_family,
            "month": selection.get("month"),
            "weeks": selection.get("canonical_weeks_param"),
            "sort": sort,
            "direction": direction,
            "limit": limit,
            "page": page,
            "unit": unit,
            "search": search,
        }

    def _basis_rankings(
        self, *, user, request, client_code, ranking_family, month, weeks,
        ranking_group, page, limit, sort, direction, unit, search, notice=None,
    ) -> PageResult:
        group = ranking_group or DEFAULT_GROUP
        limit_val = limit if (limit is not None and str(limit) != "") else str(DEFAULT_LIMIT)
        res = self._service.get_basis_ranking(
            user=user, request=request, client_code=client_code,
            ranking_family=ranking_family, month=month, weeks=weeks,
            ranking_group=group, page=page, limit=limit_val, sort=sort,
            direction=direction, search=search,
        )
        context = self._context_for({"client_code": client_code})
        if res.status_code != 200:
            back = H.link(
                H.url(H.LANDING_PATH, {"client_code": client_code, "ranking_family": ranking_family}),
                "Wróć do okresów",
                cls="portal-button secondary",
            )
            body = self._error_body(res) + f'<div class="portal-actions">{back}</div>'
            return PageResult(res.status_code, t("eco.ranking.title"), body, **context)

        items = res.body.get("data") or []
        meta = res.body.get("meta") or {}
        selection = meta.get("selection") or {}
        basis = meta.get("basis")
        distribution = meta.get("distribution")
        months = self._basis_months(user, request, client_code, ranking_family)

        base = self._basis_base(
            client_code=client_code, ranking_family=ranking_family, selection=selection,
            unit=unit, sort=sort, direction=direction, limit=limit_val,
            page=meta.get("page") or 1, search=meta.get("search"),
        )

        def href_for_weeks(weeks_param):
            return H.url(H.RANKINGS_PATH, {
                **base, "weeks": weeks_param, "ranking_group": group, "page": 1,
            })

        def href_for_month(target_month):
            # Week identity is month-relative, so changing month resets the
            # basis to the whole month rather than reusing week numbers that
            # mean something different there.
            return H.url(H.RANKINGS_PATH, {
                **base, "month": target_month, "weeks": None,
                "ranking_group": group, "page": 1,
            })

        notice_html = (
            '<div class="portal-callout" role="status">' + H.esc(notice) + "</div>"
            if notice
            else ""
        )
        bar = B.basis_bar(
            selection, basis, distribution, months,
            href_for_weeks=href_for_weeks, href_for_month=href_for_month,
        )
        if selection.get("mode") == "EMPTY":
            return PageResult(
                200, t("eco.ranking.title"), notice_html + bar + B.empty_state(), **context
            )

        # A dynamic basis that found no source rows at all is a truthful
        # "no data" state, not an empty ranking group.
        if basis is not None and int(basis.get("population_count") or 0) == 0:
            return PageResult(
                200, t("eco.ranking.title"), notice_html + bar + B.no_data_state(), **context
            )

        counts = (basis or {}).get("counts_by_group") or {}
        current = {
            "entry_counts_by_group": counts,
            "not_ranked_count": (basis or {}).get("not_ranked_count") or 0,
            "not_ranked_included_count": (
                (basis or {}).get("not_ranked_included_count") or 0
            ),
        }
        table = self._ranking_table(items, group, base, sort, direction, unit)
        # CANONICAL state, not the submitted one. `weeks=3,1` and `weeks=1,3`
        # are the same basis, and every link this page renders must carry the
        # form the server canonicalized — an export link echoing the submitted
        # order would be the one place the page still repeated a non-canonical
        # selection back at the user.
        export_scope = T.ExportScope.for_basis(
            {"client_code": client_code, "ranking_family": ranking_family,
             "ranking_group": group},
            month=base.get("month"), weeks=base.get("weeks"),
        )
        body = (
            notice_html
            + bar
            + self._toolbar(base, group, current, unit, meta.get("search"))
            + R.export_bar(export_scope, sort=sort, direction=direction,
                           unit=unit, search=meta.get("search"),
                           total=meta.get("total_count"))
            + table.html
            + self._ranking_pagination(meta, base, group)
        )
        return PageResult(200, t("eco.ranking.title"), body,
                          page_assets=table.page_assets, **context)

    def _basis_ranking_entry(
        self, *, user, request, client_code, ranking_family, month, weeks,
        assigned_id, ranking_group, page, limit, sort, direction, unit,
    ) -> PageResult:
        entry_res = self._service.get_basis_ranking_entry(
            user=user, request=request, client_code=client_code,
            ranking_family=ranking_family, month=month, weeks=weeks,
            assigned_id=assigned_id,
        )
        if entry_res.status_code != 200:
            code = (entry_res.body.get("error") or {}).get("code")
            if code == "RANKING_ENTRY_NOT_FOUND":
                return self._basis_driver_absent_fallback(
                    user=user, request=request, client_code=client_code,
                    ranking_family=ranking_family, month=month, weeks=weeks,
                    assigned_id=assigned_id, ranking_group=ranking_group,
                    page=page, limit=limit, sort=sort, direction=direction, unit=unit,
                )
            return self._error_page(entry_res)

        entry = entry_res.body.get("data") or {}
        selection = entry.get("selection") or {}
        distribution = entry.get("distribution")
        context_params = D.safe_return_context(
            ranking_group=ranking_group or entry.get("ranking_group"),
            page=page, limit=limit, sort=sort, direction=direction,
        )
        basis_params = {
            "client_code": client_code, "ranking_family": ranking_family,
            "month": selection.get("month"), "weeks": selection.get("canonical_weeks_param"),
        }
        back_href = H.url(H.RANKINGS_PATH, {
            **basis_params, "unit": unit, **context_params,
        })
        evidence_allowed = bool((entry.get("capabilities") or {}).get("can_view_trip_details"))

        # Trend and progression stay what they are: **persisted** history for
        # this driver and month. They are not recomputed for an ad-hoc basis and
        # no synthetic period is inserted into either of them. For a dynamic
        # selection the "current period" marker is stripped, because none of
        # those persisted periods is the selection on screen.
        dynamic = selection.get("mode") == "WEEKS"
        month_key = self._month_period_key(selection)
        trend = self._trend(
            user, request, client_code, ranking_family, month_key, assigned_id
        )
        progression = self._progression(
            user, request, client_code, ranking_family,
            self._month_progression_key(selection), assigned_id,
        )
        if dynamic:
            trend = [{**row, "is_current": False} for row in trend]
            progression = [{**row, "is_current": False} for row in progression]

        trips: list[dict] = []
        trips_meta: dict = {}
        trips_href = None
        if evidence_allowed:
            trips_href = H.url(H.RANKING_ENTRY_TRIPS_PATH, {
                **basis_params, "assigned_id": assigned_id, "unit": unit,
                "ranking_group": context_params.get("ranking_group"),
                "ranking_page": context_params.get("page"),
                "ranking_limit": context_params.get("limit"),
                "ranking_sort": context_params.get("sort"),
                "ranking_direction": context_params.get("direction"),
            })
            trips_res = self._service.list_basis_contributing_trips(
                user=user, request=request, client_code=client_code,
                ranking_family=ranking_family, month=month, weeks=weeks,
                assigned_id=assigned_id, page=1, limit=DETAIL_TRIP_PREVIEW_LIMIT,
            )
            if trips_res.status_code == 200:
                trips = trips_res.body.get("data") or []
                trips_meta = trips_res.body.get("meta") or {}

        months = self._basis_months(user, request, client_code, ranking_family)
        ordered_months = sorted(str(value) for value in months)
        month_token = str(selection.get("month") or "")

        def month_href(target_month):
            return H.url(H.RANKING_ENTRY_PATH, {
                "client_code": client_code, "ranking_family": ranking_family,
                "month": target_month, "weeks": None,
                "assigned_id": assigned_id, "unit": unit, **context_params,
            })

        previous_href = following_href = None
        if month_token in ordered_months:
            index = ordered_months.index(month_token)
            if index > 0:
                previous_href = month_href(ordered_months[index - 1])
            if index + 1 < len(ordered_months):
                following_href = month_href(ordered_months[index + 1])

        body = B.detail_context_line(selection) + D.render_detail(
            entry,
            back_href=back_href,
            evidence_allowed=evidence_allowed,
            trips=trips,
            trips_meta=trips_meta,
            trips_href=trips_href,
            distribution=distribution,
            trend=trend,
            progression=progression,
            unit=unit,
            unit_href_fn=lambda target: H.url(H.RANKING_ENTRY_PATH, {
                **basis_params, "assigned_id": assigned_id, "unit": target,
                **context_params,
            }),
            # `basis_params` already carries the CANONICAL month and weeks, so
            # the download link cannot name a basis the page did not compute.
            export_href_fn=lambda table, fmt: H.url(H.RANKING_ENTRY_EXPORT_PATH, {
                **basis_params, "assigned_id": assigned_id,
                "table": table, "format": fmt,
            }),
            trips_export_href_fn=lambda fmt: H.url(H.RANKING_ENTRY_TRIPS_EXPORT_PATH, {
                **basis_params, "assigned_id": assigned_id, "format": fmt,
            }) if evidence_allowed else None,
            period_switch_hrefs=(previous_href, following_href),
        )
        driver = (entry.get("current_chart") or {}).get("current_driver_name")
        return PageResult(
            200,
            str(driver or entry.get("assigned_id") or ""),
            body,
            **self._context_for({"client_code": client_code}),
        )

    def _basis_ranking_entry_trips(
        self, *, user, request, client_code, ranking_family, month, weeks,
        assigned_id, page, limit, sort, direction, trip_start_from, trip_start_to,
        provider_trip_id, min_distance_meters, max_distance_meters,
        has_scoring_events, ranking_group, ranking_page, ranking_limit,
        ranking_sort, ranking_direction, unit,
    ) -> PageResult:
        limit_value = limit if limit not in (None, "") else 50
        trips = self._service.list_basis_contributing_trips(
            user=user, request=request, client_code=client_code,
            ranking_family=ranking_family, month=month, weeks=weeks,
            assigned_id=assigned_id, page=page, limit=limit_value, sort=sort,
            direction=direction, trip_start_from=trip_start_from,
            trip_start_to=trip_start_to, provider_trip_id=provider_trip_id,
            min_distance_meters=min_distance_meters,
            max_distance_meters=max_distance_meters,
            has_scoring_events=has_scoring_events,
        )
        if trips.status_code != 200:
            return self._error_page(trips)
        entry_res = self._service.get_basis_ranking_entry(
            user=user, request=request, client_code=client_code,
            ranking_family=ranking_family, month=month, weeks=weeks,
            assigned_id=assigned_id,
        )
        if entry_res.status_code != 200:
            return self._error_page(entry_res)
        entry = entry_res.body.get("data") or {}
        selection = entry.get("selection") or {}
        context_params = D.safe_return_context(
            ranking_group=ranking_group or entry.get("ranking_group"),
            page=ranking_page, limit=ranking_limit,
            sort=ranking_sort, direction=ranking_direction,
        )
        ranking_context = {
            "client_code": client_code, "ranking_family": ranking_family,
            "month": selection.get("month"),
            "weeks": selection.get("canonical_weeks_param"),
            "ranking_group": context_params.get("ranking_group"),
            "ranking_page": context_params.get("page"),
            "ranking_limit": context_params.get("limit"),
            "ranking_sort": context_params.get("sort"),
            "ranking_direction": context_params.get("direction"),
            "unit": unit,
        }
        filter_values = {
            "trip_start_from": trip_start_from, "trip_start_to": trip_start_to,
            "provider_trip_id": provider_trip_id,
            "min_distance_meters": min_distance_meters,
            "max_distance_meters": max_distance_meters,
            "has_scoring_events": has_scoring_events,
        }
        rendered = T.render(
            entry, trips.body.get("data") or [], trips.body.get("meta") or {},
            ranking_context=ranking_context, filter_values=filter_values,
            # This page fetched by MONTH and WEEKS. It states those, and never
            # the period key -- the field is absent in two of the three basis
            # states and, where present, names the whole persisted month.
            export_scope=T.ExportScope.for_basis(
                {"client_code": client_code, "ranking_family": ranking_family,
                 "assigned_id": entry.get("assigned_id") or assigned_id},
                month=selection.get("month") or month,
                weeks=selection.get("canonical_weeks_param") or weeks,
            ),
        )
        return PageResult(
            200,
            t("eco.detail.trips"),
            B.detail_context_line(selection) + rendered.html,
            page_assets=rendered.page_assets,
            **self._context_for({"client_code": client_code}),
        )

    def _basis_driver_absent_fallback(
        self, *, user, request, client_code, ranking_family, month, weeks,
        assigned_id, ranking_group, page, limit, sort, direction, unit,
    ) -> PageResult:
        notice = t(
            "eco.state.driver_absent",
            driver=str(assigned_id or ""),
            period=f"{B.month_label(str(month or ''))} · {str(weeks or t('eco.basis.whole_month'))}",
        )
        result = self._basis_rankings(
            user=user, request=request, client_code=client_code,
            ranking_family=ranking_family, month=month, weeks=weeks,
            ranking_group=ranking_group or DEFAULT_GROUP, page=page, limit=limit,
            sort=sort, direction=direction, unit=unit, search=None, notice=notice,
        )
        if result.status_code != 200:
            return result
        return PageResult(
            404, result.title, result.body_html,
            description=result.description, active_key=result.active_key,
            context_client_name=result.context_client_name,
            context_client_code=result.context_client_code,
            context_module_name=result.context_module_name,
            context_meta_html=result.context_meta_html,
            header_actions=result.header_actions,
            page_assets=result.page_assets,
        )

    @staticmethod
    def _month_period_key(selection: dict) -> Optional[str]:
        """The persisted **monthly** period key for the selected month."""

        month_start = selection.get("month_start_date")
        month_end = selection.get("month_end_date_exclusive")
        if not month_start or not month_end:
            return None
        return RankingPeriodKey(
            period_type=PeriodType.MONTHLY,
            month_start_date=date.fromisoformat(str(month_start)),
            period_start_date=date.fromisoformat(str(month_start)),
            period_end_date=date.fromisoformat(str(month_end)),
        ).token

    @staticmethod
    def _month_progression_key(selection: dict) -> Optional[str]:
        """A weekly key at the month's final boundary, so every persisted
        cumulative snapshot of that month is listed.

        This is a read key for existing rows, not a claim that the selection is
        one of them: the progression panel says so in its own copy.
        """

        cards = selection.get("week_cards") or []
        month_start = selection.get("month_start_date")
        if not cards or not month_start:
            return None
        return RankingPeriodKey(
            period_type=PeriodType.WEEKLY,
            month_start_date=date.fromisoformat(str(month_start)),
            period_start_date=date.fromisoformat(str(month_start)),
            period_end_date=date.fromisoformat(str(cards[-1]["end_date_exclusive"])),
            period_sequence_in_month=int(cards[-1]["sequence"]),
        ).token

    # -- context bar ----------------------------------------------------------

    def _context_for(self, selected: dict) -> dict:
        """Context-bar values.

        The lineage qualifier lives here and only here: it is constant for the
        whole period, so repeating it on every ranking row was noise (`EC-29`).

        The **client identity** is resolved by one rule on every Eco surface:
        the client code, never a provider label. A provider display name such as
        ``ALPHA00001 — Eco Driving (kierowca)`` describes the module and the
        ranking family, not the client, and letting it into this field made the
        ranking and the detail page disagree about who the user was looking at.
        The provider/family description keeps its own home: the module name and
        family badge here, and the ``Dostawca / rodzina rankingu`` field in the
        detail identity grid.
        """

        client_name = str(selected.get("client_code") or "")
        meta = (
            f'<span class="lp-badge-technical">{H.esc(t("eco.ranking.family_badge"))}</span>'
            f"{H.lineage_badge()}"
        )
        return {
            "context_client_name": client_name,
            "context_client_code": str(selected.get("client_code") or ""),
            "context_module_name": PORTAL_LABEL,
            "context_meta_html": meta,
        }

    # -- landing rendering ----------------------------------------------------

    @staticmethod
    def _pick_provider(providers: list[dict], client_code: Optional[str], ranking_family: Optional[str]) -> dict:
        if client_code and ranking_family:
            for p in providers:
                if p["client_code"] == client_code and p["ranking_family"] == ranking_family:
                    return p
        if client_code:
            for p in providers:
                if p["client_code"] == client_code:
                    return p
        return providers[0]

    def _provider_chips(self, providers: list[dict], selected: dict, unit: str) -> str:
        if len(providers) < 2:
            # One authorized provider needs no selector; the context bar already
            # names the client, and an inert one-option control is worse than none.
            return ""
        items = []
        for p in providers:
            active = (
                p["client_code"] == selected["client_code"]
                and p["ranking_family"] == selected["ranking_family"]
            )
            href = H.url(H.LANDING_PATH, {
                "client_code": p["client_code"], "ranking_family": p["ranking_family"],
                "unit": unit,
            })
            items.append((p.get("display_name") or p["client_code"], href, active, None))
        return (
            f'<p class="eco-eyebrow">{H.esc(t("shell.context.client"))}</p>'
            + H.chips(items, aria_label=t("shell.context.client"))
        )

    def _period_type_chips(self, cc: str, fam: str, current: str, unit: str) -> str:
        items = []
        for pt, key in (
            (PeriodType.WEEKLY.value, "eco.period.type_weekly"),
            (PeriodType.MONTHLY.value, "eco.period.type_monthly"),
        ):
            href = H.url(H.LANDING_PATH, {
                "client_code": cc, "ranking_family": fam, "period_type": pt, "unit": unit,
            })
            items.append((t(key), href, pt == current, None))
        return (
            f'<p class="eco-eyebrow">{H.esc(t("eco.period.label"))}</p>'
            + H.chips(items, aria_label=t("eco.period.label"))
        )

    def _period_section(self, cc: str, fam: str, period_type: str, periods: list[dict], unit: str) -> str:
        note = (
            f'<p class="eco-note">{H.esc(t("eco.basis.cumulative"))}</p>'
            if period_type == "weekly"
            else ""
        )
        if not periods:
            return note + H.empty_state(
                t("eco.state.no_periods_title"),
                "Dla tego klienta nie policzono jeszcze żadnego rankingu Eco Driving w tym trybie.",
            )

        headers = (
            f'<th>{H.esc(t("eco.period.label"))}</th>'
            "<th>Zakres</th>"
            f'<th>{H.esc(t("eco.period.end_exclusive"))}</th>'
            f'<th>{H.esc(t("eco.group.included"))}</th>'
            f'<th>{H.esc(t("eco.group.excluded"))}</th>'
            f'<th>{H.esc(t("eco.group.unknown"))}</th>'
            "<th>Poza rankingiem</th><th></th>"
        )
        rows = []
        for p in periods:
            counts = p.get("entry_counts_by_group") or {}
            partial = (
                f' <span class="eco-muted">· {H.esc(t("eco.period.partial"))}</span>'
                if p.get("is_partial_period")
                else ""
            )
            open_href = H.url(H.RANKINGS_PATH, {
                "client_code": cc, "ranking_family": fam, "period_key": p.get("period_key"),
                "ranking_group": DEFAULT_GROUP, "unit": unit,
            })
            rows.append(
                "<tr>"
                f'<td>{H.esc(p.get("period_label"))}{partial}</td>'
                f'<td class="eco-num">{H.esc(F.format_date(p.get("period_start_date")))} → '
                f'{H.esc(F.format_date(p.get("period_end_date_exclusive")))}</td>'
                f'<td class="eco-num">{H.esc(F.format_date(p.get("period_end_date_exclusive")))}</td>'
                f'<td class="eco-num">{H.esc(counts.get("INCLUDED", 0))}</td>'
                f'<td class="eco-num">{H.esc(counts.get("EXCLUDED", 0))}</td>'
                f'<td class="eco-num">{H.esc(counts.get("UNKNOWN_DRIVER", 0))}</td>'
                f'<td class="eco-num">{H.esc(p.get("not_ranked_count", 0))}</td>'
                f'<td>{H.link(open_href, t("eco.ranking.title"), cls="portal-button")}</td>'
                "</tr>"
            )
        export_bar = (
            '<div class="eco-export-bar">'
            f'<span class="eco-export-caption">'
            f'{H.esc(f"Pobierz wszystkie wiersze tej tabeli ({len(periods)})")}</span>'
            + H.link(H.url(H.PERIODS_EXPORT_PATH, {
                "client_code": cc, "ranking_family": fam,
                "period_type": period_type, "format": "xlsx"}),
                "Pobierz XLSX", cls="portal-button secondary")
            + H.link(H.url(H.PERIODS_EXPORT_PATH, {
                "client_code": cc, "ranking_family": fam,
                "period_type": period_type, "format": "csv"}),
                "Pobierz CSV", cls="portal-button secondary")
            + '</div>'
        )
        return note + export_bar + H.table(headers, "".join(rows))

    # -- ranking rendering ----------------------------------------------------

    def _period_bar(
        self,
        base: dict,
        periods: list[dict],
        current: Optional[dict],
        distribution: Optional[dict],
        unit: str,
    ) -> str:
        """Period identity, the ranking-basis line and the fleet histogram.

        The basis line is the single source of truth for what is on screen. It
        states the true covered range and the exclusive end, and — for a weekly
        period — that the range is cumulative from the first of the month, so a
        reader can never mistake it for an isolated week.
        """

        key = self._decode_key(base.get("period_key"))
        prev_href, next_href = self._period_switch_hrefs(
            periods, base.get("period_key"),
            lambda token: H.url(H.RANKINGS_PATH, {**base, "period_key": token, "page": 1}),
        )
        # The unavailable step is absent, not disabled: a dead control is worse
        # than no control (PRODUCT_BEHAVIOR_CONTRACT, period edge).
        prev_html = (
            f'<a class="eco-stepper-btn" href="{H.esc(prev_href)}" '
            f'aria-label="{H.esc(t("eco.period.previous"))}">‹</a>'
            if prev_href
            else ""
        )
        next_html = (
            f'<a class="eco-stepper-btn" href="{H.esc(next_href)}" '
            f'aria-label="{H.esc(t("eco.period.next"))}">›</a>'
            if next_href
            else ""
        )
        label = (current or {}).get("period_label") or self._key_label(key)
        stepper = (
            '<div class="eco-stepper">'
            f"{prev_html}"
            f'<span class="eco-stepper-label">{H.esc(label)}</span>'
            f"{next_html}</div>"
        )

        type_key = (
            "eco.period.type_weekly"
            if key and key.period_type is PeriodType.WEEKLY
            else "eco.period.type_monthly"
        )
        basis = self._basis_line(key, current)
        return (
            '<div class="eco-period-bar">'
            '<div class="eco-period-block">'
            f'<span class="eco-eyebrow">{H.esc(t("eco.period.label"))}</span>'
            f"{stepper}"
            f'<span class="eco-muted">{H.esc(t(type_key))}</span>'
            "</div>"
            '<div class="eco-period-block eco-period-block--grow">'
            f'<span class="eco-eyebrow">{H.esc(t("eco.basis.label"))}</span>'
            f"{basis}"
            "</div>"
            + V.histogram(distribution or {})
            + "</div>"
        )

    def _basis_line(self, key: Optional[RankingPeriodKey], current: Optional[dict]) -> str:
        if key is None:
            return '<span class="eco-basis eco-muted">—</span>'
        days = (key.period_end_date - key.period_start_date).days
        parts = [
            f"<strong>{H.esc((current or {}).get('period_label') or self._key_label(key))}</strong>",
            f"{H.esc(F.format_date(key.period_start_date))} → "
            f"{H.esc(F.format_date(key.period_end_date))}",
            f"{H.esc(days)} dni",
        ]
        if (current or {}).get("is_partial_period"):
            parts.append(H.esc(t("eco.period.partial")))
        line = " · ".join(parts)

        secondary = ""
        counts = (current or {}).get("entry_counts_by_group") or {}
        if counts:
            qualified = sum(int(value or 0) for value in counts.values())
            not_ranked = int((current or {}).get("not_ranked_count") or 0)
            total = qualified + not_ranked
            secondary = (
                '<div class="eco-basis eco-basis-secondary">'
                f'{H.esc(t("eco.basis.qualified", qualified=qualified, total=total))}'
                "</div>"
            )
        cumulative = ""
        if key.period_type is PeriodType.WEEKLY:
            cumulative = (
                '<div class="eco-basis eco-basis-secondary">'
                f'{H.esc(t("eco.basis.cumulative"))}</div>'
            )
        return f'<div class="eco-basis">{line}</div>{secondary}{cumulative}'

    def _toolbar(
        self, base: dict, group: str, current: Optional[dict], unit: str,
        search: Optional[str] = None,
    ) -> str:
        counts = (current or {}).get("entry_counts_by_group") or {}
        # Permitted, but under the qualifying distance. These rows carry no
        # `ranking_group`, so they are absent from `entry_counts_by_group` and
        # are added back here — the chip has to count the rows the tab actually
        # lists, or the number becomes a promise the table does not keep.
        included_unranked = int((current or {}).get("not_ranked_included_count") or 0)
        items = []
        for name in GROUP_ORDER:
            params = dict(base)
            params["ranking_group"] = name
            params["page"] = 1  # changing the filter resets the page
            count = counts.get(name) if counts else None
            if count is not None and name == DEFAULT_GROUP:
                count = int(count) + included_unranked
            items.append((
                group_label(name),
                H.url(H.RANKINGS_PATH, params),
                name == group,
                count,
            ))
        chips = (
            f'<span class="eco-unit-label">{H.esc(t("eco.group.label"))}</span>'
            + H.chips(items, aria_label=t("eco.group.aria"))
        )
        active_filter = ""
        if group != DEFAULT_GROUP:
            clear_params = dict(base)
            clear_params["ranking_group"] = DEFAULT_GROUP
            clear_params["page"] = 1
            active_filter = (
                '<div class="eco-chips">'
                f'<span class="eco-chip eco-filter-chip">'
                f'<span>{H.esc(t("eco.group.label"))} = {H.esc(group_label(group))}</span>'
                f'<a class="eco-chip-remove" href="{H.esc(H.url(H.RANKINGS_PATH, clear_params))}" '
                f'aria-label="{H.esc(t("eco.group.clear"))}">×</a></span></div>'
            )
        not_ranked = int((current or {}).get("not_ranked_count") or 0)
        not_ranked_html = (
            f'<span class="eco-muted">{H.esc(t("eco.group.not_ranked", count=not_ranked))}</span>'
            if current
            else ""
        )
        # Said only on the tab it describes, and only when it has rows to
        # describe: elsewhere it would explain a population that is not on
        # screen.
        if current and group == DEFAULT_GROUP and included_unranked:
            not_ranked_html += (
                '<span class="eco-muted">'
                + H.esc(t("eco.group.included_unranked", count=included_unranked))
                + "</span>"
            )
        toggle = V.unit_toggle(
            unit,
            href_for_unit=lambda target: H.url(
                H.RANKINGS_PATH, {**base, "ranking_group": group, "unit": target}
            ),
        )
        return (
            '<div class="eco-toolbar">'
            + self._search_form(base, group, search)
            + f'<div class="eco-chips">{chips}</div>'
            + f"{active_filter}{self._search_chip(base, group, search)}{not_ranked_html}"
            + f'<div class="eco-toolbar-end">{toggle}</div>'
            + "</div>"
            + f'<p class="eco-note">{H.esc(t("eco.unit.note"))}</p>'
        )

    def _search_form(self, base: dict, group: str, search: Optional[str]) -> str:
        """Free-text search over driver name and tag ID.

        A plain GET form: it works with scripting disabled, and the resulting
        URL is the whole view state, so a filtered ranking is shareable and
        survives Back.
        """

        hidden = "".join(
            f'<input type="hidden" name="{H.esc(name)}" value="{H.esc(value)}">'
            for name, value in {**base, "ranking_group": group, "page": 1}.items()
            if name != "search" and value not in (None, "")
        )
        return (
            f'<form class="eco-search" method="get" action="{H.esc(H.RANKINGS_PATH)}" role="search">'
            + hidden
            + f'<label class="lp-visually-hidden" for="eco-search-input">'
            f'{H.esc(t("eco.search.label"))}</label>'
            f'<input id="eco-search-input" class="portal-input" type="search" name="search" '
            f'value="{H.esc(search or "")}" placeholder="{H.esc(t("eco.search.label"))}">'
            f'<button class="portal-button secondary" type="submit">{H.esc(t("eco.search.submit"))}</button>'
            "</form>"
        )

    def _search_chip(self, base: dict, group: str, search: Optional[str]) -> str:
        if not search:
            return ""
        clear = dict(base)
        clear["ranking_group"] = group
        clear["page"] = 1
        clear["search"] = None
        return (
            '<div class="eco-chips"><span class="eco-chip eco-filter-chip">'
            f'<span>{H.esc(t("eco.search.chip", term=search))}</span>'
            f'<a class="eco-chip-remove" href="{H.esc(H.url(H.RANKINGS_PATH, clear))}" '
            f'aria-label="{H.esc(t("eco.search.clear"))}">×</a></span></div>'
        )

    def _ranking_table(
        self, items: list[dict], group: str, base: dict,
        sort: Optional[str], direction: Optional[str], unit: str,
    ) -> H.RenderedGrid:
        """The ranking, as a selectable grid plus the assets that make it one.

        Returns the pair for the same reason the trips table does: this method
        has two success returns, and an asset argument omitted from one of them
        is exactly how the ranking-basis trips page shipped a dead selection
        host (`UI-20260827-01`). Here the empty-table return genuinely has no
        host, so it genuinely declares no module -- the pairing carries that
        distinction instead of leaving it to be remembered.

        Header cells, data cells and clipboard values all come from
        `ranking_view_models.columns(unit)` -- the same list the export reads,
        so a column cannot exist on screen and be missing from the file.
        """

        def sort_link(field: str, next_dir: str) -> str:
            params = dict(base)
            params["ranking_group"] = group
            params["sort"] = field
            params["direction"] = next_dir
            params["page"] = 1  # changing sort resets the page
            return H.url(H.RANKINGS_PATH, params)

        cols = R.columns(unit)
        # The action column is deliberately outside the enumeration: it holds a
        # link, not a value, so it is neither selectable nor exported.
        headers = R.header_cells(cols, sort=sort, direction=direction, sort_link=sort_link) + "<th></th>"

        if not items:
            # No rows, so no selection host and therefore no module: a page does
            # not download a grid layer it cannot use.
            return H.RenderedGrid(
                H.table(headers, "") + H.empty_state(
                    t("eco.state.no_entries"), t("eco.state.no_entries")
                ),
                H.ECO_DRIVING_PAGE_ASSETS,
            )

        rows = []
        for entry in items:
            detail_params = dict(base)
            detail_params["ranking_group"] = group
            detail_params["assigned_id"] = entry.get("assigned_id")
            detail_href = H.url(H.RANKING_ENTRY_PATH, detail_params)
            rows.append(
                "<tr>"
                + R.row_cells(cols, entry)
                + f'<td>{H.link(detail_href, t("eco.col.details"))}</td>'
                "</tr>"
            )
        # The selection host must be an ANCESTOR of the table: the module writes
        # `data-db-selection` onto it as the responsive cutoff moves, and the
        # shared presentation rules key off that ancestor.
        return H.RenderedGrid(
            f'<div class="eco-select-sheet"{H.selection_sheet_attrs(H.selection_strings())}>'
            + H.table(headers, "".join(rows))
            + H.selection_footer()
            + '</div>',
            H.ECO_DRIVING_SELECTABLE_PAGE_ASSETS,
        )

    def _ranking_pagination(self, meta: dict, base: dict, group: str) -> str:
        def page_link(target: int) -> str:
            params = dict(base)
            params["ranking_group"] = group
            params["page"] = target
            return H.url(H.RANKINGS_PATH, params)

        def size_link(size: int) -> str:
            params = dict(base)
            params["ranking_group"] = group
            params["limit"] = size
            params["page"] = 1
            return H.url(H.RANKINGS_PATH, params)

        return H.pagination(meta, page_link_fn=page_link, size_link_fn=size_link)

    def _ranking_footer(self, current: Optional[dict]) -> str:
        calculated = (current or {}).get("source_calculated_at")
        recalculated = (
            H.esc(t("eco.recalculated", timestamp=F.format_datetime(calculated)))
            if calculated else ""
        )
        return (
            '<div class="eco-footer-meta">'
            '<span>Nazwy kierowców pochodzą z bieżącej karty kierowcy i nie są '
            "niezmiennym zapisem historycznym.</span>"
            f"<span>{recalculated}</span></div>"
        )

    # -- service helpers ------------------------------------------------------

    def _periods_for(
        self, user, request, client_code, ranking_family, key: Optional[RankingPeriodKey]
    ) -> list[dict]:
        if key is None:
            return []
        res = self._service.list_periods(
            user=user, request=request, client_code=client_code,
            ranking_family=ranking_family, period_type=key.period_type.value,
        )
        if res.status_code != 200:
            return []
        return res.body.get("data") or []

    def _distribution(self, user, request, client_code, ranking_family, period_key, group):
        if not period_key:
            return None
        res = self._service.get_score_distribution(
            user=user, request=request, client_code=client_code,
            ranking_family=ranking_family, period_key=period_key, ranking_group=group,
        )
        return res.body.get("data") if res.status_code == 200 else None

    def _trend(self, user, request, client_code, ranking_family, period_key, assigned_id) -> list[dict]:
        res = self._service.get_driver_trend(
            user=user, request=request, client_code=client_code,
            ranking_family=ranking_family, period_key=period_key, assigned_id=assigned_id,
        )
        return (res.body.get("data") or []) if res.status_code == 200 else []

    def _progression(self, user, request, client_code, ranking_family, period_key, assigned_id) -> list[dict]:
        res = self._service.get_period_progression(
            user=user, request=request, client_code=client_code,
            ranking_family=ranking_family, period_key=period_key, assigned_id=assigned_id,
        )
        return (res.body.get("data") or []) if res.status_code == 200 else []

    def _driver_absent_fallback(
        self, *, user, request, client_code, ranking_family, period_key,
        assigned_id, ranking_group, page, limit, sort, direction, unit,
    ) -> PageResult:
        key = self._decode_key(period_key)
        period_text = key.token if key is None else (
            f"{F.format_date(key.period_start_date)} → {F.format_date(key.period_end_date)}"
        )
        notice = t(
            "eco.state.driver_absent",
            driver=str(assigned_id or ""),
            period=period_text,
        )
        result = self.rankings(
            user=user, request=request, client_code=client_code,
            ranking_family=ranking_family, period_key=period_key,
            ranking_group=ranking_group or DEFAULT_GROUP,
            page=page, limit=limit, sort=sort, direction=direction,
            unit=unit, notice=notice,
        )
        if result.status_code != 200:
            return result
        # The ranking rendered for the *new* context; the entry simply is not in
        # it. That is a 404 for the requested driver, not a broken ranking.
        return PageResult(
            404, result.title, result.body_html,
            description=result.description, active_key=result.active_key,
            context_client_name=result.context_client_name,
            context_client_code=result.context_client_code,
            context_module_name=result.context_module_name,
            context_meta_html=result.context_meta_html,
            header_actions=result.header_actions,
            page_assets=result.page_assets,
        )

    # -- helpers --------------------------------------------------------------

    @staticmethod
    def _decode_key(period_key: Optional[str]) -> Optional[RankingPeriodKey]:
        if not period_key:
            return None
        try:
            return RankingPeriodKey.from_token(str(period_key))
        except Exception:
            return None

    @staticmethod
    def _key_label(key: Optional[RankingPeriodKey]) -> str:
        """A readable period label when the persisted one is not in scope.

        Falls back to the true covered range rather than to the opaque token, so
        the period bar never shows an internal identifier to a user.
        """

        if key is None:
            return ""
        return (f"{F.format_date(key.period_start_date)} → "
                f"{F.format_date(key.period_end_date)}")

    @staticmethod
    def _match_period(periods: list[dict], period_key: Optional[str]) -> Optional[dict]:
        for period in periods:
            if period.get("period_key") == period_key:
                return period
        return None

    @staticmethod
    def _period_switch_hrefs(
        periods: list[dict], period_key: Optional[str], href_fn
    ) -> tuple[Optional[str], Optional[str]]:
        """``(previous, next)`` hrefs, or ``None`` where no such period exists."""

        tokens = [period.get("period_key") for period in periods]
        if period_key not in tokens:
            return (None, None)
        index = tokens.index(period_key)
        previous = href_fn(tokens[index - 1]) if index > 0 else None
        following = href_fn(tokens[index + 1]) if index + 1 < len(tokens) else None
        return (previous, following)

    @staticmethod
    def _error_body(res) -> str:
        err = res.body.get("error") or {}
        code = err.get("code") or "INTERNAL_ERROR"
        title = _ERROR_TITLES.get(code, _ERROR_TITLES["INTERNAL_ERROR"])
        message = err.get("message") or "Żądanie nie mogło zostać zrealizowane."
        return H.error_callout(title, message)

    def _error_page(self, res) -> PageResult:
        err = res.body.get("error") or {}
        code = err.get("code") or "INTERNAL_ERROR"
        title = _ERROR_TITLES.get(code, _ERROR_TITLES["INTERNAL_ERROR"])
        body = self._error_body(res) + (
            f'<div class="portal-actions">'
            f'{H.link(H.LANDING_PATH, PORTAL_LABEL, cls="portal-button secondary")}</div>'
        )
        return PageResult(res.status_code, title, body, context_module_name=PORTAL_LABEL)


# Re-exported so the detail renderer and its tests share one threshold source.
QUALIFYING_DISTANCE_METERS = MIN_QUALIFYING_DISTANCE_METERS
