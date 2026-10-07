#!/usr/bin/env python3
"""Client Trips global 2,000 km provider ingestion cap.

Proves the owner/business invariant:

    provider distance <= 2_000_000 m  -> normal Client Trips persistence
    provider distance >  2_000_000 m  -> discarded before `client_trips` persistence

Pure: stdlib only, no network, no database, no secrets. The two production
Client Trips ingestion paths are exercised through their real code with stubbed
provider and DB layers, so the parse gate, the persistence boundary and the
actual INSERT parameter lists are all observed for real.

What this proves, in order:

  1. Boundary semantics of the shared admission module, including the exact
     2,000,000 m accept / 2,000,001 m reject asymmetry.
  2. Missing / NULL / uninterpretable distance keeps its existing behaviour: the
     cap has nothing to compare and never rejects on absence.
  3. `sync_trips_and_speeding` (scheduled / incremental / reconciliation /
     manual-recovery path) never binds an over-cap trip to the `client_trips`
     upsert, and reports a bounded rejection counter.
  4. The persistence boundary in the same job still refuses an over-cap trip
     when the parse gate is made to leak — the future-job regression guard.
  5. `backfill_trips_insert_only` rejects at parse time and its single
     `client_trips` INSERT refuses an over-cap prepared row built by any route.
  6. Structurally, every non-test module in the repository that writes
     `client_trips` goes through the shared admission module.

Run from repo root:

    PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_client_trips_distance_cap.py
"""
from __future__ import annotations

import ast
import re
import sys
import types
from datetime import datetime, time, timezone
from decimal import Decimal
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
    for key, value in (attrs or {}).items():
        setattr(mod, key, value)
    sys.modules[name] = mod


_install_stub("requests")
_install_stub("psycopg")
_install_stub("psycopg.rows", attrs={"dict_row": object()})

from jobs.api.telematics import backfill_trips_insert_only as backfill  # noqa: E402
from jobs.api.telematics import client_trips_admission as admission  # noqa: E402
from jobs.api.telematics import sync_trips_and_speeding as sync  # noqa: E402
from jobs.api.telematics.control_plane import ClientAccountConfig, DatasetSchedule  # noqa: E402
from jobs.trip_metrics_population_source import (  # noqa: E402
    TRIP_METRICS_SOURCE_API,
    TRIP_METRICS_SOURCE_REPORT_207,
)


FAILURES: list[str] = []

CAP = 2_000_000
# Column position of `trip_distance_meters` inside an upsert row tuple. Both
# jobs share the same leading column order, so one index serves both.
DISTANCE_INDEX = backfill.TRIP_INSERT_COLUMNS.index("trip_distance_meters")
PROVIDER_TRIP_ID_INDEX = backfill.TRIP_INSERT_COLUMNS.index("provider_trip_id")


def _check(label: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {label}")
    if detail:
        print(f"        {detail}")
    if not ok:
        FAILURES.append(label)


# ---------------------------------------------------------------------------
# 1. Shared admission module — boundary and unit semantics.
# ---------------------------------------------------------------------------

def test_admission_boundary() -> None:
    _check("the cap constant is 2,000 km expressed in metres",
           admission.MAX_TRIP_DISTANCE_METERS == 2_000_000,
           f"MAX_TRIP_DISTANCE_METERS={admission.MAX_TRIP_DISTANCE_METERS}")

    _check("1,999,999 m is admitted", not admission.distance_exceeds_cap(1_999_999))
    _check("2,000,000 m exactly is admitted", not admission.distance_exceeds_cap(2_000_000))
    _check("2,000,001 m is rejected", admission.distance_exceeds_cap(2_000_001))
    _check("the clearly excessive 89,473 km value is rejected",
           admission.distance_exceeds_cap(89_472_993))

    _check("the rule is `> cap`, never `>= cap`",
           admission.evaluate_distance(CAP).admitted
           and not admission.evaluate_distance(CAP + 1).admitted)

    # Numeric forms the provider payload has been seen to use.
    _check("a numeric string over the cap is rejected", admission.distance_exceeds_cap("2000001"))
    _check("a numeric string at the cap is admitted", not admission.distance_exceeds_cap("2000000"))
    _check("a float over the cap is rejected", admission.distance_exceeds_cap(2_000_000.5))
    _check("a Decimal over the cap is rejected", admission.distance_exceeds_cap(Decimal("2000001")))

    verdict = admission.evaluate_provider_trip({"trip_distance": 2_000_001})
    _check("a rejected trip carries the stable reason and its distance",
           not verdict.admitted
           and verdict.reason == admission.REASON_DISTANCE_OVER_CAP
           and verdict.distance_meters == Decimal(2_000_001),
           f"verdict={verdict!r}")


def test_missing_distance_semantics_unchanged() -> None:
    for label, value in (
        ("absent", None),
        ("empty string", ""),
        ("whitespace", "   "),
        ("uninterpretable text", "n/a"),
        ("NaN", float("nan")),
    ):
        _check(f"a {label} distance is not rejected by the cap",
               not admission.distance_exceeds_cap(value))
        _check(f"a {label} distance yields no comparable value",
               admission.coerce_distance_meters(value) is None)

    _check("a trip with no distance key at all is admitted",
           admission.evaluate_provider_trip({"trip_id": 1}).admitted)
    _check("zero distance keeps its existing accepted behaviour",
           not admission.distance_exceeds_cap(0))
    _check("a negative distance is not this rule's business",
           not admission.distance_exceeds_cap(-1))


def test_admission_gate_observability() -> None:
    gate = admission.ClientTripsAdmission()
    for trip_id, distance in ((1, 2_000_001), (2, 89_472_993), (3, 1_000), (4, None)):
        gate.admits_provider_trip({"trip_distance": distance}, provider_trip_id=trip_id)
    summary = gate.summary()
    _check("the gate counts only over-cap rejections",
           summary[admission.REJECTED_COUNTER_KEY] == 2, f"summary={summary}")
    _check("the gate names the canonical counter key",
           admission.REJECTED_COUNTER_KEY == "client_trips_rejected_distance_over_2000km")
    _check("the gate reports the largest rejected distance",
           summary[admission.REJECTED_MAX_DISTANCE_KEY] == 89_472_993, f"summary={summary}")
    _check("the gate keeps a bounded, PII-free trip-id sample",
           summary[admission.REJECTED_SAMPLE_KEY] == [1, 2], f"summary={summary}")

    bounded = admission.ClientTripsAdmission(sample_limit=2)
    for trip_id in range(10):
        bounded.admits_distance(CAP + 1, provider_trip_id=trip_id)
    _check("the sample is capped while the counter keeps counting",
           bounded.rejected_distance_over_cap == 10
           and len(bounded.summary()[admission.REJECTED_SAMPLE_KEY]) == 2)


# ---------------------------------------------------------------------------
# 2. `sync_trips_and_speeding` — the scheduled/incremental/recovery path.
# ---------------------------------------------------------------------------

CLIENT_ID = "bd7662a5-eeb4-4614-8720-d477abfcb227"

TRIPS_UNDER_CAP = 1001
TRIPS_AT_CAP = 1002
TRIPS_OVER_CAP = 1003
TRIPS_ABSURD = 1004
TRIPS_NO_DISTANCE_KEY = 1005
TRIPS_NULL_DISTANCE = 1006


def _provider_trip(trip_id: int, distance) -> dict:
    trip = {
        "trip_id": trip_id,
        "vehicle_id": "veh-1",
        "registration": "ABC123",
        "start_timestamp": "2026-04-01 08:00:00",
        "end_timestamp": "2026-04-01 09:00:00",
        "start_location": "Depot",
        "end_location": "Client",
        "start_coordinates": {"latitude": 52.0, "longitude": 21.0},
        "end_coordinates": {"latitude": 52.1, "longitude": 21.1},
        "trip_duration_seconds": 3600,
        "driver_name": "Ann",
        "driver_surname": "Driver",
        "driver_id": "DRV-1",
        "is_private": False,
    }
    if distance is not _ABSENT:
        trip["trip_distance"] = distance
    return trip


class _Absent:
    pass


_ABSENT = _Absent()


def _provider_trips() -> list[dict]:
    return [
        _provider_trip(TRIPS_UNDER_CAP, CAP - 1),
        _provider_trip(TRIPS_AT_CAP, CAP),
        _provider_trip(TRIPS_OVER_CAP, CAP + 1),
        _provider_trip(TRIPS_ABSURD, 89_472_993),
        _provider_trip(TRIPS_NO_DISTANCE_KEY, _ABSENT),
        _provider_trip(TRIPS_NULL_DISTANCE, None),
    ]


class FakeClient:
    def __init__(self) -> None:
        self.logs: list[dict] = []

    def log(self, level, kind, source, message, *, run_id=None, context=None, error=None) -> None:
        self.logs.append({
            "level": level, "message": message, "context": context or {},
        })

    def upload_artifact(self, *args, **kwargs):
        raise AssertionError("this test must not upload artifacts")


class FakeProvider:
    def __init__(self) -> None:
        self.page_limit = 200

    def fetch_trips(self, **kwargs):
        return _provider_trips()

    def metrics_snapshot(self):
        return {
            "total_requests": 0,
            "request_count_by_endpoint": {},
            "response_parse_count_by_endpoint": {},
            "request_elapsed_seconds_by_endpoint": {},
            "response_parse_elapsed_seconds_by_endpoint": {},
        }

    def fetch_vehicles_fleet(self, **kwargs):
        return [{
            "vehicle_id": "veh-1",
            "registration": "ABC123",
            "vehicle_name": "Truck 1",
            "vehicle_description": "Fleet truck",
            "chassis_number": "VIN123",
        }]

    def fetch_drivers_fleet(self, **kwargs):
        return [{
            "driver_id": "DRV-1",
            "first_name": "Ann",
            "last_name": "Driver",
            "license_driver_restrictions": "Weekdays only",
        }]

    def fetch_vehicle_events_fleet(self, **kwargs):
        raise AssertionError("event enrichment is disabled in this test")

    def fetch_vehicle_events_registration(self, **kwargs):
        raise AssertionError("event enrichment is disabled in this test")


class FakeCursor:
    def __init__(self, conn) -> None:
        self.conn = conn
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None) -> None:
        self.conn.execute_calls.append((sql, params))

    def executemany(self, sql, rows) -> None:
        copied = list(rows)
        self.conn.executemany_calls.append((sql, copied))
        self.rowcount = len(copied)
        if sql.strip().upper().startswith("INSERT INTO") and "client_trips" in sql:
            self.conn.trip_upsert_rows.extend(copied)

    def fetchall(self):
        return []


class FakeConnection:
    def __init__(self) -> None:
        self.execute_calls: list[tuple] = []
        self.executemany_calls: list[tuple] = []
        self.trip_upsert_rows: list[tuple] = []
        self.committed = False

    def cursor(self):
        return FakeCursor(self)

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


def _fake_cfg(metrics_source: str = TRIP_METRICS_SOURCE_API) -> ClientAccountConfig:
    return ClientAccountConfig(
        client_id=CLIENT_ID,
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
        trip_metrics_population_source=metrics_source,
        trips_pagination_mode="strict_meta",
        trips_stabilization_delay_seconds=10800,
        trips_overlap_seconds=3600,
        trips_max_recovery_span_seconds=2678400,
    )


def _fake_schedule() -> DatasetSchedule:
    return DatasetSchedule(
        exists=True,
        client_id=CLIENT_ID,
        dataset_name=sync.DATASET_NAME,
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


class PatchSyncJob:
    def __init__(self, provider: FakeProvider, conn: FakeConnection,
                 *, metrics_source: str = TRIP_METRICS_SOURCE_API) -> None:
        self.provider = provider
        self.conn = conn
        self.metrics_source = metrics_source
        self.originals: dict = {}

    def __enter__(self):
        replacements = {
            "load_client_account_config": lambda *, client_id: _fake_cfg(self.metrics_source),
            "load_dataset_schedule": lambda *, client_id, dataset_name: _fake_schedule(),
            "resolve_secret": lambda ref: "secret",
            "TelematicsFleetProviderClient": lambda **kwargs: self.provider,
            "_client_business_pg_conn": lambda cfg: self.conn,
        }
        for name, value in replacements.items():
            self.originals[name] = getattr(sync, name)
            setattr(sync, name, value)
        return self

    def __exit__(self, exc_type, exc, tb):
        for name, value in self.originals.items():
            setattr(sync, name, value)
        return False


def _sync_params() -> dict:
    return {
        "client_id": CLIENT_ID,
        "window_start_ts": "2026-04-01 00:00:00",
        "window_end_ts": "2026-04-02 00:00:00",
        "event_enrichment_mode": "disabled",
    }


def _run_sync(run_id: str, *, metrics_source: str = TRIP_METRICS_SOURCE_API
              ) -> tuple[FakeClient, FakeConnection]:
    client = FakeClient()
    conn = FakeConnection()
    with PatchSyncJob(FakeProvider(), conn, metrics_source=metrics_source):
        sync.run(client, run_id, _sync_params())
    return client, conn


def _upserted_ids(conn: FakeConnection) -> list[int]:
    return [int(row[PROVIDER_TRIP_ID_INDEX]) for row in conn.trip_upsert_rows]


def _final_diagnostics(client: FakeClient) -> dict:
    for entry in reversed(client.logs):
        if admission.REJECTED_COUNTER_KEY in entry["context"]:
            return entry["context"]
    return {}


def test_sync_discards_over_cap_trips() -> None:
    client, conn = _run_sync("run-cap")

    ids = _upserted_ids(conn)
    _check("the sync job reaches the client_trips upsert and commits",
           conn.committed and conn.trip_upsert_rows,
           f"committed={conn.committed}, rows={len(conn.trip_upsert_rows)}")
    _check("1,999,999 m is persisted", TRIPS_UNDER_CAP in ids, f"ids={ids}")
    _check("2,000,000 m exactly is persisted", TRIPS_AT_CAP in ids, f"ids={ids}")
    _check("2,000,001 m never reaches the client_trips upsert",
           TRIPS_OVER_CAP not in ids, f"ids={ids}")
    _check("the 89,473 km trip never reaches the client_trips upsert",
           TRIPS_ABSURD not in ids, f"ids={ids}")
    _check("a trip with no distance field still persists (NULL preserved)",
           TRIPS_NO_DISTANCE_KEY in ids, f"ids={ids}")
    _check("a trip with an explicit NULL distance still persists",
           TRIPS_NULL_DISTANCE in ids, f"ids={ids}")

    null_rows = [
        row for row in conn.trip_upsert_rows
        if int(row[PROVIDER_TRIP_ID_INDEX]) in (TRIPS_NO_DISTANCE_KEY, TRIPS_NULL_DISTANCE)
    ]
    _check("NULL-distance rows still bind NULL, not a substituted value",
           len(null_rows) == 2 and all(row[DISTANCE_INDEX] is None for row in null_rows),
           f"values={[row[DISTANCE_INDEX] for row in null_rows]}")

    bound_distances = [row[DISTANCE_INDEX] for row in conn.trip_upsert_rows]
    _check("no bound trip_distance_meters value exceeds the cap",
           all(v is None or int(v) <= CAP for v in bound_distances),
           f"bound={bound_distances}")

    insert_sql = [
        sql for sql, _rows in conn.executemany_calls
        if sql.strip().upper().startswith("INSERT INTO") and "client_trips" in sql
    ]
    over_cap_in_parameters = any(
        int(row[PROVIDER_TRIP_ID_INDEX]) in (TRIPS_OVER_CAP, TRIPS_ABSURD)
        for _sql, rows in conn.executemany_calls
        for row in rows
        if isinstance(row, tuple) and len(row) > PROVIDER_TRIP_ID_INDEX
    )
    _check("the client_trips INSERT statement ran for the admitted trips only",
           bool(insert_sql) and not over_cap_in_parameters,
           f"insert_statements={len(insert_sql)}, over_cap_bound={over_cap_in_parameters}")

    diagnostics = _final_diagnostics(client)
    _check("the run reports two rejections under the canonical counter key",
           diagnostics.get(admission.REJECTED_COUNTER_KEY) == 2,
           f"diagnostics={ {k: v for k, v in diagnostics.items() if 'reject' in k} }")
    _check("nothing was blocked at the persistence boundary in normal operation",
           diagnostics.get("client_trips_rejected_distance_over_2000km_at_persistence") == 0,
           f"diagnostics={ {k: v for k, v in diagnostics.items() if 'reject' in k} }")
    _check("rejected trips are not counted as malformed parse errors",
           diagnostics.get("trips_rows_other_parse_error") == 0
           and diagnostics.get("trips_rows_malformed_trip_id") == 0)
    _check("four provider trips remained parseable ingestion candidates",
           diagnostics.get("trips_rows_parsed") == 4
           and diagnostics.get("trips_provider_rows_fetched") == 6,
           f"parsed={diagnostics.get('trips_rows_parsed')}")

    warnings = [
        entry for entry in client.logs
        if entry["message"].startswith("Client Trips admission discarded")
    ]
    _check("rejections are reported as ONE bounded aggregate, not per trip",
           len(warnings) == 1, f"aggregate_logs={len(warnings)}")
    if warnings:
        context = warnings[0]["context"]
        _check("the aggregate names the run's rejection count and cap",
               context.get(admission.REJECTED_COUNTER_KEY) == 2
               and context.get("max_trip_distance_meters") == CAP,
               f"context={context}")
        _check("the aggregate identifies which trips were dropped",
               sorted(context.get(admission.REJECTED_SAMPLE_KEY, []))
               == [TRIPS_OVER_CAP, TRIPS_ABSURD],
               f"sample={context.get(admission.REJECTED_SAMPLE_KEY)}")


def test_sync_persistence_boundary_holds_when_the_parse_gate_leaks() -> None:
    """Future-job regression guard.

    Simulates an edit that lets an over-cap trip past the early parse gate. The
    persistence boundary inside the row-preparation loop must still refuse it,
    so no future change between parsing and the upsert can reintroduce a row the
    invariant forbids.
    """

    class LeakyGate(admission.ClientTripsAdmission):
        def admits_provider_trip(self, trip, *, provider_trip_id=None):  # type: ignore[override]
            return True

    original = admission.ClientTripsAdmission
    admission.ClientTripsAdmission = LeakyGate  # type: ignore[assignment]
    try:
        client, conn = _run_sync("run-leaky-parse-gate")
    finally:
        admission.ClientTripsAdmission = original  # type: ignore[assignment]

    ids = _upserted_ids(conn)
    _check("a leaked over-cap trip is still refused at the persistence boundary",
           TRIPS_OVER_CAP not in ids and TRIPS_ABSURD not in ids, f"ids={ids}")
    _check("the admitted trips are unaffected by the leak",
           TRIPS_UNDER_CAP in ids and TRIPS_AT_CAP in ids, f"ids={ids}")

    diagnostics = _final_diagnostics(client)
    _check("the persistence-boundary counter records what the parse gate missed",
           diagnostics.get("client_trips_rejected_distance_over_2000km_at_persistence") == 2,
           f"diagnostics={ {k: v for k, v in diagnostics.items() if 'reject' in k} }")


def test_sync_non_api_metric_variant_enforces_the_same_row_layout() -> None:
    """The narrower row shape (`api_owns_trip_metrics=False`) must enforce it too.

    That variant omits the event-metric and speeding columns from the INSERT, so
    it produces a different row tuple. The cap is applied by row position, so the
    invariant has to hold for this shape as well.
    """
    client, conn = _run_sync("run-report-207-source",
                             metrics_source=TRIP_METRICS_SOURCE_REPORT_207)

    ids = _upserted_ids(conn)
    _check("the narrower metric variant still upserts the admitted trips",
           conn.committed and TRIPS_UNDER_CAP in ids and TRIPS_AT_CAP in ids,
           f"ids={ids}")
    _check("the narrower metric variant discards the over-cap trips",
           TRIPS_OVER_CAP not in ids and TRIPS_ABSURD not in ids, f"ids={ids}")

    distances = {
        int(row[PROVIDER_TRIP_ID_INDEX]): row[DISTANCE_INDEX]
        for row in conn.trip_upsert_rows
    }
    _check("trip_distance_meters sits at the same row position in this variant",
           distances.get(TRIPS_UNDER_CAP) == CAP - 1
           and distances.get(TRIPS_AT_CAP) == CAP
           and distances.get(TRIPS_NULL_DISTANCE) is None,
           f"distances={distances}")


# ---------------------------------------------------------------------------
# 3. `backfill_trips_insert_only` — the manual repair path.
# ---------------------------------------------------------------------------

def _backfill_raw(trip_id: int, distance) -> dict:
    raw = {
        "trip_id": trip_id,
        "registration": "ABC123",
        "start_timestamp": "2026-05-21T10:00:00Z",
        "end_timestamp": "2026-05-21T10:30:00Z",
        "trip_duration_seconds": 1800,
        "is_private": False,
    }
    if distance is not _ABSENT:
        raw["trip_distance"] = distance
    return raw


def test_backfill_parse_rejects_over_cap() -> None:
    rows = [
        _backfill_raw(TRIPS_UNDER_CAP, CAP - 1),
        _backfill_raw(TRIPS_AT_CAP, CAP),
        _backfill_raw(TRIPS_OVER_CAP, CAP + 1),
        _backfill_raw(TRIPS_ABSURD, 89_472_993),
        _backfill_raw(TRIPS_NO_DISTANCE_KEY, _ABSENT),
    ]
    parsed, rejected, _duplicate_rows, _duplicate_ids = backfill._parse_raw_trips(rows)
    parsed_ids = [trip["provider_trip_id"] for trip in parsed]

    _check("backfill keeps 1,999,999 m and 2,000,000 m",
           TRIPS_UNDER_CAP in parsed_ids and TRIPS_AT_CAP in parsed_ids, f"parsed={parsed_ids}")
    _check("backfill drops 2,000,001 m and the 89,473 km trip",
           TRIPS_OVER_CAP not in parsed_ids and TRIPS_ABSURD not in parsed_ids,
           f"parsed={parsed_ids}")
    _check("backfill preserves a missing-distance trip",
           TRIPS_NO_DISTANCE_KEY in parsed_ids, f"parsed={parsed_ids}")
    _check("backfill reports the rejections under the shared reason",
           rejected[admission.REASON_DISTANCE_OVER_CAP] == 2, f"rejected={dict(rejected)}")


def test_backfill_insert_boundary_refuses_over_cap_rows() -> None:
    def _prepared(trip_id: int, distance) -> backfill.PreparedTrip:
        values = ["x"] * len(backfill.TRIP_INSERT_COLUMNS)
        values[PROVIDER_TRIP_ID_INDEX] = trip_id
        values[DISTANCE_INDEX] = distance
        return backfill.PreparedTrip(
            provider_trip_id=trip_id,
            registration="ABC123",
            start_ts=datetime(2026, 5, 21, 10, tzinfo=timezone.utc),
            end_ts=datetime(2026, 5, 21, 10, 30, tzinfo=timezone.utc),
            values=tuple(values),
        )

    class Cursor:
        def __init__(self) -> None:
            self.calls: list[tuple] = []
            self.rowcount = 0

        def executemany(self, sql, rows) -> None:
            copied = list(rows)
            self.calls.append((sql, copied))
            self.rowcount = len(copied)

    cur = Cursor()
    inserted = backfill._insert_prepared_rows(
        cur,
        trips_table="public.client_trips",
        rows=[
            _prepared(TRIPS_AT_CAP, CAP),
            _prepared(TRIPS_OVER_CAP, CAP + 1),
            _prepared(TRIPS_NULL_DISTANCE, None),
        ],
    )
    bound_ids = [row[PROVIDER_TRIP_ID_INDEX] for _sql, rows in cur.calls for row in rows]
    _check("the backfill INSERT binds only admissible rows",
           bound_ids == [TRIPS_AT_CAP, TRIPS_NULL_DISTANCE] and inserted == 2,
           f"bound_ids={bound_ids}, inserted={inserted}")

    only_over_cap = Cursor()
    inserted_none = backfill._insert_prepared_rows(
        only_over_cap,
        trips_table="public.client_trips",
        rows=[_prepared(TRIPS_OVER_CAP, CAP + 1)],
    )
    _check("an all-over-cap batch issues no client_trips INSERT at all",
           only_over_cap.calls == [] and inserted_none == 0,
           f"calls={len(only_over_cap.calls)}, inserted={inserted_none}")


# ---------------------------------------------------------------------------
# 4. Structural contract — no production module may write client_trips except
#    through the sanctioned admission primitive.
# ---------------------------------------------------------------------------

CLIENT_TRIPS_INSERT = re.compile(
    r"INSERT\s+INTO\s+(?:\{[A-Za-z0-9_]*trips_table\}|[^\s(]*client_trips)",
    re.IGNORECASE,
)

# Cursor methods that hand SQL straight to the database.
RAW_EXECUTORS = {"execute", "executemany", "executescript", "copy"}

SANCTIONED_WRITE = "execute_client_trips_insert"


def _static_text(node: ast.AST, names: dict[str, str]) -> str:
    """Best-effort literal reconstruction of a SQL expression.

    Handles the shapes this repository actually uses to build statements: string
    constants, f-strings, `a + b` concatenation, and names bound to any of those.
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else ""
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                parts.append("{" + ast.unparse(value.value) + "}")
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _static_text(node.left, names) + _static_text(node.right, names)
    if isinstance(node, ast.Name):
        return names.get(node.id, "")
    return ""


def _string_bindings(tree: ast.AST) -> dict[str, str]:
    """Every name bound anywhere in the module to statically known SQL text."""
    names: dict[str, str] = {}
    # Two passes so a name assigned after its use site is still resolvable.
    for _ in range(2):
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                text = _static_text(node.value, names)
                if not text:
                    continue
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        names[target.id] = text
    return names


def audit_client_trips_writes(source: str) -> tuple[bool, list[str]]:
    """Return `(is_client_trips_writer, violations)` for one module's source.

    A violation is a `client_trips` INSERT handed straight to a cursor, or a
    writer that never calls the sanctioned admission primitive at all. Importing
    `client_trips_admission` is explicitly NOT sufficient to pass.
    """
    tree = ast.parse(source)
    names = _string_bindings(tree)

    is_writer = False
    violations: list[str] = []
    calls_sanctioned_write = False

    for node in ast.walk(tree):
        if CLIENT_TRIPS_INSERT.search(_static_text(node, names)):
            is_writer = True
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (isinstance(func, ast.Attribute) and func.attr == SANCTIONED_WRITE) or (
            isinstance(func, ast.Name) and func.id == SANCTIONED_WRITE
        ):
            calls_sanctioned_write = True
        if not (isinstance(func, ast.Attribute) and func.attr in RAW_EXECUTORS):
            continue
        arguments = list(node.args) + [kw.value for kw in node.keywords]
        for argument in arguments:
            if CLIENT_TRIPS_INSERT.search(_static_text(argument, names)):
                is_writer = True
                violations.append(
                    f"line {getattr(node, 'lineno', '?')}: {func.attr}() executes a "
                    f"client_trips INSERT directly instead of {SANCTIONED_WRITE}()"
                )
                break

    if is_writer and not calls_sanctioned_write:
        violations.append(
            f"module writes client_trips but never calls {SANCTIONED_WRITE}()"
        )
    return is_writer, violations


# Positive controls. These are the shapes a future writer could plausibly take;
# the guard is only worth anything if it rejects them.
ROGUE_IMPORTS_BUT_BYPASSES = '''
from jobs.api.telematics import client_trips_admission  # imported and never used


def persist(cur, trips_table, rows):
    cur.executemany(
        f"INSERT INTO {trips_table} (client_id, trip_distance_meters) VALUES (%s,%s)",
        rows,
    )
'''

ROGUE_VIA_SQL_VARIABLE = '''
from jobs.api.telematics import client_trips_admission

INSERT_SQL = "INSERT INTO public.client_trips (client_id) VALUES (%s)"


def persist(cur, rows):
    if client_trips_admission.MAX_TRIP_DISTANCE_METERS:
        cur.executemany(INSERT_SQL, rows)
'''

COMPLIANT_FUTURE_WRITER = '''
from jobs.api.telematics import client_trips_admission


def persist(cur, trips_table, rows):
    return client_trips_admission.execute_client_trips_insert(
        cur,
        sql=f"INSERT INTO {trips_table} (client_id, trip_distance_meters) VALUES (%s,%s)",
        rows=rows,
        distance_index=1,
    )
'''


def test_structural_guard_rejects_a_bypassing_future_writer() -> None:
    is_writer, violations = audit_client_trips_writes(ROGUE_IMPORTS_BUT_BYPASSES)
    _check("a future writer that imports the module but bypasses it is detected",
           is_writer, "the rogue module was not classified as a client_trips writer")
    _check("importing client_trips_admission is NOT sufficient to pass the guard",
           bool(violations), f"violations={violations}")

    is_writer, violations = audit_client_trips_writes(ROGUE_VIA_SQL_VARIABLE)
    _check("a bypass through a module-level SQL variable is also rejected",
           is_writer and bool(violations),
           f"is_writer={is_writer}, violations={violations}")

    is_writer, violations = audit_client_trips_writes(COMPLIANT_FUTURE_WRITER)
    _check("a future writer using the sanctioned primitive passes the guard",
           is_writer and not violations,
           f"is_writer={is_writer}, violations={violations}")


def test_every_production_writer_goes_through_the_admission_primitive() -> None:
    writers: list[str] = []
    violations: list[str] = []
    for root in ("jobs", "ops", "api", "scripts", "delivery"):
        root_path = REPO_ROOT / root
        if not root_path.is_dir():
            continue
        for path in sorted(root_path.rglob("*.py")):
            if "tests_manual" in path.parts or "__pycache__" in path.parts:
                continue
            source = path.read_text(encoding="utf-8", errors="replace")
            try:
                is_writer, module_violations = audit_client_trips_writes(source)
            except SyntaxError:
                continue
            if not is_writer:
                continue
            name = str(path.relative_to(REPO_ROOT))
            writers.append(name)
            violations.extend(f"{name}: {v}" for v in module_violations)

    _check("both known production client_trips writers are discovered",
           set(writers) == {
               "jobs/api/telematics/sync_trips_and_speeding.py",
               "jobs/api/telematics/backfill_trips_insert_only.py",
           },
           f"writers={writers}")
    _check("every production client_trips write goes through the admission primitive",
           not violations, f"violations={violations}")


def test_row_layout_indices_match_the_shared_column_list() -> None:
    _check("the sync job's declared row-layout indices match the column list",
           sync.CLIENT_TRIPS_DISTANCE_VALUE_INDEX == DISTANCE_INDEX
           and sync.CLIENT_TRIPS_PROVIDER_TRIP_ID_VALUE_INDEX == PROVIDER_TRIP_ID_INDEX,
           f"distance={sync.CLIENT_TRIPS_DISTANCE_VALUE_INDEX}, "
           f"provider_trip_id={sync.CLIENT_TRIPS_PROVIDER_TRIP_ID_VALUE_INDEX}")


def main() -> int:
    test_admission_boundary()
    test_missing_distance_semantics_unchanged()
    test_admission_gate_observability()
    test_sync_discards_over_cap_trips()
    test_sync_persistence_boundary_holds_when_the_parse_gate_leaks()
    test_backfill_parse_rejects_over_cap()
    test_backfill_insert_boundary_refuses_over_cap_rows()
    test_sync_non_api_metric_variant_enforces_the_same_row_layout()
    test_structural_guard_rejects_a_bypassing_future_writer()
    test_every_production_writer_goes_through_the_admission_primitive()
    test_row_layout_indices_match_the_shared_column_list()

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} checks")
        for failure in FAILURES:
            print(f" - {failure}")
        return 1
    print("All Client Trips 2,000 km ingestion cap checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
