"""JSON serialization for the Eco Driving Explorer read API.

Pure functions only (no FastAPI, no I/O). They convert the Stage 1 domain
models into JSON-safe primitives:

* dates and timestamps -> ISO 8601 strings;
* enums -> stable string values;
* Decimal / numeric scoring values -> decimal strings (no binary float drift);
* collections -> lists (never ``null``);
* period end is always the exclusive boundary.

Nothing here emits Python class names, dataclass internals, SQL, or exception
reprs. Route/location columns are not part of any DTO and cannot be serialized.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable, Mapping, Optional, Sequence

from .access import EcoDrivingClientAccess
from .models import (
    LineageQuality,
    Page,
    PeriodProgressionRow,
    ProviderIdentity,
    RankingEntry,
    RankingPeriod,
    ReconciliationResult,
    ScoreDefinition,
    ScoreDistribution,
    TrendPoint,
    TripRow,
)


# --- envelope ----------------------------------------------------------------

def ok(data: Any, meta: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    return {"data": data, "meta": dict(meta or {}), "error": None}


def error(code: str, message: str, meta: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    return {"data": None, "meta": dict(meta or {}), "error": {"code": code, "message": message}}


# --- scalar helpers ----------------------------------------------------------

def _dec(value: Optional[Decimal]) -> Optional[str]:
    """Serialize a persisted/scoring Decimal as a stable decimal string."""

    if value is None:
        return None
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return format(value, "f")


def _iso_date(value: Optional[date]) -> Optional[str]:
    return None if value is None else value.isoformat()


def _iso_ts(value: Optional[datetime]) -> Optional[str]:
    return None if value is None else value.isoformat()


def _json_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, Decimal):
        return _dec(value)
    if isinstance(value, datetime):
        return _iso_ts(value)
    if isinstance(value, date):
        return _iso_date(value)
    if isinstance(value, float):
        return _dec(Decimal(str(value)))
    return str(value)


def _metric_map(values: Mapping[str, Optional[Decimal]]) -> dict[str, Optional[str]]:
    return {key: _dec(val) for key, val in values.items()}


def _int_map(values: Mapping[str, int]) -> dict[str, int]:
    return {key: int(val) for key, val in values.items()}


# --- period key / period -----------------------------------------------------

def serialize_period(period: RankingPeriod) -> dict[str, Any]:
    return {
        "period_key": period.key.token,
        "period_type": period.period_type.value,
        "period_label": period.period_label,
        "month_start_date": _iso_date(period.month_start_date),
        "period_start_date": _iso_date(period.period_start_date),
        "period_end_date_exclusive": _iso_date(period.period_end_date),
        "period_sequence_in_month": period.period_sequence_in_month,
        "is_partial_period": bool(period.is_partial_period),
        "entry_counts_by_group": {str(k): int(v) for k, v in period.entry_counts_by_group.items()},
        "not_ranked_count": int(period.not_ranked_count),
        # Subset of the above: permitted, but under the distance threshold. The
        # INCLUDED tab lists these rows, so its chip count needs them.
        "not_ranked_included_count": int(period.not_ranked_included_count),
        "lineage_quality": period.lineage_quality.value,
        "source_calculated_at": _iso_ts(period.source_calculated_at),
    }


# --- ranking entry -----------------------------------------------------------

def serialize_ranking_entry(entry: RankingEntry) -> dict[str, Any]:
    return {
        "provider_key": entry.provider_key,
        "client_code": entry.client_code,
        "ranking_family": entry.ranking_family,
        "period_key": entry.period_key.token,
        "assigned_id": entry.assigned_id,
        # Persisted, historical ranking semantics.
        "ranking_group": entry.ranking_group.value if entry.ranking_group else None,
        "ranking_included": entry.ranking_included,
        "ranking_position": entry.ranking_position,
        "ranking_total_participants": entry.ranking_total_participants,
        "qualification_status": entry.qualification_status,
        "calculation_status": entry.calculation_status,
        "trips_count": int(entry.trips_count),
        "total_distance_meters": int(entry.total_distance_meters),
        "total_kilometers": _dec(entry.total_kilometers),
        "eco_driving_score_total": _dec(entry.eco_driving_score_total),
        "ecodriving_rating_type": entry.ecodriving_rating_type,
        "ecodriving_rating_type_share_percent": _dec(entry.ecodriving_rating_type_share_percent),
        "period_label": entry.period_label,
        "is_partial_period": bool(entry.is_partial_period),
        "event_counts": _int_map(entry.event_counts),
        "metric_rates_per_100km": _metric_map(entry.metric_rates),
        "metric_points": _metric_map(entry.metric_points),
        # Persisted `*_maxpoints_subtract`, always <= 0.
        "metric_points_lost": _metric_map(entry.metric_losses),
        # Current-chart metadata, clearly separated from persisted semantics.
        "current_chart": {
            "driver_metadata_source": entry.driver_metadata_source.value,
            "current_driver_name": entry.current_driver_name,
            "current_chart_ranking_included": entry.current_chart_ranking_included,
        },
        "lineage_quality": entry.lineage_quality.value,
    }


# --- trip row ----------------------------------------------------------------

def serialize_trip(trip: TripRow) -> dict[str, Any]:
    # Only safe scoring-explainability fields, plus the authorized vehicle
    # plate (`UI-20260820-01`). No lat/lon, no addresses, no driver name, no
    # route polyline, no raw client_trips payload, and no other vehicle field.
    return {
        "provider_trip_id": int(trip.provider_trip_id),
        "trip_start_ts": _iso_ts(trip.trip_start_ts),
        "trip_end_ts": _iso_ts(trip.trip_end_ts),
        "assigned_id": trip.assigned_id,
        "assignment_source": trip.assignment_source,
        "trip_distance_meters": trip.trip_distance_meters,
        "distance_kilometers": _dec(
            (Decimal(int(trip.trip_distance_meters)) / Decimal(1000)).quantize(Decimal("0.001"))
        ) if trip.trip_distance_meters is not None else None,
        "total_scoring_events": trip.total_scoring_events,
        "source_trip_present": bool(trip.client_trip_present),
        "aggregation_included": bool(trip.aggregation_included),
        "is_private_trip": bool(trip.is_private_trip),
        "exclusion_reason": trip.exclusion_reason,
        "event_counts": _int_map(trip.event_counts),
        "client_trip_present": bool(trip.client_trip_present),
        "vehicle_registration": trip.vehicle_registration,
    }


# --- reconciliation ----------------------------------------------------------

def serialize_reconciliation(result: ReconciliationResult) -> dict[str, Any]:
    return {
        "provider_key": result.provider_key,
        "client_code": result.client_code,
        "period_key": result.period_key.token,
        "assigned_id": result.assigned_id,
        "reconciliation_status": result.reconciliation_status.value,
        "lineage_quality": result.lineage_quality.value,
        "mismatched_fields": list(result.mismatched_fields),
        "fields": [
            {
                "name": field.name,
                "persisted": _json_scalar(field.persisted),
                "reconstructed": _json_scalar(field.reconstructed),
                "matches": bool(field.matches),
            }
            for field in result.fields
        ],
        "persisted": {k: _json_scalar(v) for k, v in result.persisted.items()},
        "reconstructed": {k: _json_scalar(v) for k, v in result.reconstructed.items()},
        "diagnostics": {k: _json_scalar(v) for k, v in result.diagnostics.items()},
    }


# --- score definition --------------------------------------------------------

def serialize_score_definition(score: ScoreDefinition) -> dict[str, Any]:
    return {
        "ranking_family": score.ranking_family,
        "max_possible_score": int(score.max_possible_score),
        "min_possible_score": int(score.min_possible_score),
        "rate_basis": score.rate_basis,
        "rate_rounding": score.rate_rounding,
        "minimum_qualifying_distance_meters": int(score.minimum_qualifying_distance_meters),
        "rating_thresholds": [
            {"minimum_score": _dec(threshold.minimum_score), "label": threshold.label}
            for threshold in score.rating_thresholds
        ],
        "metrics": [
            {
                "metric_key": metric.metric_key,
                "label": metric.label,
                "max_points": int(metric.max_points),
                "final_points": int(metric.final_points),
                "buckets": [
                    {"upper_bound": _dec(bucket.upper_bound), "points": int(bucket.points)}
                    for bucket in metric.buckets
                ],
            }
            for metric in score.metrics
        ],
    }


# --- fleet distribution / trend / progression --------------------------------

def serialize_score_distribution(distribution: ScoreDistribution) -> dict[str, Any]:
    return {
        "client_code": distribution.client_code,
        "period_key": distribution.period_key.token,
        "ranking_group": distribution.ranking_group.value if distribution.ranking_group else None,
        "bin_width": int(distribution.bin_width),
        "total_count": int(distribution.total_count),
        "median_score": _dec(distribution.median_score),
        "mean_score": _dec(distribution.mean_score),
        "bins": [
            {
                "index": int(item.index),
                "lower_bound": int(item.lower_bound),
                "upper_bound": int(item.upper_bound),
                "count": int(item.count),
            }
            for item in distribution.bins
        ],
    }


def serialize_trend_point(point: TrendPoint) -> dict[str, Any]:
    return {
        "period_key": point.period_key.token,
        "period_label": point.period_label,
        "period_start_date": _iso_date(point.period_start_date),
        "period_end_date_exclusive": _iso_date(point.period_end_date),
        "is_partial_period": bool(point.is_partial_period),
        "eco_driving_score_total": _dec(point.eco_driving_score_total),
        "ranking_position": point.ranking_position,
        "ranking_total_participants": point.ranking_total_participants,
        "qualification_status": point.qualification_status,
        "total_kilometers": _dec(point.total_kilometers),
        "is_current": bool(point.is_current),
    }


def serialize_period_progression_row(row: PeriodProgressionRow) -> dict[str, Any]:
    return {
        "period_label": row.period_label,
        "period_start_date": _iso_date(row.period_start_date),
        "period_end_date_exclusive": _iso_date(row.period_end_date),
        "period_sequence_in_month": row.period_sequence_in_month,
        "is_partial_period": bool(row.is_partial_period),
        "eco_driving_score_total": _dec(row.eco_driving_score_total),
        "qualification_status": row.qualification_status,
        "total_distance_meters": int(row.total_distance_meters),
        "total_kilometers": _dec(row.total_kilometers),
        "trips_count": int(row.trips_count),
        "event_counts": _int_map(row.event_counts),
        "metric_rates_per_100km": _metric_map(row.metric_rates),
        "is_current": bool(row.is_current),
        # Weekly periods are cumulative month-to-date snapshots. Consumers must
        # read these as running totals and must never add them together.
        "is_cumulative_snapshot": True,
    }


# --- providers ---------------------------------------------------------------

def serialize_provider(
    identity: ProviderIdentity,
    access: EcoDrivingClientAccess,
    *,
    lineage_modes: Sequence[LineageQuality],
) -> dict[str, Any]:
    return {
        "client_code": identity.client_code,
        "provider_key": identity.provider_key,
        "ranking_family": identity.ranking_family,
        "display_name": identity.display_name,
        "supported_period_types": [pt.value for pt in identity.supported_period_types],
        "capabilities": {
            "can_view_ranking": bool(access.can_view_eco_ranking),
            "can_view_trip_details": bool(access.can_view_eco_trip_details),
            "can_view_trip_routes": bool(access.can_view_eco_trip_routes),
        },
        "lineage_modes": [mode.value for mode in lineage_modes],
    }


# --- pagination --------------------------------------------------------------

def serialize_page(page: Page, item_serializer: Callable[[Any], dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    items = [item_serializer(item) for item in page.items]
    meta = {
        "page": int(page.page),
        "limit": int(page.limit),
        "count": len(items),
        "total_count": None if page.total_count is None else int(page.total_count),
        "has_next": bool(page.has_next),
    }
    return items, meta


# --- month + arbitrary-week ranking basis ------------------------------------

def serialize_week_bucket(bucket, *, selected: bool) -> dict[str, Any]:
    """One week toggle card: its label, its true isolated range and its state."""

    return {
        "sequence": int(bucket.sequence),
        "label": bucket.label,
        "start_date": _iso_date(bucket.start_date),
        "end_date_exclusive": _iso_date(bucket.end_date_exclusive),
        "day_count": int(bucket.day_count),
        "is_partial": bool(bucket.is_partial),
        "selected": bool(selected),
    }


def serialize_week_selection(selection) -> dict[str, Any]:
    """The canonical ranking basis, as the server resolved it.

    ``mode`` is the single resolved answer to "whole month or a week subset?".
    The client never sends two states and the server never carries both: a
    selection that names every week is reported as ``MONTH``.
    """

    selected = set(selection.selected_sequences)
    return {
        "mode": selection.mode,
        "month": selection.month_token,
        "month_start_date": _iso_date(selection.month_start_date),
        "month_end_date_exclusive": _iso_date(selection.month_end_date_exclusive),
        "selected_weeks": [int(value) for value in selection.selected_sequences],
        "selected_week_count": len(selection.selected_sequences),
        "canonical_weeks_param": selection.canonical_weeks_param,
        "label": selection.label,
        "covered_start_date": _iso_date(selection.covered_start_date),
        "covered_end_date_exclusive": _iso_date(selection.covered_end_date_exclusive),
        "day_count": int(selection.day_count),
        "is_contiguous": bool(selection.is_contiguous),
        "includes_partial_week": bool(selection.includes_partial_week),
        # A dynamic subset is always recomputed from the underlying
        # assignments. Whole month prefers the canonical persisted monthly
        # snapshot and falls back to dynamic aggregation when that snapshot does
        # not exist yet; the service overwrites `basis_source` with which one
        # actually ran.
        "is_dynamically_recomputed": bool(selection.is_dynamic),
        "basis_source": None,
        "week_cards": [
            serialize_week_bucket(bucket, selected=bucket.sequence in selected)
            for bucket in selection.all_buckets
        ],
    }


def serialize_dynamic_basis(basis) -> dict[str, Any]:
    """Population-level facts for the ranking basis line."""

    return {
        "population_count": int(basis.population_count),
        "qualified_count": int(basis.qualified_count),
        "counts_by_group": {k: int(v) for k, v in dict(basis.counts_by_group).items()},
        "not_ranked_count": int(basis.not_ranked_count),
        "not_ranked_included_count": int(basis.not_ranked_included_count),
        "total_trips_count": int(basis.total_trips_count),
        "total_distance_meters": int(basis.total_distance_meters),
        "total_kilometers": _dec(basis.total_kilometers),
    }
