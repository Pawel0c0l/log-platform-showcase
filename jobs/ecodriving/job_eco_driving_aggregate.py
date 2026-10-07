"""Workflow A — Eco Driving assignment, aggregation, and ranking job."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from api.eco_driving_explorer import period_domain as PD
from api.timezone_utils import (
    get_business_timezone,
    get_business_timezone_name,
    now_business_tz,
    set_pg_session_timezone,
)
from jobs.api.telematics.secret_resolver import resolve_secret
from jobs.ecodriving.eco_scoring import (
    MIN_QUALIFYING_DISTANCE_METERS,
    REQUIRED_METRICS,
)


JOB_SOURCE = "jobs.ecodriving.job_eco_driving_aggregate"
DATASET_NAME = "eco_driving_aggregate"

MODE_WEEKLY_CUMULATIVE_SNAPSHOT = "weekly_cumulative_snapshot"
MODE_FINAL_MONTH_WEEKLY_SNAPSHOT = "final_month_weekly_snapshot"
MODE_MONTHLY_FULL_AGGREGATION = "monthly_full_aggregation"
MODE_SELECTED_MONTH_FULL_REBUILD = "selected_month_full_rebuild"
MODE_EXPLICIT_PERIOD = "explicit_period"

MODE_ALIASES = {
    "previous_completed_weekly_snapshot": MODE_WEEKLY_CUMULATIVE_SNAPSHOT,
    "weekly_cumulative_snapshot": MODE_WEEKLY_CUMULATIVE_SNAPSHOT,
    "final_month_weekly_snapshot": MODE_FINAL_MONTH_WEEKLY_SNAPSHOT,
    "month_end_final_weekly_snapshot": MODE_FINAL_MONTH_WEEKLY_SNAPSHOT,
    "previous_completed_monthly_aggregation": MODE_MONTHLY_FULL_AGGREGATION,
    "monthly_full_aggregation": MODE_MONTHLY_FULL_AGGREGATION,
}

PRIVATE_EXCLUSION_REASON = "PRIVATE_DRIVER_TAG"
ASSIGNMENT_DRIVER_RESTRICTIONS = "DRIVER_RESTRICTIONS"
ASSIGNMENT_DYSPONENT_ID = "DYSPONENT_ID"
ASSIGNMENT_SKIPPED_NO_ID = "SKIPPED_NO_ID"
DRIVER_CHART_NOT_LOADED = "DRIVER_CHART_NOT_LOADED"
DRIVER_CHART_NO_SOURCE_MATCHES = "DRIVER_CHART_NO_SOURCE_MATCHES"

RATE_COLUMNS = {
    "overrev_events_count": "overrev_events_per_100km",
    "harsh_braking_events": "harsh_braking_events_per_100km",
    "harsh_acceleration_events": "harsh_acceleration_events_per_100km",
    "harsh_turning_events": "harsh_turning_events_per_100km",
    "idle_events": "idle_events_per_100km",
    "speeding_140_160_count": "speeding_140_160_events_per_100km",
    "speeding_160_170_count": "speeding_160_170_events_per_100km",
    "speeding_170_plus_count": "speeding_170_plus_events_per_100km",
}

POINT_COLUMNS = {
    "overrev_events_count": "overrev_points",
    "harsh_braking_events": "harsh_braking_points",
    "harsh_acceleration_events": "harsh_acceleration_points",
    "harsh_turning_events": "harsh_turning_points",
    "idle_events": "idle_points",
    "speeding_140_160_count": "speeding_140_160_points",
    "speeding_160_170_count": "speeding_160_170_points",
    "speeding_170_plus_count": "speeding_170_plus_points",
}

# Re-exported from the shared canonical domain so existing importers and
# operational tooling keep resolving one definition, not a second copy.
SHARE_PERCENT_QUANTUM = PD.SHARE_PERCENT_QUANTUM
RAW_RATE_QUANTUM = PD.RAW_RATE_QUANTUM
STORED_RATE_QUANTUM = PD.STORED_RATE_QUANTUM

SUBTRACTION_COLUMNS = {
    "overrev_events_count": "overrev_maxpoints_subtract",
    "harsh_braking_events": "harsh_braking_maxpoints_subtract",
    "harsh_acceleration_events": "harsh_acceleration_maxpoints_subtract",
    "harsh_turning_events": "harsh_turning_maxpoints_subtract",
    "idle_events": "idle_maxpoints_subtract",
    "speeding_140_160_count": "speeding_140_160_maxpoints_subtract",
    "speeding_160_170_count": "speeding_160_170_maxpoints_subtract",
    "speeding_170_plus_count": "speeding_170_plus_maxpoints_subtract",
}


@dataclass(frozen=True)
class RankingPeriod:
    period_start_date: date
    period_end_date: date
    month_start_date: date
    period_sequence_in_month: int
    period_label: str
    is_partial_period: bool

    @property
    def start_ts(self) -> datetime:
        return _local_midnight(self.period_start_date)

    @property
    def end_ts(self) -> datetime:
        return _local_midnight(self.period_end_date)


class EcoDrivingAggregationPreconditionError(RuntimeError):
    def __init__(self, classification: str, diagnostics: dict[str, Any]) -> None:
        self.classification = classification
        self.diagnostics = diagnostics
        super().__init__(f"{classification}: Eco Driving driver-chart precondition failed")


def _require_dependency(module: str, feature: str):
    try:
        return __import__(module)
    except ImportError as exc:
        raise RuntimeError(f"Missing dependency for {feature}: {module}") from exc


def _safe_ident(name: str) -> str:
    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", name or ""):
        raise ValueError(f"Unsafe SQL identifier: {name!r}")
    return name


def _dict_row_factory():
    try:
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise RuntimeError("Missing dependency for Postgres row factory: psycopg") from exc
    return dict_row


def _load_client_account_config(*, client_id: str):
    try:
        from jobs.api.telematics.control_plane import load_client_account_config
    except ImportError as exc:
        raise RuntimeError("Missing dependency for Workflow A control plane: psycopg") from exc
    return load_client_account_config(client_id=client_id)


def _client_business_pg_conn(cfg):
    psycopg = _require_dependency("psycopg", "Postgres connection")
    dsn = (
        f"host={cfg.client_db_host} "
        f"port={cfg.client_db_port} "
        f"dbname={cfg.client_db_name} "
        f"user={cfg.client_db_user} "
        f"password={resolve_secret(cfg.client_db_password_secret_ref)}"
    )
    return set_pg_session_timezone(psycopg.connect(dsn))


def _as_bool(params: dict, key: str, default: bool) -> bool:
    if key not in params:
        return default
    value = params[key]
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "y", "on"}:
            return True
        if lowered in {"false", "0", "no", "n", "off"}:
            return False
    raise ValueError(f"{key} must be a boolean")


def _as_positive_int(params: dict, key: str, default: int) -> int:
    raw = params.get(key, default)
    value = int(raw)
    if value <= 0:
        raise ValueError(f"{key} must be > 0")
    return value


def _as_optional_positive_int(params: dict, key: str) -> int | None:
    if params.get(key) is None:
        return None
    value = int(params[key])
    if value <= 0:
        raise ValueError(f"{key} must be > 0")
    return value


def _parse_month(raw: str) -> date:
    try:
        parsed = datetime.strptime(raw.strip(), "%Y-%m")
    except ValueError as exc:
        raise ValueError("month must use YYYY-MM format") from exc
    return parsed.date().replace(day=1)


def _parse_date_param(raw: Any, name: str) -> date:
    try:
        return date.fromisoformat(str(raw).strip())
    except ValueError as exc:
        raise ValueError(f"{name} must use YYYY-MM-DD format") from exc


_local_midnight = PD.local_midnight
_next_month_start = PD.next_month_start
_previous_month_start = PD.previous_month_start


def _month_bounded_weekly_periods(month_start: date) -> list[RankingPeriod]:
    """Cumulative month-to-date reporting periods for one calendar month.

    The boundaries come from the shared canonical domain
    (``period_domain.month_week_buckets``) so the job and the portal's dynamic
    week selection cannot drift apart. Each stored row keeps the cumulative
    ``period_start_date = month_start``; the bucket contributes the boundary,
    the sequence and the partiality of the *incremental* segment ending there.
    """

    return [
        RankingPeriod(
            period_start_date=bucket.month_start_date,
            period_end_date=bucket.end_date_exclusive,
            month_start_date=bucket.month_start_date,
            period_sequence_in_month=bucket.sequence,
            period_label=f"{bucket.month_start_date:%Y-%m}-W{bucket.sequence}",
            is_partial_period=bucket.is_partial,
        )
        for bucket in PD.month_week_buckets(month_start)
    ]


def _period_for_cumulative_boundary(boundary_date: date) -> RankingPeriod:
    if boundary_date.day == 1:
        month_start = _previous_month_start(boundary_date)
    else:
        month_start = boundary_date.replace(day=1)
    for period in _month_bounded_weekly_periods(month_start):
        if period.period_end_date == boundary_date:
            return period
    raise ValueError(f"No Eco Driving weekly period ends at {boundary_date.isoformat()}")


def resolve_previous_completed_weekly_snapshot(now_local: datetime | None = None) -> RankingPeriod:
    """Return the latest completed cumulative weekly snapshot period.

    The returned `period_start_date` is always the calendar month start for
    that snapshot, not the incremental Monday boundary.
    """
    local_now = now_local or now_business_tz()
    if local_now.tzinfo is None:
        local_now = local_now.replace(tzinfo=get_business_timezone())
    local_now = local_now.astimezone(get_business_timezone())
    today = local_now.date()
    month_start = today.replace(day=1)

    candidates = [month_start]
    latest_monday = today - timedelta(days=today.weekday())
    candidates.append(latest_monday)
    boundary_date = max(day for day in candidates if _local_midnight(day) <= local_now)
    return _period_for_cumulative_boundary(boundary_date)


def resolve_final_month_weekly_snapshot(now_local: datetime | None = None) -> RankingPeriod:
    """Return the final cumulative weekly snapshot for the previous month."""
    local_now = now_local or now_business_tz()
    if local_now.tzinfo is None:
        local_now = local_now.replace(tzinfo=get_business_timezone())
    current_month_start = local_now.astimezone(get_business_timezone()).date().replace(day=1)
    return _period_for_cumulative_boundary(current_month_start)


def resolve_previous_completed_month(now_local: datetime | None = None) -> tuple[date, date]:
    """Return (month_start, month_end) for the previous completed calendar month."""
    local_now = now_local or now_business_tz()
    if local_now.tzinfo is None:
        local_now = local_now.replace(tzinfo=get_business_timezone())
    current_month_start = local_now.astimezone(get_business_timezone()).date().replace(day=1)
    month_start = _previous_month_start(current_month_start)
    return month_start, current_month_start


def _normalize_mode(raw: Any) -> str | None:
    if raw in (None, ""):
        return None
    mode = str(raw).strip()
    return MODE_ALIASES.get(mode, mode)


def _resolve_periods(params: dict) -> tuple[date | None, list[RankingPeriod], bool, str]:
    month_raw = params.get("month")
    period_start_raw = params.get("period_start_date")
    period_end_raw = params.get("period_end_date")
    mode = _normalize_mode(params.get("mode") or params.get("resolver"))

    if month_raw and (period_start_raw or period_end_raw):
        raise ValueError("Use either month or period_start_date/period_end_date, not both")
    if mode and (month_raw or period_start_raw or period_end_raw):
        raise ValueError("Use either mode/resolver or explicit month/period params, not both")
    if bool(period_start_raw) != bool(period_end_raw):
        raise ValueError("period_start_date and period_end_date must be provided together")

    if mode == MODE_WEEKLY_CUMULATIVE_SNAPSHOT:
        period = resolve_previous_completed_weekly_snapshot()
        return period.month_start_date, [period], True, MODE_WEEKLY_CUMULATIVE_SNAPSHOT

    if mode == MODE_FINAL_MONTH_WEEKLY_SNAPSHOT:
        period = resolve_final_month_weekly_snapshot()
        return period.month_start_date, [period], True, MODE_FINAL_MONTH_WEEKLY_SNAPSHOT

    if mode == MODE_MONTHLY_FULL_AGGREGATION:
        month_start, _month_end = resolve_previous_completed_month()
        return month_start, _month_bounded_weekly_periods(month_start), False, MODE_MONTHLY_FULL_AGGREGATION

    if mode:
        raise ValueError(
            "Unsupported mode/resolver. Expected one of: "
            f"{', '.join(sorted(MODE_ALIASES))}"
        )

    if month_raw:
        month_start = _parse_month(str(month_raw))
        return month_start, _month_bounded_weekly_periods(month_start), False, MODE_SELECTED_MONTH_FULL_REBUILD

    if period_start_raw and period_end_raw:
        period_start = _parse_date_param(period_start_raw, "period_start_date")
        period_end = _parse_date_param(period_end_raw, "period_end_date")
        if period_end <= period_start:
            raise ValueError("period_end_date must be after period_start_date")
        month_start = period_start.replace(day=1)
        if period_end > _next_month_start(month_start):
            raise ValueError("explicit period must fit inside one calendar month")
        for period in _month_bounded_weekly_periods(month_start):
            if period.period_start_date == period_start and period.period_end_date == period_end:
                return month_start, [period], True, MODE_EXPLICIT_PERIOD
        raise ValueError("explicit period must match a cumulative month-to-date weekly report")

    raise ValueError("Missing required param: month, period_start_date/period_end_date, or mode/resolver")


def _include_flags(params: dict, *, selected_mode: str, explicit_period_mode: bool) -> tuple[bool, bool]:
    if selected_mode in {MODE_WEEKLY_CUMULATIVE_SNAPSHOT, MODE_FINAL_MONTH_WEEKLY_SNAPSHOT}:
        default_weekly = True
        default_monthly = False
    elif selected_mode == MODE_MONTHLY_FULL_AGGREGATION:
        default_weekly = False
        default_monthly = True
    elif explicit_period_mode:
        default_weekly = True
        default_monthly = False
    else:
        default_weekly = True
        default_monthly = True
    return (
        _as_bool(params, "include_weekly", default_weekly),
        _as_bool(params, "include_monthly", default_monthly),
    )


def _trimmed_or_none(value: Any) -> str | None:
    if value is None:
        return None
    trimmed = str(value).strip()
    return trimmed or None


def _assignment_for_values(driver_restrictions: Any, dysponent_id: Any) -> tuple[str | None, str]:
    driver_restrictions_value = _trimmed_or_none(driver_restrictions)
    if driver_restrictions_value:
        return driver_restrictions_value, ASSIGNMENT_DRIVER_RESTRICTIONS
    dysponent_id_value = _trimmed_or_none(dysponent_id)
    if dysponent_id_value:
        return dysponent_id_value, ASSIGNMENT_DYSPONENT_ID
    return None, ASSIGNMENT_SKIPPED_NO_ID


def _is_private_trip(driver_tag_description: Any) -> bool:
    return "pryw" in str(driver_tag_description or "").lower()


# The persisted arithmetic, the qualification gate and the ranking-group rule
# all live in the shared canonical domain so the portal's dynamic week
# recomputation runs the identical code rather than a copy of it.
_total_kilometers = PD.total_kilometers
_rate_per_100km = PD.rate_per_100km
_raw_rate_per_100km = PD.raw_rate_per_100km
_round_stored_per_100km = PD.round_stored_per_100km
_qualification_and_calculation = PD.qualification_and_calculation
_ranking_group = PD.ranking_group


def _rate_scoring_diagnostics_from_aggregate(row: dict, period: RankingPeriod | None, *, monthly: bool) -> dict:
    stats = _stats_row_from_aggregate(row, period, monthly=monthly)
    total_km = _total_kilometers(int(row["total_distance_meters"] or 0))
    metric_diagnostics = []
    for metric in REQUIRED_METRICS:
        raw_rate = _raw_rate_per_100km(int(row[metric] or 0), total_km)
        metric_diagnostics.append(
            {
                "metric_key": metric,
                "numerator_value": int(row[metric] or 0),
                "denominator_total_kilometers": total_km,
                "raw_rate_before_rounding": raw_rate,
                "stored_display_rate": stats[RATE_COLUMNS[metric]],
                "points": stats[POINT_COLUMNS[metric]],
                "persisted_subtract": stats[SUBTRACTION_COLUMNS[metric]],
            }
        )

    subtractions = [
        stats[SUBTRACTION_COLUMNS[metric]]
        for metric in REQUIRED_METRICS
        if stats[SUBTRACTION_COLUMNS[metric]] is not None
    ]
    recomputed_score = Decimal(100 + sum(subtractions)) if len(subtractions) == len(REQUIRED_METRICS) else None
    stored_score = stats["eco_driving_score_total"]
    difference = stored_score - recomputed_score if stored_score is not None and recomputed_score is not None else None
    return {
        "assigned_id": stats["assigned_id"],
        "period_start_date": stats.get("period_start_date") or stats.get("month_start_date"),
        "period_end_date": stats.get("period_end_date") or stats.get("month_end_date"),
        "eco_driving_score_total": stored_score,
        "metric_diagnostics": metric_diagnostics,
        "all_events_per_100km": {RATE_COLUMNS[metric]: stats[RATE_COLUMNS[metric]] for metric in REQUIRED_METRICS},
        "all_maxpoints_subtract": {SUBTRACTION_COLUMNS[metric]: stats[SUBTRACTION_COLUMNS[metric]] for metric in REQUIRED_METRICS},
        "recomputed_score_from_subtract_columns": recomputed_score,
        "score_difference": difference,
    }


def _stats_row_from_aggregate(row: dict, period: RankingPeriod | None, *, monthly: bool) -> dict:
    total_distance_meters = int(row["total_distance_meters"] or 0)
    # One scoring entry point, shared with the portal's dynamic recomputation.
    scored = PD.score_aggregate(
        {metric: int(row[metric] or 0) for metric in REQUIRED_METRICS},
        total_distance_meters,
    )
    total_km = scored.total_kilometers
    stored_rates = scored.stored_rates
    metric_points = scored.metric_points
    subtractions = scored.metric_losses
    top_1_validation = scored.top_1_validation
    top_2_validation = scored.top_2_validation
    qualification_status = scored.qualification_status
    calculation_status = scored.calculation_status

    chart_exists = bool(row.get("driver_id"))
    ranking_included = row.get("ranking_included") if chart_exists else None
    base = {
        "client_id": row["client_id"],
        "client_code": row.get("client_code"),
        "assigned_id": row["assigned_id"],
        "trips_count": int(row["trips_count"] or 0),
        "source_trips_count": int(row["source_trips_count"] or 0),
        "skipped_trips_count": int(row["skipped_trips_count"] or 0),
        "total_distance_meters": total_distance_meters,
        "total_kilometers": total_km,
        "qualification_status": qualification_status,
        "calculation_status": calculation_status,
        "ranking_included": ranking_included,
        "ranking_group": _ranking_group(qualification_status, chart_exists, ranking_included),
        "ranking_position": None,
        "ranking_total_participants": None,
        "ecodriving_rating_type_share_percent": None,
    }
    for metric in REQUIRED_METRICS:
        base[metric] = int(row[metric] or 0)
        base[RATE_COLUMNS[metric]] = stored_rates[metric]
        base[POINT_COLUMNS[metric]] = metric_points[metric]
        base[SUBTRACTION_COLUMNS[metric]] = subtractions[metric]
    base["eco_driving_score_total"] = scored.eco_driving_score_total
    base["top_1_validation"] = top_1_validation
    base["top_2_validation"] = top_2_validation
    base["ecodriving_rating_type"] = scored.ecodriving_rating_type

    if monthly:
        base["month_start_date"] = row["month_start_date"]
        base["month_end_date"] = row["month_end_date"]
    else:
        if period is None:
            raise ValueError("period is required for weekly stats")
        base.update(
            {
                "week_start_date": period.period_start_date,
                "week_end_date": period.period_end_date,
                "period_start_date": period.period_start_date,
                "period_end_date": period.period_end_date,
                "month_start_date": period.month_start_date,
                "period_sequence_in_month": period.period_sequence_in_month,
                "period_label": period.period_label,
                "is_partial_period": period.is_partial_period,
            }
        )
    return base


_sort_rank_rows = PD.sort_rank_rows


def _apply_rankings(rows: list[dict], *, monthly: bool) -> None:
    period_keys = (
        ("month_start_date",)
        if monthly
        else ("period_start_date", "period_end_date")
    )
    grouped: dict[tuple[Any, ...], list[dict]] = {}
    for row in rows:
        if row["ranking_group"] not in {"INCLUDED", "EXCLUDED"}:
            row["ranking_position"] = None
            row["ranking_total_participants"] = None
            continue
        key = tuple(row[column] for column in period_keys) + (row["ranking_group"],)
        grouped.setdefault(key, []).append(row)

    for group_rows in grouped.values():
        PD.assign_ranking_positions(group_rows)


def _apply_rating_type_share_percent(rows: list[dict], *, monthly: bool) -> None:
    period_keys = (
        ("month_start_date",)
        if monthly
        else ("period_start_date", "period_end_date")
    )
    grouped: dict[tuple[Any, ...], list[dict]] = {}
    for row in rows:
        row["ecodriving_rating_type_share_percent"] = None
        key = tuple(row[column] for column in period_keys)
        grouped.setdefault(key, []).append(row)

    for group_rows in grouped.values():
        PD.apply_rating_type_share_percent(group_rows)


def _count_rank_groups(rows: list[dict]) -> tuple[int, int, int, int]:
    included = sum(1 for row in rows if row["ranking_group"] == "INCLUDED")
    excluded = sum(1 for row in rows if row["ranking_group"] == "EXCLUDED")
    unknown = sum(1 for row in rows if row["ranking_group"] == "UNKNOWN_DRIVER")
    # Rows outside every ranking population (non-QUALIFIED), reported separately
    # so operators can see them without them inflating any ranking group.
    not_ranked = sum(1 for row in rows if row["ranking_group"] is None)
    return included, excluded, unknown, not_ranked


def _assignment_upsert_batch(
    cur,
    *,
    trips_table: str,
    assignments_table: str,
    client_id: str,
    start_ts: datetime,
    end_ts: datetime,
    assigned_id: str | None,
    batch_size: int,
    last_provider_trip_id: int,
) -> dict:
    assigned_filter = "AND prepared.assigned_id = %(assigned_id)s" if assigned_id else ""
    cur.execute(
        f"""
        WITH source_rows AS (
          SELECT
            client_id,
            client_code,
            provider_trip_id,
            record_id,
            NULLIF(btrim("Driver_Restrictions"), '') AS driver_restrictions_raw,
            NULLIF(btrim("Dysponent_ID"), '') AS dysponent_id_raw,
            driver_tag_description,
            start_timestamp,
            end_timestamp,
            trip_distance_meters,
            COALESCE(overrev_events_count, 0) AS overrev_events_count,
            COALESCE(harsh_braking_events, 0) AS harsh_braking_events,
            COALESCE(harsh_acceleration_events, 0) AS harsh_acceleration_events,
            COALESCE(harsh_turning_events, 0) AS harsh_turning_events,
            COALESCE(idle_events, 0) AS idle_events,
            COALESCE(speeding_140_160_count, 0) AS speeding_140_160_count,
            COALESCE(speeding_160_170_count, 0) AS speeding_160_170_count,
            COALESCE(speeding_170_plus_count, 0) AS speeding_170_plus_count
          FROM {trips_table}
          WHERE client_id = %(client_id)s
            AND start_timestamp IS NOT NULL
            AND start_timestamp >= %(start_ts)s
            AND start_timestamp < %(end_ts)s
            AND provider_trip_id > %(last_provider_trip_id)s
          ORDER BY provider_trip_id
          LIMIT %(batch_size)s
        ),
        prepared AS (
          SELECT
            *,
            CASE
              WHEN driver_restrictions_raw IS NOT NULL THEN driver_restrictions_raw
              WHEN dysponent_id_raw IS NOT NULL THEN dysponent_id_raw
              ELSE NULL
            END AS assigned_id,
            CASE
              WHEN driver_restrictions_raw IS NOT NULL THEN 'DRIVER_RESTRICTIONS'
              WHEN dysponent_id_raw IS NOT NULL THEN 'DYSPONENT_ID'
              ELSE 'SKIPPED_NO_ID'
            END AS assignment_source,
            (COALESCE(driver_tag_description, '') ILIKE '%%pryw%%') AS is_private_trip
          FROM source_rows
        ),
        filtered AS (
          SELECT *
          FROM prepared
          WHERE TRUE
          {assigned_filter}
        ),
        upserted AS (
          INSERT INTO {assignments_table} (
            client_id,
            client_code,
            provider_trip_id,
            record_id,
            assigned_id,
            assignment_source,
            driver_restrictions_raw,
            dysponent_id_raw,
            driver_tag_description,
            is_private_trip,
            exclusion_reason,
            aggregation_included,
            trip_start_ts,
            trip_end_ts,
            business_week_start_date,
            business_week_end_date,
            trip_distance_meters,
            overrev_events_count,
            harsh_braking_events,
            harsh_acceleration_events,
            harsh_turning_events,
            idle_events,
            speeding_140_160_count,
            speeding_160_170_count,
            speeding_170_plus_count,
            updated_at
          )
          SELECT
            client_id,
            client_code,
            provider_trip_id,
            record_id,
            assigned_id,
            assignment_source,
            driver_restrictions_raw,
            dysponent_id_raw,
            driver_tag_description,
            is_private_trip,
            CASE WHEN is_private_trip THEN 'PRIVATE_DRIVER_TAG' ELSE NULL END,
            (assigned_id IS NOT NULL AND is_private_trip IS FALSE),
            start_timestamp,
            end_timestamp,
            date_trunc('week', start_timestamp AT TIME ZONE %(timezone)s)::date,
            (date_trunc('week', start_timestamp AT TIME ZONE %(timezone)s)::date + 7),
            trip_distance_meters,
            overrev_events_count,
            harsh_braking_events,
            harsh_acceleration_events,
            harsh_turning_events,
            idle_events,
            speeding_140_160_count,
            speeding_160_170_count,
            speeding_170_plus_count,
            now()
          FROM filtered
          ON CONFLICT (client_id, provider_trip_id) DO UPDATE SET
            client_code = EXCLUDED.client_code,
            record_id = EXCLUDED.record_id,
            assigned_id = EXCLUDED.assigned_id,
            assignment_source = EXCLUDED.assignment_source,
            driver_restrictions_raw = EXCLUDED.driver_restrictions_raw,
            dysponent_id_raw = EXCLUDED.dysponent_id_raw,
            driver_tag_description = EXCLUDED.driver_tag_description,
            is_private_trip = EXCLUDED.is_private_trip,
            exclusion_reason = EXCLUDED.exclusion_reason,
            aggregation_included = EXCLUDED.aggregation_included,
            trip_start_ts = EXCLUDED.trip_start_ts,
            trip_end_ts = EXCLUDED.trip_end_ts,
            business_week_start_date = EXCLUDED.business_week_start_date,
            business_week_end_date = EXCLUDED.business_week_end_date,
            trip_distance_meters = EXCLUDED.trip_distance_meters,
            overrev_events_count = EXCLUDED.overrev_events_count,
            harsh_braking_events = EXCLUDED.harsh_braking_events,
            harsh_acceleration_events = EXCLUDED.harsh_acceleration_events,
            harsh_turning_events = EXCLUDED.harsh_turning_events,
            idle_events = EXCLUDED.idle_events,
            speeding_140_160_count = EXCLUDED.speeding_140_160_count,
            speeding_160_170_count = EXCLUDED.speeding_160_170_count,
            speeding_170_plus_count = EXCLUDED.speeding_170_plus_count,
            updated_at = now()
          RETURNING *
        )
        SELECT
          (SELECT count(*) FROM source_rows) AS source_batch_count,
          (SELECT COALESCE(max(provider_trip_id), %(last_provider_trip_id)s) FROM source_rows) AS max_provider_trip_id,
          (SELECT count(*) FROM filtered) AS source_trips_seen,
          count(*) AS assignments_upserted,
          count(*) FILTER (WHERE assigned_id IS NOT NULL) AS assigned_trips_count,
          count(*) FILTER (WHERE assignment_source = 'SKIPPED_NO_ID') AS skipped_no_id_count,
          count(*) FILTER (WHERE is_private_trip) AS private_excluded_count,
          count(*) FILTER (WHERE aggregation_included) AS aggregation_included_count
        FROM upserted
        """,
        {
            "client_id": client_id,
            "start_ts": start_ts,
            "end_ts": end_ts,
            "assigned_id": assigned_id,
            "last_provider_trip_id": last_provider_trip_id,
            "batch_size": batch_size,
            "timezone": get_business_timezone_name(),
        },
    )
    return dict(cur.fetchone())


def _upsert_assignments(
    cur,
    *,
    trips_table: str,
    assignments_table: str,
    client_id: str,
    start_ts: datetime,
    end_ts: datetime,
    assigned_id: str | None,
    batch_size: int,
    max_batches: int | None,
) -> dict:
    totals = {
        "source_trips_seen": 0,
        "assignments_upserted": 0,
        "assigned_trips_count": 0,
        "skipped_no_id_count": 0,
        "private_excluded_count": 0,
        "aggregation_included_count": 0,
    }
    last_provider_trip_id = -1
    batches = 0
    while True:
        if max_batches is not None and batches >= max_batches:
            break
        batch = _assignment_upsert_batch(
            cur,
            trips_table=trips_table,
            assignments_table=assignments_table,
            client_id=client_id,
            start_ts=start_ts,
            end_ts=end_ts,
            assigned_id=assigned_id,
            batch_size=batch_size,
            last_provider_trip_id=last_provider_trip_id,
        )
        source_batch_count = int(batch["source_batch_count"] or 0)
        if source_batch_count == 0:
            break
        batches += 1
        last_provider_trip_id = int(batch["max_provider_trip_id"])
        for key in totals:
            totals[key] += int(batch[key] or 0)
    totals["assignment_batches_processed"] = batches
    totals["assignment_batch_limit_reached"] = max_batches is not None and batches >= max_batches
    return totals


def _fetch_aggregate_rows(
    cur,
    *,
    assignments_table: str,
    driver_chart_table: str,
    client_id: str,
    start_ts: datetime,
    end_ts: datetime,
    assigned_id: str | None,
    monthly: bool,
    month_start: date | None = None,
    month_end: date | None = None,
) -> list[dict]:
    assigned_filter = "AND a.assigned_id = %(assigned_id)s" if assigned_id else ""
    date_columns = (
        "%(month_start)s::date AS month_start_date, %(month_end)s::date AS month_end_date,"
        if monthly
        else ""
    )
    cur.execute(
        f"""
        WITH grouped AS (
          SELECT
            a.client_id,
            max(a.client_code) AS client_code,
            a.assigned_id,
            {date_columns}
            count(*) FILTER (
              WHERE a.aggregation_included IS TRUE AND a.is_private_trip IS FALSE
            )::int AS trips_count,
            count(*)::int AS source_trips_count,
            count(*) FILTER (
              WHERE a.aggregation_included IS NOT TRUE OR a.is_private_trip IS TRUE
            )::int AS skipped_trips_count,
            COALESCE(sum(COALESCE(a.trip_distance_meters, 0)) FILTER (
              WHERE a.aggregation_included IS TRUE AND a.is_private_trip IS FALSE
            ), 0)::bigint AS total_distance_meters,
            COALESCE(sum(a.overrev_events_count) FILTER (
              WHERE a.aggregation_included IS TRUE AND a.is_private_trip IS FALSE
            ), 0)::bigint AS overrev_events_count,
            COALESCE(sum(a.harsh_braking_events) FILTER (
              WHERE a.aggregation_included IS TRUE AND a.is_private_trip IS FALSE
            ), 0)::bigint AS harsh_braking_events,
            COALESCE(sum(a.harsh_acceleration_events) FILTER (
              WHERE a.aggregation_included IS TRUE AND a.is_private_trip IS FALSE
            ), 0)::bigint AS harsh_acceleration_events,
            COALESCE(sum(a.harsh_turning_events) FILTER (
              WHERE a.aggregation_included IS TRUE AND a.is_private_trip IS FALSE
            ), 0)::bigint AS harsh_turning_events,
            COALESCE(sum(a.idle_events) FILTER (
              WHERE a.aggregation_included IS TRUE AND a.is_private_trip IS FALSE
            ), 0)::bigint AS idle_events,
            COALESCE(sum(a.speeding_140_160_count) FILTER (
              WHERE a.aggregation_included IS TRUE AND a.is_private_trip IS FALSE
            ), 0)::bigint AS speeding_140_160_count,
            COALESCE(sum(a.speeding_160_170_count) FILTER (
              WHERE a.aggregation_included IS TRUE AND a.is_private_trip IS FALSE
            ), 0)::bigint AS speeding_160_170_count,
            COALESCE(sum(a.speeding_170_plus_count) FILTER (
              WHERE a.aggregation_included IS TRUE AND a.is_private_trip IS FALSE
            ), 0)::bigint AS speeding_170_plus_count
          FROM {assignments_table} a
          WHERE a.client_id = %(client_id)s
            AND a.trip_start_ts >= %(start_ts)s
            AND a.trip_start_ts < %(end_ts)s
            AND a.assigned_id IS NOT NULL
            {assigned_filter}
          GROUP BY a.client_id, a.assigned_id
          HAVING count(*) FILTER (
            WHERE a.aggregation_included IS TRUE AND a.is_private_trip IS FALSE
          ) > 0
        )
        SELECT
          g.*,
          c.driver_id,
          c.ranking_included
        FROM grouped g
        LEFT JOIN {driver_chart_table} c
          ON c.client_id = g.client_id
         AND c.driver_id = g.assigned_id
        ORDER BY g.assigned_id
        """,
        {
            "client_id": client_id,
            "start_ts": start_ts,
            "end_ts": end_ts,
            "assigned_id": assigned_id,
            "month_start": month_start,
            "month_end": month_end,
        },
    )
    return [dict(row) for row in cur.fetchall()]


def _fetch_driver_chart_count(cur, *, driver_chart_table: str, client_id: str) -> int:
    cur.execute(
        f"""
        SELECT count(*)::int AS chart_row_count
        FROM {driver_chart_table}
        WHERE client_id = %s
          AND driver_id IS NOT NULL
          AND btrim(driver_id) <> ''
        """,
        (client_id,),
    )
    row = cur.fetchone()
    return int((row or {}).get("chart_row_count") or 0)


def _source_chart_match_diagnostics(rows: list[dict]) -> dict[str, int | float]:
    source_row_count = len(rows)
    matched_count = sum(1 for row in rows if row.get("driver_id"))
    unmatched_count = source_row_count - matched_count
    unmatched_percentage = (
        round(unmatched_count * 100.0 / source_row_count, 2)
        if source_row_count
        else 0.0
    )
    return {
        "source_driver_rows": source_row_count,
        "matched_driver_rows": matched_count,
        "unmatched_driver_rows": unmatched_count,
        "unmatched_driver_percentage": unmatched_percentage,
    }


def _validate_driver_chart_loaded(
    chart_row_count: int,
    *,
    period_start_date: date,
    period_end_date: date,
) -> None:
    if chart_row_count > 0:
        return
    raise EcoDrivingAggregationPreconditionError(
        DRIVER_CHART_NOT_LOADED,
        {
            "driver_chart_rows": 0,
            "source_driver_rows": 0,
            "matched_driver_rows": 0,
            "unmatched_driver_rows": 0,
            "unmatched_driver_percentage": 0.0,
            "period_start_date": period_start_date.isoformat(),
            "period_end_date": period_end_date.isoformat(),
        },
    )


def _validate_source_chart_matches(
    rows: list[dict],
    *,
    chart_row_count: int,
    period_start_date: date,
    period_end_date: date,
    snapshot_type: str,
) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {
        "driver_chart_rows": chart_row_count,
        "snapshot_type": snapshot_type,
        "period_start_date": period_start_date.isoformat(),
        "period_end_date": period_end_date.isoformat(),
    }
    diagnostics.update(_source_chart_match_diagnostics(rows))
    if diagnostics["source_driver_rows"] and diagnostics["matched_driver_rows"] == 0:
        raise EcoDrivingAggregationPreconditionError(
            DRIVER_CHART_NO_SOURCE_MATCHES,
            diagnostics,
        )
    return diagnostics


def _delete_existing_stats(
    cur,
    *,
    table: str,
    client_id: str,
    assigned_id: str | None,
    weekly_periods: list[RankingPeriod] | None = None,
    month_start: date | None = None,
    monthly: bool,
) -> None:
    assigned_filter = "AND assigned_id = %(assigned_id)s" if assigned_id else ""
    if monthly:
        cur.execute(
            f"""
            DELETE FROM {table}
            WHERE client_id = %(client_id)s
              AND month_start_date = %(month_start)s
              {assigned_filter}
            """,
            {"client_id": client_id, "month_start": month_start, "assigned_id": assigned_id},
        )
        return

    if month_start is not None:
        cur.execute(
            f"""
            DELETE FROM {table}
            WHERE client_id = %(client_id)s
              AND month_start_date = %(month_start)s
              {assigned_filter}
            """,
            {"client_id": client_id, "month_start": month_start, "assigned_id": assigned_id},
        )
        return

    if not weekly_periods:
        return
    cur.executemany(
        f"""
        DELETE FROM {table}
        WHERE client_id = %(client_id)s
          AND period_start_date = %(period_start_date)s
          AND period_end_date = %(period_end_date)s
          {assigned_filter}
        """,
        [
            {
                "client_id": client_id,
                "period_start_date": period.period_start_date,
                "period_end_date": period.period_end_date,
                "assigned_id": assigned_id,
            }
            for period in weekly_periods
        ],
    )


def _weekly_row_values(row: dict) -> tuple:
    return (
        row["client_id"], row["client_code"], row["assigned_id"],
        row["week_start_date"], row["week_end_date"],
        row["period_start_date"], row["period_end_date"], row["month_start_date"],
        row["period_sequence_in_month"], row["period_label"], row["is_partial_period"],
        row["trips_count"], row["source_trips_count"], row["skipped_trips_count"],
        row["total_distance_meters"], row["total_kilometers"],
        row["overrev_events_count"], row["harsh_braking_events"], row["harsh_acceleration_events"],
        row["harsh_turning_events"], row["idle_events"],
        row["speeding_140_160_count"], row["speeding_160_170_count"], row["speeding_170_plus_count"],
        row["overrev_events_per_100km"], row["harsh_braking_events_per_100km"],
        row["harsh_acceleration_events_per_100km"], row["harsh_turning_events_per_100km"],
        row["idle_events_per_100km"], row["speeding_140_160_events_per_100km"],
        row["speeding_160_170_events_per_100km"], row["speeding_170_plus_events_per_100km"],
        row["overrev_points"], row["harsh_braking_points"], row["harsh_acceleration_points"],
        row["harsh_turning_points"], row["idle_points"],
        row["speeding_140_160_points"], row["speeding_160_170_points"], row["speeding_170_plus_points"],
        row["overrev_maxpoints_subtract"], row["harsh_braking_maxpoints_subtract"],
        row["harsh_acceleration_maxpoints_subtract"], row["harsh_turning_maxpoints_subtract"],
        row["idle_maxpoints_subtract"], row["speeding_140_160_maxpoints_subtract"],
        row["speeding_160_170_maxpoints_subtract"], row["speeding_170_plus_maxpoints_subtract"],
        row["top_1_validation"], row["top_2_validation"], row["ecodriving_rating_type"],
        row["ecodriving_rating_type_share_percent"],
        row["eco_driving_score_total"], row["qualification_status"], row["calculation_status"],
        row["ranking_included"], row["ranking_group"], row["ranking_position"], row["ranking_total_participants"],
    )


def _monthly_row_values(row: dict) -> tuple:
    return (
        row["client_id"], row["client_code"], row["assigned_id"],
        row["month_start_date"], row["month_end_date"],
        row["trips_count"], row["source_trips_count"], row["skipped_trips_count"],
        row["total_distance_meters"], row["total_kilometers"],
        row["overrev_events_count"], row["harsh_braking_events"], row["harsh_acceleration_events"],
        row["harsh_turning_events"], row["idle_events"],
        row["speeding_140_160_count"], row["speeding_160_170_count"], row["speeding_170_plus_count"],
        row["overrev_events_per_100km"], row["harsh_braking_events_per_100km"],
        row["harsh_acceleration_events_per_100km"], row["harsh_turning_events_per_100km"],
        row["idle_events_per_100km"], row["speeding_140_160_events_per_100km"],
        row["speeding_160_170_events_per_100km"], row["speeding_170_plus_events_per_100km"],
        row["overrev_points"], row["harsh_braking_points"], row["harsh_acceleration_points"],
        row["harsh_turning_points"], row["idle_points"],
        row["speeding_140_160_points"], row["speeding_160_170_points"], row["speeding_170_plus_points"],
        row["overrev_maxpoints_subtract"], row["harsh_braking_maxpoints_subtract"],
        row["harsh_acceleration_maxpoints_subtract"], row["harsh_turning_maxpoints_subtract"],
        row["idle_maxpoints_subtract"], row["speeding_140_160_maxpoints_subtract"],
        row["speeding_160_170_maxpoints_subtract"], row["speeding_170_plus_maxpoints_subtract"],
        row["top_1_validation"], row["top_2_validation"], row["ecodriving_rating_type"],
        row["ecodriving_rating_type_share_percent"],
        row["eco_driving_score_total"], row["qualification_status"], row["calculation_status"],
        row["ranking_included"], row["ranking_group"], row["ranking_position"], row["ranking_total_participants"],
    )


def _upsert_weekly_stats(cur, *, weekly_table: str, rows: list[dict]) -> int:
    if not rows:
        return 0
    cur.executemany(
        f"""
        INSERT INTO {weekly_table} (
          client_id, client_code, assigned_id,
          week_start_date, week_end_date,
          period_start_date, period_end_date, month_start_date,
          period_sequence_in_month, period_label, is_partial_period,
          trips_count, source_trips_count, skipped_trips_count,
          total_distance_meters, total_kilometers,
          overrev_events_count, harsh_braking_events, harsh_acceleration_events,
          harsh_turning_events, idle_events,
          speeding_140_160_count, speeding_160_170_count, speeding_170_plus_count,
          overrev_events_per_100km, harsh_braking_events_per_100km,
          harsh_acceleration_events_per_100km, harsh_turning_events_per_100km,
          idle_events_per_100km, speeding_140_160_events_per_100km,
          speeding_160_170_events_per_100km, speeding_170_plus_events_per_100km,
          overrev_points, harsh_braking_points, harsh_acceleration_points,
          harsh_turning_points, idle_points,
          speeding_140_160_points, speeding_160_170_points, speeding_170_plus_points,
          overrev_maxpoints_subtract, harsh_braking_maxpoints_subtract,
          harsh_acceleration_maxpoints_subtract, harsh_turning_maxpoints_subtract,
          idle_maxpoints_subtract, speeding_140_160_maxpoints_subtract,
          speeding_160_170_maxpoints_subtract, speeding_170_plus_maxpoints_subtract,
          top_1_validation, top_2_validation, ecodriving_rating_type,
          ecodriving_rating_type_share_percent,
          eco_driving_score_total, qualification_status, calculation_status,
          ranking_included, ranking_group, ranking_position, ranking_total_participants,
          updated_at
        ) VALUES (
          %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
          %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
          %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
          %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW()
        )
        ON CONFLICT (client_id, assigned_id, period_start_date, period_end_date) DO UPDATE SET
          client_code = EXCLUDED.client_code,
          week_start_date = EXCLUDED.week_start_date,
          week_end_date = EXCLUDED.week_end_date,
          month_start_date = EXCLUDED.month_start_date,
          period_sequence_in_month = EXCLUDED.period_sequence_in_month,
          period_label = EXCLUDED.period_label,
          is_partial_period = EXCLUDED.is_partial_period,
          trips_count = EXCLUDED.trips_count,
          source_trips_count = EXCLUDED.source_trips_count,
          skipped_trips_count = EXCLUDED.skipped_trips_count,
          total_distance_meters = EXCLUDED.total_distance_meters,
          total_kilometers = EXCLUDED.total_kilometers,
          overrev_events_count = EXCLUDED.overrev_events_count,
          harsh_braking_events = EXCLUDED.harsh_braking_events,
          harsh_acceleration_events = EXCLUDED.harsh_acceleration_events,
          harsh_turning_events = EXCLUDED.harsh_turning_events,
          idle_events = EXCLUDED.idle_events,
          speeding_140_160_count = EXCLUDED.speeding_140_160_count,
          speeding_160_170_count = EXCLUDED.speeding_160_170_count,
          speeding_170_plus_count = EXCLUDED.speeding_170_plus_count,
          overrev_events_per_100km = EXCLUDED.overrev_events_per_100km,
          harsh_braking_events_per_100km = EXCLUDED.harsh_braking_events_per_100km,
          harsh_acceleration_events_per_100km = EXCLUDED.harsh_acceleration_events_per_100km,
          harsh_turning_events_per_100km = EXCLUDED.harsh_turning_events_per_100km,
          idle_events_per_100km = EXCLUDED.idle_events_per_100km,
          speeding_140_160_events_per_100km = EXCLUDED.speeding_140_160_events_per_100km,
          speeding_160_170_events_per_100km = EXCLUDED.speeding_160_170_events_per_100km,
          speeding_170_plus_events_per_100km = EXCLUDED.speeding_170_plus_events_per_100km,
          overrev_points = EXCLUDED.overrev_points,
          harsh_braking_points = EXCLUDED.harsh_braking_points,
          harsh_acceleration_points = EXCLUDED.harsh_acceleration_points,
          harsh_turning_points = EXCLUDED.harsh_turning_points,
          idle_points = EXCLUDED.idle_points,
          speeding_140_160_points = EXCLUDED.speeding_140_160_points,
          speeding_160_170_points = EXCLUDED.speeding_160_170_points,
          speeding_170_plus_points = EXCLUDED.speeding_170_plus_points,
          overrev_maxpoints_subtract = EXCLUDED.overrev_maxpoints_subtract,
          harsh_braking_maxpoints_subtract = EXCLUDED.harsh_braking_maxpoints_subtract,
          harsh_acceleration_maxpoints_subtract = EXCLUDED.harsh_acceleration_maxpoints_subtract,
          harsh_turning_maxpoints_subtract = EXCLUDED.harsh_turning_maxpoints_subtract,
          idle_maxpoints_subtract = EXCLUDED.idle_maxpoints_subtract,
          speeding_140_160_maxpoints_subtract = EXCLUDED.speeding_140_160_maxpoints_subtract,
          speeding_160_170_maxpoints_subtract = EXCLUDED.speeding_160_170_maxpoints_subtract,
          speeding_170_plus_maxpoints_subtract = EXCLUDED.speeding_170_plus_maxpoints_subtract,
          top_1_validation = EXCLUDED.top_1_validation,
          top_2_validation = EXCLUDED.top_2_validation,
          ecodriving_rating_type = EXCLUDED.ecodriving_rating_type,
          ecodriving_rating_type_share_percent = EXCLUDED.ecodriving_rating_type_share_percent,
          eco_driving_score_total = EXCLUDED.eco_driving_score_total,
          qualification_status = EXCLUDED.qualification_status,
          calculation_status = EXCLUDED.calculation_status,
          ranking_included = EXCLUDED.ranking_included,
          ranking_group = EXCLUDED.ranking_group,
          ranking_position = EXCLUDED.ranking_position,
          ranking_total_participants = EXCLUDED.ranking_total_participants,
          updated_at = NOW()
        """,
        [_weekly_row_values(row) for row in rows],
    )
    return len(rows)


def _upsert_monthly_stats(cur, *, monthly_table: str, rows: list[dict]) -> int:
    if not rows:
        return 0
    cur.executemany(
        f"""
        INSERT INTO {monthly_table} (
          client_id, client_code, assigned_id,
          month_start_date, month_end_date,
          trips_count, source_trips_count, skipped_trips_count,
          total_distance_meters, total_kilometers,
          overrev_events_count, harsh_braking_events, harsh_acceleration_events,
          harsh_turning_events, idle_events,
          speeding_140_160_count, speeding_160_170_count, speeding_170_plus_count,
          overrev_events_per_100km, harsh_braking_events_per_100km,
          harsh_acceleration_events_per_100km, harsh_turning_events_per_100km,
          idle_events_per_100km, speeding_140_160_events_per_100km,
          speeding_160_170_events_per_100km, speeding_170_plus_events_per_100km,
          overrev_points, harsh_braking_points, harsh_acceleration_points,
          harsh_turning_points, idle_points,
          speeding_140_160_points, speeding_160_170_points, speeding_170_plus_points,
          overrev_maxpoints_subtract, harsh_braking_maxpoints_subtract,
          harsh_acceleration_maxpoints_subtract, harsh_turning_maxpoints_subtract,
          idle_maxpoints_subtract, speeding_140_160_maxpoints_subtract,
          speeding_160_170_maxpoints_subtract, speeding_170_plus_maxpoints_subtract,
          top_1_validation, top_2_validation, ecodriving_rating_type,
          ecodriving_rating_type_share_percent,
          eco_driving_score_total, qualification_status, calculation_status,
          ranking_included, ranking_group, ranking_position, ranking_total_participants,
          updated_at
        ) VALUES (
          %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
          %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
          %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
          %s,%s,%s,%s,%s,NOW()
        )
        ON CONFLICT (client_id, assigned_id, month_start_date) DO UPDATE SET
          client_code = EXCLUDED.client_code,
          month_end_date = EXCLUDED.month_end_date,
          trips_count = EXCLUDED.trips_count,
          source_trips_count = EXCLUDED.source_trips_count,
          skipped_trips_count = EXCLUDED.skipped_trips_count,
          total_distance_meters = EXCLUDED.total_distance_meters,
          total_kilometers = EXCLUDED.total_kilometers,
          overrev_events_count = EXCLUDED.overrev_events_count,
          harsh_braking_events = EXCLUDED.harsh_braking_events,
          harsh_acceleration_events = EXCLUDED.harsh_acceleration_events,
          harsh_turning_events = EXCLUDED.harsh_turning_events,
          idle_events = EXCLUDED.idle_events,
          speeding_140_160_count = EXCLUDED.speeding_140_160_count,
          speeding_160_170_count = EXCLUDED.speeding_160_170_count,
          speeding_170_plus_count = EXCLUDED.speeding_170_plus_count,
          overrev_events_per_100km = EXCLUDED.overrev_events_per_100km,
          harsh_braking_events_per_100km = EXCLUDED.harsh_braking_events_per_100km,
          harsh_acceleration_events_per_100km = EXCLUDED.harsh_acceleration_events_per_100km,
          harsh_turning_events_per_100km = EXCLUDED.harsh_turning_events_per_100km,
          idle_events_per_100km = EXCLUDED.idle_events_per_100km,
          speeding_140_160_events_per_100km = EXCLUDED.speeding_140_160_events_per_100km,
          speeding_160_170_events_per_100km = EXCLUDED.speeding_160_170_events_per_100km,
          speeding_170_plus_events_per_100km = EXCLUDED.speeding_170_plus_events_per_100km,
          overrev_points = EXCLUDED.overrev_points,
          harsh_braking_points = EXCLUDED.harsh_braking_points,
          harsh_acceleration_points = EXCLUDED.harsh_acceleration_points,
          harsh_turning_points = EXCLUDED.harsh_turning_points,
          idle_points = EXCLUDED.idle_points,
          speeding_140_160_points = EXCLUDED.speeding_140_160_points,
          speeding_160_170_points = EXCLUDED.speeding_160_170_points,
          speeding_170_plus_points = EXCLUDED.speeding_170_plus_points,
          overrev_maxpoints_subtract = EXCLUDED.overrev_maxpoints_subtract,
          harsh_braking_maxpoints_subtract = EXCLUDED.harsh_braking_maxpoints_subtract,
          harsh_acceleration_maxpoints_subtract = EXCLUDED.harsh_acceleration_maxpoints_subtract,
          harsh_turning_maxpoints_subtract = EXCLUDED.harsh_turning_maxpoints_subtract,
          idle_maxpoints_subtract = EXCLUDED.idle_maxpoints_subtract,
          speeding_140_160_maxpoints_subtract = EXCLUDED.speeding_140_160_maxpoints_subtract,
          speeding_160_170_maxpoints_subtract = EXCLUDED.speeding_160_170_maxpoints_subtract,
          speeding_170_plus_maxpoints_subtract = EXCLUDED.speeding_170_plus_maxpoints_subtract,
          top_1_validation = EXCLUDED.top_1_validation,
          top_2_validation = EXCLUDED.top_2_validation,
          ecodriving_rating_type = EXCLUDED.ecodriving_rating_type,
          ecodriving_rating_type_share_percent = EXCLUDED.ecodriving_rating_type_share_percent,
          eco_driving_score_total = EXCLUDED.eco_driving_score_total,
          qualification_status = EXCLUDED.qualification_status,
          calculation_status = EXCLUDED.calculation_status,
          ranking_included = EXCLUDED.ranking_included,
          ranking_group = EXCLUDED.ranking_group,
          ranking_position = EXCLUDED.ranking_position,
          ranking_total_participants = EXCLUDED.ranking_total_participants,
          updated_at = NOW()
        """,
        [_monthly_row_values(row) for row in rows],
    )
    return len(rows)


def run(client, run_id: str, params: dict):
    if not isinstance(params, dict):
        raise ValueError("params must be a dict")

    client_id = params.get("client_id")
    if not client_id:
        raise ValueError("Missing required param: client_id")

    month_start, weekly_periods, explicit_period_mode, selected_mode = _resolve_periods(params)
    include_weekly, include_monthly = _include_flags(
        params,
        selected_mode=selected_mode,
        explicit_period_mode=explicit_period_mode,
    )
    if not include_weekly and not include_monthly:
        raise ValueError("At least one of include_weekly/include_monthly must be true")

    dry_run = _as_bool(params, "dry_run", False)
    recalculate = _as_bool(params, "recalculate", False)
    batch_size = _as_positive_int(params, "batch_size", 5000)
    max_batches = _as_optional_positive_int(params, "max_batches")
    assigned_id = _trimmed_or_none(params.get("assigned_id"))

    cfg = _load_client_account_config(client_id=str(client_id))
    schema = _safe_ident(cfg.client_db_schema)
    trips_table = f"{schema}.client_trips"
    assignments_table = f"{schema}.eco_trip_assignments"
    weekly_table = f"{schema}.eco_driver_weekly_stats"
    monthly_table = f"{schema}.eco_driver_monthly_stats"
    driver_chart_table = f"{schema}.eco_drivers_id_chart"

    process_start = min(period.start_ts for period in weekly_periods)
    process_end = max(period.end_ts for period in weekly_periods)
    if include_monthly and month_start is not None:
        process_start = _local_midnight(month_start)
        process_end = _local_midnight(_next_month_start(month_start))

    result = {
        "job_name": DATASET_NAME,
        "client_id": str(client_id),
        "selected_mode": selected_mode,
        "month": month_start.strftime("%Y-%m") if month_start else None,
        "period_start_date": weekly_periods[0].period_start_date.isoformat() if weekly_periods else None,
        "period_end_date": weekly_periods[-1].period_end_date.isoformat() if weekly_periods else None,
        "include_weekly": include_weekly,
        "include_monthly": include_monthly,
        "weekly_periods_processed": 0,
        "monthly_processed": False,
        "source_trips_seen": 0,
        "assignments_upserted": 0,
        "assigned_trips_count": 0,
        "skipped_no_id_count": 0,
        "private_excluded_count": 0,
        "aggregation_included_count": 0,
        "weekly_rows_upserted": 0,
        "monthly_rows_upserted": 0,
        "included_ranking_rows": 0,
        "excluded_ranking_rows": 0,
        "unknown_driver_rows": 0,
        "not_ranked_rows": 0,
        "driver_chart_rows": 0,
        "source_driver_rows": 0,
        "matched_driver_rows": 0,
        "unmatched_driver_rows": 0,
        "unmatched_driver_percentage": 0.0,
        "dry_run": dry_run,
        "recalculate": recalculate,
        "assigned_id": assigned_id,
        "batch_size": batch_size,
        "max_batches": max_batches,
        "business_timezone": get_business_timezone_name(),
    }

    client.log(
        "INFO",
        "SCRIPT",
        JOB_SOURCE,
        "Starting Eco Driving aggregation",
        run_id=run_id,
        context=result | {
            "process_start_local": process_start.isoformat(),
            "process_end_local": process_end.isoformat(),
        },
    )

    conn = _client_business_pg_conn(cfg)
    try:
        with conn.cursor(row_factory=_dict_row_factory()) as cur:
            chart_row_count = _fetch_driver_chart_count(
                cur,
                driver_chart_table=driver_chart_table,
                client_id=str(client_id),
            )
            result["driver_chart_rows"] = chart_row_count
            _validate_driver_chart_loaded(
                chart_row_count,
                period_start_date=weekly_periods[0].period_start_date,
                period_end_date=weekly_periods[-1].period_end_date,
            )

            assignment_counts = _upsert_assignments(
                cur,
                trips_table=trips_table,
                assignments_table=assignments_table,
                client_id=str(client_id),
                start_ts=process_start,
                end_ts=process_end,
                assigned_id=assigned_id,
                batch_size=batch_size,
                max_batches=max_batches,
            )
            result.update({key: assignment_counts[key] for key in assignment_counts if key in result})
            result["assignment_batches_processed"] = assignment_counts["assignment_batches_processed"]
            result["assignment_batch_limit_reached"] = assignment_counts["assignment_batch_limit_reached"]

            weekly_rows: list[dict] = []
            monthly_rows: list[dict] = []
            weekly_aggregate_batches: list[tuple[RankingPeriod, list[dict]]] = []
            monthly_aggregate_rows: list[dict] = []

            if include_weekly:
                for period in weekly_periods:
                    aggregate_rows = _fetch_aggregate_rows(
                        cur,
                        assignments_table=assignments_table,
                        driver_chart_table=driver_chart_table,
                        client_id=str(client_id),
                        start_ts=period.start_ts,
                        end_ts=period.end_ts,
                        assigned_id=assigned_id,
                        monthly=False,
                    )
                    diagnostics = _validate_source_chart_matches(
                        aggregate_rows,
                        chart_row_count=chart_row_count,
                        period_start_date=period.period_start_date,
                        period_end_date=period.period_end_date,
                        snapshot_type="weekly",
                    )
                    for key in (
                        "source_driver_rows",
                        "matched_driver_rows",
                        "unmatched_driver_rows",
                    ):
                        result[key] += int(diagnostics[key])
                    weekly_aggregate_batches.append((period, aggregate_rows))

            if include_monthly and month_start is not None:
                month_end = _next_month_start(month_start)
                monthly_aggregate_rows = _fetch_aggregate_rows(
                    cur,
                    assignments_table=assignments_table,
                    driver_chart_table=driver_chart_table,
                    client_id=str(client_id),
                    start_ts=_local_midnight(month_start),
                    end_ts=_local_midnight(month_end),
                    assigned_id=assigned_id,
                    monthly=True,
                    month_start=month_start,
                    month_end=month_end,
                )
                diagnostics = _validate_source_chart_matches(
                    monthly_aggregate_rows,
                    chart_row_count=chart_row_count,
                    period_start_date=month_start,
                    period_end_date=month_end,
                    snapshot_type="monthly",
                )
                for key in (
                    "source_driver_rows",
                    "matched_driver_rows",
                    "unmatched_driver_rows",
                ):
                    result[key] += int(diagnostics[key])

            if result["source_driver_rows"]:
                result["unmatched_driver_percentage"] = round(
                    result["unmatched_driver_rows"]
                    * 100.0
                    / result["source_driver_rows"],
                    2,
                )

            if recalculate and include_weekly:
                _delete_existing_stats(
                    cur,
                    table=weekly_table,
                    client_id=str(client_id),
                    assigned_id=assigned_id,
                    weekly_periods=weekly_periods if explicit_period_mode else None,
                    month_start=None if explicit_period_mode else month_start,
                    monthly=False,
                )
            if recalculate and include_monthly and month_start is not None:
                _delete_existing_stats(
                    cur,
                    table=monthly_table,
                    client_id=str(client_id),
                    assigned_id=assigned_id,
                    month_start=month_start,
                    monthly=True,
                )

            if include_weekly:
                for period, aggregate_rows in weekly_aggregate_batches:
                    weekly_rows.extend(
                        _stats_row_from_aggregate(row, period, monthly=False)
                        for row in aggregate_rows
                    )
                _apply_rankings(weekly_rows, monthly=False)
                _apply_rating_type_share_percent(weekly_rows, monthly=False)
                result["weekly_rows_upserted"] = _upsert_weekly_stats(
                    cur, weekly_table=weekly_table, rows=weekly_rows
                )
                result["weekly_periods_processed"] = len(weekly_periods)

            if include_monthly and month_start is not None:
                monthly_rows = [
                    _stats_row_from_aggregate(row, None, monthly=True)
                    for row in monthly_aggregate_rows
                ]
                _apply_rankings(monthly_rows, monthly=True)
                _apply_rating_type_share_percent(monthly_rows, monthly=True)
                result["monthly_rows_upserted"] = _upsert_monthly_stats(
                    cur, monthly_table=monthly_table, rows=monthly_rows
                )
                result["monthly_processed"] = True

            weekly_included, weekly_excluded, weekly_unknown, weekly_not_ranked = _count_rank_groups(weekly_rows)
            monthly_included, monthly_excluded, monthly_unknown, monthly_not_ranked = _count_rank_groups(monthly_rows)
            result["included_ranking_rows"] = weekly_included + monthly_included
            result["excluded_ranking_rows"] = weekly_excluded + monthly_excluded
            result["unknown_driver_rows"] = weekly_unknown + monthly_unknown
            result["not_ranked_rows"] = weekly_not_ranked + monthly_not_ranked

            if dry_run:
                conn.rollback()
            else:
                conn.commit()
    except EcoDrivingAggregationPreconditionError as exc:
        conn.rollback()
        client.log(
            "ERROR",
            "SCRIPT",
            JOB_SOURCE,
            exc.classification,
            run_id=run_id,
            context={
                "client_id": str(client_id),
                "assigned_id": assigned_id,
                "classification": exc.classification,
                **exc.diagnostics,
            },
        )
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    client.log(
        "INFO",
        "SCRIPT",
        JOB_SOURCE,
        "Eco Driving aggregation complete",
        run_id=run_id,
        context=result,
    )
    return result
