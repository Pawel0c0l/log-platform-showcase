"""The platform's one place for rendering a date, a time or a distance.

WHY THIS MODULE EXISTS. Before it, four surfaces rendered a timestamp four
different ways — the Eco trips table as raw ISO 8601 with an offset, the Eco
detail preview as a string-sliced `YYYY-MM-DD HH:MM`, the Database Explorer grid
as `%Y-%m-%d %H:%M`, and the Report Explorer as `%d.%m.%Y %H:%M` — and
`fallback()` existed in three copies. Four conventions is not a matter of style; two
tables showing the same trip disagreed about what time it started.

WHAT IS AND IS NOT IN SCOPE HERE. These functions are PRESENTATION. They are
never used to compute, compare, sort, filter, round for scoring, or build a
filename, an identifier, a log line or a JSON payload. Machine-facing surfaces
keep ISO 8601 precisely because a machine has to parse them; a person does not.

TIMEZONE. Rendered times are in the business timezone and the rendering does NOT
carry an offset, so the zone is a property of the platform rather than of the
string. That is safe here only because it is uniform: the portal connects to the
platform database and to every client business database as one role, and that
role carries `TimeZone=Europe/Warsaw` for all databases, so every `TIMESTAMPTZ`
psycopg hands back is already business-local. Verified rather than assumed —
the server's own compiled default is GMT.

One consequence to know rather than discover: during the autumn DST fold two
instants an hour apart render identically, because the offset that used to
distinguish them is gone. That is inherent to the requested format, not an
oversight.

DECIDED, 2026-08-20: the owner was shown that ambiguity and chose to ACCEPT ONE
AMBIGUOUS HOUR A YEAR rather than carry a `CET`/`CEST` suffix or a conditional
one. Do not "fix" it by reintroducing an offset or a zone abbreviation into the
rendering — the clean format was the point, and the trade was made with the
consequence in front of them.

WHICH SURFACES STATE THE ZONE, decided 2026-08-27. This module used to say the
ambiguity "is why any surface that renders these must also say which zone it
means". That read as a universal obligation, and it was never true: when it was
written exactly one surface discharged it. The owner was shown the five that did
not and chose to add the statement to ONE of them.

* the Eco contributing-trip table — a footnote naming the zone;
* the Database Explorer grid — a note under the pager, plus the exact source
  value in each cell's `title`.

Both read the zone from `timezone_label()` rather than writing it into prose.
The Report Explorer, the Eco driver-detail page, the Eco ranking footer and the
Database Explorer's error trio deliberately do NOT state it: they render few
timestamps, from known platform tables, in a context that already says what the
value is. That is a decision, not an omission — do not "complete" it.

The Database Explorer was singled out because it is the sharp case: hundreds of
timestamp cells per page, drawn from arbitrary client tables, and since
2026-08-27 its clipboard carries the displayed value rather than the source, so
the note and the tooltip are where the exact instant remains available.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

if __package__ and __package__.startswith("api."):
    from ..timezone_utils import get_business_timezone_name
else:  # pragma: no cover - import-path parity with the rest of the package
    from timezone_utils import get_business_timezone_name

#: The em-dash every surface already used for an absent value.
EMPTY = "—"

DATE_FORMAT = "%d.%m.%Y"
DATETIME_FORMAT = "%d.%m.%Y %H:%M:%S"

#: Display precision for a distance. Two places keeps a 99 m trip distinct from
#: a 0 m one, which one place would not — this data contains many of both.
DISTANCE_DECIMALS = 2


def timezone_label() -> str:
    """The zone rendered times are in, for a surface that must state it.

    Read from configuration rather than written into prose, so a footnote citing
    it cannot quietly become false the way the ISO-8601 one did.
    """

    return get_business_timezone_name()


def fallback(value: Any, label: str = EMPTY) -> str:
    """An absent value as the shared placeholder, wrapped so it reads as muted.

    Consolidated from three identical copies. Callers that only want the text
    use `EMPTY`; this returns the markup those copies returned.
    """

    if value is None or value == "":
        return f'<span class="eco-muted">{label}</span>'
    from html import escape

    return escape(str(value), quote=True)


def _as_datetime(value: Any) -> Optional[datetime]:
    """A datetime from a datetime or an ISO string, or None if it is neither.

    Accepting the string form is what lets a page format at render time while the
    JSON serializer that produced it keeps emitting ISO 8601 for the API. The
    two surfaces share a payload; they do not have to share a presentation.
    """

    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _as_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def format_date(value: Any, empty: str = EMPTY) -> str:
    """`dd.mm.yyyy`, or the placeholder when there is nothing to render."""

    moment = _as_date(value)
    return moment.strftime(DATE_FORMAT) if moment else empty


def format_datetime(value: Any, empty: str = EMPTY) -> str:
    """`dd.mm.yyyy gg:mm:ss`.

    Seconds are kept deliberately. These tables are ranking evidence, and a
    31-second trip has to stay distinguishable from a 0-second one; minute
    precision would collapse them.

    A value that will not parse is returned as the placeholder rather than as
    itself, so a surface can never silently fall back to showing raw ISO.
    """

    moment = _as_datetime(value)
    return moment.strftime(DATETIME_FORMAT) if moment else empty


def format_distance_km(metres: Any, empty: str = EMPTY, *, unit: bool = True) -> str:
    """`X,YY km` from a distance in METRES.

    `unit=False` returns just `X,YY`, for the handful of sites where the unit is
    already part of a translated sentence — `"... przejechał {km} km"` would
    otherwise read "22,90 km km". Those are prose, not table cells; a cell always
    carries its unit.

    Takes metres rather than a pre-divided figure on purpose: metres is the
    stored truth, and every caller that had already converted was rounding
    differently on the way. The Polish decimal comma matches the rest of the
    Polish-language UI, and the unit is part of the rendering because a bare
    number in a column headed `Dystans` was ambiguous between the two.

    DISPLAY ONLY. Two decimals is a rendering choice; the unrounded metres stays
    in the payload for sorting, filtering and anything that computes.
    """

    if metres is None or metres == "":
        return empty
    try:
        km = Decimal(str(metres)) / Decimal(1000)
    except (TypeError, ValueError, ArithmeticError, InvalidOperation):
        return empty
    quantised = km.quantize(Decimal("1." + "0" * DISTANCE_DECIMALS))
    rendered = f"{quantised:.{DISTANCE_DECIMALS}f}".replace(".", ",")
    return f"{rendered} km" if unit else rendered
