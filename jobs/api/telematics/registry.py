"""
Workflow A — central registry of datasets and tables.

This module is the **Python source of truth** for two things:

  1. The DATASETS catalog: which logical dataset maps to which job module
     and which set of tables it writes. Used by the dispatcher and by the
     retention worker.

  2. The TABLES catalog: per-table metadata used by the retention worker:
       - `dataset_name` (back-pointer)
       - `schema` (logical schema name in the client business DB; "public" today)
       - `business_key` (tuple of column names that uniquely identifies a row,
         informational; the actual upsert key is `record_id` once rolled out)
       - `retention_key_column` (the timestamp/date column the retention worker
         compares against `cutoff_ts`)

The TABLES catalog is also the **identifier allowlist** for the retention
worker: only schema/table/column names that appear here are allowed to be
substituted into dynamic SQL via `psycopg.sql.Identifier`. This is the
defense-in-depth against SQL injection of identifiers; values are always
parameterized via `%s`.

The platform-side migrations seed `workflow_a_control.dataset_registry` and
`workflow_a_control.table_registry` from the same constants below. Keep them in
sync — `ops/tests_manual/` includes a registry-sync sanity check.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple


@dataclass(frozen=True)
class DatasetSpec:
    name: str
    job_module: str
    description: str
    tables: Tuple[str, ...]


@dataclass(frozen=True)
class TableSpec:
    name: str
    dataset_name: str
    schema: str
    business_key: Tuple[str, ...]
    retention_key_column: str
    description: str


# ---------------------------------------------------------------------------
# DATASETS — logical units; scheduling & overwrite_existing live here.
# ---------------------------------------------------------------------------

DATASETS: Dict[str, DatasetSpec] = {
    "trips_sync": DatasetSpec(
        name="trips_sync",
        job_module="jobs.api.telematics.sync_trips_and_speeding",
        description=(
            "Telematics provider sync: trips + fleet-wide raw vehicle-event speeding and "
            "HIGH_RPM/OVERREV counts, plus odometer and location enrichment."
        ),
        tables=("client_trips", "client_speeding_notifications"),
    ),
    "fuel_daily_aggregation": DatasetSpec(
        name="fuel_daily_aggregation",
        job_module="jobs.api.telematics.aggregate_trip_fuel_daily",
        description=(
            "Daily aggregation of client_trips into per-vehicle and "
            "per-vehicle+driver daily fuel rollups."
        ),
        tables=("client_vehicle_daily_fuel", "client_vehicle_driver_daily_fuel"),
    ),
    "eco_driving_weekly_snapshot": DatasetSpec(
        name="eco_driving_weekly_snapshot",
        job_module="jobs.ecodriving.job_eco_driving_aggregate",
        description=(
            "Eco Driving cumulative month-to-date weekly snapshot aggregation."
        ),
        tables=("eco_trip_assignments", "eco_driver_weekly_stats"),
    ),
    "eco_driving_month_end_weekly_snapshot": DatasetSpec(
        name="eco_driving_month_end_weekly_snapshot",
        job_module="jobs.ecodriving.job_eco_driving_aggregate",
        description=(
            "Eco Driving final cumulative weekly snapshot for the previous month."
        ),
        tables=("eco_trip_assignments", "eco_driver_weekly_stats"),
    ),
    "eco_driving_monthly_aggregation": DatasetSpec(
        name="eco_driving_monthly_aggregation",
        job_module="jobs.ecodriving.job_eco_driving_aggregate",
        description=(
            "Eco Driving independent full calendar-month aggregation."
        ),
        tables=("eco_trip_assignments", "eco_driver_monthly_stats"),
    ),
    "eco_person_driving_weekly_snapshot": DatasetSpec(
        name="eco_person_driving_weekly_snapshot",
        job_module="jobs.ecodriving_person.job_eco_driving_person_aggregate",
        description=(
            "Eco Driving Person cumulative month-to-date weekly snapshot aggregation."
        ),
        tables=("eco_person_trip_assignments", "eco_person_weekly_stats"),
    ),
    "eco_person_driving_month_end_weekly_snapshot": DatasetSpec(
        name="eco_person_driving_month_end_weekly_snapshot",
        job_module="jobs.ecodriving_person.job_eco_driving_person_aggregate",
        description=(
            "Eco Driving Person final cumulative weekly snapshot for the previous month."
        ),
        tables=("eco_person_trip_assignments", "eco_person_weekly_stats"),
    ),
    "eco_person_driving_monthly_aggregation": DatasetSpec(
        name="eco_person_driving_monthly_aggregation",
        job_module="jobs.ecodriving_person.job_eco_driving_person_aggregate",
        description=(
            "Eco Driving Person independent full calendar-month aggregation."
        ),
        tables=("eco_person_trip_assignments", "eco_person_monthly_stats"),
    ),
    "eco_person_driving_weekly_email_notifications": DatasetSpec(
        name="eco_person_driving_weekly_email_notifications",
        job_module="jobs.ecodriving_person.job_eco_driving_person_weekly_email_notifications",
        description="Eco Driving Person weekly real-person email notifications.",
        tables=("eco_person_weekly_email_send_log",),
    ),
    "eco_person_driving_monthly_email_notifications": DatasetSpec(
        name="eco_person_driving_monthly_email_notifications",
        job_module="jobs.ecodriving_person.job_eco_driving_person_monthly_email_notifications",
        description="Eco Driving Person monthly real-person email notifications.",
        tables=("eco_person_monthly_email_send_log",),
    ),
}


# ---------------------------------------------------------------------------
# TABLES — per-table metadata used by retention; allowlist for sql.Identifier.
# ---------------------------------------------------------------------------

TABLES: Dict[str, TableSpec] = {
    "client_trips": TableSpec(
        name="client_trips",
        dataset_name="trips_sync",
        schema="public",
        business_key=("client_id", "provider_trip_id"),
        retention_key_column="start_timestamp",
        description="Provider trip facts + per-trip speeding bucket counts.",
    ),
    "client_speeding_notifications": TableSpec(
        name="client_speeding_notifications",
        dataset_name="trips_sync",
        schema="public",
        business_key=("client_id", "provider_notification_id"),
        retention_key_column="event_ts",
        description="Legacy provider notification events retained for historical data.",
    ),
    "client_vehicle_daily_fuel": TableSpec(
        name="client_vehicle_daily_fuel",
        dataset_name="fuel_daily_aggregation",
        schema="public",
        business_key=("client_id", "vehicle_id", "day"),
        retention_key_column="day",
        description="Per-vehicle, per-day fuel and distance aggregates.",
    ),
    "client_vehicle_driver_daily_fuel": TableSpec(
        name="client_vehicle_driver_daily_fuel",
        dataset_name="fuel_daily_aggregation",
        schema="public",
        business_key=("client_id", "vehicle_id", "driver_id", "day"),
        retention_key_column="day",
        description="Per-vehicle+driver, per-day fuel and distance aggregates.",
    ),
    "eco_trip_assignments": TableSpec(
        name="eco_trip_assignments",
        dataset_name="eco_driving_weekly_snapshot",
        schema="public",
        business_key=("client_id", "provider_trip_id"),
        retention_key_column="trip_start_ts",
        description="Eco Driving source trip assignment audit rows.",
    ),
    "eco_driver_weekly_stats": TableSpec(
        name="eco_driver_weekly_stats",
        dataset_name="eco_driving_weekly_snapshot",
        schema="public",
        business_key=("client_id", "assigned_id", "period_start_date", "period_end_date"),
        retention_key_column="period_start_date",
        description="Eco Driving cumulative month-to-date weekly snapshot stats.",
    ),
    "eco_driver_monthly_stats": TableSpec(
        name="eco_driver_monthly_stats",
        dataset_name="eco_driving_monthly_aggregation",
        schema="public",
        business_key=("client_id", "assigned_id", "month_start_date"),
        retention_key_column="month_start_date",
        description="Eco Driving independent full calendar-month stats.",
    ),
    "eco_person_people": TableSpec(
        name="eco_person_people",
        dataset_name="eco_person_driving_weekly_snapshot",
        schema="public",
        business_key=("client_id", "person_id_match_key"),
        retention_key_column="updated_at",
        description="Eco Driving Person real-person identity rows.",
    ),
    "eco_person_driver_mappings": TableSpec(
        name="eco_person_driver_mappings",
        dataset_name="eco_person_driving_weekly_snapshot",
        schema="public",
        business_key=("client_id", "person_id", "normalized_driver_name"),
        retention_key_column="updated_at",
        description="Eco Driving Person driver-name alias mappings.",
    ),
    "eco_person_trip_assignments": TableSpec(
        name="eco_person_trip_assignments",
        dataset_name="eco_person_driving_weekly_snapshot",
        schema="public",
        business_key=("client_id", "provider_trip_id"),
        retention_key_column="trip_start_ts",
        description="Eco Driving Person source trip assignment audit rows.",
    ),
    "eco_person_weekly_stats": TableSpec(
        name="eco_person_weekly_stats",
        dataset_name="eco_person_driving_weekly_snapshot",
        schema="public",
        business_key=("client_id", "person_name_group_key", "period_start_date", "period_end_date"),
        retention_key_column="period_start_date",
        description="Eco Driving Person cumulative month-to-date weekly snapshot stats.",
    ),
    "eco_person_monthly_stats": TableSpec(
        name="eco_person_monthly_stats",
        dataset_name="eco_person_driving_monthly_aggregation",
        schema="public",
        business_key=("client_id", "person_name_group_key", "month_start_date"),
        retention_key_column="month_start_date",
        description="Eco Driving Person independent full calendar-month stats.",
    ),
    "eco_person_weekly_email_send_log": TableSpec(
        name="eco_person_weekly_email_send_log",
        dataset_name="eco_person_driving_weekly_email_notifications",
        schema="public",
        business_key=("client_id", "person_name_group_key", "period_start_date", "period_end_date", "template_type"),
        retention_key_column="attempted_at",
        description="Eco Driving Person weekly email send/audit log.",
    ),
    "eco_person_monthly_email_send_log": TableSpec(
        name="eco_person_monthly_email_send_log",
        dataset_name="eco_person_driving_monthly_email_notifications",
        schema="public",
        business_key=("client_id", "person_name_group_key", "period_start_date", "period_end_date", "template_type"),
        retention_key_column="attempted_at",
        description="Eco Driving Person monthly email send/audit log.",
    ),
}


# ---------------------------------------------------------------------------
# Allowlist accessors (used by retention_purge.py)
# ---------------------------------------------------------------------------

def get_dataset(name: str) -> DatasetSpec:
    spec = DATASETS.get(name)
    if spec is None:
        raise KeyError(f"Unknown dataset: {name!r}. Known: {sorted(DATASETS)}")
    return spec


def get_table(name: str) -> TableSpec:
    spec = TABLES.get(name)
    if spec is None:
        raise KeyError(f"Unknown table: {name!r}. Known: {sorted(TABLES)}")
    return spec


def is_known_table(name: str) -> bool:
    return name in TABLES


def is_known_retention_column(table_name: str, column_name: str) -> bool:
    """Return True only if `column_name` is the registered retention key for `table_name`.

    Retention SQL is restricted to comparing `cutoff_ts` against this exact column.
    """
    spec = TABLES.get(table_name)
    if spec is None:
        return False
    return spec.retention_key_column == column_name
