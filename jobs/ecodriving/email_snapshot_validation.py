"""Structured validation for persisted driver-based Eco Driving email snapshots."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Mapping


INVALID_RANKING_SNAPSHOT = "INVALID_RANKING_SNAPSHOT"
RANKED_SNAPSHOT_FIELDS = (
    "ranking_position",
    "ranking_total_participants",
    "ecodriving_rating_type",
    "ecodriving_rating_type_share_percent",
)


def _presence_issue(values: Mapping[str, Any], field: str) -> str | None:
    if field not in values:
        return "absent"
    value = values[field]
    if value is None:
        return "null"
    if isinstance(value, str) and not value.strip():
        return "empty"
    return None


def _decimal(value: Any, *, decimal_comma: bool) -> Decimal | None:
    if isinstance(value, bool):
        return None
    text = str(value).strip()
    if decimal_comma:
        text = text.replace(",", ".")
    try:
        parsed = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def ranked_snapshot_issues(
    values: Mapping[str, Any],
    *,
    decimal_comma: bool = False,
) -> dict[str, str]:
    """Return field-level issues; an empty mapping means the ranked state is valid."""
    issues: dict[str, str] = {}
    for field in RANKED_SNAPSHOT_FIELDS:
        issue = _presence_issue(values, field)
        if issue:
            issues[field] = issue

    for field in ("ranking_position", "ranking_total_participants"):
        if field in issues:
            continue
        parsed = _decimal(values[field], decimal_comma=decimal_comma)
        if parsed is None or parsed != parsed.to_integral_value() or parsed <= 0:
            issues[field] = "not_positive_integer"

    if "ecodriving_rating_type" not in issues:
        if not str(values["ecodriving_rating_type"]).strip():
            issues["ecodriving_rating_type"] = "empty"

    share_field = "ecodriving_rating_type_share_percent"
    if share_field not in issues:
        parsed = _decimal(values[share_field], decimal_comma=decimal_comma)
        if parsed is None:
            issues[share_field] = "not_numeric"
        elif parsed < 0 or parsed > 100:
            issues[share_field] = "outside_0_100"

    return issues
