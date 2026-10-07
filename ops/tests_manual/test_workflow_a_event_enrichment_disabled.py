#!/usr/bin/env python3
"""Manual regression tests for disabling Workflow A vehicle-event enrichment.

Run from repo root:

    python3 ops/tests_manual/test_workflow_a_event_enrichment_disabled.py
"""
from __future__ import annotations

import sys
import types
from datetime import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


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


_install_stub("requests")
_install_stub("psycopg")
_install_stub("psycopg.rows", attrs={"dict_row": object()})

from jobs.api.telematics import sync_trips_and_speeding as job  # noqa: E402
from jobs.api.telematics.control_plane import ClientAccountConfig, DatasetSchedule  # noqa: E402
from jobs.api.telematics.provider_safety import TelematicsProviderSafetyError  # noqa: E402
from jobs.trip_metrics_population_source import (  # noqa: E402
    TRIP_METRICS_SOURCE_API,
    TRIP_METRICS_SOURCE_MISMATCH_REASON,
    TRIP_METRICS_SOURCE_REPORT_207,
)


FAILURES: list[str] = []


def _check(label: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    line = f"[{status}] {label}"
    if detail:
        line += f"\n        {detail}"
    print(line)
    if not ok:
        FAILURES.append(label)


class FakeClient:
    def __init__(self) -> None:
        self.logs: list[dict] = []

    def log(self, level, kind, source, message, *, run_id=None, context=None, error=None) -> None:
        self.logs.append({
            "level": level,
            "kind": kind,
            "source": source,
            "message": message,
            "run_id": run_id,
            "context": context or {},
            "error": error,
        })

    def upload_artifact(self, *args, **kwargs):
        raise AssertionError("disabled enrichment test should not upload artifacts")


class FakeProvider:
    def __init__(self, *, fail_on_events: bool = False) -> None:
        self.page_limit = 200
        self.fail_on_events = fail_on_events
        self.trips_calls = 0
        self.vehicles_calls = 0
        self.drivers_calls = 0
        self.vehicle_events_fleet_calls = 0
        self.vehicle_events_registration_calls = 0

    def fetch_trips(self, **kwargs):
        self.trips_calls += 1
        return [{
            "trip_id": 12345,
            "vehicle_id": "veh-1",
            "registration": "ABC123",
            "start_timestamp": "2026-04-01 08:00:00",
            "end_timestamp": "2026-04-01 09:00:00",
            "start_location": "Depot",
            "end_location": "Client",
            "start_coordinates": {"latitude": 52.0, "longitude": 21.0},
            "end_coordinates": {"latitude": 52.1, "longitude": 21.1},
            "trip_distance": 12000,
            "driver_name": "Ann",
            "driver_surname": "Driver",
            "driver_id": "DRV-1",
            "driver_tag_description": "TAG-1",
            "identification_tag_id": "TAGID-1",
            "is_private": False,
        }]

    def metrics_snapshot(self):
        return {
            "total_requests": 0,
            "request_count_by_endpoint": {},
            "response_parse_count_by_endpoint": {},
            "request_elapsed_seconds_by_endpoint": {},
            "response_parse_elapsed_seconds_by_endpoint": {},
        }

    def fetch_vehicles_fleet(self, **kwargs):
        self.vehicles_calls += 1
        return [{
            "vehicle_id": "veh-1",
            "registration": "ABC123",
            "vehicle_name": "Truck 1",
            "vehicle_description": "Fleet truck",
            "chassis_number": "VIN123",
        }]

    def fetch_drivers_fleet(self, **kwargs):
        self.drivers_calls += 1
        return [{
            "driver_id": "DRV-1",
            "first_name": "Ann",
            "last_name": "Driver",
            "identification_tag_id": "TAGID-1",
            "license_driver_restrictions": "Weekdays only",
        }]

    def fetch_vehicle_events_fleet(self, **kwargs):
        self.vehicle_events_fleet_calls += 1
        if self.fail_on_events:
            raise TelematicsProviderSafetyError(
                "TEST_EVENT_FETCH",
                "default mode attempted vehicle-events fetch",
                context={"endpoint": "/vehicles/events"},
            )
        raise AssertionError("fetch_vehicle_events_fleet must not be called when enrichment is disabled")

    def fetch_vehicle_events_registration(self, **kwargs):
        self.vehicle_events_registration_calls += 1
        raise AssertionError("registration vehicle-events fallback must not be called when enrichment is disabled")


class FakeCursor:
    def __init__(self, conn) -> None:
        self.conn = conn
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def executemany(self, sql, rows) -> None:
        copied_rows = list(rows)
        self.conn.executemany_calls.append((sql, copied_rows))
        self.rowcount = len(copied_rows)
        sql_start = sql.strip().upper()
        if sql_start.startswith("INSERT INTO") and "client_trips" in sql:
            self.conn.trip_upsert_rows.extend(copied_rows)
        if sql_start.startswith("UPDATE") and "speeding_140_160_count" in sql:
            self.conn.bucket_update_rows.extend(copied_rows)


class FakeConnection:
    def __init__(self) -> None:
        self.executemany_calls: list[tuple[str, list[tuple]]] = []
        self.trip_upsert_rows: list[tuple] = []
        self.bucket_update_rows: list[tuple] = []
        self.committed = False
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def commit(self) -> None:
        self.committed = True

    def close(self) -> None:
        self.closed = True


def _fake_cfg(source: str = TRIP_METRICS_SOURCE_API) -> ClientAccountConfig:
    return ClientAccountConfig(
        client_id="bd7662a5-eeb4-4614-8720-d477abfcb227",
        client_code="TST00001",
        client_name="Test Client",
        provider_base_url="https://provider.invalid",
        provider_basic_auth_username="user",
        provider_basic_auth_password_secret_ref="TEST_PROVIDER_KEY",
        client_db_host="127.0.0.1",
        client_db_port=5432,
        client_db_name="clientdb",
        client_db_user="clientuser",
        client_db_password_secret_ref="TEST_DB_KEY",
        client_db_schema="public",
        speed_trigger_filter_text="",
        trip_metrics_population_source=source,
        trips_pagination_mode="strict_meta",
        trips_stabilization_delay_seconds=10800,
        trips_overlap_seconds=3600,
        trips_max_recovery_span_seconds=2678400,
    )


def _fake_schedule() -> DatasetSchedule:
    return DatasetSchedule(
        exists=True,
        client_id="bd7662a5-eeb4-4614-8720-d477abfcb227",
        dataset_name=job.DATASET_NAME,
        enabled=True,
        frequency="daily",
        day_of_week=None,
        day_of_month=None,
        run_time=time(2, 0),
        timezone="UTC",
        lookback_days=1,
        overwrite_existing=True,
        event_enrichment_mode="enabled",
    )


class PatchJob:
    def __init__(self, provider: FakeProvider, conn: FakeConnection, *, source: str = TRIP_METRICS_SOURCE_API) -> None:
        self.provider = provider
        self.conn = conn
        self.source = source
        self.originals = {}

    def __enter__(self):
        names_and_values = {
            "load_client_account_config": lambda *, client_id: _fake_cfg(self.source),
            "load_dataset_schedule": lambda *, client_id, dataset_name: _fake_schedule(),
            "resolve_secret": lambda ref: "secret",
            "TelematicsFleetProviderClient": lambda **kwargs: self.provider,
            "_client_business_pg_conn": lambda cfg: self.conn,
        }
        for name, value in names_and_values.items():
            self.originals[name] = getattr(job, name)
            setattr(job, name, value)
        return self

    def __exit__(self, exc_type, exc, tb):
        for name, value in self.originals.items():
            setattr(job, name, value)
        return False


def _base_params() -> dict:
    return {
        "client_id": "bd7662a5-eeb4-4614-8720-d477abfcb227",
        "window_start_ts": "2026-04-01 00:00:00",
        "window_end_ts": "2026-04-02 00:00:00",
    }


def test_disabled_mode_skips_events_and_upserts_zero_counts() -> None:
    client = FakeClient()
    provider = FakeProvider()
    conn = FakeConnection()
    params = {**_base_params(), "event_enrichment_mode": "disabled"}

    with PatchJob(provider, conn):
        job.run(client, "run-disabled", params)

    _check("disabled mode fetches trips",
           provider.trips_calls == 1,
           f"trips_calls={provider.trips_calls}")
    _check("disabled mode still fetches vehicle inventory",
           provider.vehicles_calls == 1,
           f"vehicles_calls={provider.vehicles_calls}")
    _check("disabled mode fetches driver inventory once",
           provider.drivers_calls == 1,
           f"drivers_calls={provider.drivers_calls}")
    _check("disabled mode does not call fleet /vehicles/events",
           provider.vehicle_events_fleet_calls == 0,
           f"vehicle_events_fleet_calls={provider.vehicle_events_fleet_calls}")
    _check("disabled mode does not call registration /vehicles/events",
           provider.vehicle_events_registration_calls == 0,
           f"vehicle_events_registration_calls={provider.vehicle_events_registration_calls}")
    _check("disabled mode reaches DB upsert and commits",
           conn.committed and len(conn.trip_upsert_rows) == 1,
           f"committed={conn.committed}, trip_upsert_rows={len(conn.trip_upsert_rows)}")

    row = conn.trip_upsert_rows[0]
    _check("disabled mode populates Driver_Restrictions from bulk driver inventory",
           row[12] == "Weekdays only",
           f"Driver_Restrictions={row[12]!r}")
    _check("disabled mode writes deterministic RPM/OVERREV zeros",
           row[28] == 0 and row[29] == 0,
           f"high_rpm={row[28]!r}, overrev={row[29]!r}")
    _check("disabled mode writes deterministic speeding bucket zeros",
           row[35] == 0 and row[36] == 0 and row[37] == 0,
           f"140={row[35]!r}, 160={row[36]!r}, 170={row[37]!r}")
    _check("disabled mode still runs bucket update with zeros",
           conn.bucket_update_rows == [(0, 0, 0, "run-disabled", row[39], row[0], row[2], "run-disabled")],
           f"bucket_update_rows={conn.bucket_update_rows!r}")
    _check("disabled mode logs intentional disablement",
           any(
               "disabled" in entry["message"].lower()
               and entry["context"].get("event_enrichment_mode") == job.VEHICLE_EVENTS_ENRICHMENT_MODE_DISABLED
               for entry in client.logs
           ),
           f"messages={[entry['message'] for entry in client.logs]!r}")
    _check("disabled mode summary is not partial",
           any(
               entry["context"].get("event_enrichment_status") == "disabled"
               and entry["context"].get("complete_event_enrichment") is True
               and entry["context"].get("speeding_rpm_counts_are_partial") is False
               for entry in client.logs
           ),
           "")


def test_default_mode_attempts_vehicle_events() -> None:
    client = FakeClient()
    provider = FakeProvider(fail_on_events=True)
    conn = FakeConnection()

    try:
        with PatchJob(provider, conn):
            job.run(client, "run-default", _base_params())
        _check("default mode raises after attempted test event fetch", False)
    except TelematicsProviderSafetyError as exc:
        _check("default mode attempts fleet /vehicles/events",
               provider.vehicle_events_fleet_calls == 1,
               f"code={exc.code}, event_calls={provider.vehicle_events_fleet_calls}")
        _check("default mode fetches trips before events",
               provider.trips_calls == 1,
               f"trips_calls={provider.trips_calls}")


def test_source_mismatch_skips_event_fetch_and_metric_writes() -> None:
    client = FakeClient()
    provider = FakeProvider(fail_on_events=True)
    conn = FakeConnection()

    with PatchJob(provider, conn, source=TRIP_METRICS_SOURCE_REPORT_207):
        job.run(client, "run-source-mismatch", _base_params())

    _check("source mismatch fetches trips",
           provider.trips_calls == 1,
           f"trips_calls={provider.trips_calls}")
    _check("source mismatch does not call fleet /vehicles/events",
           provider.vehicle_events_fleet_calls == 0,
           f"vehicle_events_fleet_calls={provider.vehicle_events_fleet_calls}")
    _check("source mismatch still upserts non-metric trip row",
           conn.committed and len(conn.trip_upsert_rows) == 1,
           f"committed={conn.committed}, trip_rows={len(conn.trip_upsert_rows)}")
    row = conn.trip_upsert_rows[0]
    # 36 non-metric values before M4, plus `first_seen_request_id`, which the
    # job appends to the tail of every row regardless of metric ownership. The
    # count is a proxy for "the five metric values are absent"; the SQL check
    # immediately below states that directly and is what actually pins it.
    _check("source mismatch omits metric values from trip row",
           len(row) == 37,
           f"row_len={len(row)} row={row!r}")
    upsert_sql = "\n".join(sql for sql, _rows in conn.executemany_calls if sql.strip().upper().startswith("INSERT INTO"))
    _check("source mismatch omits metric columns from insert/update SQL",
           "high_rpm_events_count" not in upsert_sql
           and "overrev_events_count" not in upsert_sql
           and "speeding_140_160_count" not in upsert_sql
           and "speeding_160_170_count" not in upsert_sql
           and "speeding_170_plus_count" not in upsert_sql,
           upsert_sql)
    _check("source mismatch does not execute separate speeding bucket update",
           conn.bucket_update_rows == [],
           f"bucket_update_rows={conn.bucket_update_rows!r}")
    _check("source mismatch logs skip reason",
           any(entry["context"].get("skip_reason") == TRIP_METRICS_SOURCE_MISMATCH_REASON for entry in client.logs),
           f"contexts={[entry['context'] for entry in client.logs]!r}")


def main() -> int:
    test_disabled_mode_skips_events_and_upserts_zero_counts()
    test_default_mode_attempts_vehicle_events()
    test_source_mismatch_skips_event_fetch_and_metric_writes()
    if FAILURES:
        print(f"\nFAILED: {len(FAILURES)} checks")
        for failure in FAILURES:
            print(f" - {failure}")
        return 1
    print("\nAll event enrichment disabled regression checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
