from __future__ import annotations

import pandas as pd

from jobs.reports.stage2.models import CleanedReport
from jobs.reports.stage2.types.common import _clean_data_rows, _find_row_with, _from_rows, _row_values


class D1047Report:
    TYPE = "d104_7"
    REQUIRED_COLUMNS = {"Numer rejestracyjny", "Data godzina startu", "Data godzina zakończenia", "Dystans"}
    OPTIONAL_COLUMNS = {"Opis pojazdu", "Kierowca", "Lokalizacja start", "Lokalizacja koniec", "Czas jazdy"}
    COLUMN_TYPES = {"Data godzina startu": "date", "Data godzina zakończenia": "date", "Dystans": "float"}
    MULTI_TABLE = False

    @classmethod
    def detect(cls, sample: pd.DataFrame | list[pd.DataFrame]) -> float:
        df = sample[0] if isinstance(sample, list) else sample
        text = "\n".join(" | ".join(_row_values(df, i)) for i in range(min(30, len(df)))).lower()
        score = 0.0
        if "d104.7" in text:
            score += 0.7
        if "data godzina startu" in text:
            score += 0.3
        return min(score, 1.0)

    @classmethod
    def clean(cls, df_or_tables) -> CleanedReport:
        df = df_or_tables[0] if isinstance(df_or_tables, list) else df_or_tables
        header_idx = _find_row_with(df, must_have=["Numer rejestracyjny", "Data godzina startu", "Data godzina zakończenia"])
        if header_idx is None:
            return CleanedReport(report_type=cls.TYPE, df=pd.DataFrame(), metadata={"warning": "header_not_found"})
        columns = [
            "Numer rejestracyjny",
            "Opis pojazdu",
            "Kierowca",
            "Data godzina startu",
            "Lokalizacja start",
            "Data godzina zakończenia",
            "Lokalizacja koniec",
            "Czas jazdy",
            "Dystans",
        ]
        rows = _clean_data_rows(df, header_idx + 1, expected_cols=len(columns))
        return _from_rows(cls.TYPE, columns, rows)
