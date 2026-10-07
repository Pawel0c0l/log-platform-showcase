"""The ranking table's columns: ONE enumeration for the screen and the file.

WHY THIS MODULE EXISTS. The ranking table used to build three things inline and
separately: its header cells, its data cells, and each cell's clipboard value.
Adding an export would have made a fourth, and a fourth list is how a file ends
up disagreeing with the table it was downloaded from — a column added to the
screen and forgotten in the export, or a copied rectangle and a downloaded file
carrying two different numbers for the same cell.

So the ranking follows the contract the contributing-trip table already proved
(`trip_view_models.Column`): a column knows how it renders, what it copies and
what it exports, and the three cannot drift because they are one object.

    display(entry) -> the whole `<td>`, because the score and metric cells carry
                      per-cell classes and severity state that the renderer must
                      not have to reconstruct
    value(entry)   -> the MACHINE value: a number a spreadsheet can sum, not the
                      formatted text the cell prints

The clipboard value is `trip_export.text_cell(value(entry))` — the same function
the CSV writer uses — so `Ctrl`+`C` over a rectangle and a downloaded file give
the same number for the same cell, by construction rather than by review.

WHAT IS NOT A COLUMN. The trailing action cell holds a link to the driver's
detail page. It is not in this list and has no key: a control that copied itself
into a spreadsheet would be a defect, and leaving it out of the enumeration
keeps it out of selection and out of the file at once.
"""
from __future__ import annotations

from typing import Any, Optional

from . import eco_view as V
from . import html as H
from . import trip_export as X
from .trip_view_models import Column

if __package__ and __package__.startswith("api."):
    from ..portal_ui.i18n import t
    from ..portal_ui import formats as F
else:  # pragma: no cover - import-path parity with the rest of the package
    from portal_ui.i18n import t
    from portal_ui import formats as F


class RankingColumn(Column):
    """A `Column` that also knows how its header cell is drawn.

    The ranking's headers carry alignment classes and a unit caption that the
    trips table does not need, and the export label has to absorb that caption:
    a metric column headed `Gwałtowne hamowanie` with `/ 100 km` printed under
    it becomes an unlabelled number the moment it leaves the screen.
    """

    __slots__ = ("header_class", "unit_caption")

    def __init__(self, key, label, display, value, *, sort_field=None,
                 numeric=False, export_label=None, number_format=None,
                 header_class="", unit_caption=""):
        super().__init__(key, label, display, value, sort_field=sort_field,
                         numeric=numeric, export_label=export_label,
                         number_format=number_format)
        self.header_class = header_class
        self.unit_caption = unit_caption


def _driver_name(entry: dict) -> Optional[str]:
    return (entry.get("current_chart") or {}).get("current_driver_name")


def qualified(entry: dict) -> bool:
    """Did this period reach the qualifying distance?

    The `w rankingu` tab lists the client's ranking POPULATION, which includes
    drivers the roster permitted who did not reach the threshold. Those rows are
    real and belong on the tab; their SCORE is not, and this predicate is what
    the score, the rating and the metric composition are gated on.
    """

    return str(entry.get("qualification_status") or "") == "QUALIFIED"


def _score_value(entry: dict):
    """What the score cell copies, as a number.

    `score_cell` prints `fmt_decimal(parsed, 0)` and copies the same string;
    this returns the underlying decimal so the file gets a number to sort and
    average rather than text. `text_cell` renders it back to that same string
    for the clipboard, which is what keeps the two in agreement.

    A below-threshold period returns nothing. The detail page renders the
    insufficient-distance state instead of a score and the trend chart blanks
    the same value for the same reason; a ranking row that printed the number
    anyway would be the back door those two are closed against — and it would
    also let a driver with eight clean kilometres sort above the fleet.
    """
    if not qualified(entry):
        return None
    return V.to_decimal(entry.get("eco_driving_score_total"))


def _rating_value(entry: dict) -> Optional[str]:
    return entry.get("ecodriving_rating_type") if qualified(entry) else None


def _qualification_text(entry: dict) -> str:
    return t("eco.qualification.met") if qualified(entry) else t("eco.qualification.not_met")


def _metric_column(metric: str, unit: str, unit_caption: str) -> RankingColumn:
    def value(entry: dict):
        # The metric columns ARE the score composition — section 4 of the detail
        # page, which a below-threshold period does not render either. A
        # `/ 100 km` coefficient extrapolated from under 100 km is precisely the
        # number the threshold exists to distrust, so the cell states nothing
        # rather than stating that.
        if not qualified(entry):
            return None
        counts = entry.get("event_counts") or {}
        rates = entry.get("metric_rates_per_100km") or {}
        # A number, not `metric_copy_value`'s text: the XLSX writer puts this
        # in the cell as-is, and `text_cell` turns it back into that same text
        # for the clipboard and the CSV.
        return V.metric_number(
            unit=unit, event_count=counts.get(metric), rate=rates.get(metric)
        )

    def display(entry: dict) -> str:
        if not qualified(entry):
            return (
                f'<td class="eco-num"{V.selection_attrs(metric, None)}>'
                f"{V.fallback(None)}</td>"
            )
        counts = entry.get("event_counts") or {}
        rates = entry.get("metric_rates_per_100km") or {}
        points = entry.get("metric_points") or {}
        losses = entry.get("metric_points_lost") or {}
        return V.metric_cell(
            metric_key=metric,
            displayed_value=V.metric_display_value(
                unit=unit, event_count=counts.get(metric), rate=rates.get(metric)
            ),
            points=points.get(metric),
            loss=losses.get(metric),
            rate=rates.get(metric),
            column=metric,
            copy=X.text_cell(value(entry)),
        )

    label = V.metric_label(metric, short=True)
    return RankingColumn(
        metric, label, display, value,
        numeric=True,
        header_class="eco-num",
        unit_caption=unit_caption,
        # The caption is part of what the number MEANS. On screen it sits under
        # the heading; in a file it has nowhere else to go.
        export_label=f"{label} ({unit_caption})" if unit_caption else label,
        number_format="0.00" if unit == V.UNIT_RATE else None,
    )


def _cell(key: str, value_fn, inner_fn, *, cls: str = ""):
    """A plain `<td>` whose clipboard value comes from the column's own `value`.

    The copy attribute is never written by hand anywhere in this module: it is
    `text_cell(value(entry))`, always, so no cell can copy something the file
    would not contain.
    """

    class_attr = f' class="{H.esc(cls)}"' if cls else ""

    def display(entry: dict) -> str:
        return (
            f"<td{class_attr}"
            f"{V.selection_attrs(key, X.text_cell(value_fn(entry)))}>"
            f"{inner_fn(entry)}</td>"
        )

    return display


def columns(unit: str) -> list[RankingColumn]:
    """The ranking's columns, in render order, for the selected unit.

    A function rather than a constant: the metric set, the translated labels and
    the unit caption are all resolved at call time, and the unit changes what
    every metric column means.
    """
    unit_caption = t(V.UNIT_LABELS[unit])

    def col(key, label, value_fn, inner_fn, *, cls="", sort_field=None,
            numeric=False, export_label=None, header_class="") -> RankingColumn:
        return RankingColumn(
            key, label, _cell(key, value_fn, inner_fn, cls=cls), value_fn,
            sort_field=sort_field, numeric=numeric, export_label=export_label,
            header_class=header_class,
        )

    cols: list[RankingColumn] = [
        col("ranking_position", t("eco.col.position"),
            lambda e: e.get("ranking_position"),
            lambda e: V.fallback(e.get("ranking_position")),
            cls="eco-cell-position", sort_field="ranking_position",
            header_class="eco-cell-position"),
        col("driver", t("eco.col.driver"),
            _driver_name,
            lambda e: V.fallback(_driver_name(e))),
        col("assigned_id", t("eco.col.driver_tag"),
            lambda e: e.get("assigned_id"),
            lambda e: H.esc(e.get("assigned_id")),
            cls="eco-num", sort_field="assigned_id", numeric=True),
        # The score cell builds its own `<td>` — it carries a proportional bar
        # and a band class — and `score_cell` already copies the same number
        # this column exports.
        RankingColumn(
            "eco_driving_score_total", t("eco.col.score"),
            # `score_cell(None)` is already the muted placeholder, so the
            # unqualified case needs no second rendering path.
            lambda e: V.score_cell(_score_value(e), column="eco_driving_score_total"),
            _score_value,
            sort_field="eco_driving_score_total", numeric=True,
            header_class="eco-cell-score"),
        col("rating", t("eco.col.rating"),
            # The rating is a band of the score, so it goes wherever the score
            # goes: withholding one and printing the other would state the
            # same withheld number in words.
            _rating_value,
            lambda e: V.rating_badge(_rating_value(e))),
        col("total_distance_km", t("eco.col.distance"),
            lambda e: V.km_number(e.get("total_distance_meters")),
            lambda e: H.esc(F.format_distance_km(e.get("total_distance_meters"))),
            # The cell prints `36,00 km`; the file gets the number and the
            # heading gets the unit, or the column stops being arithmetic. The
            # label is already `Dystans (km)`, so it IS the export heading —
            # appending the unit again exported `Dystans (km) (km)`.
            cls="eco-num", sort_field="total_distance_meters", numeric=True),
        col("trips_count", t("eco.col.trips"),
            lambda e: e.get("trips_count"),
            lambda e: H.esc(V.fmt_int(e.get("trips_count"))),
            cls="eco-num", sort_field="trips_count", numeric=True),
    ]
    cols.extend(_metric_column(metric, unit, unit_caption) for metric in V.RANKING_METRIC_ORDER)
    cols.append(
        col("qualification", t("eco.col.qualification"),
            _qualification_text,
            lambda e: H.esc(_qualification_text(e)))
    )
    return cols


def header_cells(cols: list[RankingColumn], *, sort, direction, sort_link) -> str:
    """The header row, from the same list that renders the data.

    `data-eco-column` on the header is what puts a column in the selection
    universe: `data-grid-selection.js` reads the column order from `thead` and
    matches cells by name. Every column that shows data is here and nothing
    else is, which is the same statement as "every column here is exported".
    """
    cells = []
    for col in cols:
        mark = V.selection_attrs(col.key, None)
        if col.sort_field:
            cells.append(H.sortable_th(
                col.label, col.sort_field, current_sort=sort, current_direction=direction,
                link_fn=sort_link, cls=col.header_class, extra=mark,
            ))
            continue
        class_attr = f' class="{H.esc(col.header_class)}"' if col.header_class else ""
        unit_html = (
            f'<span class="eco-th-unit">{H.esc(col.unit_caption)}</span>'
            if col.unit_caption else ""
        )
        cells.append(f"<th{class_attr}{mark}>{H.esc(col.label)}{unit_html}</th>")
    return "".join(cells)


def row_cells(cols: list[RankingColumn], entry: dict) -> str:
    return "".join(col.display(entry) for col in cols)


def export_bar(scope, *, sort, direction, unit, search, total) -> str:
    """Download the ranking as a spreadsheet, at the current view.

    The links carry the SCOPE the page resolved, plus the sort, the unit and the
    search — and deliberately not `page`/`limit`, because the file is the whole
    filtered view rather than the visible page. The caption says which it is and
    says the row count, since that number is the one thing that tells a person
    whether they got what they meant.

    An unresolvable scope produces the sentence and no buttons. A button whose
    link cannot name the rows above it is worse than no button: it looks like
    the feature works.
    """
    if not total:
        # Nothing to download. An enabled button producing a header-only file is
        # a worse answer than an absent one.
        return ""
    if scope.unresolvable:
        return ('<div class="eco-export-bar">'
                f'<span class="eco-export-caption">{H.esc(scope.unresolvable)}</span>'
                '</div>')
    params = {**scope.params, "sort": sort, "direction": direction,
              "unit": unit, "search": search}
    caption = f"Pobierz wszystkie wiersze bieżącego widoku ({total})"
    return (
        '<div class="eco-export-bar">'
        f'<span class="eco-export-caption">{H.esc(caption)}</span>'
        + H.link(H.url(H.RANKINGS_EXPORT_PATH, {**params, "format": "xlsx"}),
                 "Pobierz XLSX", cls="portal-button secondary")
        + H.link(H.url(H.RANKINGS_EXPORT_PATH, {**params, "format": "csv"}),
                 "Pobierz CSV", cls="portal-button secondary")
        + '</div>'
    )
