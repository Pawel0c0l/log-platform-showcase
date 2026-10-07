"""Rendering for the month + arbitrary-week ranking basis (`ECO-001` level 2/3).

Pure presentation over the serialized ``selection`` / ``basis`` envelope the
service produced. It computes no business value: the week boundaries, the
covered day count, the contiguity verdict and every count arrive already
resolved, so nothing here can disagree with the ranking it labels.

The approved interaction is toggle cards with visible date ranges. They are real
links carrying the canonical ``weeks`` value, so the basis works with scripting
disabled, is keyboard operable, survives Back and is shareable.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Callable, Optional

from . import eco_view as V
from . import html as H

if __package__ and __package__.startswith("api."):
    from ..portal_ui.i18n import t
    from ..portal_ui import formats as F
else:  # pragma: no cover - import-path parity with the rest of the package
    from portal_ui.i18n import t
    from portal_ui import formats as F

EMPTY_WEEKS_TOKEN = "none"

MONTH_NAMES_PL = (
    "Styczeń", "Luty", "Marzec", "Kwiecień", "Maj", "Czerwiec",
    "Lipiec", "Sierpień", "Wrzesień", "Październik", "Listopad", "Grudzień",
)


def _d(value: Any) -> Optional[date]:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def month_label(month_token: str) -> str:
    """``2026-07`` → ``Lipiec 2026``. Falls back to the token if malformed."""

    try:
        year, month = str(month_token).split("-")
        return f"{MONTH_NAMES_PL[int(month) - 1]} {int(year)}"
    except Exception:
        return str(month_token or "")


def _inclusive_end(card: dict) -> Optional[date]:
    """The last **covered** day of a bucket, for display only.

    The stored boundary is exclusive; printing it as the visible end date would
    claim a day the basis does not contain.
    """

    end = _d(card.get("end_date_exclusive"))
    return None if end is None else date.fromordinal(end.toordinal() - 1)


def card_range_text(card: dict) -> str:
    start = _d(card.get("start_date"))
    end = _inclusive_end(card)
    if start is None or end is None:
        return ""
    if start.month == end.month:
        return f"{start:%d}–{end:%d.%m}"
    return f"{start:%d.%m}–{end:%d.%m}"


def covered_range_text(selection: dict) -> str:
    """The true covered calendar range(s) of the basis.

    A non-contiguous selection prints **each** covered run, because the span
    between the first and last selected week is not what was selected and
    stating it as one range would overstate the basis by the gap.
    """

    cards = [c for c in (selection.get("week_cards") or []) if c.get("selected")]
    if not cards:
        return ""
    runs: list[list[dict]] = []
    for card in cards:
        if runs and _d(card.get("start_date")) == _d(runs[-1][-1].get("end_date_exclusive")):
            runs[-1].append(card)
        else:
            runs.append([card])
    parts = []
    for run in runs:
        start = _d(run[0].get("start_date"))
        end = _inclusive_end(run[-1])
        if start is None or end is None:
            continue
        if start.month == end.month:
            parts.append(f"{start:%d}–{end:%d.%m.%Y}")
        else:
            parts.append(f"{start:%d.%m}–{end:%d.%m.%Y}")
    return " · ".join(parts)


# --- week toggle cards --------------------------------------------------------


def weeks_param_after_toggle(selection: dict, sequence: int) -> Optional[str]:
    """Canonical ``weeks`` value after toggling one card.

    ``None`` means "omit the parameter" — the whole-month default — so selecting
    the last missing week produces the same URL as the `Cały miesiąc` shortcut
    and the two states cannot diverge.
    """

    cards = selection.get("week_cards") or []
    everything = [int(c["sequence"]) for c in cards]
    current = {int(value) for value in (selection.get("selected_weeks") or [])}
    sequence = int(sequence)
    if sequence in current:
        current.discard(sequence)
    else:
        current.add(sequence)
    ordered = sorted(current)
    if not ordered:
        return EMPTY_WEEKS_TOKEN
    if ordered == sorted(everything):
        return None
    return ",".join(str(value) for value in ordered)


def week_cards(selection: dict, href_for_weeks: Callable[[Optional[str]], str]) -> str:
    cards = []
    for card in selection.get("week_cards") or []:
        selected = bool(card.get("selected"))
        href = href_for_weeks(weeks_param_after_toggle(selection, card["sequence"]))
        action = t(
            "eco.basis.card_deselect" if selected else "eco.basis.card_select",
            label=card.get("label"),
        )
        partial = (
            f'<span class="eco-week-partial">{H.esc(t("eco.period.partial"))}</span>'
            if card.get("is_partial")
            else ""
        )
        classes = "eco-week-card" + (" is-selected" if selected else "")
        classes += " is-partial" if card.get("is_partial") else ""
        current = ' aria-current="true"' if selected else ""
        cards.append(
            f'<a class="{H.esc(classes)}" href="{H.esc(href)}"{current} '
            f'data-week="{H.esc(card["sequence"])}">'
            f'<span class="eco-week-label">{H.esc(card.get("label"))}</span>'
            f'<span class="eco-week-range">{H.esc(card_range_text(card))}</span>'
            f"{partial}{H.visually_hidden(action)}</a>"
        )
    shortcuts = (
        f'<a class="eco-week-shortcut" href="{H.esc(href_for_weeks(None))}">'
        f'{H.esc(t("eco.basis.whole_month"))}</a>'
        f'<a class="eco-week-shortcut" href="{H.esc(href_for_weeks(EMPTY_WEEKS_TOKEN))}">'
        f'{H.esc(t("eco.basis.clear"))}</a>'
    )
    return (
        '<div class="eco-week-controls">'
        f'<span class="eco-eyebrow">{H.esc(t("eco.basis.weeks"))}</span>'
        f'<div class="eco-week-shortcuts">{shortcuts}</div>'
        f'<div class="eco-week-cards" role="group" '
        f'aria-label="{H.esc(t("eco.basis.week_aria"))}">' + "".join(cards) + "</div>"
        "</div>"
    )


# --- basis line ---------------------------------------------------------------


def basis_line(selection: dict, basis: Optional[dict]) -> str:
    """The one sentence that is the source of truth for what is on screen."""

    mode = selection.get("mode")
    if mode == "EMPTY":
        return (
            '<div class="eco-basis eco-muted">'
            f'{H.esc(t("eco.basis.empty"))}</div>'
        )

    label = selection.get("label") or ""
    key = "eco.basis.sentence_month" if mode == "MONTH" else "eco.basis.sentence_weeks"
    sentence = t(
        key,
        label=label,
        range=covered_range_text(selection),
        days=int(selection.get("day_count") or 0),
    )
    head = f'<div class="eco-basis"><strong>{H.esc(label)}</strong> · {H.esc(sentence)}</div>'
    if selection.get("includes_partial_week"):
        head += (
            '<div class="eco-basis eco-basis-secondary">'
            f'{H.esc(t("eco.period.partial"))}</div>'
        )

    secondary = ""
    if basis:
        qualified = int(basis.get("qualified_count") or 0)
        total = int(basis.get("population_count") or 0)
        parts = [t("eco.basis.qualified", qualified=qualified, total=total)]
        trips = basis.get("total_trips_count")
        kilometers = basis.get("total_kilometers")
        if trips is not None and kilometers is not None:
            parts.append(
                t(
                    "eco.basis.trips_volume",
                    trips=V.fmt_int(trips),
                    # Prose: `{trips} przejazdów · {km} km` supplies the unit.
                    km=F.format_distance_km(basis.get("total_distance_meters"), unit=False),
                )
            )
        secondary = (
            '<div class="eco-basis eco-basis-secondary">'
            + H.esc(" · ".join(parts))
            + "</div>"
        )

    # Which data path produced these numbers, stated on the page rather than
    # only in documentation. A whole month is normally the persisted monthly
    # reporting snapshot, but a month with no snapshot yet is aggregated
    # dynamically over its full range — and says so, rather than claiming a
    # persisted source it does not have.
    basis_source = selection.get("basis_source")
    if mode != "MONTH":
        source_key = "eco.basis.dynamic_source"
    elif basis_source == "MONTH_DYNAMIC":
        source_key = "eco.basis.whole_month_dynamic_source"
    else:
        source_key = "eco.basis.whole_month_source"
    source = (
        '<div class="eco-basis eco-basis-secondary">'
        f'{H.esc(t(source_key))}</div>'
    )
    return head + secondary + source


def gap_warning(selection: dict) -> str:
    """`EC-4`: non-contiguous is allowed, warned about, and never blocked."""

    if selection.get("mode") != "WEEKS" or selection.get("is_contiguous"):
        return ""
    return (
        '<p class="eco-callout eco-callout-warn" role="status">'
        f'{H.esc(t("eco.basis.gap"))}</p>'
    )


def empty_state() -> str:
    """`EC-5`: zero weeks prompts a selection instead of failing."""

    return H.state_block(t("eco.basis.empty_title"), t("eco.basis.empty"))


def no_data_state() -> str:
    """A real selection with no source data at all — stated, never fabricated."""

    return H.state_block(t("eco.basis.no_data_title"), t("eco.basis.no_data"))


# --- the period bar -----------------------------------------------------------


def month_stepper(
    month_token: str,
    months: list,
    href_for_month: Callable[[str], str],
) -> str:
    """`‹ Lipiec 2026 ›`, with the unavailable step **absent**, not disabled."""

    ordered = sorted(str(value) for value in (months or []))
    label = month_label(month_token)
    previous_html = next_html = ""
    if month_token in ordered:
        index = ordered.index(month_token)
        if index > 0:
            previous_html = (
                f'<a class="eco-stepper-btn" href="{H.esc(href_for_month(ordered[index - 1]))}" '
                f'aria-label="{H.esc(t("eco.basis.month_previous"))}">‹</a>'
            )
        if index + 1 < len(ordered):
            next_html = (
                f'<a class="eco-stepper-btn" href="{H.esc(href_for_month(ordered[index + 1]))}" '
                f'aria-label="{H.esc(t("eco.basis.month_next"))}">›</a>'
            )
    return (
        '<div class="eco-period-block">'
        f'<span class="eco-eyebrow">{H.esc(t("eco.basis.month"))}</span>'
        '<div class="eco-stepper">'
        f"{previous_html}"
        f'<span class="eco-stepper-label">{H.esc(label)}</span>'
        f"{next_html}</div></div>"
    )


def basis_bar(
    selection: dict,
    basis: Optional[dict],
    distribution: Optional[dict],
    months: list,
    *,
    href_for_weeks: Callable[[Optional[str]], str],
    href_for_month: Callable[[str], str],
) -> str:
    histogram = V.histogram(distribution or {}) if distribution else ""
    return (
        '<div class="eco-period-bar eco-period-bar--basis" '
        f'data-eco-basis-mode="{H.esc(selection.get("mode"))}">'
        + month_stepper(str(selection.get("month") or ""), months, href_for_month)
        + '<div class="eco-period-block eco-period-block--grow">'
        + week_cards(selection, href_for_weeks)
        + f'<span class="eco-eyebrow">{H.esc(t("eco.basis.label"))}</span>'
        + basis_line(selection, basis)
        + "</div>"
        + histogram
        + "</div>"
        + gap_warning(selection)
    )


def detail_context_line(selection: dict) -> str:
    """The basis, echoed on the driver page so the context is never lost."""

    label = selection.get("label") or t("eco.basis.whole_month")
    return (
        '<p class="eco-note eco-basis-context">'
        + H.esc(
            t(
                "eco.basis.detail_context",
                month=month_label(str(selection.get("month") or "")),
                label=label,
            )
        )
        + "</p>"
    )
