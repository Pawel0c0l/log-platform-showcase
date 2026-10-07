from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time
from typing import Optional

from psycopg.rows import dict_row

from api.timezone_utils import DEFAULT_BUSINESS_TIMEZONE, set_pg_session_timezone
from jobs.trip_metrics_population_source import (
    TRIP_METRICS_POPULATION_SOURCE_DEFAULT,
    normalize_trip_metrics_population_source,
)
from jobs.api.telematics.schedule_mutation_surfaces import SCHEDULE_RUN_TYPE_BASE
from jobs.trips_pagination_mode import normalize_trips_pagination_mode
from jobs.trips_stabilization_config import validate_trips_stabilization_config


def _require_dependency(module: str, feature: str):
    try:
        return __import__(module)
    except ImportError as exc:
        raise RuntimeError(f"Missing dependency for {feature}: {module}") from exc


def _platform_pg_conn():
    psycopg = _require_dependency("psycopg", "Postgres connection")
    dsn = (
        f"host={os.getenv('POSTGRES_HOST', '127.0.0.1')} "
        f"port={os.getenv('POSTGRES_PORT', '5432')} "
        f"dbname={os.getenv('POSTGRES_DB', 'logdb')} "
        f"user={os.getenv('POSTGRES_USER', 'loguser')} "
        f"password={os.getenv('POSTGRES_PASSWORD', '')}"
    )
    return set_pg_session_timezone(psycopg.connect(dsn))


@dataclass(frozen=True)
class ClientAccountConfig:
    client_id: str
    client_code: Optional[str]
    client_name: Optional[str]

    provider_base_url: str
    provider_basic_auth_username: str
    provider_basic_auth_password_secret_ref: str

    client_db_host: str
    client_db_port: int
    client_db_name: str
    client_db_user: str
    client_db_password_secret_ref: str
    client_db_schema: str

    speed_trigger_filter_text: str
    trip_metrics_population_source: str
    trips_pagination_mode: str
    trips_stabilization_delay_seconds: int
    trips_overlap_seconds: int
    trips_max_recovery_span_seconds: int


def load_client_account_config(*, client_id: str) -> ClientAccountConfig:
    """
    Load Workflow A config for a single `client_id` from platform control-plane tables.
    """
    conn = _platform_pg_conn()
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT
                  client_id::text,
                  client_code,
                  client_name,
                  provider_base_url,
                  provider_basic_auth_username,
                  provider_basic_auth_password_secret_ref,
                  client_db_host,
                  client_db_port,
                  client_db_name,
                  client_db_user,
                  client_db_password_secret_ref,
                  client_db_schema,
                  speed_trigger_filter_text,
                  trip_metrics_population_source,
                  trips_pagination_mode,
                  trips_stabilization_delay_seconds,
                  trips_overlap_seconds,
                  trips_max_recovery_span_seconds
                FROM workflow_a_control.client_account
                WHERE client_id=%s
                  AND enabled=true
                LIMIT 1
                """,
                (client_id,),
            )
            row = cur.fetchone()
            if not row:
                raise RuntimeError(f"client_account not found/enabled for client_id={client_id}")

            stabilization = validate_trips_stabilization_config(
                stabilization_delay_seconds=row[
                    "trips_stabilization_delay_seconds"
                ],
                overlap_seconds=row["trips_overlap_seconds"],
                max_recovery_span_seconds=row[
                    "trips_max_recovery_span_seconds"
                ],
            )

            return ClientAccountConfig(
                client_id=row["client_id"],
                client_code=row["client_code"],
                client_name=row["client_name"],
                provider_base_url=row["provider_base_url"],
                provider_basic_auth_username=row["provider_basic_auth_username"],
                provider_basic_auth_password_secret_ref=row["provider_basic_auth_password_secret_ref"],
                client_db_host=row["client_db_host"],
                client_db_port=int(row["client_db_port"]),
                client_db_name=row["client_db_name"],
                client_db_user=row["client_db_user"],
                client_db_password_secret_ref=row["client_db_password_secret_ref"],
                client_db_schema=row["client_db_schema"] or "public",
                speed_trigger_filter_text=row["speed_trigger_filter_text"],
                trip_metrics_population_source=normalize_trip_metrics_population_source(
                    row.get("trip_metrics_population_source")
                    or TRIP_METRICS_POPULATION_SOURCE_DEFAULT
                ),
                trips_pagination_mode=normalize_trips_pagination_mode(
                    row.get("trips_pagination_mode")
                ),
                trips_stabilization_delay_seconds=stabilization[0],
                trips_overlap_seconds=stabilization[1],
                trips_max_recovery_span_seconds=stabilization[2],
            )
    finally:
        conn.close()


@dataclass(frozen=True)
class DatasetSchedule:
    """Effective schedule + sync behavior for one (client, dataset).

    Returned by `load_dataset_schedule`. When a row does not exist (for
    pre-config-era clients) the loader returns `_default_schedule()` whose
    `exists` is False — callers should treat that as legacy back-compat
    behavior (run the job, overwrite_existing=True, log a one-shot warning).
    """

    exists: bool
    client_id: str
    dataset_name: str
    enabled: bool
    frequency: str           # 'daily' | 'weekly' | 'monthly'
    day_of_week: Optional[int]
    day_of_month: Optional[int]
    run_time: time
    timezone: str
    lookback_days: int
    overwrite_existing: bool
    event_enrichment_mode: str
    # Canonical schedule identity, added additively so existing keyword
    # constructions keep working. `None` for the legacy default returned when no
    # row exists — a schedule with no identity can never satisfy the
    # manual-recovery authority, which is the intended fail-closed behavior.
    schedule_id: Optional[str] = None


def _default_schedule(*, client_id: str, dataset_name: str) -> DatasetSchedule:
    """Back-compat default used when no client_dataset_schedule row exists.

    Mirrors pre-config behavior: enabled, daily, overwrite_existing=True.
    `lookback_days=1` is a placeholder; the runner currently passes its own
    window so the loader's lookback is informational in v1.
    """
    return DatasetSchedule(
        exists=False,
        client_id=client_id,
        dataset_name=dataset_name,
        enabled=True,
        frequency="daily",
        day_of_week=None,
        day_of_month=None,
        run_time=time(2, 0),
        timezone=DEFAULT_BUSINESS_TIMEZONE,
        lookback_days=1,
        overwrite_existing=True,
        event_enrichment_mode="enabled",
    )


def load_dataset_schedule(*, client_id: str, dataset_name: str) -> DatasetSchedule:
    """
    Load `(client_id, dataset_name)` row from
    `workflow_a_control.client_dataset_schedule`.

    Returns a `DatasetSchedule` with `exists=True` if a row was found, or a
    permissive default (`exists=False`) for clients that haven't been seeded
    yet. Callers MUST honor `enabled` and `overwrite_existing`.

    **Scoped to the BASE schedule since M5.** The `LIMIT 1` here predates the
    role discriminator and was unambiguous only because `(client_id,
    dataset_name)` could match one row. Once a reconciliation cadence exists it
    would return an arbitrary row — a `LIMIT 1` with no `ORDER BY` — and this is
    the child job's view of its own sync configuration, so an arbitrary answer
    could silently apply the wrong lookback. The base row is the configuration
    that owns forward coverage, which is what this loader has always meant.
    """
    conn = _platform_pg_conn()
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT
                  schedule_id::text AS schedule_id,
                  client_id::text AS client_id,
                  dataset_name,
                  enabled,
                  frequency,
                  day_of_week,
                  day_of_month,
                  run_time,
                  timezone,
                  lookback_days,
                  overwrite_existing,
                  event_enrichment_mode
                FROM workflow_a_control.client_dataset_schedule
                WHERE client_id=%s AND dataset_name=%s
                  AND run_type=%s
                LIMIT 1
                """,
                (client_id, dataset_name, SCHEDULE_RUN_TYPE_BASE),
            )
            row = cur.fetchone()
            if not row:
                return _default_schedule(client_id=client_id, dataset_name=dataset_name)

            return DatasetSchedule(
                exists=True,
                schedule_id=row["schedule_id"],
                client_id=row["client_id"],
                dataset_name=row["dataset_name"],
                enabled=bool(row["enabled"]),
                frequency=row["frequency"],
                day_of_week=row["day_of_week"],
                day_of_month=row["day_of_month"],
                run_time=row["run_time"],
                timezone=row["timezone"] or DEFAULT_BUSINESS_TIMEZONE,
                lookback_days=int(row["lookback_days"]),
                overwrite_existing=bool(row["overwrite_existing"]),
                event_enrichment_mode=(row["event_enrichment_mode"] or "enabled"),
            )
    finally:
        conn.close()


def load_manual_recovery_claim(recovery_run_id: str) -> Optional[dict]:
    """Read one `client_dataset_recovery_run` row plus its schedule state.

    Used only by the manual-recovery authority check in
    `jobs.api.telematics.manual_recovery_authority`. It is read-only, joins the
    schedule so `enabled` is observed in the *same* snapshot as the recovery row
    — a two-query version could see a schedule that was flipped in between — and
    returns `None` rather than raising when the row is absent, because absence is
    a refusal the caller classifies, not an error.

    Returns `None` when the recovery table does not exist at all, so a platform
    database without migration 058 fails closed instead of crashing.
    """
    conn = _platform_pg_conn()
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT to_regclass("
                "'workflow_a_control.client_dataset_recovery_run')::text AS t"
            )
            if not (cur.fetchone() or {}).get("t"):
                return None
            cur.execute(
                """
                SELECT
                  r.recovery_run_id::text AS recovery_run_id,
                  r.client_id::text       AS client_id,
                  r.client_code,
                  r.schedule_id::text     AS schedule_id,
                  r.dataset_name,
                  r.status,
                  r.window_start_ts,
                  r.window_end_ts,
                  r.approval_ref,
                  s.enabled               AS schedule_enabled
                FROM workflow_a_control.client_dataset_recovery_run AS r
                LEFT JOIN workflow_a_control.client_dataset_schedule AS s
                       ON s.schedule_id = r.schedule_id
                WHERE r.recovery_run_id = %s
                """,
                (str(recovery_run_id),),
            )
            row = cur.fetchone()
            return None if not row else dict(row)
    finally:
        conn.close()
