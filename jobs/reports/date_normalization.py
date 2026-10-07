"""Shared, column-aware date/datetime normalization for Workflow B Stage 1.

This module canonicalizes recognized date/datetime columns when raw email
report attachments (CSV / XLS / XLSX / XLSM) are converted to the normalized
CSV artifacts in :mod:`jobs.mail.fetch_reports`.

Goals / contract:

* Recognized date columns are emitted in one stable, human-readable format:
    - date-only   -> ``DD.MM.YYYY``
    - date + time -> ``DD.MM.YYYY HH:MM`` (seconds are rounded to the minute)
* Detection is column-aware and/or report-aware. Numeric strings are only ever
  interpreted as Excel serial dates when the column is a *confirmed* date column
  (report-specific schema or a temporal header name). Arbitrary numeric columns
  are never touched.
* Values that cannot be parsed confidently are preserved verbatim and flagged in
  the returned metadata so they can be debugged downstream.
* The Excel serial base and accepted serial window match the existing Stage 2
  validation / Stage 3 migration code (1899-12-30; year 2000-2100), so a second
  inconsistent serial conversion is not introduced.

The module intentionally depends only on the standard library so that
``jobs.mail.fetch_reports`` keeps its stdlib-only footprint.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta


# Excel "1900" date system with the leap-year-bug compatible base, identical to
# jobs/reports/stage2/validation.py and the report_207 migration SQL.
EXCEL_SERIAL_DATE_BASE = datetime(1899, 12, 30)
# Accepted serial window, mirrored from validation.py / the migration SQL.
EXCEL_SERIAL_MIN_DATETIME = datetime(2000, 1, 1)
EXCEL_SERIAL_MAX_DATETIME = datetime(2100, 1, 1)

_NUMERIC_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")
# Trailing timezone suffixes we tolerate on text values (e.g. "+00", "+0000",
# "+00:00", "Z", " UTC"). We keep the displayed wall-clock time and drop the
# offset (see module docstring / docs note: no silent timezone conversion). The
# lookbehind requires a preceding time component so an ISO date like
# "2026-05-04" never has its "-04" day mistaken for a "-04" UTC offset.
_TZ_SUFFIX_RE = re.compile(r"(?<=\d:\d\d)\s*(?:Z|UTC|[+-]\d{2}(?::?\d{2})?)$", re.IGNORECASE)

DATE_OUTPUT_FORMAT = "%d.%m.%Y"
DATETIME_OUTPUT_FORMAT = "%d.%m.%Y %H:%M"

# Ordered (format, has_time) text formats we accept on input. Order matters:
# more specific formats are tried before less specific ones.
_TEXT_FORMATS: tuple[tuple[str, bool], ...] = (
    ("%Y-%m-%d %H:%M:%S", True),
    ("%Y-%m-%d %H:%M", True),
    ("%Y-%m-%d", False),
    ("%Y/%m/%d %H:%M:%S", True),
    ("%Y/%m/%d %H:%M", True),
    ("%Y/%m/%d", False),
    ("%d.%m.%Y %H:%M:%S", True),
    ("%d.%m.%Y %H:%M", True),
    ("%d.%m.%Y", False),
    ("%d/%m/%Y %H:%M:%S", True),
    ("%d/%m/%Y %H:%M", True),
    ("%d/%m/%Y", False),
)


@dataclass(frozen=True)
class DateColumn:
    """A column that has been recognized as carrying date/datetime values."""

    kind: str  # "date" | "datetime"
    serial_ok: bool
    source: str  # "report_207" | "generic"


@dataclass
class CellDateResult:
    """Outcome of normalizing a single cell in a recognized date column."""

    status: str  # "empty" | "normalized" | "passthrough" | "unparseable"
    normalized_text: str | None = None  # set only when status == "normalized"
    family: str | None = None  # detected input family (for metadata)
    emitted_kind: str | None = None  # "date" | "datetime" when normalized


# Report-specific known date/datetime columns (checked first). Keys are
# diacritic-folded, lowercased, whitespace-collapsed header names.
_REPORT_DATE_COLUMNS: dict[str, DateColumn] = {
    # report_207 "Data i czas" is the recognized datetime column that feeds the
    # report_207 -> client_trips speeding migration.
    "data i czas": DateColumn(kind="datetime", serial_ok=True, source="report_207"),
}

# Whole-word temporal tokens used by the conservative generic detector.
_DATE_WORDS = {"data", "date", "daty", "dacie"}
_TIME_WORDS = {"czas", "czasu", "godzina", "godziny", "time"}
# Compact substrings (no surrounding word boundary needed).
_DATETIME_SUBSTRINGS = ("datetime", "timestamp", "event_ts", "_ts")


def fold_header(text: object) -> str:
    """Diacritic-fold, lowercase and whitespace-collapse a header label."""

    decomposed = unicodedata.normalize("NFKD", str(text if text is not None else ""))
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    stripped = stripped.strip().lower()
    stripped = re.sub(r"\s+", " ", stripped)
    return stripped.rstrip(":").strip()


def classify_date_column(header: object) -> DateColumn | None:
    """Classify a header label as a date/datetime column, or ``None``.

    Report-specific names win first; then a conservative generic heuristic based
    on temporal header words. Ambiguous structural words (start/end/from/to) are
    deliberately *not* matched on their own to avoid mislabeling numeric columns.
    """

    key = fold_header(header)
    if not key:
        return None

    report_specific = _REPORT_DATE_COLUMNS.get(key)
    if report_specific is not None:
        return report_specific

    if any(token in key for token in _DATETIME_SUBSTRINGS):
        return DateColumn(kind="datetime", serial_ok=True, source="generic")

    words = set(re.split(r"[^a-z0-9]+", key))
    words.discard("")
    has_date_word = bool(words & _DATE_WORDS)
    has_time_word = bool(words & _TIME_WORDS)
    if has_date_word and has_time_word:
        return DateColumn(kind="datetime", serial_ok=True, source="generic")
    if has_time_word:
        return DateColumn(kind="datetime", serial_ok=True, source="generic")
    if has_date_word:
        return DateColumn(kind="date", serial_ok=True, source="generic")
    return None


def _round_to_minute(dt: datetime) -> datetime:
    base = dt.replace(microsecond=0)
    if dt.microsecond >= 500_000:
        base = base + timedelta(seconds=1)
    if base.second >= 30:
        base = base + timedelta(seconds=60 - base.second)
    else:
        base = base.replace(second=0)
    return base.replace(second=0, microsecond=0)


def _format(dt: datetime, *, column_kind: str, has_time: bool) -> tuple[str, str]:
    emit_datetime = column_kind == "datetime" or has_time
    if emit_datetime:
        rounded = _round_to_minute(dt)
        return rounded.strftime(DATETIME_OUTPUT_FORMAT), "datetime"
    return dt.strftime(DATE_OUTPUT_FORMAT), "date"


def _serial_to_datetime(serial: float) -> datetime | None:
    if not math.isfinite(serial):
        return None
    try:
        dt = EXCEL_SERIAL_DATE_BASE + timedelta(days=serial)
    except OverflowError:
        return None
    if not (EXCEL_SERIAL_MIN_DATETIME <= dt <= EXCEL_SERIAL_MAX_DATETIME):
        return None
    return dt


def _serial_has_time(serial: float) -> bool:
    # A whole-day serial encodes a date only; a fractional part encodes a time.
    return abs(serial - round(serial)) > 1e-9


def _parse_text(value: str) -> tuple[datetime, bool] | None:
    stripped = _TZ_SUFFIX_RE.sub("", value).strip()
    stripped = stripped.replace("T", " ")
    stripped = re.sub(r"\s+", " ", stripped)
    if not stripped:
        return None
    for fmt, has_time in _TEXT_FORMATS:
        try:
            return datetime.strptime(stripped, fmt), has_time
        except ValueError:
            continue
    return None


def normalize_date_cell(value: object, column: DateColumn) -> CellDateResult:
    """Normalize a single raw cell value for a recognized date column."""

    if value is None:
        return CellDateResult(status="empty", family="empty")

    if isinstance(value, bool):
        # Booleans must never be treated as Excel serials.
        return CellDateResult(status="unparseable", family="bool")

    if isinstance(value, datetime):
        has_time = (value.hour, value.minute, value.second, value.microsecond) != (0, 0, 0, 0)
        text, emitted = _format(value, column_kind=column.kind, has_time=has_time)
        return CellDateResult(status="normalized", normalized_text=text, family="datetime_object", emitted_kind=emitted)

    if isinstance(value, date):
        text, emitted = _format(
            datetime(value.year, value.month, value.day),
            column_kind=column.kind,
            has_time=False,
        )
        return CellDateResult(status="normalized", normalized_text=text, family="date_object", emitted_kind=emitted)

    if isinstance(value, (int, float)):
        serial = float(value)
        if not math.isfinite(serial):
            return CellDateResult(status="unparseable", family="non_finite")
        if not column.serial_ok:
            return CellDateResult(status="passthrough", family="numeric_non_serial")
        dt = _serial_to_datetime(serial)
        if dt is None:
            return CellDateResult(status="unparseable", family="excel_serial_out_of_range")
        text, emitted = _format(dt, column_kind=column.kind, has_time=_serial_has_time(serial))
        return CellDateResult(status="normalized", normalized_text=text, family="excel_serial", emitted_kind=emitted)

    text_value = re.sub(r"\s+", " ", str(value).strip())
    if not text_value:
        return CellDateResult(status="empty", family="empty")

    parsed = _parse_text(text_value)
    if parsed is not None:
        dt, has_time = parsed
        text, emitted = _format(dt, column_kind=column.kind, has_time=has_time)
        return CellDateResult(status="normalized", normalized_text=text, family="text", emitted_kind=emitted)

    if _NUMERIC_RE.fullmatch(text_value):
        if not column.serial_ok:
            return CellDateResult(status="passthrough", family="numeric_non_serial")
        serial = float(text_value)
        dt = _serial_to_datetime(serial)
        if dt is None:
            return CellDateResult(status="unparseable", family="excel_serial_out_of_range")
        text, emitted = _format(dt, column_kind=column.kind, has_time=_serial_has_time(serial))
        return CellDateResult(status="normalized", normalized_text=text, family="excel_serial", emitted_kind=emitted)

    return CellDateResult(status="unparseable", family="text")


@dataclass
class ColumnDateStats:
    """Accumulated per-column normalization metadata for one normalized CSV."""

    column: str
    treated_as: str
    source: str
    input_format_families: dict[str, int] = field(default_factory=dict)
    parsed: int = 0
    empty: int = 0
    unparseable: int = 0
    unparseable_sample: list[str] = field(default_factory=list)

    def record(self, result: CellDateResult, raw_text: str) -> None:
        if result.family:
            self.input_format_families[result.family] = (
                self.input_format_families.get(result.family, 0) + 1
            )
        if result.status == "normalized":
            self.parsed += 1
        elif result.status == "empty":
            self.empty += 1
        elif result.status == "unparseable":
            self.unparseable += 1
            if len(self.unparseable_sample) < 10 and raw_text:
                self.unparseable_sample.append(raw_text)
        # "passthrough" cells (numeric values in non-serial columns) are left as-is
        # and intentionally not counted as parsed/unparseable.

    def as_dict(self) -> dict:
        return {
            "column": self.column,
            "treated_as": self.treated_as,
            "source": self.source,
            "input_format_families": dict(self.input_format_families),
            "parsed": self.parsed,
            "empty": self.empty,
            "unparseable": self.unparseable,
            "unparseable_sample": list(self.unparseable_sample),
        }
