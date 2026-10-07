#!/usr/bin/env python3
"""Manual checks and optional live diagnostic for final `client_trips` schema.

Static checks need no DB/network:

    PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_workflow_a_client_trips_final_schema.py

Live column-order diagnostic:

    PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_workflow_a_client_trips_final_schema.py \
      --dsn "host=127.0.0.1 port=5432 dbname=<client_db> user=<user> password=<pw>"
"""
from __future__ import annotations

import argparse
import re
import sys
import types
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

JOB_PATH = REPO_ROOT / "jobs" / "api" / "telematics" / "sync_trips_and_speeding.py"
AGGREGATE_PATH = REPO_ROOT / "jobs" / "api" / "telematics" / "aggregate_trip_fuel_daily.py"
MIGRATION_PATH = REPO_ROOT / "db" / "client_business" / "021_add_trip_mode_to_client_trips.sql"
DRIVER_RESTRICTIONS_MIGRATION_PATH = (
    REPO_ROOT / "db" / "client_business" / "026_add_driver_restrictions_to_client_trips.sql"
)

EXPECTED_CLIENT_TRIPS_COLUMNS = [
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
    "Driver_Restrictions",
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
]

REMOVED_CLIENT_TRIPS_COLUMNS = {
    "driver_id",
    "terminal_id",
    "terminal_serial",
    "speeding_bucket_140_150_events",
    "speeding_bucket_150_160_events",
    "speeding_bucket_160_170_events",
    "speeding_bucket_gt_170_events",
    "speeding_buckets_computed_at",
    "speeding_buckets_source_window_start_ts",
    "speeding_buckets_source_window_end_ts",
    "fuel_consumed_liters",
    "avg_fuel_l_per_100km",
}

FAILURES: list[str] = []


def _install_stub(name: str, attrs: dict | None = None) -> None:
    if name in sys.modules:
        return
    try:
        __import__(name)
        return
    except Exception:
        pass
    mod = types.ModuleType(name)
    for k, v in (attrs or {}).items():
        setattr(mod, k, v)
    sys.modules[name] = mod


def _check(label: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    line = f"[{status}] {label}"
    if detail:
        line += f"\n        {detail}"
    print(line)
    if not ok:
        FAILURES.append(label)


def _extract_insert_columns(src: str) -> list[str]:
    match = re.search(
        r"INSERT\s+INTO\s+\{trips_table\}\s*\((?P<cols>.*?)\)\s*VALUES",
        src,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return []
    return [
        c.strip().strip('"')
        for c in match.group("cols").split(",")
        if c.strip()
    ]


def test_sync_job_insert_shape() -> None:
    src = JOB_PATH.read_text(encoding="utf-8")
    insert_cols = _extract_insert_columns(src)
    _check("sync_trips_and_speeding INSERT column list parsed",
           bool(insert_cols), f"got={insert_cols!r}")
    _check("sync_trips_and_speeding INSERT columns match final order exactly",
           insert_cols == EXPECTED_CLIENT_TRIPS_COLUMNS,
           f"got={insert_cols!r}")
    for col in REMOVED_CLIENT_TRIPS_COLUMNS:
        if col == "driver_id":
            _check("sync_trips_and_speeding does not persist removed `driver_id`",
                   col not in insert_cols)
            continue
        _check(f"sync_trips_and_speeding does not reference removed `{col}`",
               re.search(rf"\b{re.escape(col)}\b", src) is None)
    for col in (
        "vehicle_name",
        "vehicle_description",
        "driver_tag_description",
        "identification_tag_id",
        "Driver_Restrictions",
        "trip_mode",
    ):
        _check(f"sync_trips_and_speeding includes new `{col}`",
               col in insert_cols and re.search(rf"\b{col}\b", src) is not None)
    _check("sync_trips_and_speeding refreshes trip_mode on overwrite upsert",
           "trip_mode=EXCLUDED.trip_mode" in src)
    _check("sync_trips_and_speeding refreshes Driver_Restrictions on overwrite upsert",
           '"Driver_Restrictions"=EXCLUDED."Driver_Restrictions"' in src)


def test_trip_tag_mapping_helper() -> None:
    _install_stub("requests")
    _install_stub("psycopg")
    _install_stub("psycopg.rows", attrs={"dict_row": object()})
    from jobs.api.telematics import sync_trips_and_speeding as job  # noqa: E402

    trip_payload = {
        "driver_tag_description": "  Blue tag  ",
        "identification_tag_id": 12345,
    }
    _check("driver_tag_description maps from /trips payload",
           job._extract_optional_text(trip_payload, "driver_tag_description") == "Blue tag")
    _check("identification_tag_id maps from /trips payload as text",
           job._extract_optional_text(trip_payload, "identification_tag_id") == "12345")
    _check("trip_mode maps true is_private to private",
           job._trip_mode_from_provider({"is_private": True, "trip_type": "Business"}) == "private")
    _check("trip_mode maps false is_private to business",
           job._trip_mode_from_provider({"is_private": False, "trip_type": "Private"}) == "business")
    _check("trip_mode leaves missing is_private unknown",
           job._trip_mode_from_provider({"trip_type": "Business"}) is None)
    _check("trip_mode leaves invalid is_private unknown",
           job._trip_mode_from_provider({"is_private": "not-a-bool"}) is None)


def test_vehicle_metadata_lookup_helpers() -> None:
    _install_stub("requests")
    _install_stub("psycopg")
    _install_stub("psycopg.rows", attrs={"dict_row": object()})
    from jobs.api.telematics import sync_trips_and_speeding as job  # noqa: E402

    by_vehicle_id, by_registration = job._build_vehicle_metadata_lookups([
        {
            "vehicle_id": 42,
            "registration": " ABC123 ",
            "vehicle_name": "Big car",
            "vehicle_description": "Main description",
        },
        {
            "vehicle_id": 99,
            "registration": " fallback-1 ",
            "vehicle_name": "Fallback car",
            "vehicle_description": "Fallback description",
        },
    ])

    by_id = job._vehicle_metadata_for_trip(
        vehicle_id="42",
        registration="fallback-1",
        by_vehicle_id=by_vehicle_id,
        by_registration=by_registration,
    )
    by_reg = job._vehicle_metadata_for_trip(
        vehicle_id=None,
        registration=" FALLBACK-1 ",
        by_vehicle_id=by_vehicle_id,
        by_registration=by_registration,
    )
    no_match = job._vehicle_metadata_for_trip(
        vehicle_id="missing",
        registration="missing",
        by_vehicle_id=by_vehicle_id,
        by_registration=by_registration,
    )

    _check("vehicle metadata lookup prefers vehicle_id",
           by_id["vehicle_name"] == "Big car"
           and by_id["vehicle_description"] == "Main description",
           f"by_id={by_id!r}")
    _check("vehicle metadata lookup falls back to normalized registration",
           by_reg["vehicle_name"] == "Fallback car"
           and by_reg["vehicle_description"] == "Fallback description",
           f"by_reg={by_reg!r}")
    _check("vehicle metadata lookup leaves no-match values NULL",
           no_match == {"vehicle_name": None, "vehicle_description": None},
           f"no_match={no_match!r}")


def test_driver_restriction_lookup_helpers() -> None:
    _install_stub("requests")
    _install_stub("psycopg")
    _install_stub("psycopg.rows", attrs={"dict_row": object()})
    from jobs.api.telematics import sync_trips_and_speeding as job  # noqa: E402

    lookups = job._build_driver_restriction_lookups([
        {
            "driver_id": "DRV-1",
            "first_name": "Ann",
            "last_name": "Driver",
            "identification_tag_id": "TAG-1",
            "license_driver_restrictions": "  Weekdays only  ",
        },
        {
            "driver_id": "DRV-2",
            "first_name": "No",
            "last_name": "Restriction",
            "identification_tag_id": "TAG-2",
            "license_driver_restrictions": "",
        },
        {
            "driver_id": "DRV-3",
            "first_name": "Name",
            "last_name": "Fallback",
            "license_driver_restrictions": "Night permit",
        },
    ])

    by_id = job._driver_restrictions_for_trip(
        driver_id=" drv-1 ",
        identification_tag_id=None,
        driver_name=None,
        driver_surname=None,
        lookups=lookups,
    )
    by_tag_null = job._driver_restrictions_for_trip(
        driver_id=None,
        identification_tag_id="tag-2",
        driver_name=None,
        driver_surname=None,
        lookups=lookups,
    )
    by_name = job._driver_restrictions_for_trip(
        driver_id=None,
        identification_tag_id=None,
        driver_name=" name ",
        driver_surname="fallback",
        lookups=lookups,
    )
    unmatched = job._driver_restrictions_for_trip(
        driver_id="missing",
        identification_tag_id=None,
        driver_name=None,
        driver_surname=None,
        lookups=lookups,
    )
    unidentified = job._driver_restrictions_for_trip(
        driver_id=None,
        identification_tag_id=None,
        driver_name=None,
        driver_surname=None,
        lookups=lookups,
    )

    _check("driver restrictions match by stable driver_id first",
           by_id == ("Weekdays only", "driver_id"), f"by_id={by_id!r}")
    _check("driver restrictions empty string normalizes to NULL",
           by_tag_null == (None, "identification_tag_id"), f"by_tag_null={by_tag_null!r}")
    _check("driver restrictions fall back to driver name when needed",
           by_name == ("Night permit", "driver_name"), f"by_name={by_name!r}")
    _check("identified driver without lookup match returns NULL",
           unmatched == (None, "unmatched"), f"unmatched={unmatched!r}")
    _check("unidentified driver returns NULL without matching",
           unidentified == (None, "unidentified"), f"unidentified={unidentified!r}")


def test_final_migration_shape() -> None:
    sql = MIGRATION_PATH.read_text(encoding="utf-8")
    for col in EXPECTED_CLIENT_TRIPS_COLUMNS:
        if col == "Driver_Restrictions":
            continue
        _check(f"migration declares/copies `{col}`", col in sql)
    for col in REMOVED_CLIENT_TRIPS_COLUMNS:
        _check(f"migration final table does not declare removed `{col}`",
               re.search(rf"\b{col}\b\s+[A-Z]", sql) is None)
    _check("migration preserves PRIMARY KEY (client_id, provider_trip_id)",
           "PRIMARY KEY (client_id, provider_trip_id)" in sql)
    _check("migration validates row counts before swap",
           "old_count <> new_count" in sql and "row-count mismatch" in sql)
    _check("migration keeps rollback backup table",
           "client_trips_legacy_backup_021" in sql and "DROP TABLE public.client_trips_legacy_backup_021" not in sql)
    _check("migration recreates registration/start/end lookup index",
           "idx_client_trips_registration_window" in sql
           and "(registration, start_timestamp, end_timestamp)" in sql)
    _check("migration preserves record_id unique index when present",
           "has_record_id_unique" in sql and "uq_client_trips_record_id" in sql)
    driver_sql = DRIVER_RESTRICTIONS_MIGRATION_PATH.read_text(encoding="utf-8")
    _check("Driver_Restrictions migration is additive and nullable",
           'ALTER TABLE IF EXISTS public.client_trips' in driver_sql
           and 'ADD COLUMN IF NOT EXISTS "Driver_Restrictions" TEXT NULL' in driver_sql
           and "client_trips_rebuilt" not in driver_sql)


def test_aggregate_reads_final_driver_attribution() -> None:
    src = AGGREGATE_PATH.read_text(encoding="utf-8")
    _check("daily fuel aggregate no longer reads removed client_trips.driver_id",
           "driver_id::text AS driver_id" not in src and "t.driver_id" not in src)
    _check("daily fuel aggregate uses identification_tag_id for driver-level attribution",
           "identification_tag_id::text AS driver_id" in src)


def test_vehicle_inventory_is_batch_safe() -> None:
    src = JOB_PATH.read_text(encoding="utf-8")
    _check("sync job calls batch fleet vehicle inventory once",
           src.count("fetch_vehicles_fleet(") == 1,
           f"count={src.count('fetch_vehicles_fleet(')}")
    _check("sync job does not construct per-registration vehicle detail calls",
           '"/vehicles/"' not in src and "f\"/vehicles/" not in src,
           "")
    _check("sync job calls batch fleet driver inventory once",
           src.count("fetch_drivers_fleet(") == 1,
           f"count={src.count('fetch_drivers_fleet(')}")
    _check("sync job does not construct per-driver detail calls",
           '"/drivers/"' not in src and "f\"/drivers/" not in src,
           "")


def test_trip_parse_diagnostics_wiring() -> None:
    _install_stub("requests")
    _install_stub("psycopg")
    _install_stub("psycopg.rows", attrs={"dict_row": object()})
    from jobs.api.telematics import sync_trips_and_speeding as job  # noqa: E402

    src = JOB_PATH.read_text(encoding="utf-8")
    expected_counters = {
        "trips_provider_rows_fetched",
        "trips_rows_parsed",
        "trips_rows_skipped_missing_registration",
        "trips_rows_malformed_trip_id",
        "trips_rows_malformed_timestamp",
        "trips_rows_other_parse_error",
        "client_trips_rejected_distance_over_2000km",
        "client_trips_rejected_distance_over_2000km_at_persistence",
        "trips_rows_prepared_for_upsert",
        "trips_rows_upserted",
        "trips_private_count",
        "trips_business_count",
        "trips_unknown_mode_count",
    }
    _check("trip parse diagnostics expose the expected counter keys",
           set(job.TRIP_PARSE_DIAGNOSTIC_COUNTER_KEYS) == expected_counters,
           f"got={set(job.TRIP_PARSE_DIAGNOSTIC_COUNTER_KEYS)!r}")
    for counter in expected_counters:
        _check(f"sync job references diagnostic counter `{counter}`", counter in src)
    _check("missing registration increments skip counter",
           'trip_parse_diagnostics["trips_rows_skipped_missing_registration"] += 1' in src)
    _check("malformed trip_id increments malformed counter",
           'trip_parse_diagnostics["trips_rows_malformed_trip_id"] += 1' in src)
    _check("malformed timestamp increments malformed counter",
           'trip_parse_diagnostics["trips_rows_malformed_timestamp"] += 1' in src)
    _check("unexpected parse errors increment other parse counter",
           'trip_parse_diagnostics["trips_rows_other_parse_error"] += 1' in src)
    _check("GET /trips request mode is explicitly logged",
           '"GET /trips request mode"' in src
           and "incl_private = True" in src
           and '"incl_private": incl_private' in src
           and '"page_limit": telematics.page_limit' in src)
    _check("parse diagnostics are logged before existing fatal malformed paths raise",
           src.count("_log_trip_parse_diagnostics()") >= 4)


def test_trip_diagnostic_context_is_safe() -> None:
    _install_stub("requests")
    _install_stub("psycopg")
    _install_stub("psycopg.rows", attrs={"dict_row": object()})
    from jobs.api.telematics import sync_trips_and_speeding as job  # noqa: E402

    payload = {
        "trip_id": "abc",
        "registration": "WX1234A",
        "vehicle_id": 42,
        "start_location": "Sensitive full address",
        "nested": {"do": "not log this"},
    }
    ctx = job._trip_diagnostic_context(
        payload,
        reason="malformed_trip_id",
        provider_trip_id=payload["trip_id"],
        error=ValueError("invalid literal"),
    )

    _check("trip diagnostic context includes safe identity fields",
           ctx["provider_trip_id"] == "abc"
           and ctx["registration"] == "WX1234A"
           and ctx["vehicle_id"] == 42
           and ctx["reason"] == "malformed_trip_id",
           f"ctx={ctx!r}")
    _check("trip diagnostic context logs raw payload keys only",
           ctx["raw_payload_keys"] == sorted(payload.keys())
           and "Sensitive full address" not in str(ctx)
           and "{'do': 'not log this'}" not in str(ctx),
           f"ctx={ctx!r}")
    _check("trip diagnostic context records exception type without raw payload",
           ctx["error_type"] == "ValueError" and ctx["error_detail"] == "invalid literal",
           f"ctx={ctx!r}")


def test_bad_location_payloads_do_not_force_trip_skip() -> None:
    _install_stub("requests")
    _install_stub("psycopg")
    _install_stub("psycopg.rows", attrs={"dict_row": object()})
    from jobs.api.telematics import sync_trips_and_speeding as job  # noqa: E402

    src = JOB_PATH.read_text(encoding="utf-8")
    _check("invalid coordinates are normalized to NULL values",
           job._extract_coords({"latitude": "GPS_ERROR", "longitude": "LOCALIZATION_ERROR"}) == (None, None))
    _check("missing coordinate object is normalized to NULL values",
           job._extract_coords(None) == (None, None))
    _check("location/geofence fields are not skip conditions",
           "missing_start_location" not in src
           and "missing_end_location" not in src
           and "missing_geofence" not in src)


def run_live_schema_diagnostic(dsn: str) -> None:
    import psycopg

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'client_trips'
                ORDER BY ordinal_position
                """
            )
            cols = [r[0] for r in cur.fetchall()]
            print("\nLive public.client_trips columns in ordinal_position order:")
            for idx, col in enumerate(cols, start=1):
                print(f"{idx:02d}. {col}")
            _check("live client_trips contains expected logical output columns",
                   all(col in cols for col in EXPECTED_CLIENT_TRIPS_COLUMNS),
                   f"got={cols!r}")

            cur.execute(
                """
                SELECT COUNT(*)
                FROM information_schema.table_constraints
                WHERE table_schema = 'public'
                  AND table_name = 'client_trips'
                  AND constraint_type = 'PRIMARY KEY'
                  AND constraint_name IN (
                    SELECT constraint_name
                    FROM information_schema.key_column_usage
                    WHERE table_schema = 'public'
                      AND table_name = 'client_trips'
                      AND column_name IN ('client_id', 'provider_trip_id')
                    GROUP BY constraint_name
                    HAVING COUNT(*) = 2
                  )
                """
            )
            _check("live client_trips primary key covers client_id + provider_trip_id",
                   cur.fetchone()[0] == 1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", default=None, help="Optional psycopg DSN for live client DB diagnostic.")
    args = parser.parse_args()

    test_sync_job_insert_shape()
    test_trip_tag_mapping_helper()
    test_vehicle_metadata_lookup_helpers()
    test_driver_restriction_lookup_helpers()
    test_final_migration_shape()
    test_aggregate_reads_final_driver_attribution()
    test_vehicle_inventory_is_batch_safe()
    test_trip_parse_diagnostics_wiring()
    test_trip_diagnostic_context_is_safe()
    test_bad_location_payloads_do_not_force_trip_skip()
    if args.dsn:
        run_live_schema_diagnostic(args.dsn)

    print("")
    if FAILURES:
        print(f"FAIL - {len(FAILURES)} check(s) failed:")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("OK - final client_trips schema/order checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
