"""
Workflow A — Daily trip fuel aggregation job.

Reads from client_trips and computes daily aggregations into:
  - client_vehicle_daily_fuel        (per vehicle per day)
  - client_vehicle_driver_daily_fuel (per vehicle+driver per day)

Idempotent upsert: safe to re-run for overlapping windows.
"""
from __future__ import annotations

import re
import time as time_mod
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from api.timezone_utils import set_pg_session_timezone
from jobs.api.telematics import record_id as record_id_mod
from jobs.api.telematics.control_plane import (
    ClientAccountConfig,
    load_client_account_config,
    load_dataset_schedule,
)
from jobs.api.telematics.provider_client import TelematicsFleetProviderClient, provider_page_limit_from_env
from jobs.api.telematics.provider_safety import ProviderRunBudget, SafetyLimits
from jobs.api.telematics.secret_resolver import resolve_secret


JOB_SOURCE = "jobs.api.telematics.aggregate_trip_fuel_daily"
DATASET_NAME = "fuel_daily_aggregation"
LOCAL_TZ = ZoneInfo("Europe/Warsaw")


def _require_dependency(module: str, feature: str):
    try:
        return __import__(module)
    except ImportError as exc:
        raise RuntimeError(f"Missing dependency for {feature}: {module}") from exc


def _parse_runner_iso_ts(ts: str) -> datetime:
    raw = ts.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _safe_ident(name: str) -> str:
    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", name or ""):
        raise ValueError(f"Unsafe SQL identifier: {name!r}")
    return name


def _client_business_pg_conn(cfg: ClientAccountConfig):
    psycopg = _require_dependency("psycopg", "Postgres connection")
    dsn = (
        f"host={cfg.client_db_host} "
        f"port={cfg.client_db_port} "
        f"dbname={cfg.client_db_name} "
        f"user={cfg.client_db_user} "
        f"password={resolve_secret(cfg.client_db_password_secret_ref)}"
    )
    return set_pg_session_timezone(psycopg.connect(dsn))


def _local_midnight(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=LOCAL_TZ)


def _first_full_local_day(start_ts: datetime) -> date:
    local_start = start_ts.astimezone(LOCAL_TZ)
    if local_start.timetz().replace(tzinfo=None) == time.min:
        return local_start.date()
    return local_start.date() + timedelta(days=1)


def _exclusive_full_local_day_end(end_ts: datetime, *, now_utc: datetime) -> date:
    local_end = end_ts.astimezone(LOCAL_TZ)
    exclusive_end = local_end.date()
    if local_end.timetz().replace(tzinfo=None) == time.min:
        exclusive_end = local_end.date()

    current_local_day = now_utc.astimezone(LOCAL_TZ).date()
    if exclusive_end > current_local_day:
        exclusive_end = current_local_day
    return exclusive_end


def _completed_local_day_windows(
    *, window_start_ts: datetime, window_end_ts: datetime, now_utc: datetime,
) -> List[Tuple[str, datetime, datetime]]:
    first_day = _first_full_local_day(window_start_ts)
    end_day_exclusive = _exclusive_full_local_day_end(window_end_ts, now_utc=now_utc)

    out: List[Tuple[str, datetime, datetime]] = []
    current = first_day
    while current < end_day_exclusive:
        day_start = _local_midnight(current)
        next_day = current + timedelta(days=1)
        day_end = _local_midnight(next_day)
        out.append((current.isoformat(), day_start, day_end))
        current = next_day
    return out


def _registration_key(value: Any) -> str:
    return str(value or "").strip().upper()


def _optional_float(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _fuel_used_liters_from_level(level: Dict[str, Any]) -> Tuple[Optional[float], Optional[str]]:
    start_liters = _optional_float(level.get("start_liters"))
    end_liters = _optional_float(level.get("end_liters"))

    if start_liters is None or end_liters is None:
        return None, "missing_start_or_end_liters"
    if level.get("calibrated") is False:
        return None, "not_calibrated"
    if level.get("start_accurate") is False or level.get("end_accurate") is False:
        return None, "inaccurate_start_or_end"

    fuel_used = start_liters - end_liters
    if fuel_used < 0:
        return None, "negative_fuel_delta"
    return fuel_used, None


def _avg_fuel_l_per_100km(
    fuel_used_liters: Optional[float],
    distance_meters: Optional[int],
) -> Optional[float]:
    if fuel_used_liters is None or not distance_meters or distance_meters <= 0:
        return None
    return (fuel_used_liters / (distance_meters / 1000.0)) * 100.0


def _allocated_driver_fuel_liters(
    *,
    vehicle_fuel_liters: Optional[float],
    driver_distance_meters: Optional[int],
    vehicle_distance_meters: Optional[int],
) -> Optional[float]:
    if (
        vehicle_fuel_liters is None
        or driver_distance_meters is None
        or vehicle_distance_meters is None
        or vehicle_distance_meters <= 0
    ):
        return None
    return vehicle_fuel_liters * (driver_distance_meters / vehicle_distance_meters)


def _combined_registration_fuel_liters(
    fuel_by_day_registration: Dict[Tuple[str, str], Optional[float]],
    *,
    day: str,
    registration_keys: set[str],
) -> Optional[float]:
    values: List[Optional[float]] = [
        fuel_by_day_registration.get((day, reg_key))
        for reg_key in sorted(registration_keys)
        if reg_key
    ]
    if not values or any(value is None for value in values):
        return None
    return sum(values)


def run(client, run_id: str, params: dict):
    if not isinstance(params, dict):
        raise ValueError("params must be a dict")

    client_id = params.get("client_id")
    if not client_id:
        raise ValueError("Missing required param: client_id")
    window_start_ts_raw = params.get("window_start_ts")
    window_end_ts_raw = params.get("window_end_ts")
    if not window_start_ts_raw or not window_end_ts_raw:
        raise ValueError("Missing required params: window_start_ts, window_end_ts")

    window_start_ts = _parse_runner_iso_ts(str(window_start_ts_raw))
    window_end_ts = _parse_runner_iso_ts(str(window_end_ts_raw))
    if window_end_ts < window_start_ts:
        raise ValueError("window_end_ts must be >= window_start_ts")

    client.log(
        "INFO", "SCRIPT", JOB_SOURCE,
        "Loading client config for aggregation",
        run_id=run_id,
        context={"client_id": client_id},
    )

    cfg = load_client_account_config(client_id=client_id)
    client_code = cfg.client_code
    schema = _safe_ident(cfg.client_db_schema)

    trips_table = f"{schema}.client_trips"
    veh_daily_table = f"{schema}.client_vehicle_daily_fuel"
    veh_drv_daily_table = f"{schema}.client_vehicle_driver_daily_fuel"

    schedule = load_dataset_schedule(client_id=client_id, dataset_name=DATASET_NAME)
    if not schedule.exists:
        client.log(
            "WARNING", "SCRIPT", JOB_SOURCE,
            "No client_dataset_schedule row found; using legacy defaults "
            "(enabled=true, overwrite_existing=true). Seed via onboarding to silence.",
            run_id=run_id,
            context={"client_id": client_id, "dataset_name": DATASET_NAME},
        )
    if not schedule.enabled:
        client.log(
            "INFO", "SCRIPT", JOB_SOURCE,
            "Dataset schedule disabled; skipping run.",
            run_id=run_id,
            context={"client_id": client_id, "dataset_name": DATASET_NAME},
        )
        return

    overwrite_existing = schedule.overwrite_existing

    synced_at = datetime.now(timezone.utc)
    day_windows = _completed_local_day_windows(
        window_start_ts=window_start_ts,
        window_end_ts=window_end_ts,
        now_utc=synced_at,
    )
    client.log(
        "INFO", "SCRIPT", JOB_SOURCE,
        "Run-level synced_at fixed for this run.",
        run_id=run_id,
        context={
            "client_id": client_id,
            "dataset_name": DATASET_NAME,
            "synced_at": synced_at.isoformat(),
            "overwrite_existing": overwrite_existing,
            "local_timezone": "Europe/Warsaw",
            "full_days": [d for d, _, _ in day_windows],
        },
    )

    if not day_windows:
        client.log(
            "INFO", "SCRIPT", JOB_SOURCE,
            "No fully completed Europe/Warsaw days in requested window; nothing to aggregate.",
            run_id=run_id,
            context={
                "client_id": client_id,
                "window_start_ts": str(window_start_ts),
                "window_end_ts": str(window_end_ts),
            },
        )
        return

    client.log(
        "INFO", "SCRIPT", JOB_SOURCE,
        f"Connecting to client business DB (schema={schema})",
        run_id=run_id,
    )

    provider_password = resolve_secret(cfg.provider_basic_auth_password_secret_ref)
    safety_limits = SafetyLimits()
    provider_budget = ProviderRunBudget(limits=safety_limits)

    def _provider_log(level: str, message: str, context: dict) -> None:
        client.log(level, "SCRIPT", JOB_SOURCE, message, run_id=run_id, context=context)

    telematics = TelematicsFleetProviderClient(
        base_url=cfg.provider_base_url,
        basic_auth_username=cfg.provider_basic_auth_username,
        basic_auth_password=provider_password,
        page_limit=provider_page_limit_from_env(),
        safety_limits=safety_limits,
        budget=provider_budget,
        log_fn=_provider_log,
    )

    veh_rows: List[Tuple[Any, ...]] = []
    drv_rows: List[Tuple[Any, ...]] = []

    conn = _client_business_pg_conn(cfg)
    try:
        with conn.cursor() as cur:
            query_start_utc = day_windows[0][1].astimezone(timezone.utc)
            query_end_utc = day_windows[-1][2].astimezone(timezone.utc)

            # ---- Read trips for completed local days ----
            cur.execute(
                f"""
                SELECT
                  client_id,
                  provider_trip_id,
                  registration,
                  vehicle_id::text AS vehicle_id,
                  identification_tag_id::text AS driver_id,
                  driver_name,
                  driver_surname,
                  start_timestamp,
                  end_timestamp,
                  trip_distance_meters
                FROM {trips_table}
                WHERE client_id = %s
                  AND start_timestamp >= %s
                  AND start_timestamp < %s
                """,
                (client_id, query_start_utc, query_end_utc),
            )
            rows = cur.fetchall()

            client.log(
                "INFO", "SCRIPT", JOB_SOURCE,
                f"Read {len(rows)} trips in completed local days for aggregation",
                run_id=run_id,
                context={
                    "client_id": client_id,
                    "trip_count": len(rows),
                    "query_start_utc": query_start_utc.isoformat(),
                    "query_end_utc": query_end_utc.isoformat(),
                },
            )

            if not rows:
                client.log(
                    "INFO", "SCRIPT", JOB_SOURCE,
                    "No trips in window — nothing to aggregate",
                    run_id=run_id,
                )
                conn.commit()
                return

            # ---- Aggregate by vehicle+day ----
            veh_day: Dict[Tuple[str, str], Dict[str, Any]] = {}
            # ---- Aggregate by vehicle+driver+day ----
            veh_drv_day: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
            registrations_by_day: Dict[str, Dict[str, str]] = {
                day: {} for day, _, _ in day_windows
            }

            for r in rows:
                start_ts = r[7]  # start_timestamp
                if start_ts is None:
                    continue

                day = start_ts.astimezone(LOCAL_TZ).date().isoformat()
                if day not in registrations_by_day:
                    continue
                vehicle_id = str(r[3] or "UNKNOWN")
                registration = str(r[2] or "")
                driver_id = str(r[4]) if r[4] is not None else None
                driver_name = r[5]
                driver_surname = r[6]
                end_ts = r[8]
                distance_m = r[9]

                reg_key = _registration_key(registration)
                if reg_key:
                    registrations_by_day[day][reg_key] = registration.strip()

                # -- vehicle+day --
                vk = (vehicle_id, day)
                if vk not in veh_day:
                    veh_day[vk] = {
                        "registration": registration,
                        "registration_key": reg_key,
                        "registration_keys": set(),
                        "distance_m": 0,
                        "count": 0,
                        "first_start": start_ts,
                        "last_end": end_ts,
                    }
                agg = veh_day[vk]
                if distance_m is not None:
                    agg["distance_m"] += int(distance_m)
                agg["count"] += 1
                if reg_key:
                    agg["registration_keys"].add(reg_key)
                if start_ts and (agg["first_start"] is None or start_ts < agg["first_start"]):
                    agg["first_start"] = start_ts
                if end_ts and (agg["last_end"] is None or end_ts > agg["last_end"]):
                    agg["last_end"] = end_ts
                if registration:
                    agg["registration"] = registration
                    agg["registration_key"] = reg_key

                # -- vehicle+driver+day --
                if driver_id is not None:
                    dk = (vehicle_id, driver_id, day)
                    if dk not in veh_drv_day:
                        veh_drv_day[dk] = {
                            "registration": registration,
                            "registration_key": reg_key,
                            "driver_name": driver_name,
                            "driver_surname": driver_surname,
                            "distance_m": 0,
                            "count": 0,
                            "first_start": start_ts,
                            "last_end": end_ts,
                        }
                    dagg = veh_drv_day[dk]
                    if distance_m is not None:
                        dagg["distance_m"] += int(distance_m)
                    dagg["count"] += 1
                    if start_ts and (dagg["first_start"] is None or start_ts < dagg["first_start"]):
                        dagg["first_start"] = start_ts
                    if end_ts and (dagg["last_end"] is None or end_ts > dagg["last_end"]):
                        dagg["last_end"] = end_ts
                    if registration:
                        dagg["registration"] = registration
                        dagg["registration_key"] = reg_key
                    if driver_name:
                        dagg["driver_name"] = driver_name
                    if driver_surname:
                        dagg["driver_surname"] = driver_surname

            # ---- Fetch fuel by registration/day via fuel-level provider API ----
            fuel_by_day_registration: Dict[Tuple[str, str], Optional[float]] = {}
            api_calls = 0
            planned_calls = sum(
                len(regs)
                for regs in registrations_by_day.values()
                if regs
            )

            for day, day_start_local, day_end_local in day_windows:
                registrations = sorted(registrations_by_day.get(day, {}).values())
                if not registrations:
                    client.log(
                        "WARNING", "SCRIPT", JOB_SOURCE,
                        "No registrations found for completed local day; fuel will be NULL.",
                        run_id=run_id,
                        context={"client_id": client_id, "day": day},
                    )
                    continue

                for registration in registrations:
                    if api_calls > 0:
                        time_mod.sleep(6)
                    api_calls += 1
                    reg_key = _registration_key(registration)
                    try:
                        fuel_level = telematics.fetch_fuel_level(
                            registration=registration,
                            start_timestamp=day_start_local,
                            end_timestamp=day_end_local,
                            sub_window_label=f"fuel_level:{day}:{reg_key}",
                        )
                    except Exception as e:
                        client.log(
                            "WARNING", "SCRIPT", JOB_SOURCE,
                            "Fuel level fetch failed; fuel will be NULL for this registration/day.",
                            run_id=run_id,
                            context={
                                "client_id": client_id,
                                "day": day,
                                "registration": registration,
                                "error": type(e).__name__,
                                "detail": str(e),
                            },
                        )
                        fuel_by_day_registration[(day, reg_key)] = None
                        continue

                    fuel_used_liters, invalid_reason = _fuel_used_liters_from_level(fuel_level)
                    fuel_by_day_registration[(day, reg_key)] = fuel_used_liters
                    if invalid_reason:
                        client.log(
                            "WARNING", "SCRIPT", JOB_SOURCE,
                            "Fuel level data is not reliable for daily consumption; writing NULL fuel.",
                            run_id=run_id,
                            context={
                                "client_id": client_id,
                                "day": day,
                                "registration": registration,
                                "reason": invalid_reason,
                                "start_liters": fuel_level.get("start_liters"),
                                "end_liters": fuel_level.get("end_liters"),
                                "start_accurate": fuel_level.get("start_accurate"),
                                "end_accurate": fuel_level.get("end_accurate"),
                                "calibrated": fuel_level.get("calibrated"),
                                "estimated_fuel_used": fuel_level.get("estimated_fuel_used"),
                            },
                        )

            client.log(
                "INFO", "SCRIPT", JOB_SOURCE,
                "Fuel level fetch complete",
                run_id=run_id,
                context={
                    "client_id": client_id,
                    "api_calls": api_calls,
                    "planned_calls": planned_calls,
                    "full_days": [d for d, _, _ in day_windows],
                },
            )

            vehicle_fuel_by_day: Dict[Tuple[str, str], Optional[float]] = {}

            # ---- Upsert vehicle daily ----
            for (vehicle_id, day), a in veh_day.items():
                dist_m = a["distance_m"]
                dist_km = dist_m / 1000.0 if dist_m else None
                fuel_total = _combined_registration_fuel_liters(
                    fuel_by_day_registration,
                    day=day,
                    registration_keys=a.get("registration_keys") or set(),
                )
                vehicle_fuel_by_day[(vehicle_id, day)] = fuel_total
                avg_fuel = _avg_fuel_l_per_100km(fuel_total, dist_m)
                if fuel_total is None:
                    client.log(
                        "WARNING", "SCRIPT", JOB_SOURCE,
                        "Fuel missing for vehicle/day aggregation; writing NULL fuel.",
                        run_id=run_id,
                        context={
                            "client_id": client_id,
                            "day": day,
                            "vehicle_id": vehicle_id,
                            "registration": a["registration"],
                        },
                    )

                veh_rid = record_id_mod.for_client_vehicle_daily_fuel(
                    client_id=client_id, vehicle_id=vehicle_id, day=day,
                )
                veh_rows.append((
                    str(veh_rid),
                    client_id, client_code, day, vehicle_id, a["registration"],
                    dist_m, dist_km, fuel_total, avg_fuel, a["count"],
                    a["first_start"], a["last_end"],
                    synced_at, run_id,
                ))

            if veh_rows:
                # `updated_at` stays NOT NULL DEFAULT now() and is refreshed on
                # write; `synced_at` + `sync_run_id` are added alongside.
                if overwrite_existing:
                    on_conflict_veh_sql = """
                    ON CONFLICT (client_id, vehicle_id, day) DO UPDATE SET
                      record_id=EXCLUDED.record_id,
                      client_code=EXCLUDED.client_code,
                      registration=EXCLUDED.registration,
                      distance_m=EXCLUDED.distance_m,
                      distance_km=EXCLUDED.distance_km,
                      fuel_consumed_liters=EXCLUDED.fuel_consumed_liters,
                      avg_fuel_l_per_100km=EXCLUDED.avg_fuel_l_per_100km,
                      trip_count=EXCLUDED.trip_count,
                      first_trip_start_ts=EXCLUDED.first_trip_start_ts,
                      last_trip_end_ts=EXCLUDED.last_trip_end_ts,
                      updated_at=NOW(),
                      synced_at=EXCLUDED.synced_at,
                      sync_run_id=EXCLUDED.sync_run_id
                    """
                else:
                    on_conflict_veh_sql = (
                        "ON CONFLICT (client_id, vehicle_id, day) DO NOTHING"
                    )

                cur.executemany(
                    f"""
                    INSERT INTO {veh_daily_table} (
                      record_id,
                      client_id, client_code, day, vehicle_id, registration,
                      distance_m, distance_km, fuel_consumed_liters, avg_fuel_l_per_100km, trip_count,
                      first_trip_start_ts, last_trip_end_ts,
                      synced_at, sync_run_id, updated_at
                    ) VALUES (
                      %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW()
                    )
                    {on_conflict_veh_sql}
                    """,
                    veh_rows,
                )
                client.log("INFO", "SCRIPT", JOB_SOURCE,
                           f"Upserted {len(veh_rows)} vehicle-daily aggregation rows",
                           run_id=run_id)

            # ---- Upsert vehicle+driver daily ----
            for (vehicle_id, driver_id, day), a in veh_drv_day.items():
                dist_m = a["distance_m"]
                dist_km = dist_m / 1000.0 if dist_m else None
                vehicle_total_distance = veh_day.get((vehicle_id, day), {}).get("distance_m") or 0
                vehicle_fuel = vehicle_fuel_by_day.get((vehicle_id, day))
                fuel_total = _allocated_driver_fuel_liters(
                    vehicle_fuel_liters=vehicle_fuel,
                    driver_distance_meters=dist_m,
                    vehicle_distance_meters=vehicle_total_distance,
                )
                avg_fuel = _avg_fuel_l_per_100km(fuel_total, dist_m)

                drv_rid = record_id_mod.for_client_vehicle_driver_daily_fuel(
                    client_id=client_id, vehicle_id=vehicle_id,
                    driver_id=driver_id, day=day,
                )
                drv_rows.append((
                    str(drv_rid),
                    client_id, client_code, day, vehicle_id, a["registration"],
                    driver_id, a.get("driver_name"), a.get("driver_surname"),
                    dist_m, dist_km, fuel_total, avg_fuel, a["count"],
                    a["first_start"], a["last_end"],
                    synced_at, run_id,
                ))

            if drv_rows:
                if overwrite_existing:
                    on_conflict_drv_sql = """
                    ON CONFLICT (client_id, vehicle_id, driver_id, day) DO UPDATE SET
                      record_id=EXCLUDED.record_id,
                      client_code=EXCLUDED.client_code,
                      registration=EXCLUDED.registration,
                      driver_name=EXCLUDED.driver_name,
                      driver_surname=EXCLUDED.driver_surname,
                      distance_m=EXCLUDED.distance_m,
                      distance_km=EXCLUDED.distance_km,
                      fuel_consumed_liters=EXCLUDED.fuel_consumed_liters,
                      avg_fuel_l_per_100km=EXCLUDED.avg_fuel_l_per_100km,
                      trip_count=EXCLUDED.trip_count,
                      first_trip_start_ts=EXCLUDED.first_trip_start_ts,
                      last_trip_end_ts=EXCLUDED.last_trip_end_ts,
                      updated_at=NOW(),
                      synced_at=EXCLUDED.synced_at,
                      sync_run_id=EXCLUDED.sync_run_id
                    """
                else:
                    on_conflict_drv_sql = (
                        "ON CONFLICT (client_id, vehicle_id, driver_id, day) DO NOTHING"
                    )

                cur.executemany(
                    f"""
                    INSERT INTO {veh_drv_daily_table} (
                      record_id,
                      client_id, client_code, day, vehicle_id, registration,
                      driver_id, driver_name, driver_surname,
                      distance_m, distance_km, fuel_consumed_liters, avg_fuel_l_per_100km, trip_count,
                      first_trip_start_ts, last_trip_end_ts,
                      synced_at, sync_run_id, updated_at
                    ) VALUES (
                      %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW()
                    )
                    {on_conflict_drv_sql}
                    """,
                    drv_rows,
                )
                client.log("INFO", "SCRIPT", JOB_SOURCE,
                           f"Upserted {len(drv_rows)} vehicle+driver-daily aggregation rows",
                           run_id=run_id)

            conn.commit()

    finally:
        conn.close()

    client.log(
        "INFO", "SCRIPT", JOB_SOURCE,
        "Aggregation complete",
        run_id=run_id,
        context={
            "client_id": client_id,
            "window_start_ts": str(window_start_ts),
            "window_end_ts": str(window_end_ts),
            "vehicle_daily_rows": len(veh_rows) if veh_rows else 0,
            "vehicle_driver_daily_rows": len(drv_rows) if drv_rows else 0,
        },
    )
