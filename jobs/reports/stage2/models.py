from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd


@dataclass(slots=True)
class DetectionResult:
    report_type: str | None
    detect_score: float
    candidates_top3: list[dict[str, Any]]
    pending_reason: str | None = None


@dataclass(slots=True)
class CleanedReport:
    report_type: str
    df: pd.DataFrame
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ValidationResult:
    is_valid: bool
    schema_score: float
    schema_diff: dict[str, Any]
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Decision:
    status: str
    final_score: float
    warning: str | None = None
    pending_reason: str | None = None
