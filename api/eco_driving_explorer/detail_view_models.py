"""Driver drill-down rendering (`ECO-003`).

Six sections, in the approved fixed order, each answering one question:

1. **Wynik i pozycja** — where do I stand?
2. **Trend wyniku** — am I improving?
3. **Tożsamość wpisu i okres** — what exactly am I looking at?
4. **Z czego składa się wynik** — what is hurting my score?
5. **Wkład tygodni** — which snapshot moved it?
6. **Przejazdy w podstawie rankingu** — show me the evidence.

Two panels the as-is product had are deliberately **absent** and must not be
recreated under another heading (`D-003` / `EC-30`): the reconciliation panel
and the score-definition / "how the score was calculated" panel with its
threshold-band table. Only the panels were removed — the reconciliation service
and ``ScoreDefinition`` are untouched and still serve the JSON API.

Three domain rules shape what is rendered here:

* **The 100 km threshold is a reporting-period rule.** When the persisted
  ``qualification_status`` is not ``QUALIFIED``, sections 1 and 4 render the
  truthful insufficient-distance state instead of a score, a position and a
  composition. The threshold applies to the whole period's distance; it is
  never applied again to an individual day, and section 6 keeps listing every
  contributing trip, short ones included.
* **Weekly periods are cumulative.** Section 5 shows the persisted
  month-to-date snapshots as a running total with a per-step delta. Nothing is
  summed, because two cumulative snapshots share a prefix and adding them would
  double-count it.
* **Colour follows the coefficient.** Every metric cell classifies from the
  persisted points/loss the aggregation job derived from the ``/ 100 km``
  coefficient, so the ``Σ suma`` display mode changes numbers and never states.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional

from . import eco_view as V
from . import html as H
from .eco_scoring import MIN_QUALIFYING_DISTANCE_METERS, REQUIRED_METRICS
from .score_presentation import (
    SEVERITY_BAD,
    SEVERITY_WARN,
    applicable_ladder_step,
    composition_sort_key,
    loss_share_percent,
    metric_max_points,
    metric_severity,
    total_loss_magnitude,
)

if __package__ and __package__.startswith("api."):
    from ..portal_ui.i18n import t
    from ..portal_ui import formats as F
else:  # pragma: no cover - import-path parity with the rest of the package
    from portal_ui.i18n import t
    from portal_ui import formats as F

GROUP_LABEL_KEYS = {
    "INCLUDED": "eco.group.included",
    "EXCLUDED": "eco.group.excluded",
    "UNKNOWN_DRIVER": "eco.group.unknown",
}
GROUPS = tuple(GROUP_LABEL_KEYS)
SORTS = {
    "ranking_position", "assigned_id", "eco_driving_score_total",
    "total_distance_meters", "total_kilometers", "trips_count",
}

QUALIFICATION_LABEL_KEYS = {
    "QUALIFIED": "eco.value.qualified",
    "LOW_DISTANCE": "eco.value.low_distance",
    "NO_DISTANCE": "eco.value.no_distance",
}

METADATA_SOURCE_KEYS = {
    "CURRENT_CHART": "eco.value.chart_current",
    "NONE": "eco.value.chart_none",
}


def group_label(group: Any) -> str:
    key = GROUP_LABEL_KEYS.get(str(group or ""))
    if key:
        return t(key)
    if not group:
        return t("eco.value.not_ranked")
    return str(group)


def fallback(value: Any, label: str = "—") -> str:
    return V.fallback(value, label)


def safe_return_context(*, ranking_group: Any, page: Any, limit: Any, sort: Any, direction: Any) -> dict:
    """Whitelist the ranking state carried back from a detail page.

    Anything unrecognised is dropped rather than echoed, so a hand-edited link
    cannot smuggle a value back into a ranking URL.
    """

    result: dict[str, object] = {}
    if ranking_group in GROUPS:
        result["ranking_group"] = ranking_group
    for name, value, maximum in (("page", page, None), ("limit", limit, 500)):
        try:
            parsed = int(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            parsed = None
        if parsed is not None and parsed >= 1 and (maximum is None or parsed <= maximum):
            result[name] = parsed
    if sort in SORTS:
        result["sort"] = sort
    if isinstance(direction, str) and direction.lower() in ("asc", "desc"):
        result["direction"] = direction.lower()
    return result


def _period(entry: dict) -> dict:
    from .models import RankingPeriodKey

    try:
        key = RankingPeriodKey.from_token(str(entry.get("period_key")))
    except Exception:
        return {}
    return {
        "type": key.period_type.value,
        "start": key.period_start_date.isoformat(),
        "end": key.period_end_date.isoformat(),
        "sequence": key.period_sequence_in_month,
    }


def is_period_qualified(entry: dict) -> bool:
    """Whether the **whole reporting period** met the distance threshold.

    Reads the persisted ``qualification_status`` the aggregation job wrote, so
    the presentation cannot drift from the gate the score was computed under.
    """

    return str(entry.get("qualification_status") or "") == "QUALIFIED"


# The approved six-section order. The marker is part of the contract, not a
# styling hook: the order is asserted directly against rendered HTML.
SECTION_ORDER = ("score", "trend", "identity", "composition", "progression", "trips")


def _panel(
    title: str,
    body: str,
    *,
    section: str,
    caption: str = "",
    head_end: str = "",
    data_attrs: Optional[dict] = None,
) -> str:
    caption_html = f'<span class="eco-panel-caption">{H.esc(caption)}</span>' if caption else ""
    end_html = f'<div class="eco-panel-head-end">{head_end}</div>' if head_end else ""
    extra = "".join(
        f' {H.esc(name)}="{H.esc(value)}"' for name, value in (data_attrs or {}).items()
    )
    return (
        f'<section class="eco-panel" data-eco-section="{H.esc(section)}"{extra}>'
        f'<div class="eco-panel-head"><h2>{H.esc(title)}</h2>{caption_html}{end_html}</div>'
        f"{body}</section>"
    )


def export_links(export_href_fn, table: str) -> str:
    """`XLSX` / `CSV` for one detail table, in that table's panel head.

    In the panel head rather than under the table because that is where this
    page already puts a panel-level control (the unit toggle), and because a
    download belongs to the panel, not to the last row of it.

    `export_href_fn` is supplied by the page, which knows the resolved scope.
    A renderer that built the href itself would have to guess the period mode
    back off the entry — the guess the trips export already had to stop making.
    """

    if export_href_fn is None:
        return ""
    return (
        H.link(export_href_fn(table, "xlsx"), "XLSX", cls="portal-button secondary")
        + H.link(export_href_fn(table, "csv"), "CSV", cls="portal-button secondary")
    )


# --- the page ----------------------------------------------------------------


def render_detail(
    entry: dict,
    *,
    back_href: str,
    evidence_allowed: bool,
    trips: Optional[list[dict]] = None,
    trips_meta: Optional[dict] = None,
    trips_href: Optional[str] = None,
    distribution: Optional[dict] = None,
    trend: Optional[list[dict]] = None,
    progression: Optional[list[dict]] = None,
    unit: str = V.DEFAULT_UNIT,
    unit_href_fn=None,
    export_href_fn=None,
    trips_export_href_fn=None,
    period_switch_hrefs: tuple[Optional[str], Optional[str]] = (None, None),
) -> str:
    unit = V.normalize_unit(unit)
    body = _breadcrumb(entry, back_href=back_href, period_switch_hrefs=period_switch_hrefs)
    body += (
        '<div class="eco-detail-top">'
        + render_score_and_position(entry, distribution=distribution)
        + render_trend(trend or [])
        + "</div>"
    )
    body += render_identity(entry)
    body += (
        '<div class="eco-detail-mid">'
        + render_composition(entry, unit=unit, unit_href_fn=unit_href_fn,
                             export_href_fn=export_href_fn)
        + render_progression(progression or [], export_href_fn=export_href_fn)
        + "</div>"
    )
    body += render_trips(
        entry,
        trips=trips or [],
        trips_meta=trips_meta or {},
        trips_href=trips_href,
        evidence_allowed=evidence_allowed,
        trips_export_href_fn=trips_export_href_fn,
    )
    return body


def _breadcrumb(
    entry: dict,
    *,
    back_href: str,
    period_switch_hrefs: tuple[Optional[str], Optional[str]],
) -> str:
    """The detail-page breadcrumb, carrying the full analytical path.

    Back navigation restores the ranking with its group, page, sort and
    direction intact, so `D-009` is reversible rather than merely survivable.
    """

    driver = (entry.get("current_chart") or {}).get("current_driver_name")
    period = _period(entry)
    previous, following = period_switch_hrefs
    steps = [
        H.esc(t("shell.nav.analytics")),
        H.esc("Eco Driving"),
        H.esc(entry.get("client_code")),
        # The fallback branch renders a raw date when no label exists, so it
        # needs the formatter too. An output sweep cannot see this: the fixture
        # always supplies `period_label`, so the fallback never renders.
        H.esc(entry.get("period_label") or F.format_date(period.get("start"), empty="")),
    ]
    trail = f'<span class="eco-breadcrumb-sep">/</span>'.join(f"<span>{step}</span>" for step in steps)
    switches = []
    if previous:
        switches.append(
            f'<a class="eco-stepper-btn" href="{H.esc(previous)}" '
            f'aria-label="{H.esc(t("eco.period.previous"))}">‹</a>'
        )
    if following:
        switches.append(
            f'<a class="eco-stepper-btn" href="{H.esc(following)}" '
            f'aria-label="{H.esc(t("eco.period.next"))}">›</a>'
        )
    return (
        '<nav class="eco-breadcrumb" aria-label="Ścieżka">'
        f'{H.link(back_href, t("eco.detail.back"), cls="portal-link")}'
        f'<span class="eco-breadcrumb-sep">|</span>{trail}'
        f'<span class="eco-breadcrumb-sep">/</span>'
        f'<span class="eco-breadcrumb-current">'
        f'{H.esc(driver or entry.get("assigned_id") or "")}</span>'
        '<span class="eco-breadcrumb-end">'
        f'<span class="eco-muted">{H.esc(t("eco.detail.context_kept"))}</span>'
        + "".join(switches)
        + "</span></nav>"
    )


# --- 1. score and position ---------------------------------------------------


def render_score_and_position(entry: dict, *, distribution: Optional[dict]) -> str:
    driver = (entry.get("current_chart") or {}).get("current_driver_name")
    ids = " · ".join(
        part
        for part in (
            f'driver tag {entry.get("assigned_id")}' if entry.get("assigned_id") else "",
            f'{entry.get("client_code")}',
            f'{entry.get("ranking_family")}',
        )
        if part
    )
    head = (
        f'<h2 class="eco-driver-name">{H.esc(driver or entry.get("assigned_id") or "")}</h2>'
        f'<p class="eco-driver-ids">{H.esc(ids)}</p>'
    )

    if not is_period_qualified(entry):
        return (
            '<section class="eco-panel" data-eco-section="score">'
            + head + insufficient_distance_state(entry) + "</section>"
        )

    score = entry.get("eco_driving_score_total")
    position = entry.get("ranking_position")
    participants = entry.get("ranking_total_participants")
    percentile = _percentile(position, participants)
    percentile_html = (
        f'<span class="eco-rating">{H.esc(t("eco.detail.percentile", percent=percentile))}</span>'
        if percentile is not None
        else ""
    )
    heroes = (
        '<div class="eco-hero-row">'
        '<div class="eco-hero">'
        f'<span class="eco-eyebrow">{H.esc(t("eco.detail.score"))}</span>'
        f'<span class="eco-hero-value">{H.esc(V.fmt_decimal(score, 0))}'
        f'<span class="eco-hero-suffix"> / 100</span></span></div>'
        '<div class="eco-hero">'
        f'<span class="eco-eyebrow">{H.esc(t("eco.detail.position"))}</span>'
        f'<span class="eco-hero-value">{H.esc(position if position is not None else "—")}'
        f'<span class="eco-hero-suffix"> / {H.esc(participants if participants is not None else "—")}'
        "</span></span></div>"
        '<div class="eco-hero-badges">'
        f'{V.rating_badge(entry.get("ecodriving_rating_type"))}{percentile_html}</div>'
        "</div>"
    )
    fleet = V.histogram(
        distribution or {},
        subject_score=score,
        title=V.distribution_heading(distribution or {}, subject=True),
    )
    return (
        '<section class="eco-panel" data-eco-section="score">'
        + head
        + heroes
        + fleet
        + "</section>"
    )


def _percentile(position: Any, participants: Any) -> Optional[int]:
    """`górne n% floty`, from the persisted position and participant count.

    Derived from two persisted numbers only; it is never estimated from the
    rows that happen to be on screen.
    """

    try:
        pos = int(position)
        total = int(participants)
    except (TypeError, ValueError):
        return None
    if pos < 1 or total < 1:
        return None
    percent = int((Decimal(pos) / Decimal(total) * Decimal(100)).to_integral_value(rounding="ROUND_CEILING"))
    return max(1, min(100, percent))


def insufficient_distance_state(entry: dict) -> str:
    """The truthful state for a period below the qualifying distance.

    The threshold is a **reporting-period** rule: it is evaluated once against
    the period's total qualifying distance. It is deliberately not applied to
    any single day, and the trips section keeps showing every contributing trip.
    """

    status = str(entry.get("qualification_status") or "")
    if status == "NO_DISTANCE":
        return H.state_block(
            t("eco.state.no_distance_title"),
            t("eco.state.no_distance"),
        )
    # From metres, and without the unit: the sentence already says "km".
    kilometers = F.format_distance_km(entry.get("total_distance_meters"), unit=False)
    threshold = V.fmt_int(MIN_QUALIFYING_DISTANCE_METERS // 1000)
    return H.state_block(
        t("eco.state.insufficient_title"),
        t("eco.state.insufficient", km=kilometers, threshold=threshold),
    )


# --- 2. trend ----------------------------------------------------------------


def render_trend(points: list[dict]) -> str:
    """The driver's score across the comparable periods that actually exist.

    A period whose persisted row did not meet the distance threshold is drawn
    without a score rather than with one: presenting an unqualified period's
    number here would reintroduce, through the back door, exactly the value the
    detail page refuses to show for the current period.
    """

    visible = []
    for point in points:
        qualified = str(point.get("qualification_status") or "") == "QUALIFIED"
        visible.append(
            {**point, "eco_driving_score_total": point.get("eco_driving_score_total") if qualified else None}
        )
    caption = t("eco.detail.trend_window", count=len(visible)) if visible else ""
    body = V.trend_chart(visible) + V.trend_footnotes(visible)
    body += f'<p class="eco-note">{H.esc(t("eco.detail.trend_missing"))}</p>'
    return _panel(t("eco.detail.trend"), body, section="trend", caption=caption)


# --- 3. entry identity and period -------------------------------------------


def render_identity(entry: dict) -> str:
    period = _period(entry)
    chart = entry.get("current_chart") or {}
    status = str(entry.get("qualification_status") or "")
    calculation = str(entry.get("calculation_status") or "")
    distance_meters = entry.get("total_distance_meters")
    fields = (
        (t("eco.id.client"), entry.get("client_code")),
        (t("eco.id.provider"), entry.get("provider_display_name") or entry.get("ranking_family")),
        (t("eco.id.period_type"), _period_type_label(period.get("type"))),
        (t("eco.id.period_label"), entry.get("period_label")),
        (t("eco.id.period_start"), F.format_date(period.get("start"))),
        (t("eco.id.period_end"), F.format_date(period.get("end"))),
        (
            t("eco.id.partiality"),
            t("eco.period.partial") if entry.get("is_partial_period") else t("eco.period.full"),
        ),
        (t("eco.id.sequence"), period.get("sequence")),
        (t("eco.id.assigned_id"), entry.get("assigned_id")),
        (t("eco.id.group"), group_label(entry.get("ranking_group"))),
        (t("eco.id.qualification"), _qualification_label(status)),
        (t("eco.id.calculation"), t("eco.value.calc_ok") if calculation == "OK" else calculation),
        (
            t("eco.id.metadata_source"),
            t(METADATA_SOURCE_KEYS.get(str(chart.get("driver_metadata_source") or ""), "eco.value.chart_none")),
        ),
        (t("eco.id.participants"), entry.get("ranking_total_participants")),
        (t("eco.id.band_share"), _percent(entry.get("ecodriving_rating_type_share_percent"))),
        (
            t("eco.id.total_distance"),
            F.format_distance_km(distance_meters),
        ),
    )
    cells = "".join(
        '<div class="eco-identity-cell">'
        f"<dt>{H.esc(label)}</dt><dd>{fallback(value)}</dd></div>"
        for label, value in fields
    )
    return _panel(
        t("eco.detail.identity"),
        f'<dl class="eco-identity-grid">{cells}</dl>'
        f'<p class="eco-note">{H.esc(t("eco.detail.persisted_note"))}</p>',
        section="identity",
        caption=t("eco.detail.identity_note"),
    )


def _period_type_label(period_type: Any) -> str:
    if str(period_type) == "weekly":
        return t("eco.period.type_weekly")
    if str(period_type) == "monthly":
        return t("eco.period.type_monthly")
    return str(period_type or "")


def _qualification_label(status: str) -> str:
    key = QUALIFICATION_LABEL_KEYS.get(status)
    return t(key) if key else status


def _percent(value: Any) -> str:
    parsed = V.to_decimal(value)
    return "—" if parsed is None else f"{V.fmt_decimal(parsed, 0)} %"


# --- 4. score composition ----------------------------------------------------


def render_composition(entry: dict, *, unit: str, unit_href_fn=None,
                       export_href_fn=None) -> str:
    """One row per metric, ordered by lost points descending.

    Sorting by points *lost* rather than points *earned* is deliberate: the
    question the section answers is "what is hurting me", not "what did I
    score". Every number comes from a persisted column — the event counter, the
    normalized coefficient, the awarded points and the ``*_maxpoints_subtract``
    loss — so this table cannot disagree with the score above it.
    """

    if not is_period_qualified(entry):
        return _panel(
            t("eco.detail.composition_plain"),
            insufficient_distance_state(entry),
            section="composition",
        )

    counts = entry.get("event_counts") or {}
    rates = entry.get("metric_rates_per_100km") or {}
    points = entry.get("metric_points") or {}
    losses = {metric: (entry.get("metric_points_lost") or {}).get(metric) for metric in REQUIRED_METRICS}
    loss_decimals = {metric: V.to_decimal(value) for metric, value in losses.items()}
    total_loss = total_loss_magnitude(loss_decimals)

    ordered = composition_order(entry)

    sum_is_primary = unit == V.UNIT_SUM
    rows = []
    for metric in ordered:
        loss = loss_decimals.get(metric)
        step = applicable_ladder_step(metric, V.to_decimal(rates.get(metric)))
        severity = metric_severity(
            metric, points=V.to_decimal(points.get(metric)), loss=loss
        )
        share = loss_share_percent(loss, total_loss)
        # Both business values stay on screen; the toggle moves the *emphasis*.
        # The classified cell is whichever one is primary, and `metric_cell`
        # derives its severity from the persisted points/loss — never from the
        # number it prints — so `Σ` is coloured by the `/ 100 km` coefficient
        # exactly as `/ 100 km` mode is.
        primary_cell = V.metric_cell(
            metric_key=metric,
            displayed_value=H.esc(
                V.fmt_int(counts.get(metric))
                if sum_is_primary
                else V.fmt_decimal(rates.get(metric), 2)
            ),
            points=points.get(metric),
            loss=losses.get(metric),
            rate=rates.get(metric),
            cls_extra="is-primary-unit",
        )
        secondary_cell = (
            '<td class="eco-num eco-metric-secondary" data-unit-role="secondary">'
            + H.esc(
                V.fmt_decimal(rates.get(metric), 2)
                if sum_is_primary
                else V.fmt_int(counts.get(metric))
            )
            + H.visually_hidden(t("eco.unit.secondary"))
            + "</td>"
        )
        rows.append(
            "<tr>"
            f'<th scope="row">{H.esc(V.metric_label(metric))}</th>'
            + (primary_cell + secondary_cell if sum_is_primary else secondary_cell + primary_cell)
            + f"<td>{_ladder_cell(metric, step)}</td>"
            + f'<td class="eco-num">{H.esc(V.fmt_decimal(points.get(metric), 0))}</td>'
            + f'<td class="eco-num">{H.esc(metric_max_points(metric))}</td>'
            + _loss_cell(loss)
            + f"<td>{_share_cell(share, severity)}</td>"
            "</tr>"
        )

    def _unit_header(label: str, primary: bool) -> str:
        role = "primary" if primary else "secondary"
        marker = (
            f'<span class="eco-th-unit">{H.esc(t("eco.unit.primary"))}</span>'
            if primary
            else ""
        )
        return (
            f'<th class="eco-num eco-unit-{role}" data-unit-role="{role}">'
            f"{H.esc(label)}{marker}</th>"
        )

    headers = (
        f'<th>{H.esc(t("eco.comp.metric"))}</th>'
        + _unit_header(t("eco.comp.event_sum"), sum_is_primary)
        + _unit_header(t("eco.comp.rate"), not sum_is_primary)
        + f'<th>{H.esc(t("eco.comp.threshold"))}'
        f'<span class="eco-th-unit">{H.esc(t("eco.comp.threshold_ladder"))}</span></th>'
        f'<th class="eco-num">{H.esc(t("eco.comp.points"))}</th>'
        f'<th class="eco-num">{H.esc(t("eco.comp.max"))}</th>'
        f'<th class="eco-num">{H.esc(t("eco.comp.lost"))}</th>'
        f'<th>{H.esc(t("eco.comp.share"))}</th>'
    )
    arithmetic = t(
        "eco.detail.composition_arithmetic",
        base=100,
        lost=V.fmt_decimal(total_loss, 0),
    )
    zero_note = (
        f'<p class="eco-note">{H.esc(t("eco.comp.no_loss"))}</p>' if total_loss <= 0 else ""
    )
    toggle = (
        V.unit_toggle(unit, href_for_unit=unit_href_fn) if unit_href_fn is not None else ""
    )
    toggle += export_links(export_href_fn, "composition")
    title = t("eco.detail.composition", score=V.fmt_decimal(entry.get("eco_driving_score_total"), 0))
    return _panel(
        title,
        H.table(headers, "".join(rows))
        + zero_note
        + f'<p class="eco-note">{H.esc(t("eco.unit.comp_note"))}</p>'
        + f'<p class="eco-note">{H.esc(t("eco.comp.threshold_note"))}</p>',
        section="composition",
        caption=arithmetic,
        head_end=toggle,
        # The active unit is exposed programmatically, not only by emphasis.
        data_attrs={"data-eco-unit": unit},
    )


def _ladder_cell(metric: str, step) -> str:
    """A compact, truthful view of a multi-step scoring ladder.

    The repository scores each metric with a ladder, not with a single
    threshold, so this cell states the rung that currently applies **and** its
    position in the whole ladder, with every rung available in the tooltip.
    Printing only the first bucket bound would read as "the" threshold and would
    misstate the business rule.
    """

    from .score_presentation import ladder_steps

    full = "; ".join(
        (f"> {step_item.upper_bound if step_item.upper_bound is not None else ''}" if step_item.is_final
         else f"≤ {step_item.upper_bound}")
        + f": {step_item.points} pkt"
        for step_item in ladder_steps(metric)
    )
    if step is None:
        return f'<span class="eco-muted" title="{H.esc(full)}">—</span>'
    bound = (
        f"≤ {V.fmt_decimal(step.upper_bound, 0)}"
        if step.upper_bound is not None
        else f"> {V.fmt_decimal(ladder_steps(metric)[-2].upper_bound, 0)}"
    )
    position = t("eco.comp.threshold_step", step=step.index, total=step.total_steps)
    return (
        f'<span class="eco-ladder-step" title="{H.esc(full)}">{H.esc(bound)}'
        f'<span class="eco-ladder-position">{H.esc(position)}</span></span>'
    )


def _loss_cell(loss: Optional[Decimal]) -> str:
    """``Utracone`` keeps the repository's own sign.

    ``*_maxpoints_subtract`` is a scoring delta and is always ``<= 0``; showing
    it as a positive magnitude would invert its meaning against the persisted
    column, the e-mail report and the aggregation job.
    """

    if loss is None:
        return '<td class="eco-loss-cell is-zero">—</td>'
    zero = " is-zero" if loss == 0 else ""
    return f'<td class="eco-loss-cell{zero}">{H.esc(V.fmt_decimal(loss, 0))}</td>'


def _share_cell(share: Optional[Decimal], severity: str) -> str:
    """Share of the driver's total lost points.

    When the driver lost nothing the share is undefined for every metric, and
    the cell says so instead of printing a manufactured ``0 %`` of a zero
    denominator.
    """

    if share is None:
        return '<span class="eco-muted">—</span>'
    fill = ""
    if severity == SEVERITY_BAD:
        fill = " is-bad"
    elif severity == SEVERITY_WARN:
        fill = " is-warn"
    width = int(max(Decimal(0), min(Decimal(100), share)))
    return (
        '<span class="eco-share">'
        '<span class="eco-share-track" aria-hidden="true">'
        f'<span class="eco-share-fill{fill}" style="width:{width}%"></span></span>'
        f'<span class="eco-share-value">{H.esc(V.fmt_decimal(share, 0))} %</span></span>'
    )


# --- 5. snapshot progression within the month --------------------------------


def render_progression(rows: list[dict], *, export_href_fn=None) -> str:
    """Per-snapshot diagnosis of the selected month.

    The rows are the persisted **cumulative month-to-date** snapshots, shown as
    a running total with a delta between consecutive snapshots. They are not
    week slices and must not be added: consecutive cumulative snapshots overlap,
    so their sum would double-count the shared prefix. Arbitrary week selection
    needs dynamic recomputation from the underlying assignments, which is a
    separate task.
    """

    if not rows:
        return _panel(
            t("eco.detail.progression"),
            f'<p class="eco-muted">{H.esc(t("eco.detail.progression_empty"))}</p>'
            f'<p class="eco-note">{H.esc(t("eco.detail.progression_note"))}</p>',
            section="progression",
            caption=t("eco.detail.progression_caption"),
        )

    headers = (
        f'<th>{H.esc(t("eco.period.label"))}</th>'
        f'<th class="eco-num">{H.esc(t("eco.col.score"))}</th>'
        f'<th class="eco-num">Δ</th>'
        f'<th class="eco-num">{H.esc(t("eco.col.distance"))}</th>'
        f'<th class="eco-num">{H.esc(t("eco.col.trips"))}</th>'
    )
    body_rows = []
    for row in progression_rows(rows):
        current = ' class="eco-cell-score"' if row["is_current"] else ""
        body_rows.append(
            f"<tr{current}>"
            f'<th scope="row">{H.esc(row["period_label"])}</th>'
            f'<td class="eco-num">{H.esc(V.fmt_decimal(row["score"], 0))}</td>'
            f'<td class="eco-num">{H.esc(row["delta_text"])}</td>'
            f'<td class="eco-num">{H.esc(F.format_distance_km(row["total_distance_meters"]))}</td>'
            f'<td class="eco-num">{H.esc(V.fmt_int(row["trips_count"]))}</td>'
            "</tr>"
        )
    return _panel(
        t("eco.detail.progression"),
        H.table(headers, "".join(body_rows))
        + f'<p class="eco-note">{H.esc(t("eco.detail.progression_note"))}</p>',
        section="progression",
        caption=t("eco.detail.progression_caption"),
        head_end=export_links(export_href_fn, "progression"),
    )


# --- 6. contributing trips ---------------------------------------------------


def render_trips(
    entry: dict,
    *,
    trips: list[dict],
    trips_meta: dict,
    trips_href: Optional[str],
    evidence_allowed: bool,
    trips_export_href_fn=None,
) -> str:
    """The evidence behind the score.

    Without the trip-evidence grant the section stays on the page and explains
    itself; it never disappears silently (`EC-26`). With the grant it lists the
    contributing trips exactly as the reconstruction returns them — a short trip
    is still a contributing trip, because the qualifying-distance rule applies
    to the reporting period as a whole and never to one row.
    """

    if not evidence_allowed:
        return _panel(
            t("eco.detail.trips"),
            H.state_block(
                t("eco.detail.trips_denied_title"),
                t("eco.detail.trips_denied"),
                variant="eco-state--neutral",
                role="note",
            ),
            section="trips",
        )

    total = trips_meta.get("total_count")
    caption = f"{V.fmt_int(total)} przejazdów" if total is not None else ""
    action = (
        H.link(trips_href, t("eco.detail.trips_open"), cls="portal-button secondary")
        if trips_href
        else ""
    )
    # THE FILE IS THE WHOLE SET, NOT THE PREVIEW. This panel shows the first
    # `DETAIL_TRIP_PREVIEW_LIMIT` rows and its caption already states the true
    # total; the download is that total, through the same endpoint the full trip
    # surface uses. A second export producing a 25-row file would give this page
    # and the trips page two different answers for one question.
    if trips_export_href_fn is not None and total:
        action += (
            H.link(trips_export_href_fn("xlsx"), "XLSX", cls="portal-button secondary")
            + H.link(trips_export_href_fn("csv"), "CSV", cls="portal-button secondary")
        )
    if not trips:
        return _panel(
            t("eco.detail.trips"),
            f'<p class="eco-muted">{H.esc(t("eco.detail.trips_empty"))}</p>',
            section="trips",
            caption=caption,
            head_end=action,
        )

    sum_caption = t("eco.trip.sum_caption")
    metric_headers = "".join(
        f'<th class="eco-num">{H.esc(V.metric_label(metric, short=True))}'
        f'<span class="eco-th-unit">{H.esc(sum_caption)}</span></th>'
        for metric in V.RANKING_METRIC_ORDER
    )
    # `UI-20260820-02` B: this preview and the full trip surface show the same
    # rows, so they must not disagree about what identifies a trip. The plate
    # replaces the provider trip id and leads the table, as it does there;
    # leaving it in the vacated fifth slot would bury the identifier in the
    # middle of the numeric run. It is text, so it carries no `eco-num`.
    headers = (
        f'<th>{H.esc(t("eco.trip.registration"))}</th>'
        f'<th>{H.esc(t("eco.trip.start"))}</th>'
        f'<th>{H.esc(t("eco.trip.end"))}</th>'
        f'<th class="eco-num">{H.esc(t("eco.trip.distance"))}</th>'
        f'<th class="eco-num">{H.esc(t("eco.trip.duration"))}</th>'
        f'<th>{H.esc(t("eco.trip.source"))}</th>'
        + metric_headers
    )
    rows = []
    for trip in trips:
        counts = trip.get("event_counts") or {}
        # Trip-level values are always Σ sums for that one trip: a rate for a
        # single trip would be meaningless. There is no per-trip score
        # contribution — the score is a step function of aggregate normalized
        # rates and is not additive across trips, so no such column exists.
        metric_cells = "".join(
            f'<td class="eco-num">{H.esc(V.fmt_int(counts.get(metric)))}</td>'
            for metric in V.RANKING_METRIC_ORDER
        )
        rows.append(
            "<tr>"
            f'<td>{V.fallback(trip.get("vehicle_registration"))}</td>'
            f'<td class="eco-num">{H.esc(_ts(trip.get("trip_start_ts")))}</td>'
            f'<td class="eco-num">{H.esc(_ts(trip.get("trip_end_ts")))}</td>'
            f'<td class="eco-num">{H.esc(F.format_distance_km(trip.get("trip_distance_meters")))}</td>'
            f'<td class="eco-num">{H.esc(_duration(trip))}</td>'
            f'<td>{H.esc(trip.get("assignment_source"))}</td>'
            + metric_cells
            + "</tr>"
        )
    return _panel(
        t("eco.detail.trips"),
        H.table(headers, "".join(rows))
        + f'<p class="eco-note">{H.esc(t("eco.trip.sum_note"))} '
        f'{H.esc(t("eco.detail.trips_footer"))}</p>',
        section="trips",
        caption=caption,
        head_end=action,
    )


def _ts(value: Any) -> str:
    """Delegates to the shared formatter.

    The string-slicing this used to do produced `YYYY-MM-DD HH:MM`, so this
    preview and the full trips table disagreed about what time the same trip
    started — different format, and different precision.
    """

    return F.format_datetime(value)


def _duration(trip: dict) -> str:
    """`h : mm` from the two persisted timestamps. Nothing new is read."""

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


# --- exports of the two driver-detail tables ---------------------------------
#
# THE ROWS ARE BUILT ONCE AND RENDERED TWICE. `composition_order` decides the
# order the composition table appears in, and `progression_rows` computes the
# running delta between consecutive snapshots — both are called by the renderer
# above and by the export below, so a file cannot list the metrics in a
# different order, or compute a different delta, than the screen it came from.
#
# The exported columns are the columns those tables render, in their order, with
# ONE deliberate difference: a file has no unit toggle. On screen the toggle
# moves the emphasis between `Σ zdarzeń` and `/ 100 km` while both stay visible;
# in a file both are simply columns, in a fixed order, so two files taken under
# two toggle states are comparable.


def composition_order(entry: dict) -> list[str]:
    """The metric order of the composition table: worst loss first.

    Sorting by points *lost* rather than points *earned* is the section's whole
    argument — "what is hurting me", not "what did I score" — so the export
    inherits it rather than choosing an order of its own.
    """

    losses = {
        metric: V.to_decimal((entry.get("metric_points_lost") or {}).get(metric))
        for metric in REQUIRED_METRICS
    }
    return sorted(
        REQUIRED_METRICS,
        key=lambda metric: composition_sort_key(metric, losses.get(metric)),
    )


def _ladder_bound_text(metric: str, step) -> str:
    """The applicable rung, as the cell prints it, without its markup."""

    from .score_presentation import ladder_steps

    if step is None:
        return ""
    if step.upper_bound is not None:
        return f"≤ {V.fmt_decimal(step.upper_bound, 0)}"
    return f"> {V.fmt_decimal(ladder_steps(metric)[-2].upper_bound, 0)}"


def composition_rows(entry: dict) -> list[dict]:
    """One row per metric, with the persisted numbers the table prints."""

    counts = entry.get("event_counts") or {}
    rates = entry.get("metric_rates_per_100km") or {}
    points = entry.get("metric_points") or {}
    losses = entry.get("metric_points_lost") or {}
    loss_decimals = {
        metric: V.to_decimal(losses.get(metric)) for metric in REQUIRED_METRICS
    }
    total_loss = total_loss_magnitude(loss_decimals)
    rows = []
    for metric in composition_order(entry):
        step = applicable_ladder_step(metric, V.to_decimal(rates.get(metric)))
        rows.append({
            "metric": metric,
            "metric_label": V.metric_label(metric),
            "event_count": counts.get(metric),
            "rate_per_100km": V.to_decimal(rates.get(metric)),
            "threshold": _ladder_bound_text(metric, step),
            "threshold_step": None if step is None else step.index,
            "threshold_steps_total": None if step is None else step.total_steps,
            "points": V.to_decimal(points.get(metric)),
            "max_points": metric_max_points(metric),
            "points_lost": loss_decimals.get(metric),
            "loss_share_percent": loss_share_percent(loss_decimals.get(metric), total_loss),
        })
    return rows


def progression_rows(rows: list[dict]) -> list[dict]:
    """The snapshot rows, with the delta the table shows between them.

    The delta is a property of the SEQUENCE, not of a row, so it is computed
    here once. An export that recomputed it would be free to disagree with the
    screen about the first row a driver qualified in — the row where the running
    previous score does not exist yet and the cell prints a dash.
    """

    out = []
    previous_score: Optional[Decimal] = None
    for row in rows:
        qualified = str(row.get("qualification_status") or "") == "QUALIFIED"
        score = V.to_decimal(row.get("eco_driving_score_total")) if qualified else None
        delta = None if (score is None or previous_score is None) else score - previous_score
        out.append({
            "period_label": row.get("period_label") or "",
            "score": score,
            "delta": delta,
            "delta_text": "—" if delta is None else V.fmt_signed(delta),
            "total_distance_meters": row.get("total_distance_meters"),
            "total_distance_km": V.km_number(row.get("total_distance_meters")),
            "trips_count": row.get("trips_count"),
            "is_current": bool(row.get("is_current")),
        })
        if score is not None:
            previous_score = score
    return out


def _column(key: str, label: str, *, export_label: str = None):
    """A value-only column for the spreadsheet writers.

    These two tables are not selection surfaces and have no clipboard values, so
    a column needs nothing but a heading and a value — the writers in
    `trip_export` read exactly `export_label` and `value(row)`.
    """

    from .trip_view_models import Column

    return Column(key, label, lambda row: "", lambda row, k=key: row.get(k),
                  export_label=export_label or label)


def composition_columns() -> list:
    return [
        _column("metric_label", t("eco.comp.metric")),
        _column("event_count", t("eco.comp.event_sum")),
        _column("rate_per_100km", t("eco.comp.rate"),
                export_label=f'{t("eco.comp.rate")} (/ 100 km)'),
        _column("threshold", t("eco.comp.threshold")),
        _column("threshold_step", t("eco.comp.threshold_ladder")),
        _column("points", t("eco.comp.points")),
        _column("max_points", t("eco.comp.max")),
        _column("points_lost", t("eco.comp.lost")),
        _column("loss_share_percent", t("eco.comp.share"), export_label=f'{t("eco.comp.share")} (%)'),
    ]


def progression_columns() -> list:
    return [
        _column("period_label", t("eco.period.label")),
        _column("score", t("eco.col.score")),
        _column("delta", "Δ"),
        # `eco.col.distance` already reads `Dystans (km)`.
        _column("total_distance_km", t("eco.col.distance")),
        _column("trips_count", t("eco.col.trips")),
    ]
