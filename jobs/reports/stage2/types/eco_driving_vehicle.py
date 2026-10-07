from __future__ import annotations

import pandas as pd


class EcoDrivingVehicle:
    TYPE = "eco_driving_vehicle"
    REQUIRED_COLUMNS = {"Nr rejestracyjny", "Ocena"}
    OPTIONAL_COLUMNS = set()
    MULTI_TABLE = True

    @classmethod
    def detect(cls, sample: pd.DataFrame | list[pd.DataFrame]) -> float:
        tables = sample if isinstance(sample, list) else [sample]
        text = "\n".join(
            " | ".join(str(v).strip() for v in t.head(20).values.flatten().tolist()) for t in tables
        ).lower()
        score = 0.0
        if "raport ecodriving" in text:
            score += 0.5
        if "nr rejestr" in text or "pojazd" in text:
            score += 0.2
        if len(tables) >= 2:
            score += 0.3
        return min(score, 1.0)

    @classmethod
    def clean(cls, df_or_tables):
        raise NotImplementedError("TODO: EcoDriving Vehicle cleaning for multi-table CSV")
