"""Server-rendered Report Explorer pages: the library (`REP-001`/`REP-002`),
the report detail page (`REP-003`) and the approved states (`REP-004`).

Framework-independent on purpose — no FastAPI import — so a page can be rendered
in a test without a running application, exactly like the Eco Driving pages
controller. The integration layer in `api/main.py` supplies the portal chrome
and the byte-serving primitives; nothing here reads the database directly or
decides access. Every method authorizes through `ReportExplorerService`, which
authorizes the CLIENT before a provider is consulted at all.

Two shape rules the approved contract makes non-negotiable:

* **This is not a data grid** (`RP-1`). There is no column-visibility control,
  no density control and no per-column sort menu. Instances are status-bearing
  card-rows under stated group headings.
* **The list state is the URL** (`SH-13`). Filters, page, page size and the
  captured scroll offset travel in the query string, so returning from the
  detail page restores the list the user left without any server-side cursor.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from . import html as H
from .errors import (
    ReportAccessDeniedError,
    ReportClientNotFoundError,
    ReportExplorerError,
    ReportFileNotFoundError,
    ReportFilePreviewUnsupportedError,
    ReportFileUnavailableError,
    ReportInstanceNotFoundError,
    ReportSchemaUnavailableError,
)
from .models import (
    STATUS_EXPIRED,
    STATUS_FAILED,
    STATUS_GENERATING,
    STATUS_READY,
    ClientContext,
    LibraryPage,
    LibraryQuery,
    ReportInstanceView,
)
from .periods import format_period_range
from .service import (
    ReportDetail,
    ReportExplorerService,
    normalize_limit,
    normalize_page,
    normalize_status,
    normalize_year,
)
from .store import PAGE_SIZES

ACTIVE_KEY = "reports"
PORTAL_LABEL = "User Portal"

# The library-state parameters a detail link carries back (`SH-13`). Kept in one
# tuple so a new filter cannot be added to the toolbar and silently forgotten by
# the return link.
RETURN_PARAMS = ("client_code", "type", "year", "status", "q", "page", "limit", "scroll")


@dataclass(frozen=True)
class PageResult:
    status_code: int
    title: str
    body_html: str
    description: str = ""
    active_key: str = ACTIVE_KEY
    context_client_name: str = ""
    context_client_code: str = ""
    context_module_name: str = ""
    context_meta_html: str = ""
    header_actions: str = ""
    page_assets: tuple[str, ...] = field(default=H.REPORT_EXPLORER_PAGE_ASSETS)


def _param(params, name: str) -> str:
    value = (params or {}).get(name)
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return str(value or "").strip()


def build_query(params) -> LibraryQuery:
    """Canonicalize the URL into the library state.

    Every value is normalized to a value the page can render: an unknown status,
    an out-of-range year, a page size that is not one of the approved five and a
    negative page all fall back to their defaults rather than reaching a query.
    """
    scroll = _param(params, "scroll")
    try:
        scroll_value = max(0, min(int(scroll), 10_000_000)) if scroll else 0
    except ValueError:
        scroll_value = 0
    return LibraryQuery(
        client_code=_param(params, "client_code"),
        type_key=_param(params, "type"),
        year=normalize_year(_param(params, "year")),
        status=normalize_status(_param(params, "status")),
        search=_param(params, "q")[:120],
        page=normalize_page(_param(params, "page")),
        limit=normalize_limit(_param(params, "limit")),
        scroll=scroll_value,
    )


def query_params(query: LibraryQuery, **overrides) -> dict:
    """The library state as URL parameters, with optional overrides."""
    values = {
        "client_code": query.client_code,
        "type": query.type_key,
        "year": query.year,
        "status": query.status,
        "q": query.search,
        "page": query.page if query.page > 1 else "",
        "limit": query.limit if query.limit != PAGE_SIZES[2] else "",
        "scroll": query.scroll or "",
    }
    values.update(overrides)
    return values


class ReportExplorerPages:
    def __init__(self, service: ReportExplorerService) -> None:
        self._service = service

    # -- library ------------------------------------------------------------

    def library(self, *, user: dict, params=None) -> PageResult:
        query = build_query(params)
        try:
            context = self._service.client_context(user, query.client_code)
        except ReportAccessDeniedError as exc:
            return self._no_report_access(exc)
        except ReportClientNotFoundError:
            return self._client_unavailable()
        query.client_code = context.client_code
        try:
            page = self._service.library(user, context, query)
        except ReportSchemaUnavailableError:
            return self._schema_unavailable(context)
        except ReportExplorerError:
            return self._library_failure(context)
        except Exception:  # noqa: BLE001 - never surface a driver message
            return self._library_failure(context)
        return self._render_library(context, query, page)

    def _render_library(
        self, context: ClientContext, query: LibraryQuery, page: LibraryPage
    ) -> PageResult:
        body = (
            '<div class="rep-layout">'
            + self._rail_html(query, page)
            + '<div class="rep-main">'
            + self._toolbar_html(query, page)
            + self._chips_html(query, page)
            + self._groups_html(query, page)
            + self._footer_html(query, page)
            + "</div></div>"
        )
        return PageResult(
            status_code=200,
            title=H.COPY["module"],
            body_html=body,
            active_key=ACTIVE_KEY,
            context_client_name=context.display_name,
            context_client_code=context.client_code,
            context_module_name=H.MODULE_LABEL,
            context_meta_html=self._context_meta(page),
            header_actions=self._client_switcher(context, query),
        )

    def _context_meta(self, page: LibraryPage) -> str:
        """`RP-7`: the library total the rail's per-type counts must sum to."""
        return (
            '<span class="rep-context-total" data-library-total="'
            f'{H.esc(page.library_total)}">{H.esc(H.format_count(page.library_total))} '
            "w bibliotece</span>"
        )

    def _client_switcher(self, context: ClientContext, query: LibraryQuery) -> str:
        """`SH-6`: the client is never hidden, and switching keeps the module.

        Only clients this account currently has report access to are offered —
        an inaccessible client is ABSENT, not listed and disabled.
        """
        if len(context.available) <= 1:
            return ""
        options = "".join(
            f'<option value="{H.esc(code)}"'
            + (" selected" if code == context.client_code else "")
            + f">{H.esc(name)}</option>"
            for code, name in context.available
        )
        return (
            f'<form class="rep-client-switch" method="get" action="{H.esc(H.LIBRARY_PATH)}">'
            f'<label class="lp-visually-hidden" for="rep-client">{H.esc(H.COPY["client"])}</label>'
            '<select id="rep-client" name="client_code" data-rep-autosubmit>'
            f"{options}</select>"
            f'<noscript><button type="submit" class="rep-button secondary">Zmień</button></noscript>'
            "</form>"
        )

    def _rail_html(self, query: LibraryQuery, page: LibraryPage) -> str:
        """The 288 px report-type rail (`RP-2`).

        Each entry states the type's declared cadence and its instance count,
        and carries an attention dot when the newest instance of that type
        presents as a genuine file-less failure.
        """
        entries = [
            '<li class="rep-rail-item'
            + (" active" if not query.type_key else "")
            + '">'
            + f'<a href="{H.esc(H.url(H.LIBRARY_PATH, query_params(query, **{"type": "", "page": ""})))}"'
            + (' aria-current="true"' if not query.type_key else "")
            + ">"
            + f'<span class="rep-rail-name">{H.esc(H.COPY["rail_all"])}</span>'
            + f'<span class="rep-rail-count">{H.esc(H.format_count(page.library_total))}</span>'
            + "</a></li>"
        ]
        for type_view in page.types:
            active = query.type_key == type_view.type_key
            dot = (
                '<span class="rep-attention" title="Ostatni raport tego typu zakończył się błędem">'
                + H.visually_hidden("uwaga: ostatnia próba zakończyła się błędem")
                + "</span>"
                if type_view.needs_attention
                else ""
            )
            href = H.url(
                H.LIBRARY_PATH, query_params(query, **{"type": type_view.type_key, "page": ""})
            )
            entries.append(
                '<li class="rep-rail-item' + (" active" if active else "") + '">'
                f'<a href="{H.esc(href)}"' + (' aria-current="true"' if active else "") + ">"
                f'<span class="rep-rail-name">{H.esc(type_view.display_name)}{dot}</span>'
                f'<span class="rep-rail-cadence">'
                f'{H.esc(H.cadence_label(type_view.cadence_class, type_view.cadence_detail))}</span>'
                f'<span class="rep-rail-count">{H.esc(H.format_count(type_view.instance_count))}</span>'
                "</a></li>"
            )
        return (
            '<aside class="rep-rail" aria-label="' + H.esc(H.COPY["rail_heading"]) + '">'
            f'<h2 class="rep-rail-heading">{H.esc(H.COPY["rail_heading"])}</h2>'
            '<div class="rep-rail-filter">'
            f'<label class="lp-visually-hidden" for="rep-rail-filter">{H.esc(H.COPY["rail_filter"])}</label>'
            f'<input id="rep-rail-filter" type="search" data-rep-rail-filter '
            f'placeholder="{H.esc(H.COPY["rail_filter"])}" autocomplete="off">'
            "</div>"
            f'<ul class="rep-rail-list">{"".join(entries)}</ul>'
            "</aside>"
        )

    def _toolbar_html(self, query: LibraryQuery, page: LibraryPage) -> str:
        year_options = "".join(
            f'<option value="{H.esc(year)}"'
            + (" selected" if query.year == year else "")
            + f">{H.esc(year)}</option>"
            for year in page.available_years
        )
        status_options = "".join(
            f'<option value="{H.esc(value)}"'
            + (" selected" if query.status == value else "")
            + f">{H.esc(label)}</option>"
            for value, label in H.STATUS_LABELS.items()
        )
        type_options = "".join(
            f'<option value="{H.esc(t.type_key)}"'
            + (" selected" if query.type_key == t.type_key else "")
            + f">{H.esc(t.display_name)}</option>"
            for t in page.types
        )
        hidden = (
            f'<input type="hidden" name="client_code" value="{H.esc(query.client_code)}">'
            f'<input type="hidden" name="limit" value="{H.esc(query.limit)}">'
        )
        return (
            '<div class="rep-toolbar">'
            f'<form class="rep-filters" method="get" action="{H.esc(H.LIBRARY_PATH)}">'
            + hidden
            + '<div class="rep-filter rep-filter-search">'
            f'<label for="rep-q">{H.esc(H.COPY["search_label"])}</label>'
            f'<input id="rep-q" type="search" name="q" value="{H.esc(query.search)}" '
            f'placeholder="{H.esc(H.COPY["search_label"])}" autocomplete="off">'
            "</div>"
            '<div class="rep-filter">'
            f'<label for="rep-type">{H.esc(H.COPY["type"])}</label>'
            '<select id="rep-type" name="type" data-rep-autosubmit>'
            f'<option value="">{H.esc(H.COPY["rail_all"])}</option>{type_options}</select>'
            "</div>"
            '<div class="rep-filter">'
            f'<label for="rep-year">{H.esc(H.COPY["year"])}</label>'
            '<select id="rep-year" name="year" data-rep-autosubmit>'
            f'<option value="">wszystkie</option>{year_options}</select>'
            "</div>"
            '<div class="rep-filter">'
            f'<label for="rep-status">{H.esc(H.COPY["status"])}</label>'
            '<select id="rep-status" name="status" data-rep-autosubmit>'
            f'<option value="">wszystkie</option>{status_options}</select>'
            "</div>"
            '<button type="submit" class="rep-button primary">Zastosuj</button>'
            "</form>"
            f'<p class="rep-grouping-rule">{H.esc(H.COPY["grouping_rule"])}</p>'
            "</div>"
        )

    def _chips_html(self, query: LibraryQuery, page: LibraryPage) -> str:
        """Active filters as removable chips — the Database Explorer vocabulary."""
        chips = []
        if query.type_key:
            label = next(
                (t.display_name for t in page.types if t.type_key == query.type_key),
                query.type_key,
            )
            chips.append((f'{H.COPY["type"]}: {label}', query_params(query, **{"type": "", "page": ""})))
        if query.year:
            chips.append((f'{H.COPY["year"]}: {query.year}', query_params(query, year="", page="")))
        if query.status:
            chips.append(
                (
                    f'{H.COPY["status"]}: {H.STATUS_LABELS.get(query.status, query.status)}',
                    query_params(query, status="", page=""),
                )
            )
        if query.search:
            chips.append((f'„{query.search}”', query_params(query, q="", page="")))
        if not chips:
            return ""
        rendered = "".join(
            f'<a class="rep-chip" href="{H.esc(H.url(H.LIBRARY_PATH, params))}">'
            f"<span>{H.esc(label)}</span><span aria-hidden=\"true\">×</span>"
            f'{H.visually_hidden("usuń filtr")}</a>'
            for label, params in chips
        )
        clear = H.url(
            H.LIBRARY_PATH,
            {"client_code": query.client_code, "limit": query.limit if query.limit != 100 else ""},
        )
        return (
            '<div class="rep-chips" role="group" aria-label="Aktywne filtry">'
            + rendered
            + f'<a class="rep-chip rep-chip-clear" href="{H.esc(clear)}">'
            f'{H.esc(H.COPY["clear_filters"])}</a>'
            "</div>"
        )

    def _groups_html(self, query: LibraryQuery, page: LibraryPage) -> str:
        if not page.groups:
            return self._empty_library_html(query, page)
        sections = []
        for group in page.groups:
            heading = H.esc(group.heading)
            rows = "".join(self._instance_row_html(query, i) for i in group.instances)
            sections.append(
                f'<section class="rep-group" data-group="{H.esc(group.key)}">'
                f'<h2 class="rep-group-heading">{heading}'
                f'<span class="rep-group-count">{H.esc(H.format_count(group.count))}</span></h2>'
                f'<ol class="rep-rows">{rows}</ol>'
                "</section>"
            )
        return f'<div class="rep-groups">{"".join(sections)}</div>'

    def _instance_row_html(self, query: LibraryQuery, instance: ReportInstanceView) -> str:
        """One status-bearing card-row with the six approved regions (`PBC` §3.2).

        `NOWY` is deliberately absent: the owner deferred the per-account seen
        state (`docs/40` §13.2 = B), so nothing here claims to know whether an
        account has seen an instance.
        """
        detail_href = H.url(
            f"{H.INSTANCE_PATH}/{instance.instance_ref}", query_params(query)
        )
        period = (
            format_period_range(instance.period_start, instance.period_end)
            if instance.period_start and instance.period_end
            else "nie dotyczy"
        )
        files_html, actions_html = self._files_and_actions(instance, detail_href, query)
        moment = (
            f'{H.COPY["started_at"]} {H.format_moment(instance.generation_started_at)}'
            if instance.status == STATUS_GENERATING and instance.generation_started_at
            else H.format_moment(instance.library_timestamp)
        )
        return (
            f'<li class="rep-row rep-row-{H.esc(H.STATUS_VARIANTS.get(instance.status, "neutral"))}"'
            f' data-status="{H.esc(instance.status)}" data-type="{H.esc(instance.type_key)}">'
            '<div class="rep-cell rep-cell-name">'
            f'<a class="rep-row-link" href="{H.esc(detail_href)}">{H.esc(instance.display_name)}</a>'
            "</div>"
            f'<div class="rep-cell rep-cell-type"><span class="rep-cell-label">'
            f'{H.esc(H.COPY["report_type"])}</span>{H.esc(instance.type_label)}</div>'
            f'<div class="rep-cell rep-cell-period"><span class="rep-cell-label">'
            f'{H.esc(H.COPY["reporting_period"])}</span>{H.esc(period)}</div>'
            f'<div class="rep-cell rep-cell-status">{H.status_badge(instance.status)}'
            f'<span class="rep-row-moment">{H.esc(moment)}</span></div>'
            f'<div class="rep-cell rep-cell-files"><span class="rep-cell-label">'
            f'{H.esc(H.COPY["files"])}</span>{files_html}</div>'
            f'<div class="rep-cell rep-cell-actions">{actions_html}</div>'
            "</li>"
        )

    def _files_and_actions(
        self, instance: ReportInstanceView, detail_href: str, query: LibraryQuery
    ) -> tuple[str, str]:
        """`PBC` §3.3 — status governs the actions, and an absent action is absent.

        No state renders a disabled control, which is exactly what `RP-8`
        requires of `W generowaniu`.
        """
        available = instance.available_files
        if instance.status == STATUS_GENERATING:
            return f'<span class="rep-files-note">{H.esc(H.COPY["no_files_yet"])}</span>', ""
        if instance.status == STATUS_FAILED:
            report_href = H.url(
                f"{H.INSTANCE_PATH}/{instance.instance_ref}", query_params(query, problem="1")
            )
            return (
                f'<span class="rep-files-note">{H.esc(H.COPY["no_files_after_failure"])}</span>',
                H.button(report_href, H.COPY["report_problem"], variant="secondary"),
            )
        if instance.status == STATUS_EXPIRED or not available:
            return '<span class="rep-files-note">—</span>', ""
        badges = "".join(H.format_badge(f.file_format, f.size_bytes) for f in available)
        actions = H.button(detail_href, H.COPY["open_report"], variant="primary")
        actions += self._download_action(instance, query)
        return badges, actions

    def _download_action(self, instance: ReportInstanceView, query: LibraryQuery) -> str:
        """`RP-21`: `Pobierz` at one file, `Pobierz wszystkie (n)` above one."""
        available = instance.available_files
        if not available:
            return ""
        if len(available) == 1:
            href = self._download_href(instance, available[0].member_ref)
            return H.button(href, H.COPY["download"], variant="secondary")
        href = H.url(
            f"{H.INSTANCE_PATH}/{instance.instance_ref}/files/download-all",
            {"client_code": query.client_code},
        )
        return H.button(
            href, f'{H.COPY["download_all"]} ({len(available)})', variant="secondary"
        )

    def _download_href(self, instance: ReportInstanceView, member_ref: str) -> str:
        return H.url(
            f"{H.INSTANCE_PATH}/{instance.instance_ref}/files/{member_ref}/download",
            {"client_code": instance.client_code},
        )

    def _preview_href(self, instance: ReportInstanceView, member_ref: str) -> str:
        return H.url(
            f"{H.INSTANCE_PATH}/{instance.instance_ref}/files/{member_ref}/preview",
            {"client_code": instance.client_code},
        )

    def _empty_library_html(self, query: LibraryQuery, page: LibraryPage) -> str:
        """The two truly different empty states (`REP-004`).

        A filtered-empty list names the filter that emptied it and states the
        oldest instance that DOES exist; a client with no reports at all says
        that instead, because offering `Wyczyść filtry` to someone with nothing
        to clear would be a dead end.
        """
        has_filters = bool(query.type_key or query.year or query.status or query.search)
        if not has_filters or page.library_total == 0:
            return H.empty_state(
                "Brak raportów dla tego klienta",
                "Dla tego klienta nie opublikowano jeszcze żadnego raportu. "
                "Nowe raporty pojawią się tutaj po pierwszym wygenerowaniu.",
            )
        named = []
        if query.type_key:
            label = next(
                (t.display_name for t in page.types if t.type_key == query.type_key),
                query.type_key,
            )
            named.append(f'{H.COPY["type"]}: {label}')
        if query.year:
            named.append(f'{H.COPY["year"]}: {query.year}')
        if query.status:
            named.append(f'{H.COPY["status"]}: {H.STATUS_LABELS.get(query.status, query.status)}')
        if query.search:
            named.append(f'„{query.search}”')
        oldest = (
            f"Najstarsza dostępna pozycja w bibliotece pochodzi z "
            f"{H.format_moment(page.oldest_library_timestamp)}."
            if page.oldest_library_timestamp
            else ""
        )
        clear = H.url(H.LIBRARY_PATH, {"client_code": query.client_code})
        return H.empty_state(
            "Brak pozycji dla tych filtrów",
            f'Żadna pozycja nie pasuje do filtrów: {" · ".join(named)}. {oldest}'.strip(),
            H.button(clear, H.COPY["clear_filters"], variant="primary"),
        )

    def _footer_html(self, query: LibraryQuery, page: LibraryPage) -> str:
        """`RP-5`: the filtered count and the library total, never one number."""
        offset = (page.page - 1) * page.limit
        first = offset + 1 if page.rendered_count else 0
        last = offset + page.rendered_count
        counter = (
            f"{H.format_count(first)}–{H.format_count(last)} z "
            f"{H.format_count(page.filtered_total)} po filtrach · "
            f"{H.format_count(page.library_total)} w bibliotece"
        )
        buttons = []
        if page.page > 1:
            buttons.append(
                H.button(
                    H.url(H.LIBRARY_PATH, query_params(query, page=page.page - 1)),
                    H.COPY["previous_page"],
                )
            )
        if page.page < page.page_count:
            buttons.append(
                H.button(
                    H.url(H.LIBRARY_PATH, query_params(query, page=page.page + 1)),
                    H.COPY["next_page"],
                )
            )
        sizes = "".join(
            f'<a class="rep-chip" href="{H.esc(H.url(H.LIBRARY_PATH, query_params(query, limit=size, page="")))}"'
            + (' aria-current="true"' if size == page.limit else "")
            + f">{H.esc(size)}</a>"
            for size in PAGE_SIZES
        )
        return (
            '<div class="rep-footer">'
            f'<p class="rep-counter" role="status">{H.esc(counter)}</p>'
            f'<div class="rep-pager">{"".join(buttons)}</div>'
            '<div class="rep-page-sizes" role="group" aria-label="'
            + H.esc(H.COPY["per_page"])
            + '">'
            f'<span class="rep-muted">{H.esc(H.COPY["per_page"])}</span>{sizes}</div>'
            "</div>"
        )

    # -- detail -------------------------------------------------------------

    def detail(self, *, user: dict, instance_ref: str, params=None) -> PageResult:
        query = build_query(params)
        try:
            context = self._service.client_context(user, query.client_code)
        except ReportAccessDeniedError as exc:
            return self._no_report_access(exc)
        except ReportClientNotFoundError:
            return self._client_unavailable()
        query.client_code = context.client_code
        try:
            detail = self._service.detail(user, context, instance_ref)
        except (ReportInstanceNotFoundError, ReportSchemaUnavailableError):
            return self._instance_unavailable(context, query)
        except ReportExplorerError:
            return self._instance_unavailable(context, query)
        except Exception:  # noqa: BLE001
            return self._instance_unavailable(context, query)
        return self._render_detail(context, query, detail, params)

    def _render_detail(
        self, context: ClientContext, query: LibraryQuery, detail: ReportDetail, params
    ) -> PageResult:
        instance = detail.instance
        selected = self._selected_preview_member(instance, _param(params, "file"))
        body = (
            self._breadcrumb_html(query, detail)
            + '<div class="rep-detail">'
            + self._header_card_html(query, detail)
            + self._preview_html(instance, selected, query)
            + self._files_panel_html(instance, query)
            + self._history_html(detail, query)
            + "</div>"
        )
        return PageResult(
            status_code=200,
            title=instance.display_name,
            body_html=body,
            active_key=ACTIVE_KEY,
            context_client_name=context.display_name,
            context_client_code=context.client_code,
            context_module_name=H.MODULE_LABEL,
            context_meta_html=(
                f'<span class="rep-context-type">{H.esc(instance.type_label)}</span>'
            ),
        )

    def _selected_preview_member(self, instance: ReportInstanceView, requested: str):
        """The previewable member the preview region shows.

        Defaults to the explicit main file (`RP-13`) — never to the first row,
        never to a filename or an extension — and honours the format switcher
        only for a member that is itself previewable and available.
        """
        previewable = [f for f in instance.available_files if f.is_previewable]
        if not previewable:
            return None
        if requested:
            for candidate in previewable:
                if candidate.member_ref == requested:
                    return candidate
        for candidate in previewable:
            if candidate.is_main_file:
                return candidate
        return previewable[0]

    def _breadcrumb_html(self, query: LibraryQuery, detail: ReportDetail) -> str:
        """`SH-12`/`SH-13` — the full path, the return link and period siblings.

        The return link carries the whole library state, so `‹ Wróć do
        biblioteki` reopens the list exactly as it was left, and the note says
        so in the approved words.
        """
        instance = detail.instance
        back = H.url(H.LIBRARY_PATH, query_params(query))
        siblings = ""
        if instance.supports_periods:
            if detail.previous_period:
                href = H.url(
                    f'{H.INSTANCE_PATH}/{detail.previous_period["instance_ref"]}',
                    query_params(query),
                )
                siblings += (
                    f'<a class="rep-sibling" href="{H.esc(href)}" rel="prev">'
                    f'‹ {H.esc(detail.previous_period["display_name"])}</a>'
                )
            # `RP-17`: at the newest period the forward control is ABSENT.
            if detail.next_period:
                href = H.url(
                    f'{H.INSTANCE_PATH}/{detail.next_period["instance_ref"]}',
                    query_params(query),
                )
                siblings += (
                    f'<a class="rep-sibling" href="{H.esc(href)}" rel="next">'
                    f'{H.esc(detail.next_period["display_name"])} ›</a>'
                )
        period = (
            format_period_range(instance.period_start, instance.period_end)
            if instance.period_start and instance.period_end
            else ""
        )
        trail = " · ".join(
            part
            for part in (
                instance.client_display_name,
                H.MODULE_LABEL,
                instance.type_label,
                period,
            )
            if part
        )
        return (
            '<nav class="rep-breadcrumb" aria-label="Ścieżka">'
            f'<a class="rep-back" href="{H.esc(back)}">{H.esc(H.COPY["back_to_library"])}</a>'
            f'<span class="rep-breadcrumb-note">{H.esc(H.COPY["filters_preserved"])}</span>'
            f'<span class="rep-breadcrumb-trail">{H.esc(trail)}</span>'
            f'<span class="rep-siblings">{siblings}</span>'
            "</nav>"
        )

    def _header_card_html(self, query: LibraryQuery, detail: ReportDetail) -> str:
        """The approved 8-field metadata grid plus the header actions (`PBC` §3.6)."""
        instance = detail.instance
        period = (
            format_period_range(instance.period_start, instance.period_end)
            if instance.period_start and instance.period_end
            else "nie dotyczy"
        )
        fields = (
            (H.COPY["client"], instance.client_display_name),
            (H.COPY["report_type"], instance.type_label),
            (H.COPY["cycle"], H.cadence_label(instance.cadence_class, instance.cadence_detail)),
            (H.COPY["reporting_period"], period),
            (H.COPY["period_ordinal"], instance.period_key or "nie dotyczy"),
            (
                H.COPY["generated_at"],
                (
                    f'{H.COPY["started_at"]} {H.format_moment(instance.generation_started_at)}'
                    if instance.status == STATUS_GENERATING and instance.generation_started_at
                    else H.format_moment(instance.generation_finished_at or instance.library_timestamp)
                ),
            ),
            (
                H.COPY["row_count"],
                H.format_count(instance.row_count) if instance.row_count is not None else "—",
            ),
            (
                H.COPY["retention"],
                (
                    f"do {H.format_day(instance.retention_until)}"
                    if instance.retention_until
                    else "—"
                ),
            ),
        )
        grid = "".join(
            f'<div class="rep-meta-field"><dt>{H.esc(label)}</dt><dd>{H.esc(value)}</dd></div>'
            for label, value in fields
        )
        actions = ""
        if instance.status == STATUS_READY:
            actions += self._download_action(instance, query)
        if detail.source_url:
            # `RP-18`. Present ONLY because the account currently passes the
            # Database Explorer gate for this dataset; otherwise absent, and no
            # dataset metadata is named.
            actions += H.button(
                detail.source_url,
                f'{H.COPY["source_data"]} · {detail.source_dataset_name}'
                if detail.source_dataset_name
                else H.COPY["source_data"],
                variant="secondary",
            )
        if instance.status == STATUS_FAILED:
            actions += H.button("#rep-problem", H.COPY["report_problem"], variant="secondary")
        description = (
            f'<p class="rep-detail-description">{H.esc(instance.type_description)}</p>'
            if instance.type_description
            else ""
        )
        failure = ""
        if instance.status == STATUS_FAILED:
            failure = (
                '<p class="rep-detail-failure" id="rep-problem" role="alert">'
                f'{H.esc(H.COPY["no_files_after_failure"])}'
                + (
                    f' <span class="rep-muted">({H.esc(instance.safe_error_code)})</span>'
                    if instance.safe_error_code
                    else ""
                )
                + "</p>"
            )
        return (
            '<section class="rep-card rep-header-card">'
            '<div class="rep-header-top">'
            # NOT a second `<h1>`. The shell already renders this exact string
            # as the page's one top-level heading (`title=instance.display_name`
            # below), so an `<h1>` here duplicated the document title and broke
            # the accepted `S10` contract of exactly one heading per page. The
            # card keeps its visual title through the class; the semantic title
            # is the shell's, stated once. No ARIA role or label is added — that
            # would put the duplicate back in the accessibility tree.
            f'<div><p class="rep-detail-title">{H.esc(instance.display_name)}</p>{description}</div>'
            f'<div class="rep-header-status">{H.status_badge(instance.status)}</div>'
            "</div>"
            f"{failure}"
            f'<dl class="rep-meta-grid">{grid}</dl>'
            f'<div class="rep-header-actions">{actions}</div>'
            "</section>"
        )

    def _preview_html(self, instance: ReportInstanceView, selected, query: LibraryQuery) -> str:
        """`RP-12`: embedded in the layout, with page navigation and full screen.

        Not a modal and not a new tab. When no member is previewable the region
        explains why and the files panel still offers every download, which is
        exactly the `RP-14` / `RP-20` behaviour.
        """
        previewable = [f for f in instance.available_files if f.is_previewable]
        if not selected:
            if instance.status == STATUS_GENERATING:
                message = H.COPY["no_files_yet"]
            elif instance.status == STATUS_FAILED:
                message = H.COPY["no_files_after_failure"]
            elif instance.status == STATUS_EXPIRED:
                message = "Pliki tego raportu wygasły; pozycja pozostaje w historii."
            else:
                message = "Żaden z plików tego raportu nie nadaje się do podglądu w przeglądarce."
            return (
                '<section class="rep-card rep-preview" aria-label="'
                + H.esc(H.COPY["preview"])
                + '">'
                f'<h2>{H.esc(H.COPY["preview"])}</h2>'
                f'<p class="rep-preview-unavailable" role="status">{H.esc(message)}</p>'
                "</section>"
            )
        switcher = ""
        if len(previewable) > 1:
            switcher = '<div class="rep-format-switch" role="group" aria-label="Format podglądu">'
            for member in previewable:
                href = H.url(
                    f"{H.INSTANCE_PATH}/{instance.instance_ref}",
                    query_params(query, file=member.member_ref),
                )
                current = ' aria-current="true"' if member.member_ref == selected.member_ref else ""
                switcher += (
                    f'<a class="rep-chip" href="{H.esc(href)}"{current}>'
                    f"{H.esc(member.file_format)}</a>"
                )
            switcher += "</div>"
        pages = (
            int(selected.content_metric_value)
            if selected.content_metric_kind == "pages" and selected.content_metric_value
            else 0
        )
        page_nav = ""
        if pages > 1:
            page_nav = (
                '<div class="rep-page-nav" data-rep-page-nav data-rep-pages="'
                f'{H.esc(pages)}">'
                '<button type="button" class="rep-button secondary" data-rep-page-prev>‹</button>'
                '<span class="rep-page-indicator" data-rep-page-indicator>'
                f"1 / {H.esc(pages)}</span>"
                '<button type="button" class="rep-button secondary" data-rep-page-next>›</button>'
                "</div>"
            )
        src = self._preview_href(instance, selected.member_ref)
        meta = " · ".join(
            part
            for part in (
                selected.display_filename,
                H.metric_label(selected.content_metric_kind, selected.content_metric_value),
                H.format_size(selected.size_bytes),
            )
            if part
        )
        return (
            '<section class="rep-card rep-preview" aria-label="'
            + H.esc(H.COPY["preview"])
            + '">'
            '<div class="rep-preview-head">'
            f'<h2>{H.esc(H.COPY["preview"])}</h2>'
            f'<p class="rep-preview-meta">{H.esc(meta)}</p>'
            f"{switcher}{page_nav}"
            '<button type="button" class="rep-button secondary" data-rep-fullscreen>'
            f'{H.esc(H.COPY["fullscreen"])}</button>'
            "</div>"
            '<div class="rep-preview-frame" data-rep-preview-frame>'
            f'<object data="{H.esc(src)}" type="{H.esc(selected.content_type)}" '
            f'data-rep-preview-src="{H.esc(src)}" aria-label="'
            + H.esc(selected.display_filename)
            + '">'
            f'<p>{H.esc("Podgląd nie jest dostępny w tej przeglądarce.")} '
            f'<a class="rep-link" href="{H.esc(self._download_href(instance, selected.member_ref))}">'
            f'{H.esc(H.COPY["download"])}</a></p>'
            "</object></div>"
            "</section>"
        )

    def _files_panel_html(self, instance: ReportInstanceView, query: LibraryQuery) -> str:
        """`Pliki w tej pozycji` (`PBC` §3.5, `RP-13`, `RP-14`).

        The main file is distinguished by a tinted row AND a stated role — the
        flag is explicit on the member, so the distinction never depends on the
        panel's ordering, the filename or the extension.
        """
        if not instance.files:
            note = (
                H.COPY["no_files_yet"]
                if instance.status == STATUS_GENERATING
                else H.COPY["no_files_after_failure"]
                if instance.status == STATUS_FAILED
                else "Brak plików."
            )
            return (
                '<section class="rep-card rep-files-panel">'
                f'<h2>{H.esc(H.COPY["files_panel"])}</h2>'
                f'<p class="rep-files-note" role="status">{H.esc(note)}</p></section>'
            )
        rows = []
        for member in instance.files:
            role = H.ROLE_LABELS.get(member.semantic_role, member.semantic_role)
            actions = ""
            if member.is_available:
                if member.is_previewable:
                    href = H.url(
                        f"{H.INSTANCE_PATH}/{instance.instance_ref}",
                        query_params(query, file=member.member_ref),
                    )
                    actions += H.button(href, H.COPY["preview"], variant="secondary")
                actions += H.button(
                    self._download_href(instance, member.member_ref),
                    H.COPY["download"],
                    variant="secondary",
                )
            else:
                actions = (
                    '<span class="rep-files-note">plik wygasł i nie jest już dostępny</span>'
                )
            metric = H.metric_label(member.content_metric_kind, member.content_metric_value)
            rows.append(
                '<li class="rep-file'
                + (" rep-file-main" if member.is_main_file else "")
                + (" rep-file-unavailable" if not member.is_available else "")
                + '">'
                f'<span class="rep-file-format">{H.esc(member.file_format)}</span>'
                '<span class="rep-file-name">'
                f"{H.esc(member.display_filename)}"
                + (
                    f'<span class="rep-file-main-flag">{H.esc(role)}</span>'
                    if member.is_main_file
                    else f'<span class="rep-file-role">{H.esc(role)}</span>'
                )
                + "</span>"
                f'<span class="rep-file-size">{H.esc(H.format_size(member.size_bytes))}'
                + (f' · {H.esc(metric)}' if metric else "")
                + "</span>"
                f'<span class="rep-file-actions">{actions}</span>'
                "</li>"
            )
        return (
            '<section class="rep-card rep-files-panel">'
            f'<h2>{H.esc(H.COPY["files_panel"])}</h2>'
            f'<ul class="rep-files">{"".join(rows)}</ul>'
            "</section>"
        )

    def _history_html(self, detail: ReportDetail, query: LibraryQuery) -> str:
        """`Historia tego raportu` — built from instance records only (`RP-15`).

        An expired or failed period therefore still renders its period, its own
        status, its generation time and `0` files: history outlives the bytes.
        """
        instance = detail.instance
        if not instance.supports_history or not detail.history:
            return ""
        rows = "".join(
            '<tr'
            + (' class="rep-history-current" aria-current="true"' if entry.is_current else "")
            + ">"
            f'<td><a class="rep-link" href="'
            f'{H.esc(H.url(f"{H.INSTANCE_PATH}/{entry.instance_ref}", query_params(query)))}">'
            f"{H.esc(entry.period_label)}</a></td>"
            f"<td>{H.esc(entry.period_key)}</td>"
            f"<td>{H.status_badge(entry.status)}</td>"
            f"<td>{H.esc(H.format_moment(entry.library_timestamp))}</td>"
            f"<td>{H.esc(H.format_count(entry.file_count))}</td>"
            "</tr>"
            for entry in detail.history
        )
        expand = ""
        if detail.total_periods > len(detail.history):
            href = H.url(
                H.LIBRARY_PATH, query_params(query, **{"type": instance.type_key, "page": ""})
            )
            expand = (
                f'<a class="rep-link rep-history-expand" href="{H.esc(href)}">'
                + H.esc(H.COPY["show_all_periods"].format(n=detail.total_periods))
                + "</a>"
            )
        return (
            '<section class="rep-card rep-history">'
            f'<h2>{H.esc(H.COPY["history"])}</h2>'
            '<div class="rep-history-wrap"><table class="rep-history-table">'
            f'<thead><tr><th>{H.esc(H.COPY["reporting_period"])}</th>'
            f'<th>{H.esc(H.COPY["period_ordinal"])}</th>'
            f'<th>{H.esc(H.COPY["status"])}</th>'
            f'<th>{H.esc(H.COPY["generated_at"])}</th>'
            f'<th>{H.esc(H.COPY["files"])}</th></tr></thead>'
            f"<tbody>{rows}</tbody></table></div>{expand}"
            "</section>"
        )

    # -- states (`REP-004`) -------------------------------------------------

    def _no_report_access(self, exc: ReportAccessDeniedError) -> PageResult:
        """`RP-19`: dataset access and report access are separate grants.

        Client context is preserved and the two grants are named, but nothing
        about the client's reports is disclosed — not a count, not a type, not
        an instance.
        """
        if exc.has_database_access:
            message = (
                "To konto ma dostęp do danych tego klienta, ale nie ma dostępu do jego raportów. "
                "Dostęp do danych i dostęp do raportów to dwa osobne uprawnienia."
            )
        else:
            message = (
                "To konto nie ma dostępu do raportów żadnego klienta. "
                "Dostęp do danych i dostęp do raportów to dwa osobne uprawnienia."
            )
        body = H.empty_state(
            "Brak dostępu do raportów tego klienta",
            message,
            H.button("mailto:", H.COPY["request_access"], variant="secondary"),
        )
        return PageResult(
            status_code=403,
            title=H.COPY["module"],
            body_html=body,
            context_client_name=exc.client_display_name,
            context_client_code=exc.client_code,
            context_module_name=H.MODULE_LABEL,
        )

    def _client_unavailable(self) -> PageResult:
        """One state for absent, inactive and unauthorized — existence never leaks."""
        return PageResult(
            status_code=404,
            title=H.COPY["module"],
            body_html=H.empty_state(
                "Klient niedostępny",
                "Ten klient nie jest dostępny dla tego konta.",
            ),
            context_module_name=H.MODULE_LABEL,
        )

    def _instance_unavailable(self, context: ClientContext, query: LibraryQuery) -> PageResult:
        """A malformed, foreign, deleted or unauthorized reference — one answer.

        `REP-004` requires that an authorization boundary not leak existence, so
        a report belonging to another client is indistinguishable from one that
        never existed.
        """
        back = H.url(H.LIBRARY_PATH, query_params(query))
        return PageResult(
            status_code=404,
            title=H.COPY["module"],
            body_html=H.empty_state(
                "Nie znaleziono raportu",
                "Ten raport nie istnieje albo nie jest dostępny dla tego konta.",
                H.button(back, H.COPY["back_to_library"], variant="secondary"),
            ),
            context_client_name=context.display_name,
            context_client_code=context.client_code,
            context_module_name=H.MODULE_LABEL,
        )

    def _schema_unavailable(self, context: ClientContext) -> PageResult:
        """Migration 068 is missing. Say so; never render an empty library.

        An empty library and an unavailable one look identical to a reader, and
        only one of them means "this client has no reports".
        """
        return PageResult(
            status_code=503,
            title=H.COPY["module"],
            body_html=H.error_state(
                "Biblioteka raportów jest niedostępna",
                "Ta instalacja nie ma jeszcze schematu raportów generowanych. "
                "Lista nie może zostać odczytana; nie oznacza to braku raportów.",
                reference="REPORT_SCHEMA_UNAVAILABLE",
            ),
            context_client_name=context.display_name,
            context_client_code=context.client_code,
            context_module_name=H.MODULE_LABEL,
        )

    def _library_failure(self, context: ClientContext) -> PageResult:
        return PageResult(
            status_code=503,
            title=H.COPY["module"],
            body_html=H.error_state(
                "Nie udało się odczytać biblioteki raportów",
                "Lista raportów jest chwilowo niedostępna. Spróbuj ponownie za chwilę.",
                reference="REPORT_LIBRARY_READ_FAILED",
            ),
            context_client_name=context.display_name,
            context_client_code=context.client_code,
            context_module_name=H.MODULE_LABEL,
        )

    # -- file delivery ------------------------------------------------------

    def resolve_file(self, *, user: dict, instance_ref: str, member_ref: str, params=None,
                     for_preview: bool = False):
        """Authorize a byte request and hand back the resolved member.

        Returns `(resolved, None)` or `(None, PageResult)`. The page result is a
        rendered state — `REP-004` distinguishes an unavailable FILE from an
        unavailable LIST, and neither of them is a raw exception.

        `for_preview` is the INLINE request. It is answered by the service from
        the stored object's own content type, so a member cannot become
        embeddable by describing itself as one.
        """
        query = build_query(params)
        try:
            context = self._service.client_context(user, query.client_code)
        except ReportAccessDeniedError as exc:
            return None, self._no_report_access(exc)
        except ReportClientNotFoundError:
            return None, self._client_unavailable()
        query.client_code = context.client_code
        try:
            resolved = self._service.resolve_file(
                user, context, instance_ref, member_ref, for_preview=for_preview
            )
        except ReportFilePreviewUnsupportedError:
            return None, self._preview_unsupported(context, query, instance_ref)
        except ReportFileUnavailableError:
            return None, self._file_unavailable(context, query, instance_ref)
        except (ReportInstanceNotFoundError, ReportFileNotFoundError, ReportSchemaUnavailableError):
            return None, self._instance_unavailable(context, query)
        except ReportExplorerError:
            return None, self._instance_unavailable(context, query)
        return resolved, None

    def _preview_unsupported(
        self, context: ClientContext, query: LibraryQuery, instance_ref: str
    ) -> PageResult:
        """`RP-14`: this file downloads, it does not embed.

        A 415 rather than a 404 or a 410, because nothing is missing and nothing
        expired: the request asked for a representation this member does not
        have. The download the files panel already offers still works, and the
        state says so.
        """
        back = H.url(f"{H.INSTANCE_PATH}/{instance_ref}", query_params(query))
        return PageResult(
            status_code=415,
            title=H.COPY["module"],
            body_html=H.error_state(
                "Ten plik nie ma podglądu",
                "Tego pliku nie da się wyświetlić w przeglądarce. Pozostaje dostępny "
                "do pobrania z listy plików raportu.",
                H.button(back, "Wróć do raportu", variant="secondary"),
                reference="REPORT_FILE_NOT_PREVIEWABLE",
            ),
            context_client_name=context.display_name,
            context_client_code=context.client_code,
            context_module_name=H.MODULE_LABEL,
        )

    def resolve_all_files(self, *, user: dict, instance_ref: str, params=None):
        """Every available member of one instance, each independently authorized.

        `Pobierz wszystkie (n)` must not become a second, weaker path to bytes:
        each member is resolved through exactly the same walk a single download
        uses, so a member whose bytes are gone is simply not in the archive.
        """
        query = build_query(params)
        try:
            context = self._service.client_context(user, query.client_code)
        except ReportAccessDeniedError as exc:
            return None, None, self._no_report_access(exc)
        except ReportClientNotFoundError:
            return None, None, self._client_unavailable()
        query.client_code = context.client_code
        try:
            instance = self._service.instance(user, context, instance_ref)
        except ReportExplorerError:
            return None, None, self._instance_unavailable(context, query)
        resolved = []
        for member in instance.available_files:
            try:
                resolved.append(
                    self._service.resolve_file(
                        user, context, instance_ref, member.member_ref
                    )
                )
            except ReportExplorerError:
                continue
        if not resolved:
            return None, None, self._file_unavailable(context, query, instance_ref)
        return instance, resolved, None

    def _file_unavailable(
        self, context: ClientContext, query: LibraryQuery, instance_ref: str
    ) -> PageResult:
        """`RP-20`: the file is unavailable; the list and the record are not.

        The instance keeps existing, the library keeps working, and the state
        carries a copyable reference instead of a storage key or a driver error.
        """
        back = H.url(f"{H.INSTANCE_PATH}/{instance_ref}", query_params(query))
        library = H.url(H.LIBRARY_PATH, query_params(query))
        return PageResult(
            status_code=410,
            title=H.COPY["module"],
            body_html=H.error_state(
                "Plik nie jest dostępny",
                "Ten plik wygasł lub magazyn plików jest chwilowo niedostępny. "
                "Sama pozycja i jej historia pozostają dostępne, a lista raportów działa.",
                H.button(back, "Wróć do raportu", variant="secondary")
                + H.button(library, H.COPY["back_to_library"], variant="secondary"),
                reference="REPORT_FILE_UNAVAILABLE",
            ),
            context_client_name=context.display_name,
            context_client_code=context.client_code,
            context_module_name=H.MODULE_LABEL,
        )

    def archive_too_large(self, *, client_name: str = "", client_code: str = "",
                          instance_ref: str = "", params=None) -> PageResult:
        """`Pobierz wszystkie` does not fit; the per-file downloads do.

        A TRUTHFUL degradation. The reviewed implementation answered this case
        with the file-store failure state, which told the user the storage was
        unavailable while it was working perfectly and every individual download
        would have succeeded. `RP-21`'s per-file alternative is what actually
        applies, so that is what the state offers.
        """
        query = build_query(params)
        back = (
            H.url(f"{H.INSTANCE_PATH}/{instance_ref}", query_params(query))
            if instance_ref
            else H.LIBRARY_PATH
        )
        return PageResult(
            status_code=413,
            title=H.COPY["module"],
            body_html=H.error_state(
                "Pakiet ZIP jest za duży",
                "Ten raport zawiera zbyt dużo danych, aby pobrać go jako jedno "
                "archiwum. Pobierz pliki pojedynczo z listy plików raportu — "
                "każdy z nich jest dostępny osobno.",
                H.button(back, "Wróć do raportu", variant="secondary"),
                reference="REPORT_ARCHIVE_TOO_LARGE",
            ),
            context_client_name=client_name,
            context_client_code=client_code,
            context_module_name=H.MODULE_LABEL,
        )

    def file_store_failure(self, *, client_name: str = "", client_code: str = "") -> PageResult:
        """A storage-layer failure while the list itself is healthy (`RP-20`)."""
        return PageResult(
            status_code=503,
            title=H.COPY["module"],
            body_html=H.error_state(
                "Magazyn plików jest niedostępny",
                "Lista raportów działa; niedostępne są tylko podgląd i pobieranie plików.",
                H.button(H.LIBRARY_PATH, H.COPY["retry"], variant="secondary"),
                reference="REPORT_FILE_STORE_UNAVAILABLE",
            ),
            context_client_name=client_name,
            context_client_code=client_code,
            context_module_name=H.MODULE_LABEL,
        )


__all__ = [
    "ACTIVE_KEY",
    "PORTAL_LABEL",
    "PageResult",
    "ReportExplorerPages",
    "build_query",
    "query_params",
]
