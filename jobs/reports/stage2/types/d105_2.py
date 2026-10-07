from __future__ import annotations

import pandas as pd

from jobs.reports.stage2.models import CleanedReport
from jobs.reports.stage2.types.common import _from_rows, _row_values


class D1052Report:
    TYPE = "d105_2"
    REQUIRED_COLUMNS = {"Nr Rejestracyjny", "Data rozpoczęcia", "Data zakończenia"}
    OPTIONAL_COLUMNS = {
        "Marka",
        "Model",
        "Flota",
        "Dysponent ID",
        "Dysponent imię i nazwisko",
        "Kierowca ID",
        "Kierowca imię i nazwisko",
    }
    COLUMN_TYPES = {"Data rozpoczęcia": "date", "Data zakończenia": "date"}
    MULTI_TABLE = False

    @classmethod
    def detect(cls, sample: pd.DataFrame | list[pd.DataFrame]) -> float:
        df = sample[0] if isinstance(sample, list) else sample
        header = " | ".join(_row_values(df, 0)).lower() if len(df) else ""
        score = 0.0
        if "nr rejestracyjny" in header and "kierowca id" in header:
            score += 0.6
        if "data rozpoczęcia" in header and "data zakończenia" in header:
            score += 0.4
        return min(score, 1.0)

    @classmethod
    def clean(cls, df_or_tables) -> CleanedReport:
        df = df_or_tables[0] if isinstance(df_or_tables, list) else df_or_tables
        if len(df) < 2:
            return CleanedReport(report_type=cls.TYPE, df=pd.DataFrame(), metadata={"warning": "empty"})
        columns = _row_values(df, 0)
        rows = [_row_values(df, i)[: len(columns)] for i in range(1, len(df))]
        return _from_rows(cls.TYPE, columns, rows)
