from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Iterable

import pandas as pd

from jobs.reports.stage2.models import CleanedReport
from jobs.reports.stage2.types.common import _from_rows


class AlphaGPSBazaLog:
    """ALPHA00001 Alpha GPS workbook LOG output."""

    TYPE = "Alpha_GPS_Baza_LOG"
    report_type = TYPE
    DEFAULT_CLIENT_CODE = "ALPHA00001"
    MULTI_TABLE = False

    DETECTION_COLUMNS = [
        "ID",
        "Nr rejestracyjny",
        "Data przydziału",
        "RFID",
        "PRYW",
        "EDYS",
        "OPTIMA",
        "OTK",
    ]
    OUTPUT_COLUMNS = [
        "ID",
        "Nr rejestracyjny",
        "Data przydziału",
        "Nazwa Pliku csv",
    ]
    STOP_COLUMNS = [
        "ID",
        "Data przydziału",
        "PRYW stary",
        "PRYW aktualny",
    ]
    REQUIRED_COLUMNS = set(OUTPUT_COLUMNS)
    OPTIONAL_COLUMNS = set()
    COLUMN_TYPES = {"Data przydziału": "date"}
    REGISTRY_COLUMN_TYPES = {
        "ID": "string",
        "Nr rejestracyjny": "string",
        "Data przydziału": "date",
        "Nazwa Pliku csv": "string",
    }

    @classmethod
    def detect(cls, sample: pd.DataFrame | list[pd.DataFrame]) -> float:
        tables = sample if isinstance(sample, list) else [sample]
        return 1.0 if any(_find_row_with_labels(df, cls.DETECTION_COLUMNS) is not None for df in tables) else 0.0

    @classmethod
    def clean(cls, df_or_tables) -> CleanedReport:
        df = df_or_tables[0] if isinstance(df_or_tables, list) else df_or_tables
        header_idx = _find_row_with_labels(df, cls.OUTPUT_COLUMNS)
        if header_idx is None:
            raise ValueError(
                "Alpha_GPS_Baza_LOG output header not found: "
                + ", ".join(cls.OUTPUT_COLUMNS)
            )

        header = [_normalize_cell(value) for value in df.iloc[header_idx].tolist()]
        positions_by_label = {_norm_key(value): idx for idx, value in enumerate(header) if value}
        selected_positions = [positions_by_label[_norm_key(label)] for label in cls.OUTPUT_COLUMNS]

        stop_idx = _find_row_with_labels(df, cls.STOP_COLUMNS, start=header_idx + 1)
        end_idx = stop_idx if stop_idx is not None else len(df)

        rows: list[list[str]] = []
        for idx in range(header_idx + 1, end_idx):
            raw_values = df.iloc[idx].tolist()
            selected = [
                _normalize_cell(raw_values[pos] if pos < len(raw_values) else "")
                for pos in selected_positions
            ]
            if all(value == "" for value in selected):
                continue
            selected[2] = _parse_assignment_date(selected[2], row_number=idx + 1)
            rows.append(selected)

        return _from_rows(
            cls.TYPE,
            cls.OUTPUT_COLUMNS,
            rows,
            metadata={
                "header_row_number": header_idx + 1,
                "stop_row_number": (stop_idx + 1) if stop_idx is not None else None,
                "empty_rows_removed": max(0, (end_idx - header_idx - 1) - len(rows)),
                "default_client_code": cls.DEFAULT_CLIENT_CODE,
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
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return re.sub(r"\s+", " ", str(value).strip())


def _norm_key(value: str) -> str:
    return _normalize_cell(value).casefold()


def _row_values(df: pd.DataFrame, idx: int) -> list[str]:
    return [_normalize_cell(value) for value in df.iloc[idx].tolist()]


def _find_row_with_labels(
    df: pd.DataFrame,
    labels: Iterable[str],
    *,
    start: int = 0,
    max_scan: int | None = None,
) -> int | None:
    wanted = {_norm_key(label) for label in labels}
    end = len(df) if max_scan is None else min(len(df), start + max_scan)
    for idx in range(start, end):
        row_labels = {_norm_key(value) for value in _row_values(df, idx) if value}
        if wanted.issubset(row_labels):
            return idx
    return None


def _parse_assignment_date(value: str, *, row_number: int) -> str:
    text = _normalize_cell(value)
    if not text:
        raise ValueError(
            f"Invalid Data przydziału in Alpha_GPS_Baza_LOG at source row {row_number}: empty value"
        )

    if re.fullmatch(r"\d+(?:\.0+)?", text):
        serial = int(float(text))
        if serial > 0:
            # Excel's 1900 date system with leap-year bug compatibility.
            return (datetime(1899, 12, 30) + timedelta(days=serial)).date().isoformat()

    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass

    try:
        return datetime.fromisoformat(text).date().isoformat()
    except ValueError as exc:
        raise ValueError(
            f"Invalid Data przydziału in Alpha_GPS_Baza_LOG at source row {row_number}: {text!r}"
        ) from exc
