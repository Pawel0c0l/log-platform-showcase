from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from jobs.api.telematics import client_trips_admission
from jobs.api.telematics import record_id as record_id_mod
from jobs.api.telematics.control_plane import ClientAccountConfig, load_client_account_config
from jobs.api.telematics.provider_client import TelematicsFleetProviderClient, provider_page_limit_from_env
from jobs.api.telematics.provider_safety import ProviderRunBudget, SafetyLimits
from jobs.api.telematics.secret_resolver import resolve_secret
from jobs.api.telematics import sync_trips_and_speeding as trip_sync


JOB_SOURCE = "jobs.api.telematics.backfill_trips_insert_only"
MAX_BACKFILL_DAYS = 31
EXISTING_ID_BATCH_SIZE = 5000


@dataclass(frozen=True)
class BackfillRequest:
    client_id: str
    client_code: str
    window_start_ts: datetime
    window_end_ts: datetime
    chunk_days: int
    provider_trip_ids: Optional[Tuple[int, ...]]
    dry_run: bool
    insert_only: bool
    expected_insert_count: Optional[int]


@dataclass(frozen=True)
class PreparedTrip:
    provider_trip_id: int
    registration: str
    start_ts: datetime
    end_ts: datetime
    values: Tuple[Any, ...]


TRIP_INSERT_COLUMNS = (
    "client_id",
    "client_code",
    "provider_trip_id",
    "vehicle_id",
    "registration",
    "vehicle_name",
    "vehicle_description",
    "chassis_number",
    "driver_name",
    "driver_surname",
    "driver_tag_description",
    "identification_tag_id",
    '"Driver_Restrictions"',
    "trip_mode",
    "start_timestamp",
    "start_location",
    "start_latitude",
    "start_longitude",
    "start_geofence_name",
    "start_odometer_value",
    "end_timestamp",
    "end_location",
    "end_latitude",
    "end_longitude",
    "end_geofence_name",
    "end_odometer_value",
    "trip_duration_seconds",
    "trip_distance_meters",
    "high_rpm_events_count",
    "overrev_events_count",
    "harsh_braking_events",
    "harsh_acceleration_events",
    "harsh_turning_events",
    "idle_events",
    "idle_time_seconds",
    "speeding_140_160_count",
    "speeding_160_170_count",
    "speeding_170_plus_count",
    "record_id",
    "synced_at",
    "sync_run_id",
)


# Position of `trip_distance_meters` inside a `PreparedTrip.values` tuple. Derived
# from the column list so it cannot drift if the list is reordered.
TRIP_DISTANCE_VALUE_INDEX = TRIP_INSERT_COLUMNS.index("trip_distance_meters")


def _explicit_bool(params: dict, key: str, *, default: Optional[bool] = None) -> bool:
    if key not in params:
        if default is not None:
            return default
        raise ValueError(f"Missing required param: {key}")
    value = params.get(key)
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "f", "no", "n", "off"}:
        return False
    raise ValueError(f"{key} must be a boolean")


def _parse_explicit_utc(value: Any, field_name: str) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"Missing required param: {field_name}")
    iso_value = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(iso_value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must include an explicit UTC offset (Z or +00:00)")
    return parsed.astimezone(timezone.utc)


def _parse_provider_trip_ids(params: dict) -> Optional[Tuple[int, ...]]:
    if "provider_trip_ids" not in params:
        return None
    raw_values = params.get("provider_trip_ids")
    if not isinstance(raw_values, list) or not raw_values:
        raise ValueError("provider_trip_ids must be a non-empty list of positive integers")

    parsed: List[int] = []
    seen: set[int] = set()
    for raw_value in raw_values:
        if isinstance(raw_value, bool):
            raise ValueError("provider_trip_ids must contain only positive integers")
        try:
            provider_trip_id = int(raw_value)
        except (TypeError, ValueError) as exc:
            raise ValueError("provider_trip_ids must contain only positive integers") from exc
        if provider_trip_id <= 0:
            raise ValueError("provider_trip_ids must contain only positive integers")
        if provider_trip_id in seen:
            raise ValueError("provider_trip_ids must not contain duplicates")
        seen.add(provider_trip_id)
        parsed.append(provider_trip_id)
    return tuple(parsed)


def _parse_request(params: dict) -> BackfillRequest:
    client_id = str(params.get("client_id") or "").strip()
    client_code = str(params.get("client_code") or "").strip()
    if not client_id:
        raise ValueError("Missing required param: client_id")
    if not client_code:
        raise ValueError("Missing required param: client_code")

    window_start_ts = _parse_explicit_utc(params.get("window_start_ts"), "window_start_ts")
    window_end_ts = _parse_explicit_utc(params.get("window_end_ts"), "window_end_ts")
    if window_end_ts <= window_start_ts:
        raise ValueError("window_end_ts must be greater than window_start_ts")
    if window_end_ts - window_start_ts > timedelta(days=MAX_BACKFILL_DAYS):
        raise ValueError(f"Backfill interval must not exceed {MAX_BACKFILL_DAYS} days")

    insert_only = _explicit_bool(params, "insert_only")
    if not insert_only:
        raise ValueError("insert_only=true is required; updates are not supported")
    if trip_sync._param_bool(params, "overwrite_existing", False):
        raise ValueError("overwrite_existing=true is forbidden for insert-only trip backfill")

    dry_run = _explicit_bool(params, "dry_run", default=True)
    expected_insert_count: Optional[int] = None
    if not dry_run:
        if "expected_insert_count" not in params:
            raise ValueError(
                "expected_insert_count is required when dry_run=false; use the preceding dry-run value"
            )
        raw_expected = params.get("expected_insert_count")
        if isinstance(raw_expected, bool):
            raise ValueError("expected_insert_count must be a non-negative integer")
        try:
            expected_insert_count = int(raw_expected)
        except (TypeError, ValueError) as exc:
            raise ValueError("expected_insert_count must be a non-negative integer") from exc
        if expected_insert_count < 0:
            raise ValueError("expected_insert_count must be a non-negative integer")

    return BackfillRequest(
        client_id=client_id,
        client_code=client_code,
        window_start_ts=window_start_ts,
        window_end_ts=window_end_ts,
        chunk_days=trip_sync._trips_chunk_days(params),
        provider_trip_ids=_parse_provider_trip_ids(params),
        dry_run=dry_run,
        insert_only=insert_only,
        expected_insert_count=expected_insert_count,
    )


def _provider_log_fn(client: Any, run_id: str):
    def _log(level: str, message: str, context: dict) -> None:
        client.log(level, "SCRIPT", JOB_SOURCE, message, run_id=run_id, context=context)

    return _log


def _fetch_trips(
    *,
    provider: TelematicsFleetProviderClient,
    client: Any,
    run_id: str,
    request: BackfillRequest,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    chunks = trip_sync._build_trip_fetch_chunks(
        window_start_ts=request.window_start_ts,
        window_end_ts=request.window_end_ts,
        chunk_days=request.chunk_days,
    )
    rows: List[Dict[str, Any]] = []
    summaries: List[Dict[str, Any]] = []
    for chunk in chunks:
        context = {
            "client_id": request.client_id,
            "client_code": request.client_code,
            "endpoint": "/trips",
            "chunk_index": chunk.index,
            "chunk_total": chunk.total,
            "chunk_start_ts": chunk.request_start_ts.isoformat(),
            "chunk_end_ts": chunk.request_end_ts.isoformat(),
            "chunk_exclusive_end_ts": chunk.exclusive_end_ts.isoformat(),
            "incl_private": True,
        }
        client.log(
            "INFO",
            "SCRIPT",
            JOB_SOURCE,
            "Fetching insert-only backfill trip chunk",
            run_id=run_id,
            context=context,
        )
        chunk_rows = provider.fetch_trips(
            window_start_ts=chunk.request_start_ts,
            window_end_ts=chunk.request_end_ts,
            incl_private=True,
        )
        rows.extend(chunk_rows)
        summaries.append({**context, "records_fetched": len(chunk_rows)})
    return rows, summaries


def _parse_raw_trips(
    rows: Iterable[Any],
    *,
    window_start_ts: Optional[datetime] = None,
    window_end_ts: Optional[datetime] = None,
) -> Tuple[List[Dict[str, Any]], Counter[str], int, int]:
    parsed: List[Dict[str, Any]] = []
    rejected: Counter[str] = Counter()
    seen_ids: set[int] = set()
    duplicate_rows = 0
    duplicate_ids: set[int] = set()

    for raw in rows:
        if not isinstance(raw, dict):
            rejected["not_an_object"] += 1
            continue
        try:
            provider_trip_id = int(raw["trip_id"])
        except (KeyError, TypeError, ValueError):
            rejected["invalid_trip_id"] += 1
            continue
        # Client Trips global admission rule: provider distance > 2,000 km is
        # discarded before anything else reads the row (see
        # `jobs/api/telematics/client_trips_admission.py`). Counted, never logged
        # per trip; it surfaces in the preflight as an ordinary rejection
        # reason plus the canonical counter key.
        if client_trips_admission.distance_exceeds_cap(
            raw.get(client_trips_admission.PROVIDER_DISTANCE_FIELD)
        ):
            rejected[client_trips_admission.REASON_DISTANCE_OVER_CAP] += 1
            continue
        registration = str(raw.get("registration") or "").strip()
        if not registration:
            rejected["missing_registration"] += 1
            continue
        try:
            start_ts = trip_sync._parse_provider_dt(raw.get("start_timestamp"))
            end_ts = trip_sync._parse_provider_dt(raw.get("end_timestamp"))
        except (TypeError, ValueError):
            rejected["invalid_timestamp"] += 1
            continue
        if start_ts is None or end_ts is None:
            rejected["missing_timestamp"] += 1
            continue
        if end_ts < start_ts:
            rejected["end_before_start"] += 1
            continue
        if (
            window_start_ts is not None
            and window_end_ts is not None
            and (end_ts < window_start_ts or start_ts > window_end_ts)
        ):
            rejected["outside_requested_window"] += 1
            continue
        if provider_trip_id in seen_ids:
            duplicate_rows += 1
            duplicate_ids.add(provider_trip_id)
            continue
        seen_ids.add(provider_trip_id)

        start_lat, start_lon = trip_sync._extract_coords(raw.get("start_coordinates"))
        end_lat, end_lon = trip_sync._extract_coords(raw.get("end_coordinates"))
        parsed.append(
            {
                "provider_trip_id": provider_trip_id,
                "registration": registration,
                "start_ts": start_ts,
                "end_ts": end_ts,
                "start_latitude": start_lat,
                "start_longitude": start_lon,
                "end_latitude": end_lat,
                "end_longitude": end_lon,
                "start_odometer_value": trip_sync._extract_odometer_value(
                    raw, "start_odometer_value", "start_odometer", "odometer_start"
                ),
                "end_odometer_value": trip_sync._extract_odometer_value(
                    raw, "end_odometer_value", "end_odometer", "odometer_end"
                ),
                "driver_tag_description": trip_sync._extract_optional_text(
                    raw,
                    "driver_tag_description",
                    "driver_tag_desc",
                    "driver_tag",
                    "tag_description",
                ),
                "driver_id": trip_sync._extract_optional_text(raw, "driver_id", "driver_uuid"),
                "identification_tag_id": trip_sync._extract_optional_text(
                    raw,
                    "identification_tag_id",
                    "driver_identification_tag_id",
                    "driver_tag_id",
                    "tag_id",
                ),
                "trip_mode": trip_sync._trip_mode_from_provider(raw),
                "raw": raw,
            }
        )

    return parsed, rejected, duplicate_rows, len(duplicate_ids)


def _prepare_trip_rows(
    *,
    parsed_trips: Iterable[Dict[str, Any]],
    request: BackfillRequest,
    run_id: str,
    synced_at: datetime,
    vehicle_rows: Iterable[Dict[str, Any]],
    driver_rows: Iterable[Dict[str, Any]],
) -> List[PreparedTrip]:
    vehicle_by_id, vehicle_by_registration = trip_sync._build_vehicle_metadata_lookups(vehicle_rows)
    driver_lookups = trip_sync._build_driver_restriction_lookups(driver_rows)
    prepared: List[PreparedTrip] = []

    for parsed in parsed_trips:
        raw = parsed["raw"]
        provider_trip_id = parsed["provider_trip_id"]
        vehicle_metadata = trip_sync._vehicle_metadata_for_trip(
            vehicle_id=raw.get("vehicle_id"),
            registration=parsed["registration"],
            by_vehicle_id=vehicle_by_id,
            by_registration=vehicle_by_registration,
        )
        driver_restrictions, _match_source = trip_sync._driver_restrictions_for_trip(
            driver_id=parsed["driver_id"],
            identification_tag_id=parsed["identification_tag_id"],
            driver_name=raw.get("driver_name"),
            driver_surname=raw.get("driver_surname"),
            lookups=driver_lookups,
        )
        values = (
            request.client_id,
            request.client_code,
            provider_trip_id,
            raw.get("vehicle_id"),
            parsed["registration"],
            vehicle_metadata["vehicle_name"],
            vehicle_metadata["vehicle_description"],
            raw.get("chassis_number"),
            raw.get("driver_name"),
            raw.get("driver_surname"),
            parsed["driver_tag_description"],
            parsed["identification_tag_id"],
            driver_restrictions,
            parsed["trip_mode"],
            parsed["start_ts"],
            raw.get("start_location"),
            parsed["start_latitude"],
            parsed["start_longitude"],
            raw.get("start_geofence_name"),
            parsed["start_odometer_value"],
            parsed["end_ts"],
            raw.get("end_location"),
            parsed["end_latitude"],
            parsed["end_longitude"],
            raw.get("end_geofence_name"),
            parsed["end_odometer_value"],
            raw.get("trip_duration_seconds"),
            raw.get("trip_distance"),
            0,
            0,
            raw.get("harsh_braking_events"),
            raw.get("harsh_acceleration_events"),
            raw.get("harsh_cornering_events"),
            raw.get("events_idle"),
            raw.get("idle_time_seconds"),
            0,
            0,
            0,
            str(
                record_id_mod.for_client_trips(
                    client_id=request.client_id,
                    provider_trip_id=provider_trip_id,
                )
            ),
            synced_at,
            run_id,
        )
        prepared.append(
            PreparedTrip(
                provider_trip_id=provider_trip_id,
                registration=parsed["registration"],
                start_ts=parsed["start_ts"],
                end_ts=parsed["end_ts"],
                values=values,
            )
        )
    return prepared


def _batched(values: Sequence[int], size: int) -> Iterable[Sequence[int]]:
    for offset in range(0, len(values), size):
        yield values[offset:offset + size]


def _load_existing_trip_ids(
    cur: Any,
    *,
    trips_table: str,
    client_id: str,
    provider_trip_ids: Sequence[int],
) -> set[int]:
    existing: set[int] = set()
    for batch in _batched(provider_trip_ids, EXISTING_ID_BATCH_SIZE):
        cur.execute(
            f"""
            SELECT provider_trip_id
            FROM {trips_table}
            WHERE client_id=%s
              AND provider_trip_id = ANY(%s)
            """,
            (client_id, list(batch)),
        )
        existing.update(int(row[0]) for row in cur.fetchall())
    return existing


def _load_existing_intervals(
    cur: Any,
    *,
    trips_table: str,
    client_id: str,
    window_start_ts: datetime,
    window_end_ts: datetime,
) -> List[Tuple[str, datetime, datetime]]:
    cur.execute(
        f"""
        SELECT registration, start_timestamp, end_timestamp
        FROM {trips_table}
        WHERE client_id=%s
          AND end_timestamp >= %s
          AND start_timestamp <= %s
        """,
        (client_id, window_start_ts, window_end_ts),
    )
    return [
        (str(registration or ""), start_ts, end_ts)
        for registration, start_ts, end_ts in cur.fetchall()
        if start_ts is not None and end_ts is not None
    ]


def _count_temporal_overlaps(
    prepared: Iterable[PreparedTrip],
    existing_intervals: Iterable[Tuple[str, datetime, datetime]],
) -> int:
    by_registration: Dict[str, List[Tuple[datetime, datetime]]] = defaultdict(list)
    for registration, start_ts, end_ts in existing_intervals:
        normalized = trip_sync._normalize_registration(registration)
        if normalized:
            by_registration[normalized].append((start_ts, end_ts))

    overlap_count = 0
    for trip in prepared:
        intervals = by_registration.get(trip_sync._normalize_registration(trip.registration), ())
        if any(start_ts <= trip.end_ts and end_ts >= trip.start_ts for start_ts, end_ts in intervals):
            overlap_count += 1
    return overlap_count


def _insert_prepared_rows(cur: Any, *, trips_table: str, rows: Sequence[PreparedTrip]) -> int:
    # This is the only statement in this job that writes `client_trips`, and it
    # is issued through `client_trips_admission.execute_client_trips_insert` —
    # the only sanctioned `client_trips` write in this repository. That call
    # applies the 2,000 km cap to the exact value each row binds to
    # `trip_distance_meters`, so a caller that builds `PreparedTrip` rows by
    # another route still cannot persist an over-cap trip. `_parse_raw_trips`
    # has already dropped them, so nothing is expected to be rejected here.
    if not rows:
        return 0
    columns_sql = ",\n              ".join(TRIP_INSERT_COLUMNS)
    placeholders = ",".join(["%s"] * len(TRIP_INSERT_COLUMNS))
    result = client_trips_admission.execute_client_trips_insert(
        cur,
        sql=f"""
        INSERT INTO {trips_table} (
              {columns_sql}
        )
        VALUES ({placeholders})
        ON CONFLICT (client_id, provider_trip_id) DO NOTHING
        """,
        rows=[row.values for row in rows],
        distance_index=TRIP_DISTANCE_VALUE_INDEX,
        provider_trip_id_index=TRIP_INSERT_COLUMNS.index("provider_trip_id"),
    )
    return result.rowcount


def _preflight_context(
    *,
    request: BackfillRequest,
    cfg: ClientAccountConfig,
    chunk_summaries: Sequence[Dict[str, Any]],
    raw_count: int,
    prepared: Sequence[PreparedTrip],
    rejected: Counter[str],
    duplicate_rows: int,
    duplicate_ids: int,
    rows_filtered_by_provider_trip_ids: int,
    existing_ids: set[int],
    temporal_overlap_count: int,
) -> Dict[str, Any]:
    date_distribution = Counter(trip.start_ts.date().isoformat() for trip in prepared)
    registrations = sorted({trip_sync._normalize_registration(trip.registration) for trip in prepared})
    candidate_provider_trip_ids = sorted(
        trip.provider_trip_id
        for trip in prepared
        if trip.provider_trip_id not in existing_ids
    )
    would_insert = len(candidate_provider_trip_ids)
    return {
        "client_id": request.client_id,
        "client_code": request.client_code,
        "database_host": cfg.client_db_host,
        "database_port": cfg.client_db_port,
        "database_name": cfg.client_db_name,
        "database_schema": cfg.client_db_schema,
        "window_start_ts": request.window_start_ts.isoformat(),
        "window_end_ts": request.window_end_ts.isoformat(),
        "insert_only": request.insert_only,
        "dry_run": request.dry_run,
        "provider_trip_ids_filter": list(request.provider_trip_ids or ()),
        "provider_request_windows": list(chunk_summaries),
        "rows_fetched": raw_count,
        "rows_valid_unique": len(prepared),
        "rows_filtered_by_provider_trip_ids": rows_filtered_by_provider_trip_ids,
        "rows_would_insert": would_insert,
        "candidate_provider_trip_ids": candidate_provider_trip_ids,
        "rows_existing_skipped": len(existing_ids),
        "duplicate_rows_in_fetched_data": duplicate_rows,
        "duplicate_provider_trip_ids": duplicate_ids,
        "rows_rejected": sum(rejected.values()),
        "rejected_by_reason": dict(sorted(rejected.items())),
        client_trips_admission.REJECTED_COUNTER_KEY: rejected[
            client_trips_admission.REASON_DISTANCE_OVER_CAP
        ],
        "max_trip_distance_meters": client_trips_admission.MAX_TRIP_DISTANCE_METERS,
        "registrations_affected": len(registrations),
        "registrations_sample": registrations[:25],
        "fetched_trip_start_date_distribution_utc": dict(sorted(date_distribution.items())),
        "fetched_rows_overlapping_existing_trip_intervals": temporal_overlap_count,
        "existing_row_overlap_detected": bool(temporal_overlap_count),
        "event_enrichment_requested": False,
        "existing_rows_will_be_updated": False,
    }


def run(client, run_id: str, params: dict) -> None:
    request = _parse_request(params)
    cfg = load_client_account_config(client_id=request.client_id)
    configured_client_code = str(cfg.client_code or "").strip()
    if configured_client_code != request.client_code:
        raise ValueError(
            "client_code does not match the enabled client_account row for the supplied client_id"
        )

    schema = trip_sync._safe_ident(cfg.client_db_schema)
    trips_table = f"{schema}.client_trips"
    client.log(
        "INFO",
        "SCRIPT",
        JOB_SOURCE,
        "Starting scoped insert-only trip backfill preflight",
        run_id=run_id,
        context={
            "client_id": request.client_id,
            "client_code": request.client_code,
            "window_start_ts": request.window_start_ts.isoformat(),
            "window_end_ts": request.window_end_ts.isoformat(),
            "chunk_days": request.chunk_days,
            "provider_trip_ids_filter": list(request.provider_trip_ids or ()),
            "dry_run": request.dry_run,
            "insert_only": request.insert_only,
            "trips_pagination_mode": cfg.trips_pagination_mode,
            "database_host": cfg.client_db_host,
            "database_port": cfg.client_db_port,
            "database_name": cfg.client_db_name,
            "database_schema": schema,
        },
    )

    limits = SafetyLimits()
    budget = ProviderRunBudget(limits=limits)
    # The pagination mode must come from the same control-plane row the
    # scheduled path reads (`sync_trips_and_speeding.run`). Omitting it made
    # `normalize_trips_pagination_mode(None)` fall back to `strict_meta`, so a
    # recovery for a client configured as `data_invariants_v1` aborted
    # fail-closed with PAGINATION_MISMATCH on the first sub-window — after the
    # full provider fetch had already been paid for.
    provider = TelematicsFleetProviderClient(
        base_url=cfg.provider_base_url,
        basic_auth_username=cfg.provider_basic_auth_username,
        basic_auth_password=resolve_secret(cfg.provider_basic_auth_password_secret_ref),
        page_limit=provider_page_limit_from_env(),
        trips_pagination_mode=cfg.trips_pagination_mode,
        safety_limits=limits,
        budget=budget,
        log_fn=_provider_log_fn(client, run_id),
    )
    raw_trips, chunk_summaries = _fetch_trips(
        provider=provider,
        client=client,
        run_id=run_id,
        request=request,
    )
    parsed_trips, rejected, duplicate_rows, duplicate_ids = _parse_raw_trips(
        raw_trips,
        window_start_ts=request.window_start_ts,
        window_end_ts=request.window_end_ts,
    )
    rows_filtered_by_provider_trip_ids = 0
    if request.provider_trip_ids is not None:
        allowed_provider_trip_ids = set(request.provider_trip_ids)
        filtered_trips = [
            trip
            for trip in parsed_trips
            if trip["provider_trip_id"] in allowed_provider_trip_ids
        ]
        rows_filtered_by_provider_trip_ids = len(parsed_trips) - len(filtered_trips)
        parsed_trips = filtered_trips
    vehicle_rows = provider.fetch_vehicles_fleet(sub_window_label="backfill_vehicle_inventory")
    driver_rows = provider.fetch_drivers_fleet(sub_window_label="backfill_driver_inventory")
    synced_at = datetime.now(timezone.utc)
    prepared = _prepare_trip_rows(
        parsed_trips=parsed_trips,
        request=request,
        run_id=run_id,
        synced_at=synced_at,
        vehicle_rows=vehicle_rows,
        driver_rows=driver_rows,
    )

    conn = trip_sync._client_business_pg_conn(cfg)
    try:
        if request.dry_run:
            # The shared connector sets the session timezone with SELECT
            # set_config(), which starts a transaction. Reset it before
            # declaring the diagnostic transaction read-only.
            conn.rollback()
        with conn.cursor() as cur:
            if request.dry_run:
                cur.execute("SET TRANSACTION READ ONLY")
            existing_ids = _load_existing_trip_ids(
                cur,
                trips_table=trips_table,
                client_id=request.client_id,
                provider_trip_ids=[trip.provider_trip_id for trip in prepared],
            )
            existing_intervals = _load_existing_intervals(
                cur,
                trips_table=trips_table,
                client_id=request.client_id,
                window_start_ts=request.window_start_ts,
                window_end_ts=request.window_end_ts,
            )
            temporal_overlap_count = _count_temporal_overlaps(prepared, existing_intervals)
            preflight = _preflight_context(
                request=request,
                cfg=cfg,
                chunk_summaries=chunk_summaries,
                raw_count=len(raw_trips),
                prepared=prepared,
                rejected=rejected,
                duplicate_rows=duplicate_rows,
                duplicate_ids=duplicate_ids,
                rows_filtered_by_provider_trip_ids=rows_filtered_by_provider_trip_ids,
                existing_ids=existing_ids,
                temporal_overlap_count=temporal_overlap_count,
            )
            client.log(
                "INFO",
                "SCRIPT",
                JOB_SOURCE,
                "Insert-only trip backfill preflight result",
                run_id=run_id,
                context=preflight,
            )

            if request.dry_run:
                conn.rollback()
                print(json.dumps(preflight, sort_keys=True), flush=True)
                client.log(
                    "INFO",
                    "SCRIPT",
                    JOB_SOURCE,
                    "Dry-run completed without database mutation",
                    run_id=run_id,
                    context=preflight,
                )
                return

            would_insert = int(preflight["rows_would_insert"])
            if request.expected_insert_count != would_insert:
                raise ValueError(
                    "expected_insert_count does not match current preflight: "
                    f"expected={request.expected_insert_count}, current={would_insert}"
                )
            rows_to_insert = [
                trip for trip in prepared if trip.provider_trip_id not in existing_ids
            ]
            inserted = _insert_prepared_rows(
                cur,
                trips_table=trips_table,
                rows=rows_to_insert,
            )
            conn.commit()
            client.log(
                "INFO",
                "SCRIPT",
                JOB_SOURCE,
                "Insert-only trip backfill committed",
                run_id=run_id,
                context={
                    **preflight,
                    "rows_insert_attempted": len(rows_to_insert),
                    "rows_inserted": inserted,
                    "rows_conflicted_during_insert": len(rows_to_insert) - inserted,
                    "existing_rows_updated": 0,
                },
            )
    finally:
        conn.close()
