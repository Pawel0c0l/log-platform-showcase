"""Pure HTML primitives and approved Polish copy for Report Explorer.

No FastAPI and no I/O. Everything dynamic is escaped with the same
`html.escape(..., quote=True)` the portal `_html` helper uses, so a value is
safe both as element text and inside a double-quoted attribute.

The copy constants below are the approved vocabulary
(`COPY_AND_TERMINOLOGY.md` §6, §6.1) held in ONE place. A page that needs a
label reads it here rather than spelling it again, because the approved wording
is an acceptance criterion and a second spelling is a silent divergence.

Styling lives in the repository-native `api/static/css/report-explorer.css`,
requested through the shared page-asset mechanism exactly like the Database
Explorer grid layer and the Eco Driving stylesheet. There is no inline `<style>`
block and no second palette: every colour resolves to a shared `--lp-*` token.
"""
from __future__ import annotations

import html as _htmllib
from datetime import date, datetime
from urllib.parse import urlencode

if __package__ and __package__.startswith("api."):
    from ..portal_ui import formats as _formats
else:  # pragma: no cover - import-path parity with the rest of the package
    from portal_ui import formats as _formats

from .models import (
    ROLE_DETAILED,
    ROLE_MAIN,
    ROLE_RAW,
    STATUS_EXPIRED,
    STATUS_FAILED,
    STATUS_GENERATING,
    STATUS_READY,
)

# ---------------------------------------------------------------------------
# Routes. The library is the `Raporty` nav slot; the detail page is a dedicated
# page under it (`RP-11` — not a modal, not a new tab).
# ---------------------------------------------------------------------------
LIBRARY_PATH = "/user/reports"
INSTANCE_PATH = "/user/reports/instances"
LEGACY_FOLDERS_PATH = "/user/reports/folders"

REPORT_EXPLORER_PAGE_ASSETS: tuple[str, ...] = ("css/report-explorer.css", "js/report-explorer.js")

MODULE_LABEL = "Raporty"

# ---------------------------------------------------------------------------
# Approved Polish copy (`COPY_AND_TERMINOLOGY.md` §6).
# ---------------------------------------------------------------------------
COPY = {
    "module": "Raporty",
    "rail_heading": "Typy raportów",
    "rail_filter": "Filtruj typy",
    "rail_all": "Wszystkie typy",
    "search_label": "Nazwa raportu lub okres",
    "reporting_period": "Okres raportowania",
    "period_ordinal": "Numer okresu",
    "cycle": "Cykl",
    "generated_at": "Wygenerowano",
    "started_at": "rozpoczęto",
    "files": "Pliki",
    "files_panel": "Pliki w tej pozycji",
    "row_count": "Wiersze w raporcie",
    "retention": "Retencja plików",
    "open_report": "Otwórz raport",
    "download": "Pobierz",
    "download_all": "Pobierz wszystkie",
    "preview": "Podgląd",
    "fullscreen": "Pełny ekran",
    "source_data": "Dane źródłowe",
    "history": "Historia tego raportu",
    "show_all_periods": "Pokaż wszystkie {n} okresów",
    "back_to_library": "‹ Wróć do biblioteki",
    "filters_preserved": "filtry biblioteki zachowane",
    "grouping_rule": "grupowanie po miesiącu wygenerowania, najnowsze u góry",
    "report_problem": "Zgłoś problem",
    "client": "Klient",
    "report_type": "Typ raportu",
    "status": "Status",
    "year": "Rok",
    "type": "Typ",
    "clear_filters": "Wyczyść filtry",
    "request_access": "Poproś o dostęp",
    "retry": "Ponów",
    "copy_reference": "Skopiuj referencję",
    "no_files_yet": "pliki pojawią się po zakończeniu",
    "no_files_after_failure": "brak plików — generowanie nie ukończyło się",
    "per_page": "Wierszy na stronę",
    "previous_page": "Poprzednia",
    "next_page": "Następna",
}

STATUS_LABELS = {
    STATUS_READY: "Gotowy",
    STATUS_GENERATING: "W generowaniu",
    STATUS_FAILED: "Błąd generowania",
    STATUS_EXPIRED: "Pliki wygasły",
}

# The left accent edge carries the status colour (`PBC` §3.3). Colour is never
# the only carrier: the badge text and a visually hidden phrase say the same
# thing, which is what `ACCESSIBILITY_SPEC` §2 requires.
STATUS_VARIANTS = {
    STATUS_READY: "neutral",
    STATUS_GENERATING: "warning",
    STATUS_FAILED: "negative",
    STATUS_EXPIRED: "neutral",
}

CADENCE_LABELS = {
    "weekly": "tygodniowy",
    "monthly": "miesięczny",
    "quarterly": "kwartalny",
    "on_demand": "na żądanie",
}

ROLE_LABELS = {
    ROLE_MAIN: "dokument główny",
    ROLE_DETAILED: "dane szczegółowe",
    ROLE_RAW: "dane surowe",
}

METRIC_LABELS = {
    "pages": ("strona", "strony", "stron"),
    "sheets": ("arkusz", "arkusze", "arkuszy"),
    "rows": ("wiersz", "wiersze", "wierszy"),
}

_MONTHS_NOMINATIVE = (
    "styczeń", "luty", "marzec", "kwiecień", "maj", "czerwiec",
    "lipiec", "sierpień", "wrzesień", "październik", "listopad", "grudzień",
)


def esc(value: object) -> str:
    if value is None:
        return ""
    return _htmllib.escape(str(value), quote=True)


def query(params: dict) -> str:
    clean = {k: str(v) for k, v in params.items() if v is not None and str(v) != ""}
    return urlencode(clean)


def url(path: str, params: dict) -> str:
    qs = query(params)
    return f"{path}?{qs}" if qs else path


def visually_hidden(text: object) -> str:
    return f'<span class="lp-visually-hidden">{esc(text)}</span>'


def cadence_label(cadence_class: str, cadence_detail: str = "") -> str:
    """`tygodniowy · pon. 04:00` — the class, then its declared schedule detail.

    Both halves are persisted on the definition, so the rail renders the full
    string without re-deriving it from a timer or a mail schedule
    (`docs/40` §6).
    """
    base = CADENCE_LABELS.get(str(cadence_class or ""), str(cadence_class or ""))
    detail = str(cadence_detail or "").strip()
    return f"{base} · {detail}" if detail else base


def format_size(value) -> str:
    """`2,4 MB` — the portal's size scale rendered with a Polish decimal comma."""
    try:
        size = float(value)
    except (TypeError, ValueError):
        return ""
    units = ("B", "kB", "MB", "GB", "TB")
    unit = 0
    while size >= 1024 and unit < len(units) - 1:
        size /= 1024
        unit += 1
    if unit == 0:
        return f"{int(size)} {units[unit]}"
    return f"{size:.1f}".replace(".", ",") + f" {units[unit]}"


def format_count(value) -> str:
    """`4 118` — a non-breaking thin space as the thousands separator."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return ""
    return f"{number:,}".replace(",", " ")


def format_moment(moment: datetime | None) -> str:
    """`15.07.2026 04:22:31` — the platform timestamp rendering.

    Delegates to the shared formatter so this surface cannot drift from the
    others. It gained seconds when the platform standardised on them: these
    tables are evidence, and minute precision collapses distinct events.
    """

    if not isinstance(moment, datetime):
        return ""
    return _formats.format_datetime(moment, empty="")


def format_day(value: date | datetime | None) -> str:
    if isinstance(value, datetime):
        value = value.date()
    if not isinstance(value, date):
        return ""
    return f"{value.day:02d}.{value.month:02d}.{value.year}"


def month_label(moment: datetime | None) -> str:
    if not isinstance(moment, datetime):
        return ""
    return f"{_MONTHS_NOMINATIVE[moment.month - 1]} {moment.year}"


def metric_label(kind: str | None, value: int | None) -> str:
    """`12 stron`, `3 arkusze`, `4 118 wierszy` — the secondary file fact."""
    if not kind or value is None:
        return ""
    forms = METRIC_LABELS.get(str(kind))
    if not forms:
        return ""
    number = int(value)
    if number == 1:
        form = forms[0]
    elif number % 10 in (2, 3, 4) and number % 100 not in (12, 13, 14):
        form = forms[1]
    else:
        form = forms[2]
    return f"{format_count(number)} {form}"


def status_badge(status: str) -> str:
    """The status pill. The label is the carrier; the tint only reinforces it."""
    label = STATUS_LABELS.get(status, status)
    variant = STATUS_VARIANTS.get(status, "neutral")
    return (
        f'<span class="rep-status rep-status-{esc(variant)}" data-status="{esc(status)}">'
        f"{esc(label)}</span>"
    )


def format_badge(file_format: str, size_bytes: int) -> str:
    """`PDF · 2,4 MB` — one badge per file, with its OWN size (`RP-10`)."""
    return (
        '<span class="rep-format-badge">'
        f'<span class="rep-format">{esc(file_format)}</span>'
        f'<span class="rep-format-size">{esc(format_size(size_bytes))}</span>'
        "</span>"
    )


def empty_state(title: str, message: str, actions_html: str = "") -> str:
    actions = f'<div class="rep-state-actions">{actions_html}</div>' if actions_html else ""
    return (
        '<div class="rep-state" role="status">'
        f"<h2>{esc(title)}</h2><p>{esc(message)}</p>{actions}</div>"
    )


def error_state(title: str, message: str, actions_html: str = "", reference: str = "") -> str:
    """A named infrastructure state, never a raw exception.

    `REP-004` requires a copyable reference, and requires that it be a reference
    — not a driver message, a DSN or a storage key.
    """
    actions = f'<div class="rep-state-actions">{actions_html}</div>' if actions_html else ""
    ref = (
        f'<p class="rep-state-ref">Referencja: <code>{esc(reference)}</code></p>'
        if reference
        else ""
    )
    return (
        '<div class="rep-state rep-state-error" role="alert">'
        f"<h2>{esc(title)}</h2><p>{esc(message)}</p>{ref}{actions}</div>"
    )


def button(href: str, label: str, *, variant: str = "secondary", extra: str = "") -> str:
    suffix = f" {extra}" if extra else ""
    return (
        f'<a class="rep-button {esc(variant)}" href="{esc(href)}"{suffix}>{esc(label)}</a>'
    )


__all__ = [
    "CADENCE_LABELS",
    "COPY",
    "INSTANCE_PATH",
    "LEGACY_FOLDERS_PATH",
    "LIBRARY_PATH",
    "MODULE_LABEL",
    "REPORT_EXPLORER_PAGE_ASSETS",
    "ROLE_LABELS",
    "STATUS_LABELS",
    "STATUS_VARIANTS",
    "button",
    "cadence_label",
    "empty_state",
    "error_state",
    "esc",
    "format_badge",
    "format_count",
    "format_day",
    "format_moment",
    "format_size",
    "metric_label",
    "month_label",
    "query",
    "status_badge",
    "url",
    "visually_hidden",
]
