"""Pure HTML rendering primitives for the server-rendered Eco Driving Explorer.

No FastAPI and no I/O. All dynamic content is HTML-escaped (matching the portal
`_html` helper: `html.escape(..., quote=True)`), so these helpers are safe for
both element text and double-quoted attribute values. The portal chrome
(app bar, client context bar, theme) is applied by the integration layer in
`api/main.py`; this module only builds page-body fragments.

Since S12 the module's styling lives in the repository-native stylesheet
`api/static/css/eco-driving.css` and is requested through the shared page-asset
mechanism, exactly like the Database Explorer's grid layer. There is no inline
`<style>` block and no second theme palette: every colour resolves to a shared
`--lp-*` token.
"""

from __future__ import annotations

import html as _htmllib
from typing import Iterable, NamedTuple, Optional
from urllib.parse import urlencode

if __package__ and __package__.startswith("api."):
    from ..portal_ui.i18n import t
else:  # pragma: no cover - import-path parity with the rest of the package
    from portal_ui.i18n import t


LANDING_PATH = "/user/eco-driving"
RANKINGS_PATH = "/user/eco-driving/rankings"
RANKING_ENTRY_PATH = "/user/eco-driving/ranking-entry"
RANKING_ENTRY_TRIPS_PATH = "/user/eco-driving/ranking-entry/trips"
RANKINGS_EXPORT_PATH = "/user/eco-driving/rankings/export"
PERIODS_EXPORT_PATH = "/user/eco-driving/periods/export"
RANKING_ENTRY_EXPORT_PATH = "/user/eco-driving/ranking-entry/export"
RANKING_ENTRY_TRIPS_EXPORT_PATH = "/user/eco-driving/ranking-entry/trips/export"

# Module-scoped assets, resolved by ``portal_ui.assets.page_asset_tags``.
ECO_DRIVING_PAGE_ASSETS: tuple[str, ...] = ("css/eco-driving.css",)

#: The two selectable Eco tables -- contributing trips and the ranking -- drive
#: rectangular cell selection. Only those pages carry the module, so no other
#: Eco surface pays for a behaviour it does not offer, and no other Eco table
#: silently acquires selection without the `data-eco-column` marking that
#: decides what is selectable.
#:
#: `grid-selection.css` is the selection presentation layer the Database
#: Explorer loads too, and it follows `eco-driving.css` because some of its
#: rules win on source order. It is the same tuple for both tables because they
#: need the same thing; a per-table tuple would be two places to forget.
ECO_DRIVING_SELECTABLE_PAGE_ASSETS: tuple[str, ...] = (
    "css/eco-driving.css",
    "css/grid-selection.css",
    "js/data-grid-selection.js",
)

#: Historical name, kept because the trips page is still the surface most
#: callers mean. It is the same tuple.
ECO_DRIVING_TRIPS_PAGE_ASSETS: tuple[str, ...] = ECO_DRIVING_SELECTABLE_PAGE_ASSETS


SELECTION_MIN_WIDTH_PX = 769


class RenderedGrid(NamedTuple):
    """A rendered grid together with the assets its markup requires.

    A selectable table emits a host and `data-eco-column` cells that are inert
    until `data-grid-selection.js` is on the page, so the markup and the asset
    set are one decision, not two. Returning them as a pair is what makes the
    omission unrepresentable: a caller cannot obtain the HTML without also being
    handed what animates it. That omission is not hypothetical — it is how the
    ranking-basis trips page shipped a dead selection host (`UI-20260827-01`),
    and the ranking table has the same two-return-site shape.
    """

    html: str
    page_assets: tuple[str, ...]


def selection_sheet_attrs(strings_json: str) -> str:
    """The host attributes `data-grid-selection.js` reads at init.

    `data-db-sheet` is the module's host hook and keeps its original name; the
    three `data-grid-*` attributes are the vocabulary overrides that point it at
    this table instead of the Database Explorer's.
    """

    return (
        ' data-db-sheet'
        ' data-grid-table="table.eco-table"'
        ' data-grid-column-attr="data-eco-column"'
        ' data-grid-copy-attr="data-eco-copy"'
        f' data-db-select-min-width="{SELECTION_MIN_WIDTH_PX}"'
        f' data-db-select-strings="{esc(strings_json)}"'
    )


def selection_strings() -> str:
    """The selection module's vocabulary, from the ONE shared catalogue.

    These are the `db.select.*` keys the Database Explorer already uses. Reused
    rather than duplicated under an `eco.` prefix: the words describe the
    interaction, not the table, and two catalogues for one behaviour is how two
    tables start announcing different things for the same gesture.
    """

    import json

    catalogue = {
        "summary": t("db.select.summary"),
        "copied": t("db.select.copied"),
        "copyFailed": t("db.select.copy_failed"),
        "row": [t("db.select.row.one"), t("db.select.row.few"), t("db.select.row.many")],
        "column": [t("db.select.column.one"), t("db.select.column.few"),
                   t("db.select.column.many")],
        "cell": [t("db.select.cell.one"), t("db.select.cell.few"), t("db.select.cell.many")],
    }
    return json.dumps(catalogue, ensure_ascii=False)


def selection_footer() -> str:
    """The live count, the copy hint, and the region that announces changes.

    The status region is not decoration: selection is a visual state, and without
    an `aria-live` announcement the whole interaction is invisible to a screen
    reader. Both elements are optional to the module and mandatory here.

    The classes are the Database Explorer's because the appearance is now shared
    (`css/grid-selection.css`); only the wrapper is this module's, because where
    the footer sits under a table is the table's business.
    """

    return (
        '<div class="eco-select-footer">'
        '<span class="db-select-count" data-db-select-count hidden></span>'
        f'<span class="db-select-hint">{esc(t("db.select.hint"))}</span>'
        '<span class="lp-visually-hidden" role="status" aria-live="polite" '
        f'aria-label="{esc(t("db.select.status_aria"))}" data-db-select-status></span>'
        '</div>'
    )


LINEAGE_LABEL = "Linia danych: zrekonstruowana ze stanu bieżącego"
LINEAGE_TOOLTIP = (
    "Wartości rankingowe są utrwalone, ale dokładny historyczny skład przejazdów "
    "jest odtwarzany z bieżącego modelu przypisań. Zgodność w chwili odczytu nie "
    "jest niezmienną linią danych."
)


def esc(value: object) -> str:
    if value is None:
        return ""
    return _htmllib.escape(str(value), quote=True)


def query(params: dict) -> str:
    """Build a query string, dropping ``None``/empty values, URL-encoded."""

    clean = {k: str(v) for k, v in params.items() if v is not None and str(v) != ""}
    return urlencode(clean)


def url(path: str, params: dict) -> str:
    qs = query(params)
    return f"{path}?{qs}" if qs else path


def link(href: str, label: object, *, cls: str = "portal-link", extra: str = "") -> str:
    suffix = f" {extra}" if extra else ""
    return f'<a class="{esc(cls)}" href="{esc(href)}"{suffix}>{esc(label)}</a>'


def badge(label: object, *, variant: str = "", title: Optional[str] = None) -> str:
    cls = "portal-badge" + (f" {variant}" if variant else "")
    title_attr = f' title="{esc(title)}"' if title else ""
    return f'<span class="{esc(cls)}"{title_attr}>{esc(label)}</span>'


def visually_hidden(text: object) -> str:
    """Text carried for assistive technology only.

    Used as the non-colour carrier next to every semantic colour, so a state is
    never announced by hue alone (ACCESSIBILITY_SPEC §2).
    """

    return f'<span class="lp-visually-hidden">{esc(text)}</span>'


def lineage_badge() -> str:
    """The data-lineage qualifier.

    It is constant for a whole period, so since S12 it is rendered **once** in
    the context bar and never per ranking row (`EC-29`).
    """

    return (
        f'<span class="lp-badge-technical" title="{esc(LINEAGE_TOOLTIP)}">'
        f"{esc(LINEAGE_LABEL)}</span>"
    )


def empty_state(title: str, message: str) -> str:
    return (
        '<div class="portal-empty-state" role="status">'
        f"<h2>{esc(title)}</h2><p>{esc(message)}</p></div>"
    )


def error_callout(title: str, message: str) -> str:
    return (
        '<div class="portal-callout error" role="alert">'
        f"<strong>{esc(title)}</strong> {esc(message)}</div>"
    )


def state_block(title: str, message: str, *, variant: str = "", role: str = "status") -> str:
    """A named, explanatory in-place state — never a silently missing section."""

    cls = "eco-state" + (f" {variant}" if variant else "")
    return (
        f'<div class="{esc(cls)}" role="{esc(role)}">'
        f"<h3>{esc(title)}</h3><p>{esc(message)}</p></div>"
    )


def chips(items: Iterable[tuple[str, str, bool, Optional[int]]], *, aria_label: str) -> str:
    """Filter chips: iterable of ``(label, href, is_active, count)``.

    Chips, not tabs (`D-004`/`EC-12`): there is one ranking and the group is a
    filter over it. The active chip carries ``aria-current`` in addition to its
    tint and weight step, so the selection is not colour-only.
    """

    parts = [f'<div class="eco-chips" role="group" aria-label="{esc(aria_label)}">']
    for label, href, active, count in items:
        current = ' aria-current="true"' if active else ""
        count_html = (
            f'<span class="eco-chip-count">{esc(count)}</span>' if count is not None else ""
        )
        parts.append(
            f'<a class="eco-chip" href="{esc(href)}"{current}>'
            f"<span>{esc(label)}</span>{count_html}</a>"
        )
    parts.append("</div>")
    return "".join(parts)


def table(
    headers_html: str,
    body_rows_html: str,
    *,
    caption: Optional[str] = None,
    wrap_class: str = "eco-table-wrap",
    table_class: str = "eco-table",
) -> str:
    caption_html = "" if caption is None else f"<caption>{esc(caption)}</caption>"
    return (
        f'<div class="{esc(wrap_class)}"><table class="{esc(table_class)}">'
        f"{caption_html}<thead><tr>{headers_html}</tr></thead>"
        f"<tbody>{body_rows_html}</tbody></table></div>"
    )


def sortable_th(
    label: str,
    field: str,
    *,
    current_sort: Optional[str],
    current_direction: Optional[str],
    link_fn,
    cls: str = "",
    unit_caption: str = "",
    extra: str = "",
) -> str:
    """A sortable header cell; ``link_fn(sort, direction)`` returns the href.

    The direction caret is visible at rest on the sorted column and the sorted
    state is also carried by ``aria-sort``, so it survives without colour.
    """

    is_active = current_sort == field
    direction = (current_direction or "asc").lower()
    next_direction = "desc" if (is_active and direction == "asc") else "asc"
    indicator = ""
    aria_sort = ""
    if is_active:
        indicator = " ↓" if direction == "desc" else " ↑"
        aria_sort = f' aria-sort="{"descending" if direction == "desc" else "ascending"}"'
    href = link_fn(field, next_direction)
    class_attr = f' class="{esc(cls)}"' if cls else ""
    unit_html = f'<span class="eco-th-unit">{esc(unit_caption)}</span>' if unit_caption else ""
    return (
        f"<th{class_attr}{aria_sort}{extra}>"
        f'<a class="portal-link" href="{esc(href)}">{esc(label)}{esc(indicator)}</a>'
        f"{unit_html}</th>"
    )


def pagination(meta: dict, *, page_link_fn, size_options=(50, 100, 200), size_link_fn=None) -> str:
    """Render Previous/Next + page indicator + optional page-size links.

    ``page_link_fn(page)`` returns the href for a given 1-based page.
    ``size_link_fn(limit)`` returns the href for a given page size.
    """

    page = int(meta.get("page") or 1)
    limit = int(meta.get("limit") or 0)
    count = int(meta.get("count") or 0)
    total = meta.get("total_count")
    has_next = bool(meta.get("has_next"))

    buttons = []
    if page > 1:
        buttons.append(
            f'<a class="portal-button secondary" href="{esc(page_link_fn(page - 1))}">Poprzednia</a>'
        )
    if total is not None:
        total_pages = max(1, (int(total) + limit - 1) // limit) if limit else 1
        indicator = f"Strona {page} z {total_pages} · {int(total)} wierszy"
    else:
        indicator = f"Strona {page} · {count} wierszy"
    buttons.append(f'<span class="eco-muted">{esc(indicator)}</span>')
    if has_next:
        buttons.append(
            f'<a class="portal-button secondary" href="{esc(page_link_fn(page + 1))}">Następna</a>'
        )

    size_html = ""
    if size_link_fn is not None:
        size_links = []
        for size in size_options:
            current = ' aria-current="true"' if size == limit else ""
            size_links.append(
                f'<a class="eco-chip" href="{esc(size_link_fn(size))}"{current}>{esc(size)}</a>'
            )
        size_html = (
            '<div class="eco-chips" role="group" aria-label="Wierszy na stronę">'
            '<span class="eco-muted">Wierszy na stronę</span>' + "".join(size_links) + "</div>"
        )

    return (
        '<div class="portal-actions" style="margin-top:14px; flex-wrap:wrap; gap:10px;">'
        + "".join(buttons)
        + "</div>"
        + size_html
    )
