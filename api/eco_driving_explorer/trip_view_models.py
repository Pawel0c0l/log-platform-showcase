"""The full contributing-trip evidence surface.

Section 6 of `ECO-003` embeds the first page of this evidence; this page is
where it is sorted, filtered and paged. It is reached only with the separate
trip-details grant, and its column set is deliberately unchanged by S12:

* violation values are **Σ sums for that one trip** — a rate for a single trip
  would be meaningless — and every caption says so;
* there is **no per-trip contribution-to-score column**. The Eco score is a step
  function of aggregate normalized rates and is not additive across trips, so no
  per-trip contribution is derivable from the business logic. Inventing one, or
  estimating it proportionally, would fabricate a number the product cannot
  stand behind;
* there is **no link to the raw database row**. Exposing a raw ``record_id``
  needs dataset row-identifier configuration that is separately gated;
* the vehicle **registration number** is rendered, and it is the only vehicle
  attribute that is (`UI-20260820-01`, authorized 2026-08-20). It replaces the
  provider trip id, which operators do not use to identify a trip. No other
  route, location, vehicle, personal or raw source field is rendered; route and
  location still live behind their own grant.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, NamedTuple

from . import eco_view as V
from . import html as H
from .models import EVENT_METRIC_COLUMNS
from . import trip_export as X

if __package__ and __package__.startswith("api."):
    from ..portal_ui.i18n import t
    from ..portal_ui import formats as F
else:  # pragma: no cover - import-path parity with the rest of the package
    from portal_ui.i18n import t
    from portal_ui import formats as F

# Rendered, sortable columns of the trip table, in display order.
# ``provider_trip_id`` is deliberately absent: the provider trip id carries no
# operational meaning for the operators who read this evidence. It remains a
# valid server-side sort key in ``queries.TRIP_SORT_FIELDS`` and the stable
# ORDER BY tiebreaker, so a bookmarked ``sort=provider_trip_id`` URL still
# sorts and still renders a populated table — it simply has no header link.
SORT_LABELS = {
    # The plate leads, and it is sortable so trips can be grouped by vehicle
    # (`UI-20260820-02` C). Its ORDER BY fragment is a fixed entry in
    # `queries.TRIP_SORT_FIELDS`; nothing user-supplied reaches the clause.
    # One source of truth for this label: the ranking-entry detail page renders
    # the same column from the same i18n key, and the two screens must not drift.
    "vehicle_registration": t("eco.trip.registration"),
    "trip_start_ts": "Start podróży",
    "trip_end_ts": "Koniec podróży",
    "trip_distance_meters": "Dystans",
    "total_scoring_events": "Σ zdarzeń",
}
REGISTRATION_LABEL = SORT_LABELS["vehicle_registration"]
# `provider_trip_id` is deliberately still here even though the panel no longer
# offers that filter: a legacy URL that carries it is still honoured, so it must
# keep riding along on sort and pagination links. Dropping it here would make
# page 2 of a filtered legacy URL show a different result set than page 1.
FILTER_NAMES = ("trip_start_from", "trip_start_to", "provider_trip_id",
                "min_distance_meters", "max_distance_meters", "has_scoring_events")


def _fallback(value: Any, label: str = "—") -> str:
    return V.fallback(value, label)


def _hidden(name: str, value: Any) -> str:
    return "" if value is None or value == "" else f'<input type="hidden" name="{H.esc(name)}" value="{H.esc(value)}">'


#: The trips surface and the assets its markup requires, as one value. The
#: ranking table returns the same type: one grid, one pairing, one rule.
RenderedTrips = H.RenderedGrid


class ExportScope(NamedTuple):
    """What reproduces THIS table's rows, stated by the page that fetched them.

    THE DEFECT THIS EXISTS FOR. The export link used to be built by reading
    `period_key` back off the rendered `entry`. That works for the period-key
    page and silently fails for the ranking-basis page, whose entry carries a
    `period_key` only in one of its three states -- whole month AND persisted.
    In the other two the field is None, `H.url()` drops empty parameters, and
    the link went out with no period identity at all. The endpoint then refused
    it, so both buttons were dead on a page that rendered them normally.

    Reading an identity back off a rendered payload is a guess about how the
    rows were obtained. The page does not have to guess: it just called the
    service. So the page states the scope, and the renderer cannot construct
    one -- `render()` takes it as a required argument, which is what makes the
    basis page unable to inherit the period-key page's identity by accident.

    `params` are exactly what the export endpoint needs to re-run the same
    query. `filename_parts` name the scope in a downloaded file. An
    `unresolvable` scope is a first-class state: the bar refuses to offer a
    download rather than offering one that cannot answer.
    """

    params: dict
    filename_parts: tuple
    unresolvable: str = ""

    @staticmethod
    def for_period_key(identity: dict) -> "ExportScope":
        """The persisted-period page. Unchanged: this variant works today."""

        return ExportScope(dict(identity), ("assigned_id", "period_key"))

    @staticmethod
    def for_basis(identity: dict, *, month, weeks) -> "ExportScope":
        """The ranking-basis page: the month, and the weeks within it.

        `weeks` is absent for a whole-month basis, and the export must then mean
        the whole month -- which is what the screen showed, not a widening.
        """

        if not month:
            return ExportScope.refused(
                "eksport nie zna zakresu tego widoku (brak miesiąca)")
        params = {**identity, "month": month}
        if weeks:
            params["weeks"] = weeks
        # `period_key` never rides along on a basis export. It is absent in two
        # of the three basis states and, where present, names the persisted
        # MONTH -- so on a week selection it would widen the file to a scope the
        # screen never showed. Requirement 3: never fall back to a wider scope.
        params.pop("period_key", None)
        return ExportScope(params, ("assigned_id", "month", "weeks"))

    @staticmethod
    def refused(reason: str) -> "ExportScope":
        return ExportScope({}, (), reason)


def render(entry: dict, items: list[dict], meta: dict, *, ranking_context: dict,
           filter_values: dict, export_scope: ExportScope) -> RenderedTrips:
    identity = {name: entry.get(name) for name in ("client_code", "ranking_family", "period_key", "assigned_id")}
    page, limit = meta.get("page") or 1, meta.get("limit") or 50
    sort, direction = meta.get("sort") or "trip_start_ts", meta.get("direction") or "ASC"
    base = {**identity, **ranking_context, "page": page, "limit": limit, "sort": sort, "direction": direction}
    base.update({name: filter_values.get(name) for name in FILTER_NAMES})
    entry_href = H.url(H.RANKING_ENTRY_PATH, {
        **identity, "ranking_group": ranking_context.get("ranking_group"),
        "page": ranking_context.get("ranking_page"), "limit": ranking_context.get("ranking_limit"),
        "sort": ranking_context.get("ranking_sort"), "direction": ranking_context.get("ranking_direction"),
        "unit": ranking_context.get("unit"),
    })
    ranking_href = H.url(H.RANKINGS_PATH, {
        "client_code": identity["client_code"], "ranking_family": identity["ranking_family"],
        "period_key": identity["period_key"], "ranking_group": ranking_context.get("ranking_group"),
        "page": ranking_context.get("ranking_page"), "limit": ranking_context.get("ranking_limit"),
        "sort": ranking_context.get("ranking_sort"), "direction": ranking_context.get("ranking_direction"),
        "unit": ranking_context.get("unit"),
    })
    chart = entry.get("current_chart") or {}
    driver = chart.get("current_driver_name") or entry.get("assigned_id")
    breadcrumbs = (
        '<nav class="eco-breadcrumb" aria-label="Ścieżka">'
        + H.link(entry_href, t("eco.detail.back"), cls="portal-link")
        + '<span class="eco-breadcrumb-sep">|</span>'
        + f'<span>{H.esc(entry.get("client_code"))}</span>'
        + '<span class="eco-breadcrumb-sep">/</span>'
        + f'<span>{H.esc(entry.get("period_label") or "")}</span>'
        + '<span class="eco-breadcrumb-sep">/</span>'
        + f'<span>{H.esc(driver)}</span>'
        + '<span class="eco-breadcrumb-sep">/</span>'
        + f'<span class="eco-breadcrumb-current">{H.esc(t("eco.detail.trips"))}</span>'
        + '<span class="eco-breadcrumb-end">'
        + H.link(ranking_href, t("eco.ranking.title"), cls="portal-link")
        + "</span></nav>"
    )
    summary = _summary(entry, meta)
    form = render_filters(identity, ranking_context, filter_values, limit=limit, sort=sort, direction=direction)
    table = render_table(items, base, sort, direction, filters_active=bool(meta.get("filters_active")))
    pagination = H.pagination(
        meta,
        page_link_fn=lambda target: H.url(H.RANKING_ENTRY_TRIPS_PATH, {**base, "page": target}),
        size_options=(25, 50, 100, 200),
        size_link_fn=lambda size: H.url(H.RANKING_ENTRY_TRIPS_PATH, {**base, "page": 1, "limit": size}),
    )
    export_bar = _export_bar(export_scope, sort, direction, filter_values, meta)
    # The table decides the assets, because the table is what emits the markup
    # that needs them.
    return H.RenderedGrid(
        breadcrumbs + summary + form + export_bar + table.html + pagination,
        table.page_assets,
    )


def _export_bar(export_scope: "ExportScope", sort: str, direction: str,
                filter_values: dict, meta: dict) -> str:
    """Download the table as a spreadsheet.

    The links carry the FILTERS and the SORT and deliberately not `page`/`limit`:
    the file is the whole filtered view, so offering it from a paginated screen
    without saying so would be a quiet mismatch between what was on screen and
    what lands in the file. The caption says which it is, and says the row count,
    because that number is the one thing that tells a person whether they got
    what they meant before they start doing arithmetic on it.
    """

    total = meta.get("total_count")
    if not total:
        # Nothing to download. An enabled button producing a header-only file is
        # a worse answer than an absent one.
        return ""
    if export_scope.unresolvable:
        # FAIL CLOSED, before anything is offered. A button whose link cannot
        # name the rows above it is worse than no button: it looks like the
        # feature works. Say why, in the place the buttons would have been.
        return ('<div class="eco-export-bar">'
                f'<span class="eco-export-caption">{H.esc(export_scope.unresolvable)}</span>'
                '</div>')
    params = {**export_scope.params, "sort": sort, "direction": direction}
    params.update({name: filter_values.get(name) for name in FILTER_NAMES})
    filtered = bool(meta.get("filters_active"))
    phrase = "z bieżących filtrów" if filtered else "z całego widoku"
    caption = f"Pobierz wszystkie wiersze {phrase} ({total})"
    return (
        '<div class="eco-export-bar">'
        f'<span class="eco-export-caption">{H.esc(caption)}</span>'
        + H.link(H.url(H.RANKING_ENTRY_TRIPS_EXPORT_PATH, {**params, "format": "xlsx"}),
                 "Pobierz XLSX", cls="portal-button secondary")
        + H.link(H.url(H.RANKING_ENTRY_TRIPS_EXPORT_PATH, {**params, "format": "csv"}),
                 "Pobierz CSV", cls="portal-button secondary")
        + '</div>'
    )


def _summary(entry: dict, meta: dict) -> str:
    fields = (
        (t("eco.id.client"), entry.get("provider_display_name") or entry.get("client_code")),
        (t("eco.id.period_label"), entry.get("period_label")),
        (t("eco.id.period_start"), F.format_date(meta.get("period_start"))),
        (t("eco.id.period_end"), F.format_date(meta.get("period_end_exclusive"))),
        (t("eco.id.assigned_id"), entry.get("assigned_id")),
        ("Utrwalona liczba przejazdów", entry.get("trips_count")),
        ("Odtworzone przejazdy", meta.get("unfiltered_reconstructed_count")),
        ("Wiersze po filtrach", meta.get("total_count")),
    )
    cells = "".join(
        '<div class="eco-identity-cell">'
        f"<dt>{H.esc(label)}</dt><dd>{_fallback(value)}</dd></div>"
        for label, value in fields
    )
    return (
        '<section class="eco-panel">'
        f'<div class="eco-panel-head"><h2>{H.esc(t("eco.detail.trips"))}</h2>'
        f'<span class="eco-panel-caption">{H.lineage_badge()}</span></div>'
        f'<dl class="eco-identity-grid">{cells}</dl>'
        '<p class="eco-note">Te wiersze pochodzą z bieżącego modelu przypisań Eco Driving. '
        "Nie są niezmiennym zapisem dokładnych wierszy źródłowych użytych przy pierwotnym "
        "wyliczeniu rankingu; zgodne liczniki nie dowodzą niezmiennej linii danych.</p></section>"
    )


def render_filters(identity: dict, ranking_context: dict, values: dict, *, limit: Any, sort: Any, direction: Any) -> str:
    hidden = "".join(_hidden(name, value) for name, value in {
        **identity, **ranking_context, "limit": limit, "sort": sort, "direction": direction}.items())
    clear_href = H.url(H.RANKING_ENTRY_TRIPS_PATH, {
        **identity, **ranking_context, "page": 1, "limit": limit, "sort": sort, "direction": direction})
    selected = values.get("has_scoring_events")
    return (
        '<section class="eco-panel"><div class="eco-panel-head"><h2>Filtry</h2></div>'
        '<form method="get" action="'
        + H.esc(H.RANKING_ENTRY_TRIPS_PATH) + '">' + hidden + '<div class="portal-form-grid">'
        f'<label>Start od (włącznie)<input type="datetime-local" name="trip_start_from" value="{H.esc(values.get("trip_start_from"))}"></label>'
        f'<label>Start do (wyłącznie)<input type="datetime-local" name="trip_start_to" value="{H.esc(values.get("trip_start_to"))}"></label>'
        f'<label>Dystans min. (m)<input type="number" min="0" name="min_distance_meters" value="{H.esc(values.get("min_distance_meters"))}"></label>'
        f'<label>Dystans maks. (m)<input type="number" min="0" name="max_distance_meters" value="{H.esc(values.get("max_distance_meters"))}"></label>'
        '<label>Zdarzenia punktowane<select name="has_scoring_events">'
        f'<option value=""{" selected" if selected in (None, "") else ""}>Dowolne</option>'
        f'<option value="true"{" selected" if str(selected).lower() == "true" else ""}>Ze zdarzeniami</option>'
        f'<option value="false"{" selected" if str(selected).lower() == "false" else ""}>Bez zdarzeń</option>'
        '</select></label></div><div class="portal-actions" style="margin-top:12px">'
        '<button class="portal-button" type="submit">Zastosuj filtry</button>'
        + H.link(clear_href, "Wyczyść filtry", cls="portal-button secondary") + "</div></form></section>"
    )


class Column:
    """One column of the trips table: how it renders, and what it exports.

    THE POINT OF THIS CLASS IS THE SECURITY BOUNDARY, not tidiness.

    This screen sits under the Stage 5 contract (`docs/06_security.md`), which
    enumerates a safe row DTO and was amended once — deliberately, with owner
    authorization — to add the vehicle registration and nothing else. An export
    is exactly where "the page shows a safe subset" gets bypassed, by someone
    serialising the underlying row dict instead of the rendered view.

    So the page and the export are built from ONE list of columns. The export
    cannot carry a field the page does not show, because there is no second
    enumeration to drift from the first — not a list someone remembered to keep
    in sync, but the same objects rendering twice.

    `display` is what a person reads. `value` is what a spreadsheet gets: a real
    datetime, a real number. They differ on purpose — `4,00 km` pastes into Excel
    as text and cannot be summed.
    """

    __slots__ = ("key", "label", "export_label", "sort_field", "numeric", "display", "value",
                 "number_format")

    def __init__(self, key, label, display, value, *, sort_field=None, numeric=False,
                 export_label=None, number_format=None):
        self.key = key
        self.label = label
        #: The screen and the file can need different headings. The distance
        #: cell carries a unit on screen (`36,00 km`) and must NOT in a file, or
        #: the column becomes text — so the unit moves into the file's heading.
        self.export_label = export_label or label
        self.display = display
        self.value = value
        self.sort_field = sort_field
        self.numeric = numeric
        #: The XLSX display format for this column's cells, or `None` for the
        #: writer's default. A number keeps its value either way; this only
        #: stops `2.00` showing as `2` where the screen shows two places.
        self.number_format = number_format


def _status_text(trip: dict) -> str:
    return ("wiersz źródłowy dostępny" if trip.get("source_trip_present")
            else "brak bieżącego wiersza client_trips")


def _km_value(trip: dict):
    """Distance as a NUMBER in kilometres, unrounded beyond two places.

    The column header carries the unit, so the cell does not: a unit inside the
    cell is what makes the column text rather than arithmetic.
    """

    return V.km_number(trip.get("trip_distance_meters"))


def _dt_value(key: str):
    def read(trip: dict):
        raw = trip.get(key)
        if not raw:
            return None
        try:
            return datetime.fromisoformat(str(raw)).replace(tzinfo=None)
        except (TypeError, ValueError):
            return None
    return read


def columns() -> list:
    """The table's columns, in render order. The one enumeration.

    Built as a function rather than a constant because the metric set and the
    translated labels are resolved at call time.
    """

    cols = [
        Column("vehicle_registration", SORT_LABELS["vehicle_registration"],
               lambda tr: _fallback(tr.get("vehicle_registration")),
               lambda tr: tr.get("vehicle_registration"),
               sort_field="vehicle_registration"),
        Column("trip_start_ts", SORT_LABELS["trip_start_ts"],
               lambda tr: H.esc(F.format_datetime(tr.get("trip_start_ts"))),
               _dt_value("trip_start_ts"), sort_field="trip_start_ts", numeric=True),
        Column("trip_end_ts", SORT_LABELS["trip_end_ts"],
               lambda tr: H.esc(F.format_datetime(tr.get("trip_end_ts"))),
               _dt_value("trip_end_ts"), sort_field="trip_end_ts", numeric=True),
        Column("trip_distance_km", SORT_LABELS["trip_distance_meters"],
               lambda tr: H.esc(F.format_distance_km(tr.get("trip_distance_meters"))),
               _km_value, sort_field="trip_distance_meters", numeric=True,
               export_label=f'{SORT_LABELS["trip_distance_meters"]} (km)'),
        Column("total_scoring_events", SORT_LABELS["total_scoring_events"],
               lambda tr: _fallback(tr.get("total_scoring_events")),
               lambda tr: tr.get("total_scoring_events"),
               sort_field="total_scoring_events", numeric=True),
        Column("duration", t("eco.trip.duration"),
               lambda tr: H.esc(_duration(tr)), lambda tr: _duration(tr), numeric=True),
        Column("assignment_source", t("eco.trip.source"),
               lambda tr: _fallback(tr.get("assignment_source")),
               lambda tr: tr.get("assignment_source")),
        Column("source_state", "Stan źródła",
               lambda tr: H.esc(_status_text(tr)), _status_text),
    ]
    for metric in V.RANKING_METRIC_ORDER:
        cols.append(Column(
            metric, V.metric_label(metric, short=True),
            (lambda m: lambda tr: H.esc(int((tr.get("event_counts") or {}).get(m) or 0)))(metric),
            (lambda m: lambda tr: int((tr.get("event_counts") or {}).get(m) or 0))(metric),
            numeric=True))
    return cols


def render_table(items: list[dict], base: dict, sort: str, direction: str, *,
                 filters_active: bool) -> H.RenderedGrid:
    def sort_link(field: str, next_direction: str) -> str:
        return H.url(H.RANKING_ENTRY_TRIPS_PATH, {**base, "page": 1, "sort": field, "direction": next_direction})

    cols = columns()
    metric_keys = set(V.RANKING_METRIC_ORDER)
    sum_caption = t("eco.trip.sum_caption")

    def header(col) -> str:
        # `data-eco-column` is what makes a column part of the selection universe
        # (`data-grid-selection.js`). Every column the table shows is in it, and
        # nothing else exists to add -- the enumeration in `columns()` is the same
        # one the export uses, so selection cannot reach a field the screen omits.
        mark = f' data-eco-column="{H.esc(col.key)}"'
        if col.sort_field:
            # `sortable_th` adds no alignment class, so the plate stays free of
            # `eco-num` while gaining the same sort affordance as the rest.
            return H.sortable_th(col.label, col.sort_field, current_sort=sort,
                                 current_direction=direction, link_fn=sort_link,
                                 extra=mark)
        if col.key in metric_keys:
            return (f'<th scope="col" class="eco-num"{mark}>{H.esc(col.label)}'
                    f'<span class="eco-th-unit">{H.esc(sum_caption)}</span></th>')
        return f'<th{mark}>{H.esc(col.label)}</th>'

    def cell(col, trip) -> str:
        # `data-eco-copy` carries the MACHINE value, through the same function the
        # CSV writer uses. Copying a rectangle and downloading the same rows must
        # not yield two different numbers.
        copy = H.esc(X.text_cell(col.value(trip)))
        cls = ' class="eco-num"' if col.numeric else ""
        return (f'<td{cls} data-eco-column="{H.esc(col.key)}" '
                f'data-eco-copy="{copy}">{col.display(trip)}</td>')

    headers = "".join(header(col) for col in cols)
    rows = [
        "<tr>" + "".join(cell(col, trip) for col in cols) + "</tr>"
        for trip in items
    ]
    empty = ""
    if not rows:
        message = (
            "Żaden odtworzony wiersz nie pasuje do aktywnych filtrów."
            if filters_active
            else t("eco.detail.trips_empty")
        )
        empty = H.empty_state(t("eco.detail.trips_empty"), message)
    # The selection sheet must be an ANCESTOR of the table: the module writes
    # `data-db-selection="on"/"off"` onto it as the responsive cutoff moves, and
    # the cursor rule keys off that ancestor.
    selection = H.selection_sheet_attrs(H.selection_strings()) if rows else ""
    return H.RenderedGrid(
        '<section class="eco-panel">'
        f'<div class="eco-panel-head"><h2>{H.esc(t("eco.detail.trips"))}</h2>'
        f'<span class="eco-panel-caption">{H.esc(t("eco.trip.sum_note"))}</span></div>'
        + f'<div class="eco-select-sheet"{selection}>'
        + H.table(headers, "".join(rows))
        + (H.selection_footer() if rows else "")
        + '</div>'
        + empty
        # The zone is read from configuration rather than written into the
        # sentence: the previous version of this note promised ISO 8601 with an
        # offset, and stopped being true the moment the rendering changed.
        + '<p class="eco-note">Znaczniki czasu są podane w formacie dd.mm.rrrr gg:mm:ss '
        f'w strefie czasowej {H.esc(F.timezone_label())}. Brak wiersza '
        "źródłowego nie usuwa wiersza przypisania — w takim przypadku numer rejestracyjny nie jest "
        "znany i pozostaje pusty. Poza numerem rejestracyjnym pojazdu nie są prezentowane żadne dane "
        "trasy, lokalizacji, pojazdu, osobowe ani surowe pola źródłowe.</p></section>",
        # Host and module travel together, in both directions. An empty table
        # emits no host and so declares no module: the same rule the ranking
        # table follows, rather than each table having its own.
        H.ECO_DRIVING_SELECTABLE_PAGE_ASSETS if rows else H.ECO_DRIVING_PAGE_ASSETS,
    )


def _duration(trip: dict) -> str:
    from datetime import datetime

    start, end = trip.get("trip_start_ts"), trip.get("trip_end_ts")
    if not start or not end:
        return "—"
    try:
        started = datetime.fromisoformat(str(start))
        ended = datetime.fromisoformat(str(end))
    except ValueError:
        return "—"
    seconds = int((ended - started).total_seconds())
    if seconds < 0:
        return "—"
    return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}"


assert set(V.RANKING_METRIC_ORDER) == set(EVENT_METRIC_COLUMNS)
