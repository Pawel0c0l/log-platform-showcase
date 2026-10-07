"""Driver Eco Dashboard V1 — per-pipeline-family source adapters.

Two Eco Driving pipelines populate the same downstream contract:

  * `eco_driver_*`  (ALPHA00001) — identity `assigned_id`,
    **private trips are excluded** from the Eco aggregate;
  * `eco_person_*`  (BRAVO00016) — identity `person_name_group_key`,
    **all qualifying trips assigned to the person are included**, private
    designation included.

That difference is intentional and is preserved verbatim here. The adapter
never re-derives identity, eligibility or scoring; it reads what the existing
aggregation job already produced (`eco_*_{weekly,monthly}_stats`) and the
per-trip layer it produced them from (`eco_*_trip_assignments`).

Every query in this module is read-only and deliberately projects away roster
columns: no driver name, e-mail, phone or vehicle data is ever selected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Mapping, Sequence

from jobs.ecodriving.eco_scoring import REQUIRED_METRICS
from jobs.ecodriving_dashboard.snapshot_builder import (
    DailyInput,
    PeriodIdentity,
    PeriodInput,
    RankingFacts,
    SeriesInput,
)
from jobs.ecodriving_dashboard.snapshot_contract import PERIOD_TYPE_MONTHLY, PERIOD_TYPE_WEEKLY


class PipelineFamilyError(RuntimeError):
    """Raised when a client cannot be mapped to a known Eco pipeline family."""


@dataclass(frozen=True)
class PipelineFamily:
    name: str
    identity_column: str
    assignments_table: str
    weekly_stats_table: str
    monthly_stats_table: str
    include_private_trips: bool


DRIVER_FAMILY = PipelineFamily(
    name="eco_driver",
    identity_column="assigned_id",
    assignments_table="eco_trip_assignments",
    weekly_stats_table="eco_driver_weekly_stats",
    monthly_stats_table="eco_driver_monthly_stats",
    include_private_trips=False,
)

PERSON_FAMILY = PipelineFamily(
    name="eco_person",
    identity_column="person_name_group_key",
    assignments_table="eco_person_trip_assignments",
    weekly_stats_table="eco_person_weekly_stats",
    monthly_stats_table="eco_person_monthly_stats",
    include_private_trips=True,
)

# Fail closed: a client without an explicitly declared family is not published.
PIPELINE_FAMILY_BY_CLIENT_CODE: dict[str, PipelineFamily] = {
    "ALPHA00001": DRIVER_FAMILY,
    "BRAVO00016": PERSON_FAMILY,
}

PIPELINE_FAMILY_BY_NAME: dict[str, PipelineFamily] = {
    DRIVER_FAMILY.name: DRIVER_FAMILY,
    PERSON_FAMILY.name: PERSON_FAMILY,
}


def resolve_pipeline_family(client_code: str | None) -> PipelineFamily:
    family = PIPELINE_FAMILY_BY_CLIENT_CODE.get(str(client_code or "").strip().upper())
    if family is None:
        raise PipelineFamilyError(
            f"No Eco Driving dashboard pipeline family declared for client_code={client_code!r}"
        )
    return family


def is_trip_included(family: PipelineFamily, *, aggregation_included: bool, is_private_trip: bool) -> bool:
    """Python mirror of `trip_inclusion_predicate_sql`.

    Kept in one place so the intentional per-client private-trip difference is
    stated exactly once and is directly testable without a database.
    """

    if not aggregation_included:
        return False
    if family.include_private_trips:
        return True
    return not is_private_trip


def trip_inclusion_predicate_sql(family: PipelineFamily, alias: str = "a") -> str:
    if family.include_private_trips:
        return f"{alias}.aggregation_included IS TRUE"
    return f"{alias}.aggregation_included IS TRUE AND {alias}.is_private_trip IS FALSE"


def _safe_ident(name: str) -> str:
    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", name or ""):
        raise ValueError(f"Unsafe SQL identifier: {name!r}")
    return name


def stats_table(family: PipelineFamily, period_type: str) -> str:
    if period_type == PERIOD_TYPE_WEEKLY:
        return family.weekly_stats_table
    if period_type == PERIOD_TYPE_MONTHLY:
        return family.monthly_stats_table
    raise ValueError(f"Unknown period_type: {period_type!r}")


def _period_columns(period_type: str) -> tuple[str, str]:
    if period_type == PERIOD_TYPE_WEEKLY:
        return "period_start_date", "period_end_date"
    return "month_start_date", "month_end_date"


# --- SQL builders --------------------------------------------------------------


def period_aggregate_sql(family: PipelineFamily, schema: str) -> str:
    """Per-driver period totals recomputed from the per-trip layer."""

    schema = _safe_ident(schema)
    table = f"{schema}.{_safe_ident(family.assignments_table)}"
    identity = _safe_ident(family.identity_column)
    predicate = trip_inclusion_predicate_sql(family)
    sums = ",\n            ".join(
        f"COALESCE(sum(a.{_safe_ident(metric)}) FILTER (WHERE {predicate}), 0)::bigint AS {_safe_ident(metric)}"
        for metric in REQUIRED_METRICS
    )
    return f"""
        SELECT
            count(*) FILTER (WHERE {predicate})::int AS trips_count,
            COALESCE(sum(COALESCE(a.trip_distance_meters, 0)) FILTER (WHERE {predicate}), 0)::bigint
                AS total_distance_meters,
            {sums}
        FROM {table} a
        WHERE a.client_id = %(client_id)s
          AND a.{identity} = %(identity_key)s
          AND a.trip_start_ts >= %(start_ts)s
          AND a.trip_start_ts < %(end_ts)s
    """


def daily_aggregate_sql(family: PipelineFamily, schema: str) -> str:
    """Per-day totals. A trip belongs entirely to its local start date."""

    schema = _safe_ident(schema)
    table = f"{schema}.{_safe_ident(family.assignments_table)}"
    identity = _safe_ident(family.identity_column)
    predicate = trip_inclusion_predicate_sql(family)
    sums = ",\n            ".join(
        f"COALESCE(sum(a.{_safe_ident(metric)}) FILTER (WHERE {predicate}), 0)::bigint AS {_safe_ident(metric)}"
        for metric in REQUIRED_METRICS
    )
    return f"""
        SELECT
            (a.trip_start_ts AT TIME ZONE %(business_timezone)s)::date AS local_date,
            count(*) FILTER (WHERE {predicate})::int AS trips_count,
            COALESCE(sum(COALESCE(a.trip_distance_meters, 0)) FILTER (WHERE {predicate}), 0)::bigint
                AS total_distance_meters,
            {sums}
        FROM {table} a
        WHERE a.client_id = %(client_id)s
          AND a.{identity} = %(identity_key)s
          AND a.trip_start_ts >= %(start_ts)s
          AND a.trip_start_ts < %(end_ts)s
        GROUP BY 1
        ORDER BY 1
    """


def stats_row_sql(family: PipelineFamily, schema: str, period_type: str) -> str:
    """Ranking facts and freshness for one driver and one closed period.

    Only ranking/qualification/freshness columns are selected. No roster join,
    so no name or e-mail can leak into the snapshot pipeline.
    """

    schema = _safe_ident(schema)
    table = f"{schema}.{_safe_ident(stats_table(family, period_type))}"
    identity = _safe_ident(family.identity_column)
    start_column, end_column = _period_columns(period_type)
    return f"""
        SELECT
            s.{start_column} AS period_start_date,
            s.{end_column} AS period_end_date,
            s.qualification_status,
            s.calculation_status,
            s.ranking_group,
            s.ranking_position,
            s.ranking_total_participants,
            s.ecodriving_rating_type,
            s.ecodriving_rating_type_share_percent,
            s.eco_driving_score_total,
            s.total_distance_meters,
            s.trips_count,
            s.updated_at
        FROM {table} s
        WHERE s.client_id = %(client_id)s
          AND s.{identity} = %(identity_key)s
          AND s.{start_column} = %(period_start_date)s
          AND s.{end_column} = %(period_end_date)s
    """


def rating_group_distribution_sql(family: PipelineFamily, schema: str, period_type: str) -> str:
    """Period-level rating split of the ranked (`INCLUDED`) population.

    A population statistic only. No individual score, rank or identity of any
    other driver is selected.
    """

    schema = _safe_ident(schema)
    table = f"{schema}.{_safe_ident(stats_table(family, period_type))}"
    start_column, end_column = _period_columns(period_type)
    return f"""
        SELECT
            s.ecodriving_rating_type,
            count(*)::int AS rating_type_rows
        FROM {table} s
        WHERE s.client_id = %(client_id)s
          AND s.{start_column} = %(period_start_date)s
          AND s.{end_column} = %(period_end_date)s
          AND s.ranking_included IS TRUE
          AND s.qualification_status = 'QUALIFIED'
          AND s.ecodriving_rating_type IS NOT NULL
        GROUP BY 1
    """


def weekly_series_sql(family: PipelineFamily, schema: str) -> str:
    """Every closed cumulative period of one month, for the trend series."""

    schema = _safe_ident(schema)
    table = f"{schema}.{_safe_ident(family.weekly_stats_table)}"
    identity = _safe_ident(family.identity_column)
    return f"""
        SELECT
            s.period_label,
            s.period_start_date,
            s.period_end_date,
            s.eco_driving_score_total,
            s.total_distance_meters
        FROM {table} s
        WHERE s.client_id = %(client_id)s
          AND s.{identity} = %(identity_key)s
          AND s.month_start_date = %(month_start_date)s
          AND s.period_end_date <= %(period_end_date)s
        ORDER BY s.period_end_date
    """


# --- fetch helpers -------------------------------------------------------------


def fetch_period_input(
    cur,
    *,
    family: PipelineFamily,
    schema: str,
    client_id: str,
    identity_key: str,
    identity: PeriodIdentity,
    start_ts: datetime,
    end_ts: datetime,
    ranking: RankingFacts,
    snapshot_updated_at_utc: datetime,
    persisted_eco_score_total: Decimal | None = None,
) -> PeriodInput:
    cur.execute(
        period_aggregate_sql(family, schema),
        {
            "client_id": client_id,
            "identity_key": identity_key,
            "start_ts": start_ts,
            "end_ts": end_ts,
        },
    )
    row = dict(cur.fetchone() or {})
    return PeriodInput(
        identity=identity,
        total_distance_meters=int(row.get("total_distance_meters") or 0),
        trips_count=int(row.get("trips_count") or 0),
        counts={metric: int(row.get(metric) or 0) for metric in REQUIRED_METRICS},
        snapshot_updated_at_utc=snapshot_updated_at_utc,
        ranking=ranking,
        persisted_eco_score_total=persisted_eco_score_total,
    )


def fetch_daily_inputs(
    cur,
    *,
    family: PipelineFamily,
    schema: str,
    client_id: str,
    identity_key: str,
    start_ts: datetime,
    end_ts: datetime,
    business_timezone: str,
) -> list[DailyInput]:
    cur.execute(
        daily_aggregate_sql(family, schema),
        {
            "client_id": client_id,
            "identity_key": identity_key,
            "start_ts": start_ts,
            "end_ts": end_ts,
            "business_timezone": business_timezone,
        },
    )
    daily: list[DailyInput] = []
    for row in cur.fetchall():
        row = dict(row)
        daily.append(
            DailyInput(
                day=row["local_date"],
                total_distance_meters=int(row.get("total_distance_meters") or 0),
                trips_count=int(row.get("trips_count") or 0),
                counts={metric: int(row.get(metric) or 0) for metric in REQUIRED_METRICS},
            )
        )
    return daily


def fetch_stats_row(
    cur,
    *,
    family: PipelineFamily,
    schema: str,
    period_type: str,
    client_id: str,
    identity_key: str,
    period_start_date: date,
    period_end_date: date,
) -> dict | None:
    cur.execute(
        stats_row_sql(family, schema, period_type),
        {
            "client_id": client_id,
            "identity_key": identity_key,
            "period_start_date": period_start_date,
            "period_end_date": period_end_date,
        },
    )
    row = cur.fetchone()
    return dict(row) if row else None


def fetch_rating_group_distribution(
    cur,
    *,
    family: PipelineFamily,
    schema: str,
    period_type: str,
    client_id: str,
    period_start_date: date,
    period_end_date: date,
) -> dict[str, Decimal] | None:
    from jobs.ecodriving_dashboard.snapshot_contract import RATING_TYPE_BY_STORED_LABEL

    cur.execute(
        rating_group_distribution_sql(family, schema, period_type),
        {
            "client_id": client_id,
            "period_start_date": period_start_date,
            "period_end_date": period_end_date,
        },
    )
    rows = [dict(row) for row in cur.fetchall()]
    total = sum(int(row["rating_type_rows"]) for row in rows)
    if total <= 0:
        return None
    distribution: dict[str, Decimal] = {}
    for row in rows:
        key = RATING_TYPE_BY_STORED_LABEL.get(row["ecodriving_rating_type"])
        if key is None:
            continue
        distribution[key] = (Decimal(int(row["rating_type_rows"])) * Decimal("100") / Decimal(total)).quantize(
            Decimal("0.01")
        )
    return distribution or None


def ranking_facts_from_stats_row(
    row: Mapping[str, Any] | None,
    *,
    rating_group_distribution: Mapping[str, Decimal] | None = None,
) -> RankingFacts:
    if row is None:
        return RankingFacts()
    return RankingFacts(
        ranking_group=row.get("ranking_group"),
        ranking_position=row.get("ranking_position"),
        ranking_total_participants=row.get("ranking_total_participants"),
        rating_group_share_percent=row.get("ecodriving_rating_type_share_percent"),
        rating_group_distribution=rating_group_distribution,
    )


def series_inputs_from_rows(rows: Sequence[Mapping[str, Any]]) -> list[SeriesInput]:
    series: list[SeriesInput] = []
    for row in rows:
        series.append(
            SeriesInput(
                period_label=row["period_label"],
                period_start_date=row["period_start_date"],
                period_end_date_exclusive=row["period_end_date"],
                eco_score_total=row.get("eco_driving_score_total"),
                total_distance_meters=int(row.get("total_distance_meters") or 0),
            )
        )
    return series
