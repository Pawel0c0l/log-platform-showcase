from __future__ import annotations

import pandas as pd

from jobs.reports.stage2.models import CleanedReport
from jobs.reports.stage2.types.common import _clean_data_rows, _find_row_with, _from_rows, _row_values


class N1041Report:
    TYPE = "n104_1"
    REQUIRED_COLUMNS = {
        "Nr rejestracyjny",
        "Opis pojazdu",
        "Rodzaj podróży",
        "Czas-Start",
        "Czas-Koniec",
        "Czas jazdy",
        "Czas postoju",
        "Dystans",
    }
    OPTIONAL_COLUMNS = {
        "Lokalizacja start",
        "Geostrefa start",
        "Lokalizacja koniec",
        "Geostrefa koniec",
        "Przekraczanie prędkości",
        "Ostre Hamowania",
        "Silne Przyśpieszenia",
        "Ostre Skręty",
        "Nadmierny postój",
    }
    COLUMN_TYPES = {
        "Czas-Start": "date",
        "Czas-Koniec": "date",
        "Dystans": "float",
    }
    MULTI_TABLE = False

    @classmethod
    def detect(cls, sample: pd.DataFrame | list[pd.DataFrame]) -> float:
        df = sample[0] if isinstance(sample, list) else sample
        text = "\n".join(" | ".join(_row_values(df, i)) for i in range(min(40, len(df)))).lower()

        score = 0.0
        if "104.1 ogólny raport podróży - podsumowanie" in text:
            score += 0.50
        if "opis pojazdu" in text and "rodzaj" in text:
            score += 0.30
        if "silne przyśpieszenia" in text:
            score += 0.20
        return min(score, 1.0)

    @classmethod
    def clean(cls, df_or_tables) -> CleanedReport:
        df = df_or_tables[0] if isinstance(df_or_tables, list) else df_or_tables

        header_idx = _find_row_with(df, must_have=["Opis pojazdu", "Rodzaj", "Dystans"])
        if header_idx is None:
            return CleanedReport(report_type=cls.TYPE, df=pd.DataFrame(), metadata={"warning": "header_not_found"})

        alarms_idx = _find_row_with(df, must_have=["Przekraczanie prędkości", "Ostre Hamowania"], max_scan=header_idx)
        alarms = _row_values(df, alarms_idx) if alarms_idx is not None else []

        columns = [
            "Nr rejestracyjny",
            "Opis pojazdu",
            "Rodzaj podróży",
            "Czas-Start",
            "Lokalizacja start",
            "Geostrefa start",
            "Czas-Koniec",
            "Lokalizacja koniec",
            "Geostrefa koniec",
            "Czas jazdy",
            "Czas postoju",
            "Dystans",
        ]
        for label in alarms[:5]:
            if label:
                columns.append(label)

        rows = _clean_data_rows(df, header_idx + 2, expected_cols=len(columns))
        return _from_rows(cls.TYPE, columns, rows)
