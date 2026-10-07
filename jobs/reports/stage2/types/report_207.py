from __future__ import annotations

import re
from typing import Iterable

import pandas as pd

from jobs.reports.stage2.models import CleanedReport
from jobs.reports.stage2.types.common import _from_rows


class Report207:
    """207 Raport przekroczeń limitów prędkości drogowej."""

    TYPE = "report_207"
    report_type = TYPE
    REQUIRED_COLUMNS = {
        "Data i czas",
        "Nr rejestracyjny",
        "Prędkość",
        "Ograniczenie prędkości drogowej",
        "Lokalizacja",
    }
    OPTIONAL_COLUMNS = set()
    COLUMN_TYPES = {
        "Data i czas": "date",
        "Prędkość": "float",
        "Ograniczenie prędkości drogowej": "float",
    }
    REGISTRY_COLUMN_TYPES = {
        "Data i czas": "datetime",
        "Nr rejestracyjny": "string",
        "Prędkość": "numeric",
        "Ograniczenie prędkości drogowej": "numeric",
        "Lokalizacja": "string",
    }
    MULTI_TABLE = False

    TITLE = "207 Raport przekroczeń limitów prędkości drogowej"
    COLUMNS = [
        "Data i czas",
        "Nr rejestracyjny",
        "Prędkość",
        "Ograniczenie prędkości drogowej",
        "Lokalizacja",
    ]

    @classmethod
    def detect(cls, sample: pd.DataFrame | list[pd.DataFrame]) -> float:
        tables = sample if isinstance(sample, list) else [sample]
        has_header = any(_find_header_idx(df, cls.COLUMNS) is not None for df in tables)
        has_title = any(_contains_normalized_text(df, cls.TITLE) for df in tables)

        score = 0.0
        if has_header:
            score += 0.70
        if has_title:
            score += 0.30
        return min(score, 1.0)

    @classmethod
    def clean(cls, df_or_tables) -> CleanedReport:
        df = df_or_tables[0] if isinstance(df_or_tables, list) else df_or_tables
        header_idx = _find_header_idx(df, cls.COLUMNS)
        if header_idx is None:
            return CleanedReport(report_type=cls.TYPE, df=pd.DataFrame(), metadata={"warning": "header_not_found"})

        header = [_normalize_cell(value) for value in df.iloc[header_idx].tolist()]
        positions_by_label = {_norm_key(value): idx for idx, value in enumerate(header) if value}
        selected_positions = [positions_by_label[_norm_key(label)] for label in cls.COLUMNS]
        registration_idx = positions_by_label[_norm_key("Nr rejestracyjny")]

        data_row_values = [
            df.iloc[idx].tolist()
            for idx in range(header_idx + 1, len(df))
            if _is_data_row(df.iloc[idx].tolist(), registration_idx)
        ]
        active_positions = {idx for idx, value in enumerate(header) if value}
        for raw_values in data_row_values:
            active_positions.update(idx for idx, value in enumerate(raw_values) if _normalize_cell(value))
        actual_count = len(active_positions)
        expected_count = len(cls.COLUMNS)
        if actual_count != expected_count:
            raise ValueError(
                "Detection classified this file as report_207, but the expected column count "
                "does not match the actual column count. "
                f"report_type={cls.TYPE} expected_column_count={expected_count} actual_column_count={actual_count}"
            )

        rows: list[list[str]] = []
        for raw_values in data_row_values:
            rows.append([
                _normalize_cell(raw_values[pos] if pos < len(raw_values) else "")
                for pos in selected_positions
            ])

        return _from_rows(cls.TYPE, cls.COLUMNS, rows)


def _normalize_cell(value) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except TypeError:
        pass
    return re.sub(r"\s+", " ", str(value).strip())


def _norm_key(value: str) -> str:
    return _normalize_cell(value).lower()


def _row_values(df: pd.DataFrame, idx: int) -> list[str]:
    return [_normalize_cell(value) for value in df.iloc[idx].tolist()]


def _find_header_idx(df: pd.DataFrame, labels: Iterable[str], *, max_scan: int = 80) -> int | None:
    wanted = {_norm_key(label) for label in labels}
    for idx in range(min(len(df), max_scan)):
        row_labels = {_norm_key(value) for value in _row_values(df, idx) if value}
        if wanted.issubset(row_labels):
            return idx
    return None


def _contains_normalized_text(df: pd.DataFrame, needle: str) -> bool:
    wanted = _norm_key(needle)
    for value in df.values.flatten().tolist():
        if wanted in _norm_key(value):
            return True
    return False


def _is_data_row(raw_values: list, registration_idx: int) -> bool:
    registration = _normalize_cell(raw_values[registration_idx] if registration_idx < len(raw_values) else "")
    return bool(registration) and _norm_key(registration) != _norm_key("Nr rejestracyjny")
