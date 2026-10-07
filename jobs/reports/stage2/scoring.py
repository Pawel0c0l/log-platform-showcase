from __future__ import annotations

from jobs.reports.stage2.models import CleanedReport, Decision, ValidationResult

FINAL_SCORE_OK_THRESHOLD = 0.75
SCHEMA_BLOCK_THRESHOLD = 0.30


def compute_clean_score(cleaned: CleanedReport, report_cls) -> float:
    required = set(getattr(report_cls, "REQUIRED_COLUMNS", set()))
    present = set(cleaned.df.columns.tolist())

    if len(cleaned.df) <= 0:
        return 0.0

    if required.issubset(present):
        return 1.0

    if required:
        ratio = len(required & present) / len(required)
        if ratio >= 0.5:
            return 0.5
    return 0.0


def compute_final_score(*, detect_score: float, clean_score: float, schema_score: float) -> float:
    return round(0.45 * detect_score + 0.20 * clean_score + 0.35 * schema_score, 4)


def make_decision(*, final_score: float, validation: ValidationResult) -> Decision:
    schema_diff = validation.schema_diff
    missing_required = schema_diff.get("missing_required") or []
    row_count = int(schema_diff.get("row_count") or 0)
    blocking_parse_cols = schema_diff.get("type_parse_blocking_cols") or []

    if blocking_parse_cols:
        return Decision(status="PENDING_REVIEW", final_score=final_score, pending_reason="type_parse_blocking")

    if missing_required or row_count <= 0:
        return Decision(status="PENDING_REVIEW", final_score=final_score, pending_reason="schema_contract_mismatch")

    if validation.schema_score < SCHEMA_BLOCK_THRESHOLD:
        return Decision(status="PENDING_REVIEW", final_score=final_score, pending_reason="schema_contract_mismatch")

    if final_score < FINAL_SCORE_OK_THRESHOLD:
        return Decision(status="PENDING_REVIEW", final_score=final_score, pending_reason="schema_contract_mismatch")

    if final_score < 0.90:
        warning = None
        if validation.schema_diff.get("missing_required") or validation.schema_diff.get("extra_columns"):
            warning = "drift_warning"
        return Decision(status="OK", final_score=final_score, warning=warning)

    return Decision(status="OK", final_score=final_score)
