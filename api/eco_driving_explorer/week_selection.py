"""The month + arbitrary-week ranking basis, canonicalized.

One selection belongs to exactly one client and exactly one reporting month.
Everything a caller can influence is a *selection*, never an authorization: the
client binding is resolved server-side from the session and is not reachable
from anything in this module.

Three modes, resolved by the server so the UI can never submit two at once:

``MONTH``
    Every week bucket of the month is selected. This is the whole reporting
    month. Its **execution source** is resolved by the server and is not a
    second user-facing mode:

    * ``MONTH_PERSISTED`` — a canonical monthly snapshot exists, so that row is
      served. It is the official monthly reporting truth, shared with the
      monthly e-mail and report, and is preferred even when the underlying
      assignments have since changed. History is not silently rewritten.
    * ``MONTH_DYNAMIC`` — no monthly snapshot exists yet for a month that *is*
      backed by assignments, so the full canonical month range is aggregated
      dynamically. A current month must not read as empty merely because
      snapshot materialisation has not run.

    The two are proved equivalent for the same inputs by test.

``WEEKS``
    A proper, non-empty subset of the month's buckets. There is no persisted
    row for such a range and there never will be one, so it is recomputed from
    the underlying assignments over the union of the selected isolated
    intervals.

``EMPTY``
    Zero weeks. An empty state prompting a selection (``EC-5``) — not an error,
    and not a silent widening to the whole month.

The canonical URL form is ``month=YYYY-MM`` plus ``weeks=1,3``: ascending,
deduplicated, month-relative. Whole-month mode omits ``weeks`` entirely, so
"all weeks listed" and "no weeks parameter" collapse to one URL and one mode.
The empty state is the explicit token ``weeks=none``, because an empty query
value disappears when a URL is built and would silently become whole month.
No timestamp ever comes from the browser.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional, Sequence

from .errors import InvalidWeekSelectionError
from .models import PeriodType, RankingPeriodKey
from .period_domain import (
    WeekBucket,
    month_week_buckets,
    next_month_start,
    selection_day_count,
    selection_intervals,
)

# An explicit token, because an empty query value is indistinguishable from an
# absent one once a URL is built — and "no weeks" and "whole month" are
# different states that must never collapse into each other.
EMPTY_WEEKS_TOKEN = "none"

MODE_MONTH = "MONTH"
MODE_WEEKS = "WEEKS"
MODE_EMPTY = "EMPTY"

# How the server actually executed the logical selection. This is diagnostic
# and audit-safe; it is deliberately NOT a user-visible mode, because the user
# made one whole-month selection either way.
BASIS_MONTH_PERSISTED = "MONTH_PERSISTED"
BASIS_MONTH_DYNAMIC = "MONTH_DYNAMIC"
BASIS_WEEKS_DYNAMIC = "WEEKS_DYNAMIC"
BASIS_EMPTY = "EMPTY"

# Selection input is bounded before it is parsed: a month string is 7 characters
# and a week list is at most six one-digit identifiers with separators.
MAX_MONTH_LENGTH = 7
MAX_WEEKS_PARAM_LENGTH = 32


@dataclass(frozen=True)
class WeekSelection:
    """A canonical, bounded ranking basis for one client and one month."""

    month_start_date: date
    all_buckets: tuple[WeekBucket, ...]
    selected_sequences: tuple[int, ...]
    mode: str

    # -- identity -------------------------------------------------------------

    @property
    def month_token(self) -> str:
        return f"{self.month_start_date:%Y-%m}"

    @property
    def month_end_date_exclusive(self) -> date:
        return next_month_start(self.month_start_date)

    @property
    def selected_buckets(self) -> tuple[WeekBucket, ...]:
        chosen = set(self.selected_sequences)
        return tuple(b for b in self.all_buckets if b.sequence in chosen)

    @property
    def is_whole_month(self) -> bool:
        return self.mode == MODE_MONTH

    @property
    def is_dynamic(self) -> bool:
        return self.mode == MODE_WEEKS

    @property
    def is_empty(self) -> bool:
        return self.mode == MODE_EMPTY

    # -- canonical URL state --------------------------------------------------

    @property
    def canonical_weeks_param(self) -> Optional[str]:
        """``None`` for whole month, ``"none"`` for the empty state, else ``1,3``.

        ``None`` means "omit the parameter": whole month is the default basis,
        so its canonical URL carries no week list at all. The empty state is the
        explicit ``none`` token rather than an empty value.
        """

        if self.mode == MODE_MONTH:
            return None
        if self.mode == MODE_EMPTY:
            return EMPTY_WEEKS_TOKEN
        return ",".join(str(sequence) for sequence in self.selected_sequences)

    def canonical_params(self) -> dict[str, Optional[str]]:
        return {"month": self.month_token, "weeks": self.canonical_weeks_param}

    # -- covered range --------------------------------------------------------

    @property
    def intervals(self) -> tuple[tuple[datetime, datetime], ...]:
        """Merged half-open timestamp intervals; empty for the empty state."""

        return selection_intervals(self.selected_buckets)

    @property
    def day_count(self) -> int:
        return selection_day_count(self.selected_buckets)

    @property
    def is_contiguous(self) -> bool:
        """False when the basis has a gap (``EC-4``): a warning, never a block."""

        if len(self.selected_sequences) <= 1:
            return True
        first, last = self.selected_sequences[0], self.selected_sequences[-1]
        return list(self.selected_sequences) == list(range(first, last + 1))

    @property
    def includes_partial_week(self) -> bool:
        return any(bucket.is_partial for bucket in self.selected_buckets)

    @property
    def covered_start_date(self) -> Optional[date]:
        buckets = self.selected_buckets
        return buckets[0].start_date if buckets else None

    @property
    def covered_end_date_exclusive(self) -> Optional[date]:
        buckets = self.selected_buckets
        return buckets[-1].end_date_exclusive if buckets else None

    @property
    def label(self) -> str:
        """``W1 + W3`` — the human name of the basis, never an internal token."""

        return " + ".join(bucket.label for bucket in self.selected_buckets)

    # -- persisted-period identity -------------------------------------------

    def monthly_period_key(self) -> RankingPeriodKey:
        """The canonical persisted monthly period this month resolves to."""

        return RankingPeriodKey(
            period_type=PeriodType.MONTHLY,
            month_start_date=self.month_start_date,
            period_start_date=self.month_start_date,
            period_end_date=self.month_end_date_exclusive,
            period_sequence_in_month=None,
        )

    def synthetic_period_key(self) -> RankingPeriodKey:
        """A period identity for a dynamic basis, used for display plumbing only.

        It is never persisted, never written to a snapshot table and never
        compared against a stored ``period_key``: the covered range of a
        non-contiguous selection is not the range this key describes, which is
        exactly why the basis line — not this key — is the source of truth.
        """

        return RankingPeriodKey(
            period_type=PeriodType.WEEKLY,
            month_start_date=self.month_start_date,
            period_start_date=self.covered_start_date or self.month_start_date,
            period_end_date=self.covered_end_date_exclusive or self.month_start_date,
            period_sequence_in_month=None,
        )


# --- parsing -----------------------------------------------------------------


def parse_month(raw: object) -> date:
    """``YYYY-MM`` to the first of that month. Rejects anything else."""

    text = "" if raw is None else str(raw).strip()
    if not text or len(text) > MAX_MONTH_LENGTH:
        raise InvalidWeekSelectionError("month must use YYYY-MM format")
    try:
        parsed = datetime.strptime(text, "%Y-%m")
    except ValueError as exc:
        raise InvalidWeekSelectionError("month must use YYYY-MM format") from exc
    return parsed.date().replace(day=1)


def parse_week_selection(month: object, weeks: object = None) -> WeekSelection:
    """Canonicalize one ``month`` + ``weeks`` pair into a single resolved mode.

    * duplicates collapse;
    * order is forced ascending;
    * a week identifier that does not exist in *this* month is rejected, so a
      forged or stale cross-month combination cannot silently widen the basis.
      Validity is month-relative: ``W6`` is legitimate in a month that has six
      buckets and refused in one that does not, and no timestamp is ever
      derived from an identifier the month does not define;
    * listing every week is the same state as omitting ``weeks``.
    """

    month_start = parse_month(month)
    buckets = month_week_buckets(month_start)
    valid = {bucket.sequence for bucket in buckets}

    if weeks is None:
        return WeekSelection(month_start, buckets, tuple(sorted(valid)), MODE_MONTH)

    text = str(weeks).strip()
    if len(text) > MAX_WEEKS_PARAM_LENGTH:
        raise InvalidWeekSelectionError("week selection is too long")
    if text == "" or text.casefold() == EMPTY_WEEKS_TOKEN:
        return WeekSelection(month_start, buckets, (), MODE_EMPTY)

    selected: set[int] = set()
    for token in text.split(","):
        candidate = token.strip()
        if candidate == "":
            continue
        if not candidate.isdigit():
            raise InvalidWeekSelectionError("week identifiers must be positive integers")
        sequence = int(candidate)
        if sequence not in valid:
            raise InvalidWeekSelectionError("week identifier does not exist in this month")
        selected.add(sequence)

    if not selected:
        return WeekSelection(month_start, buckets, (), MODE_EMPTY)
    if selected == valid:
        return WeekSelection(month_start, buckets, tuple(sorted(valid)), MODE_MONTH)
    return WeekSelection(month_start, buckets, tuple(sorted(selected)), MODE_WEEKS)


def whole_month_selection(month_start: date) -> WeekSelection:
    buckets = month_week_buckets(month_start.replace(day=1))
    return WeekSelection(
        month_start.replace(day=1),
        buckets,
        tuple(bucket.sequence for bucket in buckets),
        MODE_MONTH,
    )


def toggle_weeks(selection: WeekSelection, sequence: int) -> tuple[int, ...]:
    """The selected set after toggling one card — always ascending and unique."""

    current = set(selection.selected_sequences)
    if sequence in current:
        current.discard(sequence)
    else:
        current.add(sequence)
    return tuple(sorted(current))


def weeks_param_for(sequences: Sequence[int], selection: WeekSelection) -> Optional[str]:
    """Canonical ``weeks`` value for an arbitrary set inside this month."""

    ordered = tuple(sorted(set(int(value) for value in sequences)))
    if not ordered:
        return EMPTY_WEEKS_TOKEN
    if ordered == tuple(bucket.sequence for bucket in selection.all_buckets):
        return None
    return ",".join(str(value) for value in ordered)
