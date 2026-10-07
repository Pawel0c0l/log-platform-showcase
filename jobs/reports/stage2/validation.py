from __future__ import annotations

import math
import re
from datetime import datetime, timedelta

import pandas as pd

from jobs.reports.stage2.models import CleanedReport, ValidationResult

TYPE_PARSE_WARN_RATE = 0.02
TYPE_PARSE_BLOCK_RATE = 0.20
TYPE_PARSE_MIN_SAMPLE = 10
EXCEL_SERIAL_DATE_BASE = datetime(1899, 12, 30)
EXCEL_SERIAL_MIN_DATETIME = datetime(2000, 1, 1)
EXCEL_SERIAL_MAX_DATETIME = datetime(2100, 1, 1)
EXCEL_SERIAL_NUMBER_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")


def _is_empty(value) -> bool:
    return value is None or str(value).strip() == ""


def _normalize_datetime_text(value) -> str:
    return re.sub(r"\s+", " ", str(value).strip())


def _round_to_second(dt: datetime) -> datetime:
    if dt.microsecond >= 500_000:
        dt = dt + timedelta(seconds=1)
    return dt.replace(microsecond=0)


def _parse_excel_serial_datetime(value) -> datetime | None:
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        serial = float(value)
    else:
        s = _normalize_datetime_text(value)
        if not s or not EXCEL_SERIAL_NUMBER_RE.fullmatch(s):
            return None
        try:
            serial = float(s)
        except ValueError:
            return None

    if not math.isfinite(serial):
        return None

    try:
        dt = EXCEL_SERIAL_DATE_BASE + timedelta(days=serial)
    except OverflowError:
        return None

    dt = _round_to_second(dt)
    if not (EXCEL_SERIAL_MIN_DATETIME <= dt <= EXCEL_SERIAL_MAX_DATETIME):
        return None
    return dt


def _parse_datetime_value(value) -> datetime | None:
    s = _normalize_datetime_text(value)
    if not s:
        return None

    excel_dt = _parse_excel_serial_datetime(value)
    if excel_dt is not None:
        return excel_dt

    fmts = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d",
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%d.%m.%Y",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%d/%m/%Y",
    ]
    for fmt in fmts:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    try:
        parsed = pd.to_datetime(s)
        if pd.isna(parsed):
            return None
        if hasattr(parsed, "to_pydatetime"):
            return parsed.to_pydatetime()
        if isinstance(parsed, datetime):
            return parsed
        return None
    except Exception:
        return None


def _validate_date(value: str) -> bool:
    if not _normalize_datetime_text(value):
        return True
    return _parse_datetime_value(value) is not None


def _validate_float(value: str) -> bool:
    s = str(value).strip().replace(",", ".")
    if not s:
        return True
    try:
        float(s)
        return True
    except ValueError:
        return False


def validate(cleaned: CleanedReport, report_cls) -> ValidationResult:
    df = cleaned.df.copy()
    required = set(report_cls.REQUIRED_COLUMNS)
    optional = set(getattr(report_cls, "OPTIONAL_COLUMNS", set()))
    allowed = required | optional

    cols = set(df.columns.tolist())
    missing_required = sorted(required - cols)
    extra_columns = sorted(cols - allowed)

    row_count = int(len(df))
    errors: list[str] = []
    warnings: list[str] = []

    if row_count <= 0:
        errors.append("empty_result")
    if missing_required:
        errors.append("missing_required_columns")
    if extra_columns:
        warnings.append("extra_columns_detected")

    column_types = getattr(report_cls, "COLUMN_TYPES", {})
    type_parse_stats: dict[str, dict[str, float | int]] = {}
    type_parse_warning_cols: list[str] = []
    type_parse_blocking_cols: list[str] = []
    for col, kind in column_types.items():
        if col not in df.columns:
            continue
        non_null = 0
        invalid = 0
        for v in df[col].tolist():
            if _is_empty(v):
                continue
            non_null += 1
            ok = _validate_date(v) if kind == "date" else (_validate_float(v) if kind == "float" else True)
            if not ok:
                invalid += 1
        fail_rate = float(invalid / max(non_null, 1))
        type_parse_stats[col] = {
            "non_null": int(non_null),
            "fails": int(invalid),
            "fail_rate": round(fail_rate, 6),
        }
        if invalid <= 0:
            continue
        if non_null < TYPE_PARSE_MIN_SAMPLE:
            warnings.append(f"type_parse_warning_small_sample:{col}:fail_rate={fail_rate:.4f}")
            type_parse_warning_cols.append(col)
            continue
        if fail_rate > TYPE_PARSE_BLOCK_RATE:
            errors.append(f"type_parse_blocking:{col}:fail_rate={fail_rate:.4f}")
            type_parse_blocking_cols.append(col)
        elif fail_rate > TYPE_PARSE_WARN_RATE:
            warnings.append(f"type_parse_warning_high:{col}:fail_rate={fail_rate:.4f}")
            type_parse_warning_cols.append(col)
        else:
            warnings.append(f"type_parse_warning:{col}:fail_rate={fail_rate:.4f}")
            type_parse_warning_cols.append(col)

    null_rate_by_col = {}
    if row_count > 0:
        for col in df.columns:
            nulls = int(df[col].map(_is_empty).sum())
            ratio = nulls / row_count
            if ratio > 0:
                null_rate_by_col[col] = round(ratio, 4)
        null_rate_by_col = dict(sorted(null_rate_by_col.items(), key=lambda kv: kv[1], reverse=True)[:8])

    penalty = 0.0
    penalty += min(0.7, 0.20 * len(missing_required))
    penalty += min(0.15, 0.02 * len(extra_columns))
    if row_count <= 0:
        penalty += 0.5
    for col, stats in type_parse_stats.items():
        non_null = int(stats["non_null"])
        fail_rate = float(stats["fail_rate"])
        if non_null < TYPE_PARSE_MIN_SAMPLE:
            continue
        if fail_rate > TYPE_PARSE_BLOCK_RATE:
            penalty += min(0.9, 0.9 * fail_rate)
        elif fail_rate > TYPE_PARSE_WARN_RATE:
            penalty += min(0.2, 0.2 * fail_rate)
        elif fail_rate > 0.0:
            penalty += min(0.05, 0.05 * fail_rate)

    schema_score = max(0.0, round(1.0 - penalty, 4))
    if type_parse_blocking_cols:
        # Hard cap to keep final_score below OK threshold when parse quality is critically degraded.
        schema_score = min(schema_score, 0.2)

    schema_diff = {
        "missing_required": missing_required,
        "extra_columns": extra_columns,
        "row_count": row_count,
        "null_rate_by_col": null_rate_by_col,
        "type_parse_stats": type_parse_stats,
        "type_parse_warning_cols": sorted(set(type_parse_warning_cols)),
        "type_parse_blocking_cols": sorted(set(type_parse_blocking_cols)),
        "errors": errors,
        "warnings": warnings,
    }

    return ValidationResult(
        is_valid=(len(errors) == 0),
        schema_score=schema_score,
        schema_diff=schema_diff,
        errors=errors,
        warnings=warnings,
    )
