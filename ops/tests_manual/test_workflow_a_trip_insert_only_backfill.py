#!/usr/bin/env python3
"""Manual tests for the scoped insert-only trip backfill job.

Run from the repository root:

    PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_workflow_a_trip_insert_only_backfill.py
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace


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
    for key, value in (attrs or {}).items():
        setattr(mod, key, value)
    sys.modules[name] = mod


_install_stub("psycopg")
_install_stub("psycopg.rows", attrs={"dict_row": object()})

from jobs.api.telematics import backfill_trips_insert_only as job  # noqa: E402
from jobs.api.telematics import registry  # noqa: E402


FAILURES: list[str] = []


def _check(label: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {label}")
    if detail:
        print(f"        {detail}")
    if not ok:
        FAILURES.append(label)


def _sample_trip(trip_id: int = 1001) -> dict:
    return {
        "trip_id": trip_id,
        "vehicle_id": "vehicle-1",
        "registration": "WX 1234",
        "chassis_number": "VIN1",
        "driver_name": "Jan",
        "driver_surname": "Kowalski",
        "driver_id": "driver-1",
        "identification_tag_id": "tag-1",
        "driver_tag_description": "TAG",
        "is_private": False,
        "start_timestamp": "2026-05-21T10:00:00Z",
        "end_timestamp": "2026-05-21T10:30:00Z",
        "start_location": "A",
        "end_location": "B",
        "start_coordinates": {"latitude": 52.1, "longitude": 21.0},
        "end_coordinates": {"latitude": 52.2, "longitude": 21.1},
        "start_odometer_value": 1000,
        "end_odometer_value": 1010,
        "trip_duration_seconds": 1800,
        "trip_distance": 10000,
        "harsh_braking_events": 2,
        "harsh_acceleration_events": 3,
        "harsh_cornering_events": 4,
        "events_idle": 1,
        "idle_time_seconds": 60,
    }


def _request(
    *,
    dry_run: bool = True,
    expected_insert_count: int | None = None,
    provider_trip_ids: list[int] | None = None,
) -> job.BackfillRequest:
    params = {
        "client_id": "client-1",
        "client_code": "TEST00001",
        "window_start_ts": "2026-05-20T02:00:00Z",
        "window_end_ts": "2026-05-28T02:00:00Z",
        "insert_only": True,
        "dry_run": dry_run,
    }
    if expected_insert_count is not None:
        params["expected_insert_count"] = expected_insert_count
    if provider_trip_ids is not None:
        params["provider_trip_ids"] = provider_trip_ids
    return job._parse_request(params)


def test_parameter_safety() -> None:
    parsed = _request()
    _check(
        "client and UTC interval are explicit",
        parsed.client_id == "client-1"
        and parsed.client_code == "TEST00001"
        and parsed.window_start_ts == datetime(2026, 5, 20, 2, tzinfo=timezone.utc),
    )
    _check("dry-run uses the safe insert-only contract", parsed.dry_run and parsed.insert_only)

    filtered = _request(provider_trip_ids=[1001, 1002])
    _check(
        "provider trip allowlist is parsed explicitly",
        filtered.provider_trip_ids == (1001, 1002),
    )

    unsafe = {
        "client_id": "client-1",
        "client_code": "TEST00001",
        "window_start_ts": "2026-05-20T02:00:00Z",
        "window_end_ts": "2026-05-28T02:00:00Z",
        "insert_only": True,
        "dry_run": False,
    }
    try:
        job._parse_request(unsafe)
    except ValueError as exc:
        _check("real run requires expected_insert_count", "expected_insert_count" in str(exc), str(exc))
    else:
        _check("real run requires expected_insert_count", False)

    unsafe["dry_run"] = True
    unsafe["overwrite_existing"] = True
    try:
        job._parse_request(unsafe)
    except ValueError as exc:
        _check("overwrite_existing=true is refused", "forbidden" in str(exc), str(exc))
    else:
        _check("overwrite_existing=true is refused", False)

    unsafe["overwrite_existing"] = False
    unsafe["provider_trip_ids"] = [1001, 1001]
    try:
        job._parse_request(unsafe)
    except ValueError as exc:
        _check("duplicate provider trip IDs are refused", "duplicates" in str(exc), str(exc))
    else:
        _check("duplicate provider trip IDs are refused", False)


def test_parse_dedup_and_mapping() -> None:
    bad = {"trip_id": "bad", "registration": "WX 9999"}
    parsed, rejected, duplicate_rows, duplicate_ids = job._parse_raw_trips(
        [_sample_trip(), _sample_trip(), bad]
    )
    _check(
        "duplicate provider trip rows are deduplicated",
        len(parsed) == 1 and duplicate_rows == 1 and duplicate_ids == 1,
    )
    _check("invalid provider rows are counted by reason", rejected["invalid_trip_id"] == 1)

    prepared = job._prepare_trip_rows(
        parsed_trips=parsed,
        request=_request(),
        run_id="run-1",
        synced_at=datetime(2026, 6, 8, tzinfo=timezone.utc),
        vehicle_rows=[{
            "vehicle_id": "vehicle-1",
            "registration": "WX 1234",
            "vehicle_name": "Truck 1",
            "vehicle_description": "Alpha",
        }],
        driver_rows=[{
            "driver_id": "driver-1",
            "license_driver_restrictions": "B",
        }],
    )
    values = prepared[0].values
    _check(
        "normal vehicle and driver metadata helpers are reused",
        values[5] == "Truck 1" and values[6] == "Alpha" and values[12] == "B",
    )
    _check(
        "new rows initialize event counters without touching existing rows",
        values[28:30] == (0, 0) and values[35:38] == (0, 0, 0),
    )


class FakeCursor:
    def __init__(self, existing_ids: set[int] | None = None) -> None:
        self.existing_ids = existing_ids or set()
        self.executed: list[tuple[str, object]] = []
        self.executemany_calls: list[tuple[str, list[tuple]]] = []
        self._rows: list[tuple] = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql: str, params=None) -> None:
        self.executed.append((sql, params))
        if "SELECT provider_trip_id" in sql:
            requested = set(params[1])
            self._rows = [(trip_id,) for trip_id in sorted(requested & self.existing_ids)]
        elif "SELECT registration" in sql:
            self._rows = []
        else:
            self._rows = []

    def fetchall(self):
        return list(self._rows)

    def executemany(self, sql: str, rows) -> None:
        materialized = list(rows)
        self.executemany_calls.append((sql, materialized))
        self.rowcount = len(materialized)


class FakeConnection:
    def __init__(self, existing_ids: set[int] | None = None) -> None:
        self.cursor_obj = FakeCursor(existing_ids)
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return self.cursor_obj

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


class FakeClient:
    def __init__(self) -> None:
        self.logs: list[dict] = []

    def log(self, level, kind, source, message, *, run_id, context):
        self.logs.append({
            "level": level,
            "kind": kind,
            "source": source,
            "message": message,
            "run_id": run_id,
            "context": context,
        })


class FakeProvider:
    last_instance: "FakeProvider | None" = None

    def __init__(self, **kwargs) -> None:
        self.trip_calls: list[tuple[datetime, datetime, bool]] = []
        self.init_kwargs = dict(kwargs)
        FakeProvider.last_instance = self

    def fetch_trips(self, *, window_start_ts, window_end_ts, incl_private):
        self.trip_calls.append((window_start_ts, window_end_ts, incl_private))
        return [_sample_trip()]

    def fetch_vehicles_fleet(self, **_kwargs):
        return []

    def fetch_drivers_fleet(self, **_kwargs):
        return []


def _cfg():
    return SimpleNamespace(
        client_id="client-1",
        client_code="TEST00001",
        provider_base_url="https://provider.invalid",
        provider_basic_auth_username="user",
        provider_basic_auth_password_secret_ref="PROVIDER_SECRET",
        client_db_host="127.0.0.1",
        client_db_port=5432,
        client_db_name="test_main",
        client_db_user="test",
        client_db_password_secret_ref="DB_SECRET",
        client_db_schema="public",
        # The control-plane row carries the client's frozen `/trips`
        # pagination mode. The recovery job must hand it to the provider
        # client: without it `normalize_trips_pagination_mode(None)` selects
        # `strict_meta`, and a recovery for a `data_invariants_v1` client
        # aborts with PAGINATION_MISMATCH on the first sub-window, after the
        # whole provider fetch has already been paid for.
        trips_pagination_mode="data_invariants_v1",
    )


def _run_with_fakes(params: dict, *, existing_ids: set[int] | None = None):
    fake_client = FakeClient()
    fake_connection = FakeConnection(existing_ids)
    original = {
        "load_client_account_config": job.load_client_account_config,
        "resolve_secret": job.resolve_secret,
        "provider": job.TelematicsFleetProviderClient,
        "db_conn": job.trip_sync._client_business_pg_conn,
    }
    try:
        job.load_client_account_config = lambda **_kwargs: _cfg()
        job.resolve_secret = lambda _ref: "secret"
        job.TelematicsFleetProviderClient = FakeProvider
        job.trip_sync._client_business_pg_conn = lambda _cfg_value: fake_connection
        job.run(fake_client, "run-1", params)
    finally:
        job.load_client_account_config = original["load_client_account_config"]
        job.resolve_secret = original["resolve_secret"]
        job.TelematicsFleetProviderClient = original["provider"]
        job.trip_sync._client_business_pg_conn = original["db_conn"]
    return fake_client, fake_connection


def test_dry_run_and_scope() -> None:
    params = {
        "client_id": "client-1",
        "client_code": "TEST00001",
        "window_start_ts": "2026-05-20T02:00:00Z",
        "window_end_ts": "2026-05-28T02:00:00Z",
        "insert_only": True,
        "dry_run": True,
        "chunk_days": 5,
    }
    client, conn = _run_with_fakes(params)
    _check("dry-run performs no INSERT", not conn.cursor_obj.executemany_calls)
    _check("dry-run performs no commit", conn.commits == 0)
    _check(
        "dry-run uses a read-only transaction",
        any("SET TRANSACTION READ ONLY" in sql for sql, _params in conn.cursor_obj.executed),
    )
    preflight = next(
        log["context"] for log in client.logs
        if log["message"] == "Insert-only trip backfill preflight result"
    )
    windows = preflight["provider_request_windows"]
    _check(
        "provider fetch is scoped to the requested UTC interval",
        windows[0]["chunk_start_ts"] == "2026-05-20T02:00:00+00:00"
        and windows[-1]["chunk_end_ts"] == "2026-05-28T02:00:00+00:00",
    )
    _check("preflight reports one candidate insert", preflight["rows_would_insert"] == 1)
    _check(
        "preflight reports exact candidate provider trip IDs",
        preflight["candidate_provider_trip_ids"] == [1001],
    )
    _check(
        "the client's pagination mode is passed to the provider client",
        (FakeProvider.last_instance.init_kwargs.get("trips_pagination_mode")
         == "data_invariants_v1"),
        f"init_kwargs={FakeProvider.last_instance.init_kwargs!r}",
    )
    preflight_start = next(
        log["context"] for log in client.logs
        if log["message"] == "Starting scoped insert-only trip backfill preflight"
    )
    _check(
        "the pagination mode is recorded in the preflight evidence",
        preflight_start.get("trips_pagination_mode") == "data_invariants_v1",
        f"context={preflight_start!r}",
    )

    params["provider_trip_ids"] = [9999]
    filtered_client, filtered_conn = _run_with_fakes(params)
    filtered_preflight = next(
        log["context"] for log in filtered_client.logs
        if log["message"] == "Insert-only trip backfill preflight result"
    )
    _check(
        "provider trip allowlist removes unrelated fetched trips",
        filtered_preflight["rows_would_insert"] == 0
        and filtered_preflight["rows_filtered_by_provider_trip_ids"] == 1
        and not filtered_conn.cursor_obj.executemany_calls,
    )


def test_insert_only_sql_and_idempotence() -> None:
    parsed, _rejected, _duplicate_rows, _duplicate_ids = job._parse_raw_trips([_sample_trip()])
    prepared = job._prepare_trip_rows(
        parsed_trips=parsed,
        request=_request(dry_run=False, expected_insert_count=1),
        run_id="run-1",
        synced_at=datetime(2026, 6, 8, tzinfo=timezone.utc),
        vehicle_rows=[],
        driver_rows=[],
    )
    cursor = FakeCursor()
    inserted = job._insert_prepared_rows(cursor, trips_table="public.client_trips", rows=prepared)
    sql = cursor.executemany_calls[0][0]
    _check(
        "insert SQL is conflict-skip only",
        "ON CONFLICT (client_id, provider_trip_id) DO NOTHING" in sql and "DO UPDATE" not in sql,
    )
    _check(
        "insert SQL cannot reset existing speeding counters",
        "UPDATE public.client_trips" not in sql and inserted == 1,
    )

    params = {
        "client_id": "client-1",
        "client_code": "TEST00001",
        "window_start_ts": "2026-05-20T02:00:00Z",
        "window_end_ts": "2026-05-28T02:00:00Z",
        "insert_only": True,
        "dry_run": False,
        "expected_insert_count": 0,
        "chunk_days": 5,
    }
    _client, conn = _run_with_fakes(params, existing_ids={1001})
    _check(
        "rerun with all keys present performs no INSERT",
        not conn.cursor_obj.executemany_calls and conn.commits == 1,
    )


def test_scheduled_sync_unchanged() -> None:
    _check(
        "maintenance job is not dispatcher-registered",
        "trip_insert_only_backfill" not in registry.DATASETS
        and "backfill_trips_insert_only" not in registry.DATASETS,
    )
    sync_source = (REPO_ROOT / "jobs/api/telematics/sync_trips_and_speeding.py").read_text()
    _check(
        "scheduled sync still reads schedule overwrite_existing",
        "overwrite_existing = schedule.overwrite_existing" in sync_source,
    )


def main() -> int:
    test_parameter_safety()
    test_parse_dedup_and_mapping()
    test_dry_run_and_scope()
    test_insert_only_sql_and_idempotence()
    test_scheduled_sync_unchanged()
    if FAILURES:
        print("\nFailures:")
        for failure in FAILURES:
            print(f"- {failure}")
        return 1
    print("\nAll insert-only trip backfill manual tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
