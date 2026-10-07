"""Shared Eco Driving view helpers: units, severity, chips, histogram, trend.

Pure presentation. Every business value arrives already resolved from the
service envelope; nothing here queries, scores or classifies. Two rules are
enforced structurally rather than by convention:

* **The display unit and the semantic colour are separate inputs.**
  :func:`metric_cell` takes the value to *print* and, separately, the persisted
  point/loss values to *classify by*. The classification path never receives the
  printed value, so switching the unit to ``Σ suma`` cannot move a colour. This
  is the invariant the adversarial regression fixture is built to break.

* **Rating bands are the production three-band vocabulary.** ``bezpieczny`` /
  ``akceptowalny`` / ``niebezpieczny`` come from the persisted
  ``ecodriving_rating_type`` column and are mapped to a CSS class only. The
  four-band vocabulary in the visual design is presentation sample data and is
  deliberately not adopted; the bands are business-significant and are shared
  with Eco reporting and e-mail.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Optional

from . import html as H
from .models import EVENT_METRIC_COLUMNS
from .score_presentation import (
    SEVERITY_BAD,
    SEVERITY_MARKER,
    SEVERITY_NONE,
    SEVERITY_OK,
    SEVERITY_WARN,
    applicable_ladder_step,
    metric_severity,
)

if __package__ and __package__.startswith("api."):
    from ..portal_ui.i18n import t
    from ..portal_ui import formats as F
else:  # pragma: no cover - import-path parity with the rest of the package
    from portal_ui.i18n import t
    from portal_ui import formats as F

# --- display units -----------------------------------------------------------

UNIT_RATE = "rate"
UNIT_SUM = "sum"
UNITS = (UNIT_RATE, UNIT_SUM)
DEFAULT_UNIT = UNIT_RATE


def normalize_unit(value: Any) -> str:
    """Coerce the ``unit`` query parameter; anything unknown falls back to rate.

    The unit is view state only. It never reaches a query, a scoring call or a
    ranking order, so an invalid value can degrade silently to the default
    instead of failing a page.
    """

    text = str(value or "").strip().lower()
    return text if text in UNITS else DEFAULT_UNIT


UNIT_LABELS = {UNIT_RATE: "eco.unit.rate", UNIT_SUM: "eco.unit.sum"}

# --- metric vocabulary -------------------------------------------------------

# Column order is the approved one (`ECO-001`): braking, acceleration, cornering,
# idling, high RPM, then the three speed thresholds. It differs from the
# canonical scoring order in `REQUIRED_METRICS`, which stays untouched.
RANKING_METRIC_ORDER: tuple[str, ...] = (
    "harsh_braking_events",
    "harsh_acceleration_events",
    "harsh_turning_events",
    "idle_events",
    "overrev_events_count",
    "speeding_140_160_count",
    "speeding_160_170_count",
    "speeding_170_plus_count",
)

METRIC_LABEL_KEYS = {
    "harsh_braking_events": ("eco.metric.harsh_braking", "eco.metric.harsh_braking_short"),
    "harsh_acceleration_events": (
        "eco.metric.harsh_acceleration",
        "eco.metric.harsh_acceleration_short",
    ),
    "harsh_turning_events": ("eco.metric.harsh_turning", "eco.metric.harsh_turning_short"),
    "idle_events": ("eco.metric.idle", "eco.metric.idle_short"),
    "overrev_events_count": ("eco.metric.overrev", "eco.metric.overrev_short"),
    "speeding_140_160_count": ("eco.metric.speeding_140", "eco.metric.speeding_140_short"),
    "speeding_160_170_count": ("eco.metric.speeding_160", "eco.metric.speeding_160_short"),
    "speeding_170_plus_count": ("eco.metric.speeding_170", "eco.metric.speeding_170_short"),
}

assert set(RANKING_METRIC_ORDER) == set(EVENT_METRIC_COLUMNS)


def metric_label(metric_key: str, *, short: bool = False) -> str:
    keys = METRIC_LABEL_KEYS.get(metric_key)
    if keys is None:
        return metric_key
    return t(keys[1] if short else keys[0])


SEVERITY_WORD_KEYS = {
    SEVERITY_OK: "eco.severity.ok",
    SEVERITY_WARN: "eco.severity.warn",
    SEVERITY_BAD: "eco.severity.bad",
    SEVERITY_NONE: "eco.severity.none",
}

# --- number formatting -------------------------------------------------------


def to_decimal(value: Any) -> Optional[Decimal]:
    """Parse a serialized decimal string back into ``Decimal``, or ``None``."""

    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def fmt_decimal(value: Any, places: int = 2) -> str:
    """Polish decimal formatting: comma separator, fixed places."""

    parsed = to_decimal(value)
    if parsed is None:
        return "—"
    quantum = Decimal(1).scaleb(-places) if places > 0 else Decimal(1)
    return format(parsed.quantize(quantum), "f").replace(".", ",")


def fmt_int(value: Any) -> str:
    """Integer with a non-breaking thin space as the thousands separator."""

    if value is None or value == "":
        return "—"
    try:
        number = int(Decimal(str(value)))
    except (InvalidOperation, ValueError, TypeError):
        return H.esc(value)
    return f"{number:,}".replace(",", " ")


def fmt_signed(value: Any) -> str:
    parsed = to_decimal(value)
    if parsed is None:
        return "—"
    whole = parsed.quantize(Decimal(1)) if parsed == parsed.to_integral_value() else parsed
    if whole > 0:
        return f"+{format(whole, 'f')}"
    return format(whole, "f")


def fallback(value: Any, label: str = "—") -> str:
    if value is None or value == "":
        return f'<span class="eco-muted">{H.esc(label)}</span>'
    return H.esc(value)


# --- semantic severity -------------------------------------------------------


def selection_attrs(column: Optional[str], copy: Optional[str]) -> str:
    """The two attributes that put a cell into the selection universe.

    OPT-IN, and deliberately so. `data-grid-selection.js` decides what is
    selectable purely from `data-eco-column`, so a cell builder that emitted it
    unconditionally would silently enrol every table that reuses the builder —
    including the four Eco tables that are not selection surfaces. A caller that
    wants selection asks for it by naming the column.

    ``copy`` is the clipboard value and must be what the cell already shows.
    Omitting it entirely means "empty", which is what the module reads a missing
    attribute as, and is the right answer for a placeholder cell.
    """

    if not column:
        return ""
    attrs = f' data-eco-column="{H.esc(column)}"'
    if copy not in (None, ""):
        attrs += f' data-eco-copy="{H.esc(copy)}"'
    return attrs


def metric_cell(
    *,
    metric_key: str,
    displayed_value: str,
    points: Any,
    loss: Any,
    rate: Any = None,
    cls_extra: str = "",
    column: Optional[str] = None,
    copy: Optional[str] = None,
) -> str:
    """One metric cell: printed value + coefficient-derived semantic state.

    ``displayed_value`` is already-formatted text and is used for **nothing**
    except printing. The severity comes from ``points``/``loss`` — persisted
    values the aggregation job derived from the rounded ``/ 100 km``
    coefficient — falling back to re-entering the scoring ladder with ``rate``
    when a row carries no point column. There is no code path in which the
    printed number influences the class, which is what keeps ``Σ suma`` mode
    honest.
    """

    points_dec = to_decimal(points)
    loss_dec = to_decimal(loss)
    if points_dec is None and loss_dec is None and rate is not None:
        step = applicable_ladder_step(metric_key, to_decimal(rate))
        points_dec = None if step is None else Decimal(step.points)
    severity = metric_severity(metric_key, points=points_dec, loss=loss_dec)
    marker = SEVERITY_MARKER.get(severity, "")
    marker_html = (
        f'<span class="eco-severity-marker" aria-hidden="true">{H.esc(marker)}</span>'
        if marker
        else ""
    )
    word = t(SEVERITY_WORD_KEYS.get(severity, "eco.severity.none"))
    classes = f"eco-num eco-metric-cell is-{severity}"
    if cls_extra:
        classes = f"{classes} {cls_extra}"
    return (
        f'<td class="{H.esc(classes)}"{selection_attrs(column, copy)} '
        f'data-severity="{H.esc(severity)}">'
        f"{displayed_value}{marker_html}{H.visually_hidden(word)}</td>"
    )


def metric_display_value(
    *,
    unit: str,
    event_count: Any,
    rate: Any,
) -> str:
    """The number the cell prints for the selected unit.

    Both sides already exist in the persisted stats row: ``Σ`` is the stored
    event counter and ``/ 100 km`` is the stored normalized coefficient. The
    Explorer derives neither.
    """

    return H.esc(metric_copy_value(unit=unit, event_count=event_count, rate=rate))


def metric_copy_value(
    *,
    unit: str,
    event_count: Any,
    rate: Any,
) -> str:
    """The same number, unescaped, for the clipboard.

    The printed cell also carries a severity marker and a visually-hidden
    severity word, so its text content reads ``"0,00bez straty punktów"``.
    Copying that would paste an annotation as if it were data. This returns the
    value alone, from the same expression that prints it, so the two cannot
    drift into disagreeing about what the cell says.

    ``Σ`` mode returns a BARE integer rather than the printed one: the printed
    form separates thousands with a narrow no-break space, which Polish Excel
    does not read as a number. `trip_export.text_cell` is the repository's
    definition of "a value in the form a Polish Excel reads", and the trips
    table's clipboard already follows it; this follows the same rule rather than
    inventing a third one for the same gesture.
    """

    if unit == UNIT_SUM:
        return "" if event_count in (None, "") else str(event_count)
    parsed = to_decimal(rate)
    return "" if parsed is None else fmt_decimal(parsed, 2)


def metric_number(
    *,
    unit: str,
    event_count: Any,
    rate: Any,
):
    """The same number as a NUMBER: ``int`` for ``Σ``, a two-place ``float`` for ``/ 100 km``.

    `metric_copy_value` is text, which is right for a clipboard and wrong for a
    workbook: ``"2,00"`` written into a cell is a number stored as text, and the
    column stops summing. This is the machine form behind that text, rounded to
    the same quantum `fmt_decimal` uses, so `trip_export.text_cell` — whose
    float rule `km_number` already relies on — renders it back to exactly the
    string `metric_copy_value` returns.
    """

    if unit == UNIT_SUM:
        return None if event_count in (None, "") else int(event_count)
    parsed = to_decimal(rate)
    return None if parsed is None else float(parsed.quantize(Decimal("0.01")))


def km_number(metres: Any):
    """Distance as a NUMBER in kilometres — the one definition, for every use.

    The trips table's clipboard and spreadsheet export and the ranking table's
    clipboard all read distance through here, so a rectangle copied out of
    either table and a downloaded file cannot carry two different numbers. The
    unit is never included: the column header carries it, and a unit inside the
    cell is what makes a column text rather than arithmetic.
    """

    if metres in (None, ""):
        return None
    try:
        return round(int(metres) / 1000, F.DISTANCE_DECIMALS)
    except (TypeError, ValueError):
        return None


# --- rating band -------------------------------------------------------------

# The production classification, verbatim. `eco_scoring.RATING_THRESHOLDS` owns
# the boundaries; this map owns only the CSS class.
RATING_CLASS = {
    "bezpieczny": "is-safe",
    "akceptowalny": "is-acceptable",
    "niebezpieczny": "is-dangerous",
}


def rating_badge(rating: Any) -> str:
    """The persisted three-band rating as a word badge.

    The word is the state: the badge is legible with colour removed, and an
    ``EXCLUDED`` ranking group never replaces it — group membership and driving
    classification are different facts about the same driver.
    """

    if not rating:
        return '<span class="eco-muted">—</span>'
    text = str(rating)
    cls = RATING_CLASS.get(text.strip().lower(), "")
    class_attr = f"eco-rating {cls}".strip()
    return f'<span class="{H.esc(class_attr)}">{H.esc(text)}</span>'


def score_band_class(score: Any) -> str:
    """Score-bar hue, keyed to the repository's own rating boundaries."""

    from .eco_scoring import RATING_THRESHOLDS

    parsed = to_decimal(score)
    if parsed is None:
        return ""
    ordered = list(RATING_THRESHOLDS)
    if ordered and parsed >= ordered[0][0]:
        return "is-good"
    if len(ordered) > 1 and parsed >= ordered[1][0]:
        return "is-mid"
    return "is-low"


def score_cell(score: Any, *, max_score: int = 100, column: Optional[str] = None) -> str:
    """Score with a proportional bar, at the same weight as the position.

    The bar is decoration for the number, so a selected cell copies the number.
    """

    parsed = to_decimal(score)
    if parsed is None:
        return (
            f'<td class="eco-cell-score"{selection_attrs(column, None)}>'
            '<span class="eco-muted">—</span></td>'
        )
    ratio = max(Decimal(0), min(Decimal(1), parsed / Decimal(max_score))) if max_score else Decimal(0)
    percent = int(ratio * 100)
    band = score_band_class(parsed)
    return (
        f'<td class="eco-cell-score"{selection_attrs(column, fmt_decimal(parsed, 0))}>'
        '<span class="eco-score-value">'
        f'<span class="eco-score-number">{H.esc(fmt_decimal(parsed, 0))}</span>'
        '<span class="eco-score-bar" aria-hidden="true">'
        f'<span class="eco-score-fill {H.esc(band)}" style="width:{percent}%"></span>'
        "</span></span></td>"
    )


# --- unit toggle -------------------------------------------------------------


def unit_toggle(current_unit: str, *, href_for_unit) -> str:
    """The `/ 100 km ⇄ Σ suma` switch.

    Two real links, so it works with scripting disabled and is keyboard
    operable by default. The current option carries ``aria-current`` — the state
    is exposed programmatically and is not communicated by fill colour alone.
    """

    options = []
    for unit in UNITS:
        label = t(UNIT_LABELS[unit])
        current = ' aria-current="true"' if unit == current_unit else ""
        options.append(
            f'<a class="eco-unit-option" href="{H.esc(href_for_unit(unit))}"{current}>'
            f"{H.esc(label)}</a>"
        )
    return (
        '<div class="eco-unit-toggle">'
        f'<span class="eco-unit-label" id="eco-unit-label">{H.esc(t("eco.unit.label"))}</span>'
        '<div class="eco-unit-segments" role="group" aria-labelledby="eco-unit-label">'
        + "".join(options)
        + "</div></div>"
    )


# --- fleet distribution histogram -------------------------------------------


GROUP_LABEL_KEYS = {
    "INCLUDED": "eco.group.included",
    "EXCLUDED": "eco.group.excluded",
    "UNKNOWN_DRIVER": "eco.group.unknown",
}


def distribution_heading(distribution: dict, *, subject: bool = False) -> str:
    """Name the universe the plot is actually over.

    A histogram restricted to one ranking group is **not** the fleet, and
    labelling it as one would misdescribe every bar in it. With no group filter
    the universe is the whole ranked population, which the neutral wording names
    truthfully; with a filter the approved group label is used verbatim.
    """

    group = (distribution or {}).get("ranking_group")
    key = GROUP_LABEL_KEYS.get(str(group or ""))
    if key is None:
        return t("eco.detail.fleet_position" if subject else "eco.distribution.title")
    return t(
        "eco.detail.fleet_position_group" if subject else "eco.distribution.title_group",
        group=t(key),
    )


def histogram(
    distribution: dict,
    *,
    subject_score: Any = None,
    title: Optional[str] = None,
) -> str:
    """The fleet score histogram, over one client + period + ranking group.

    Every bar states its own numbers in its accessible name, and the whole plot
    is summarised in text above it, so the distribution is readable without
    seeing the bars at all. The subject's bar is marked by an accent **and** by
    the axis label naming the subject's score.
    """

    heading = title or distribution_heading(distribution or {})
    bins = (distribution or {}).get("bins") or []
    total = int((distribution or {}).get("total_count") or 0)
    if not bins or total <= 0:
        return (
            '<section class="eco-histogram" aria-label="' + H.esc(heading) + '">'
            f'<div class="eco-histogram-head"><span class="eco-eyebrow">{H.esc(heading)}</span></div>'
            f'<p class="eco-muted">{H.esc(t("eco.distribution.empty"))}</p></section>'
        )

    subject = to_decimal(subject_score)
    subject_bin = None
    if subject is not None:
        for item in bins:
            low = Decimal(str(item.get("lower_bound")))
            high = Decimal(str(item.get("upper_bound")))
            is_last = item is bins[-1]
            if (low <= subject < high) or (is_last and subject >= low):
                subject_bin = int(item.get("index"))
                break

    peak = max(int(item.get("count") or 0) for item in bins) or 1
    bars = []
    for item in bins:
        count = int(item.get("count") or 0)
        low = item.get("lower_bound")
        high = item.get("upper_bound")
        height = 0 if count == 0 else max(6, int(round(count * 100 / peak)))
        marked = " is-subject" if subject_bin is not None and int(item.get("index")) == subject_bin else ""
        bars.append(
            f'<span class="eco-histogram-bar{marked}" style="height:{height}%" '
            f'title="{H.esc(t("eco.distribution.bin_aria", low=low, high=high, count=count))}"></span>'
        )

    median = (distribution or {}).get("median_score")
    mean = (distribution or {}).get("mean_score")
    stats = (
        f'{H.esc(t("eco.distribution.median"))} {H.esc(fmt_decimal(median, 0))} · '
        f'{H.esc(t("eco.distribution.mean"))} {H.esc(fmt_decimal(mean, 0))}'
    )
    axis_low = bins[0].get("lower_bound")
    axis_high = bins[-1].get("upper_bound")
    subject_label = (
        f'<span>{H.esc(fmt_decimal(subject, 0))} ▲</span>' if subject is not None else "<span></span>"
    )
    return (
        f'<section class="eco-histogram" aria-label="{H.esc(heading)}">'
        '<div class="eco-histogram-head">'
        f'<span class="eco-eyebrow">{H.esc(heading)}</span>'
        f'<span class="eco-histogram-stats">{stats}</span>'
        "</div>"
        f'<div class="eco-histogram-plot" role="img" aria-label="'
        f'{H.esc(t("eco.distribution.summary", count=total))} · {H.esc(t("eco.distribution.median"))} '
        f'{H.esc(fmt_decimal(median, 0))}">' + "".join(bars) + "</div>"
        '<div class="eco-histogram-axis">'
        f"<span>{H.esc(axis_low)}</span>{subject_label}<span>{H.esc(axis_high)}</span>"
        "</div>"
        f'<p class="eco-note">{H.esc(t("eco.distribution.universe_note"))}</p>'
        "</section>"
    )


# --- trend -------------------------------------------------------------------


def trend_chart(points: Iterable[dict]) -> str:
    """The driver's own score across the comparable periods that exist.

    A period with no persisted row is simply not in ``points`` and therefore
    draws no column: the chart has no notion of a zero-filled period, so it
    cannot invent one.
    """

    rows = [point for point in points or []]
    if not rows:
        return f'<p class="eco-muted">{H.esc(t("eco.detail.trend_empty"))}</p>'

    scores = [to_decimal(row.get("eco_driving_score_total")) for row in rows]
    known = [score for score in scores if score is not None]
    peak = max(known) if known else Decimal(1)
    floor_value = min(known) if known else Decimal(0)
    span = peak - floor_value
    if span <= 0:
        span = Decimal(1)

    columns = []
    for row, score in zip(rows, scores):
        current = " is-current" if row.get("is_current") else ""
        if score is None:
            value_html = '<span class="eco-trend-value eco-muted">—</span>'
            height = 0
        else:
            value_html = f'<span class="eco-trend-value">{H.esc(fmt_decimal(score, 0))}</span>'
            height = 12 + int(((score - floor_value) / span) * Decimal(88))
        columns.append(
            f'<div class="eco-trend-col{current}">'
            f"{value_html}"
            f'<span class="eco-trend-bar" style="height:{height}%" aria-hidden="true"></span>'
            f'<span class="eco-trend-label">{H.esc(row.get("period_label") or "")}</span>'
            "</div>"
        )

    return (
        f'<div class="eco-trend" role="img" aria-label="{H.esc(_trend_aria(rows))}">'
        + "".join(columns)
        + "</div>"
    )


def _trend_aria(rows: list[dict]) -> str:
    parts = []
    for row in rows:
        score = row.get("eco_driving_score_total")
        parts.append(f'{row.get("period_label") or ""}: {fmt_decimal(score, 0)}')
    return "; ".join(parts)


def trend_footnotes(points: Iterable[dict]) -> str:
    """Change versus the previous period, plus best and worst in the window.

    All three are read off the returned periods; none is extrapolated, and each
    is omitted rather than guessed when the window does not contain it.
    """

    rows = [row for row in points or [] if to_decimal(row.get("eco_driving_score_total")) is not None]
    if not rows:
        return ""
    notes = []
    if len(rows) >= 2:
        latest = to_decimal(rows[-1]["eco_driving_score_total"])
        previous = to_decimal(rows[-2]["eco_driving_score_total"])
        notes.append(
            f'<span>{H.esc(t("eco.detail.trend_change"))} '
            f"<strong>{H.esc(fmt_signed(latest - previous))}</strong></span>"
        )
    best = max(rows, key=lambda row: to_decimal(row["eco_driving_score_total"]))
    worst = min(rows, key=lambda row: to_decimal(row["eco_driving_score_total"]))
    notes.append(
        f'<span>{H.esc(t("eco.detail.trend_best"))} '
        f'<strong>{H.esc(best.get("period_label"))}</strong> · '
        f'{H.esc(fmt_decimal(best.get("eco_driving_score_total"), 0))}</span>'
    )
    notes.append(
        f'<span>{H.esc(t("eco.detail.trend_worst"))} '
        f'<strong>{H.esc(worst.get("period_label"))}</strong> · '
        f'{H.esc(fmt_decimal(worst.get("eco_driving_score_total"), 0))}</span>'
    )
    return '<div class="eco-trend-foot">' + "".join(notes) + "</div>"
