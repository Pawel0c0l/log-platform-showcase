from __future__ import annotations

import pandas as pd

from jobs.reports.stage2.models import CleanedReport
from jobs.reports.stage2.types.common import _clean_data_rows, _find_row_with, _from_rows, _row_values


class Report602EV:
    TYPE = "report_602_ev"
    REQUIRED_COLUMNS = {"Nr rejestracyjny", "Zużyta energia", "Dystans"}
    OPTIONAL_COLUMNS = {"Marka i model", "Pojemność baterii", "Średnie zuzycie energii"}
    COLUMN_TYPES = {"Zużyta energia": "float", "Dystans": "float"}
    MULTI_TABLE = False

    @classmethod
    def detect(cls, sample: pd.DataFrame | list[pd.DataFrame]) -> float:
        df = sample[0] if isinstance(sample, list) else sample
        text = "\n".join(" | ".join(_row_values(df, i)) for i in range(min(35, len(df)))).lower()
        score = 0.0
        if "602 raport zużycia energii" in text:
            score += 0.7
        if "zużyta energia" in text:
            score += 0.3
        return min(score, 1.0)

    @classmethod
    def clean(cls, df_or_tables) -> CleanedReport:
        df = df_or_tables[0] if isinstance(df_or_tables, list) else df_or_tables
        header_idx = _find_row_with(df, must_have=["Nr rejestracyjny", "Zużyta energia", "Dystans"])
        if header_idx is None:
            return CleanedReport(report_type=cls.TYPE, df=pd.DataFrame(), metadata={"warning": "header_not_found"})
        columns = [
            "Nr rejestracyjny",
            "Marka i model",
            "Pojemność baterii",
            "Zużyta energia",
            "Średnie zuzycie energii",
            "Dystans",
        ]
        rows = _clean_data_rows(df, header_idx + 2, expected_cols=len(columns))
        return _from_rows(cls.TYPE, columns, rows)
