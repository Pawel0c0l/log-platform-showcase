from __future__ import annotations

import re
from datetime import date as Date, datetime, time as Time
from decimal import Decimal, InvalidOperation
from typing import Iterable

import pandas as pd

from jobs.reports.stage2.models import CleanedReport
from jobs.reports.stage2.types.common import _from_rows
from jobs.reports.stage2.validation import _parse_datetime_value


class D1052EcoDrivingReport:
    """Special D105.2 variant carrying EcoDriving/event trip metrics."""

    TYPE = "report_d105_2_ecodriving"
    report_type = TYPE
    REQUIRED_COLUMNS = {
        "Nr Rejestracyjny",
        "Czas rozpoczęcia",
        "Czas zakończenia",
        "przekroczenia obr/min",
        "> 140kmh",
        "> 160kmh",
        "> 170kmh",
    }
    OPTIONAL_COLUMNS = set()
    COLUMN_TYPES = {
        "Czas rozpoczęcia": "date",
        "Czas zakończenia": "date",
        "przekroczenia obr/min": "float",
        "> 140kmh": "float",
        "> 160kmh": "float",
        "> 170kmh": "float",
    }
    REGISTRY_COLUMN_TYPES = {
        "Nr Rejestracyjny": "string",
        "Czas rozpoczęcia": "datetime",
        "Czas zakończenia": "datetime",
        "przekroczenia obr/min": "numeric",
        "> 140kmh": "numeric",
        "> 160kmh": "numeric",
        "> 170kmh": "numeric",
    }
    MULTI_TABLE = False

    COLUMNS = [
        "Nr Rejestracyjny",
        "Czas rozpoczęcia",
        "Czas zakończenia",
        "przekroczenia obr/min",
        "> 140kmh",
        "> 160kmh",
        "> 170kmh",
    ]
    SPLIT_TIMESTAMP_COLUMNS = [
        "Data rozpoczęcia",
        "Data ukończenia",
        "Czas rozpoczęcia",
        "Czas zakończenia",
    ]

    @classmethod
    def detect(cls, sample: pd.DataFrame | list[pd.DataFrame]) -> float:
        tables = sample if isinstance(sample, list) else [sample]
        return 1.0 if any(_find_header_idx(df, cls.COLUMNS) is not None for df in tables) else 0.0

    @classmethod
    def clean(cls, df_or_tables) -> CleanedReport:
        df = df_or_tables[0] if isinstance(df_or_tables, list) else df_or_tables
        header_idx = _find_header_idx(df, cls.COLUMNS)
        if header_idx is None:
            return CleanedReport(report_type=cls.TYPE, df=pd.DataFrame(), metadata={"warning": "header_not_found"})

        raw_header = [_normalize_cell(value) for value in df.iloc[header_idx].tolist()]
        positions_by_key = {
            _norm_key(_canonical_column_name(value)): idx
            for idx, value in enumerate(raw_header)
            if _normalize_cell(value)
        }
        split_timestamps = _has_split_timestamp_columns(positions_by_key)

        output_columns: list[str] = []
        used_keys: set[str] = set()
        for label in raw_header:
            if not label:
                continue
            canonical = _canonical_column_name(label)
            key = _norm_key(canonical)
            if key in used_keys:
                continue
            used_keys.add(key)
            output_columns.append(canonical)

        # Split FleetWeb exports carry separate date and time columns. The
        # migration contract still consumes the canonical combined timestamp
        # columns, so make sure they are present even if the raw date columns
        # appear before the time columns in the sheet.
        for canonical in cls.COLUMNS:
            if _norm_key(canonical) not in used_keys:
                output_columns.append(canonical)
                used_keys.add(_norm_key(canonical))

        missing = [label for label in cls.COLUMNS if _norm_key(label) not in positions_by_key]
        if split_timestamps:
            missing = [label for label in missing if label not in {"Czas rozpoczęcia", "Czas zakończenia"}]
        if missing:
            return CleanedReport(
                report_type=cls.TYPE,
                df=pd.DataFrame(),
                metadata={"warning": "required_columns_missing_after_header_detection", "missing": missing},
            )

        rows: list[list[str]] = []
        invalid_registration_rows = 0
        invalid_timestamp_rows = 0
        invalid_metric_rows = 0
        skipped_empty_rows = 0
        header_keys = {_norm_key(label) for label in cls.COLUMNS}
        split_header_keys = {_norm_key(label) for label in _split_required_columns()}

        for row_idx in range(header_idx + 1, len(df)):
            raw_values = df.iloc[row_idx].tolist()
            if not any(_normalize_cell(value) for value in raw_values):
                skipped_empty_rows += 1
                continue

            normalized_by_key: dict[str, str] = {}
            for column in output_columns:
                key = _norm_key(column)
                if split_timestamps and column == "Czas rozpoczęcia":
                    normalized_by_key[key] = _normalize_datetime_parts(
                        _raw_value(raw_values, positions_by_key, "Data rozpoczęcia"),
                        _raw_value(raw_values, positions_by_key, "Czas rozpoczęcia"),
                    )
                    continue
                if split_timestamps and column == "Czas zakończenia":
                    normalized_by_key[key] = _normalize_datetime_parts(
                        _raw_value(raw_values, positions_by_key, "Data ukończenia"),
                        _raw_value(raw_values, positions_by_key, "Czas zakończenia"),
                    )
                    continue
                pos = positions_by_key.get(key)
                raw_value = raw_values[pos] if pos is not None and pos < len(raw_values) else ""
                normalized_by_key[key] = _normalize_output_value(column, raw_value)

            row_keys = {
                _norm_key(_canonical_column_name(_normalize_cell(value)))
                for value in raw_values
                if _normalize_cell(value)
            }
            if header_keys.issubset(row_keys) or split_header_keys.issubset(row_keys):
                continue

            if not any(normalized_by_key.get(_norm_key(column), "") for column in output_columns):
                skipped_empty_rows += 1
                continue

            if not normalized_by_key.get(_norm_key("Nr Rejestracyjny"), ""):
                invalid_registration_rows += 1
            if (
                not _valid_normalized_datetime(normalized_by_key.get(_norm_key("Czas rozpoczęcia"), ""))
                or not _valid_normalized_datetime(normalized_by_key.get(_norm_key("Czas zakończenia"), ""))
            ):
                invalid_timestamp_rows += 1
            if any(
                not _valid_normalized_count(normalized_by_key.get(_norm_key(column), ""))
                for column in ["przekroczenia obr/min", "> 140kmh", "> 160kmh", "> 170kmh"]
            ):
                invalid_metric_rows += 1

            rows.append([normalized_by_key.get(_norm_key(column), "") for column in output_columns])

        return _from_rows(
            cls.TYPE,
            output_columns,
            rows,
            metadata={
                "header_row_index": header_idx,
                "split_timestamp_columns": split_timestamps,
                "skipped_empty_rows": skipped_empty_rows,
                "invalid_registration_rows": invalid_registration_rows,
                "invalid_timestamp_rows": invalid_timestamp_rows,
                "invalid_metric_rows": invalid_metric_rows,
            },
        )


def _normalize_cell(value) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except TypeError:
        pass
    return " ".join(str(value).strip().split())


def _norm_key(value: str) -> str:
    return _normalize_cell(value).casefold()


def _canonical_column_name(label: str) -> str:
    key = _norm_key(label)
    metric_aliases = {
        ">140kmh": "> 140kmh",
        ">160kmh": "> 160kmh",
        ">170kmh": "> 170kmh",
    }
    if key in metric_aliases:
        return metric_aliases[key]
    for canonical in D1052EcoDrivingReport.COLUMNS + D1052EcoDrivingReport.SPLIT_TIMESTAMP_COLUMNS:
        if key == _norm_key(canonical):
            return canonical
    return _normalize_cell(label)


def _split_required_columns() -> list[str]:
    return [
        "Nr Rejestracyjny",
        "Data rozpoczęcia",
        "Data ukończenia",
        "Czas rozpoczęcia",
        "Czas zakończenia",
        "przekroczenia obr/min",
        "> 140kmh",
        "> 160kmh",
        "> 170kmh",
    ]


def _has_split_timestamp_columns(positions_by_key: dict[str, int]) -> bool:
    return all(_norm_key(label) in positions_by_key for label in D1052EcoDrivingReport.SPLIT_TIMESTAMP_COLUMNS)


def _find_header_idx(df: pd.DataFrame, labels: Iterable[str], *, max_scan: int = 30) -> int | None:
    wanted = {_norm_key(label) for label in labels}
    split_wanted = {_norm_key(label) for label in _split_required_columns()}
    for idx in range(min(len(df), max_scan)):
        row_labels = {_norm_key(_canonical_column_name(value)) for value in df.iloc[idx].tolist() if _normalize_cell(value)}
        if wanted.issubset(row_labels) or split_wanted.issubset(row_labels):
            return idx
    return None


def _raw_value(raw_values: list, positions_by_key: dict[str, int], column: str):
    pos = positions_by_key.get(_norm_key(column))
    return raw_values[pos] if pos is not None and pos < len(raw_values) else ""


def _normalize_datetime(value) -> str:
    text = _normalize_cell(value)
    if not text:
        return ""
    parsed = _parse_datetime_value(text)
    if parsed is None:
        return text
    return parsed.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


def _date_part(value) -> Date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, Date):
        return value
    parsed = _parse_datetime_value(_normalize_cell(value))
    return parsed.date() if parsed is not None else None


def _time_part(value) -> Time | None:
    if isinstance(value, datetime):
        return value.time().replace(microsecond=0)
    if isinstance(value, Time):
        return value.replace(microsecond=0)
    text = _normalize_cell(value)
    if not text:
        return None
    parsed = _parse_datetime_value(text)
    if parsed is not None:
        return parsed.time().replace(microsecond=0)
    match = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$", text)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    second = int(match.group(3) or 0)
    if hour > 23 or minute > 59 or second > 59:
        return None
    return Time(hour, minute, second)


def _normalize_datetime_parts(date_value, time_value) -> str:
    date_part = _date_part(date_value)
    time_part = _time_part(time_value)
    if date_part is not None and time_part is not None:
        return datetime.combine(date_part, time_part).strftime("%Y-%m-%d %H:%M:%S")
    joined = " ".join(part for part in [_normalize_cell(date_value), _normalize_cell(time_value)] if part)
    return _normalize_datetime(joined)


def _normalize_metric_count(value) -> str:
    text = _normalize_cell(value)
    if not text:
        return ""
    try:
        number = Decimal(text.replace(",", "."))
    except InvalidOperation:
        return text
    if number < 0 or number != number.to_integral_value():
        return text
    return str(int(number))


def _normalize_output_value(column: str, value) -> str:
    canonical = _canonical_column_name(column)
    if canonical in {"Czas rozpoczęcia", "Czas zakończenia"}:
        return _normalize_datetime(value)
    if canonical in {"przekroczenia obr/min", "> 140kmh", "> 160kmh", "> 170kmh"}:
        return _normalize_metric_count(value)
    return _normalize_cell(value)


def _valid_normalized_datetime(value: str) -> bool:
    return bool(value) and _parse_datetime_value(value) is not None


def _valid_normalized_count(value: str) -> bool:
    if not value:
        return False
    try:
        number = Decimal(value.replace(",", "."))
    except InvalidOperation:
        return False
    return number >= 0 and number == number.to_integral_value()
