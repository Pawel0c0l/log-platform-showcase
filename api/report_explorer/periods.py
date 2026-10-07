"""Reporting-period vocabulary, validation and presentation.

`docs/39` §4–§5 and `docs/40` §5 are binding here, and the whole module exists to
make one prohibition structural: **Report Explorer never computes a reporting
period.** A period is a declaration of the report definition, and this module's
job is to (a) let a generator declare one *canonically*, so the same week is
always the same key, and (b) render a declared one.

Nothing here reads a filename, an artifact timestamp, a generation timestamp or
a folder name. `canonical_period()` takes a kind and a date and derives the
calendar period that date falls in — which is a *calendar* fact, not an
inference about a file — and every derived value it returns is exactly what the
migration's alignment CHECK will accept.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import NamedTuple

# The platform's business time zone, and this module's declared default. A
# definition that means anything else says so in `period_timezone`; a reader
# must never assume (`docs/40` §5).
DEFAULT_PERIOD_TIMEZONE = "Europe/Warsaw"

PERIOD_KINDS = ("week", "month", "quarter", "day", "none")

# Locative case, because the approved group heading is
# `Wygenerowane w lipcu 2026` — "generated IN July".
_MONTHS_LOCATIVE = (
    "styczniu", "lutym", "marcu", "kwietniu", "maju", "czerwcu",
    "lipcu", "sierpniu", "wrześniu", "październiku", "listopadzie", "grudniu",
)


class InvalidPeriodError(ValueError):
    """A declared period is not internally consistent."""


@dataclass(frozen=True)
class ReportingPeriod:
    """One declared reporting period.

    `end` is INCLUSIVE — the approved rendering `06–12.07.2026` covers both
    endpoints — and `key` is a stable identity (`2026-W28`), never an
    incrementing counter.
    """

    kind: str
    key: str
    start: date
    end: date
    timezone: str = DEFAULT_PERIOD_TIMEZONE

    def __post_init__(self) -> None:
        if self.kind not in PERIOD_KINDS:
            raise InvalidPeriodError(f"unknown period kind: {self.kind!r}")
        if not self.key or len(self.key) > 40:
            raise InvalidPeriodError("period key must be 1..40 characters")
        if self.start > self.end:
            raise InvalidPeriodError("period_start must not be after period_end")
        # Validation calls the pure calendar helper, never `canonical_period`:
        # the constructor cannot ask a factory that constructs it.
        expected_start, expected_end, derived_key = _calendar_bounds(self.kind, self.start)
        expected = _Bounds(expected_start, expected_end)
        if (expected.start, expected.end) != (self.start, self.end):
            # `I-3`. A key that disagrees with its dates is invalid data, not a
            # display quirk, and the database would refuse it anyway — refusing
            # here means the generator learns at the boundary rather than at
            # COMMIT.
            raise InvalidPeriodError(
                f"{self.kind} period {self.key} must span {expected.start}..{expected.end}, "
                f"not {self.start}..{self.end}"
            )
        if self.key != derived_key:
            # THE KEY IS A NAME, NOT AN IDENTITY. An independent review found
            # that a caller-chosen key over canonical dates created a second
            # logical identity for one period: the row was accepted under the
            # custom key, and the canonical retry that followed collided with it
            # on the period-start uniqueness instead of reusing it. There is now
            # exactly one name for one calendar period, here and in migration
            # `069`'s `period_key` CHECK, which enforces the same derivation in
            # the database.
            raise InvalidPeriodError(
                f"{self.kind} period {self.start}..{self.end} is named {derived_key!r}, "
                f"not {self.key!r}; a reporting period has one canonical key"
            )

    @property
    def instant_range(self) -> tuple[date, date]:
        """The half-open `[start, end + 1 day)` range for a date filter.

        Derived on demand and never stored (`docs/40` §5). `RP-18` applies it to
        Database Explorer, whose date filters are half-open at the top.
        """
        return self.start, self.end + timedelta(days=1)

    @property
    def label(self) -> str:
        return format_period_range(self.start, self.end)


class _Bounds(NamedTuple):
    start: date
    end: date


def _calendar_bounds(kind: str, day: date) -> tuple[date, date, str]:
    """`(start, end, derived_key)` of the calendar period of `kind` containing `day`.

    Pure, and deliberately free of `ReportingPeriod`: it is what BOTH the
    constructor's consistency check and `canonical_period` read, so neither can
    call the other. Its output is exactly what the migration's alignment CHECK
    accepts.
    """
    if kind == "week":
        start = day - timedelta(days=day.weekday())
        end = start + timedelta(days=6)
        iso_year, iso_week, _ = start.isocalendar()
        return start, end, f"{iso_year:04d}-W{iso_week:02d}"
    if kind == "month":
        start = day.replace(day=1)
        return start, _add_months(start, 1) - timedelta(days=1), f"{start.year:04d}-{start.month:02d}"
    if kind == "quarter":
        start = _quarter_start(day)
        return (
            start,
            _add_months(start, 3) - timedelta(days=1),
            f"{start.year:04d}-Q{(start.month - 1) // 3 + 1}",
        )
    if kind in ("day", "none"):
        return day, day, day.isoformat()
    raise InvalidPeriodError(f"unknown period kind: {kind!r}")


def _quarter_start(day: date) -> date:
    return date(day.year, 3 * ((day.month - 1) // 3) + 1, 1)


def _add_months(day: date, months: int) -> date:
    total = (day.year * 12 + (day.month - 1)) + months
    return date(total // 12, total % 12 + 1, 1)


def canonical_period(
    kind: str,
    day: date,
    *,
    key: str | None = None,
    timezone: str = DEFAULT_PERIOD_TIMEZONE,
) -> ReportingPeriod:
    """The calendar period of `kind` that contains `day`.

    This is the one supported way for a generator to *declare* a period, so two
    generators cannot disagree about what `2026-W28` means. It is a calendar
    computation over a date the generator chose, never an inference from a file.

    `key` is kept as a parameter so an existing caller that passes the key it
    believes it is asking for still compiles — but it is now an ASSERTION, not
    an override: a key that disagrees with the calendar is refused rather than
    persisted. A period has one canonical name.
    """
    start, end, derived = _calendar_bounds(kind, day)
    if key is not None and str(key) != derived:
        raise InvalidPeriodError(
            f"the {kind} period containing {day} is named {derived!r}, not {key!r}"
        )
    return ReportingPeriod(kind=kind, key=derived, start=start, end=end, timezone=timezone)


def canonical_period_key(kind: str, start: date) -> str:
    """The one name of the calendar period of `kind` starting on `start`.

    The Python side of migration `069`'s `period_key` CHECK. Both derive the
    same string from the same two facts, which is what makes the persisted key a
    projection of the dates rather than a competing identity.
    """
    _start, _end, derived = _calendar_bounds(kind, start)
    return derived


def adjacent_period(period: ReportingPeriod, *, forward: bool) -> ReportingPeriod:
    """The next or previous calendar period of the same kind.

    Used only to *describe* adjacency; which sibling actually exists is decided
    by the persisted instances, never by this function (`RP-16`).
    """
    step = 1 if forward else -1
    if period.kind == "week":
        anchor = period.start + timedelta(days=7 * step)
    elif period.kind == "month":
        anchor = _add_months(period.start, step)
    elif period.kind == "quarter":
        anchor = _add_months(period.start, 3 * step)
    else:
        anchor = period.start + timedelta(days=step)
    return canonical_period(period.kind, anchor, timezone=period.timezone)


def format_period_range(start: date, end: date) -> str:
    """`06–12.07.2026`, or `29.06–05.07.2026` when the period spans months.

    The approved prototype renders both forms; the compact one is used whenever
    it is unambiguous.
    """
    if start == end:
        return f"{start.day:02d}.{start.month:02d}.{start.year}"
    if (start.year, start.month) == (end.year, end.month):
        return f"{start.day:02d}–{end.day:02d}.{end.month:02d}.{end.year}"
    if start.year == end.year:
        return f"{start.day:02d}.{start.month:02d}–{end.day:02d}.{end.month:02d}.{end.year}"
    return (
        f"{start.day:02d}.{start.month:02d}.{start.year}–"
        f"{end.day:02d}.{end.month:02d}.{end.year}"
    )


def generation_month_label(moment) -> str:
    """`Wygenerowane w lipcu 2026` — the approved group heading (`RP-3`).

    Named for what it groups by: the GENERATION month. It is a presentation rule
    over the library timestamp and is never a substitute for the reporting
    period (`docs/39` §5).
    """
    return f"Wygenerowane w {_MONTHS_LOCATIVE[moment.month - 1]} {moment.year}"


def generation_month_key(moment) -> str:
    return f"{moment.year:04d}-{moment.month:02d}"
