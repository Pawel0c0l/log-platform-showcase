from __future__ import annotations

import re
from typing import Iterable

import pandas as pd

from jobs.reports.stage2.models import CleanedReport


def _normalize_str(value: str) -> str:
    s = (value or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s


def _row_values(df: pd.DataFrame, idx: int) -> list[str]:
    return [_normalize_str(str(v)) for v in df.iloc[idx].tolist()]


def _contains_all(haystack: str, needles: Iterable[str]) -> bool:
    text = haystack.lower()
    return all(n.lower() in text for n in needles)


def _find_row_with(df: pd.DataFrame, *, must_have: list[str], max_scan: int = 80) -> int | None:
    for i in range(min(len(df), max_scan)):
        row = " | ".join(_row_values(df, i))
        if _contains_all(row, must_have):
            return i
    return None


def _trim_row(values: list[str]) -> list[str]:
    end = len(values)
    while end > 0 and values[end - 1] == "":
        end -= 1
    return values[:end]


def _clean_data_rows(df: pd.DataFrame, start_idx: int, expected_cols: int) -> list[list[str]]:
    rows: list[list[str]] = []
    for i in range(start_idx, len(df)):
        row = _trim_row(_row_values(df, i))
        if not row:
            continue
        if row[0].lower().startswith("razem"):
            break
        if len(row) < max(3, expected_cols - 5):
            continue
        row = row[:expected_cols] + [""] * max(0, expected_cols - len(row))
        rows.append(row)
    return rows


def _from_rows(report_type: str, columns: list[str], rows: list[list[str]], *, metadata: dict | None = None) -> CleanedReport:
    frame = pd.DataFrame(rows, columns=columns)
    return CleanedReport(report_type=report_type, df=frame, metadata=metadata or {})
