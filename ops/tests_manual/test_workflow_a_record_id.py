#!/usr/bin/env python3
"""Manual sanity test for `jobs.api.telematics.record_id`.

What this checks (no DB, no network):

  * The frozen namespace UUID is exactly the value expected by every
    record_id ever produced. Changing this value silently re-derives every
    id and breaks dedup; the test guards that.
  * Every table formula is **deterministic**: same business key in →
    same UUID out, across two independent calls.
  * Every formula returns a UUID v5 (`version == 5`).
  * Different tables with the same business key produce **different**
    UUIDs (table name is part of the formula).
  * Cross-table collisions on representative inputs do not happen.
  * `compute(table_name, **kwargs)` dispatches to the per-table formula
    and matches the explicit `for_*` call.
  * Required components must be present; passing `None` raises
    `ValueError`.

Run:

    cd /opt/log-platform
    python3 ops/tests_manual/test_workflow_a_record_id.py
"""
from __future__ import annotations

import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.api.telematics import record_id as rid  # noqa: E402


FAILURES: list[str] = []


def _check(label: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    line = f"[{status}] {label}"
    if detail:
        line += f"  ({detail})"
    print(line)
    if not ok:
        FAILURES.append(label)


def test_namespace_frozen() -> None:
    expected = uuid.UUID("331a59e5-a43c-4447-895a-ebc72cdd4eac")
    _check(
        "namespace UUID is frozen",
        rid.NAMESPACE_WORKFLOW_A == expected,
        f"got={rid.NAMESPACE_WORKFLOW_A}",
    )
    # And it matches the canonical derivation: NAMESPACE_DNS,
    # 'log-platform.workflow_a'. If anyone ever changes the input string,
    # this test fails.
    derived = uuid.uuid5(uuid.NAMESPACE_DNS, "log-platform.workflow_a")
    _check(
        "namespace matches uuid5(DNS, 'log-platform.workflow_a')",
        rid.NAMESPACE_WORKFLOW_A == derived,
    )


def test_determinism_and_version() -> None:
    client_id = "2944c91f-c9be-485b-8bcc-362bc43432dd"
    cases = [
        ("client_trips",
         dict(client_id=client_id, provider_trip_id=12345)),
        ("client_speeding_notifications",
         dict(client_id=client_id,
              provider_notification_id="12a97845-b46c-4006-8dfc-2863a8df332c")),
        ("client_vehicle_daily_fuel",
         dict(client_id=client_id, vehicle_id="V-99",
              day=date(2026, 4, 27))),
        ("client_vehicle_driver_daily_fuel",
         dict(client_id=client_id, vehicle_id="V-99",
              driver_id="D-11", day="2026-04-27")),
        # V2 staging tables (DDL: 017_v2_staging_tables.sql).
        ("source_trips",
         dict(client_id=client_id, provider_trip_id=12345)),
        ("source_notifications",
         dict(client_id=client_id,
              provider_notification_id="12a97845-b46c-4006-8dfc-2863a8df332c")),
        ("source_fuel_observations",
         dict(client_id=client_id, registration_norm="ABC123",
              window_start_ts="2026-04-27T08:00:00Z",
              window_end_ts="2026-04-27T08:30:00Z")),
    ]
    seen: set[uuid.UUID] = set()
    for table, kwargs in cases:
        a = rid.compute(table, **kwargs)
        b = rid.compute(table, **kwargs)
        _check(f"{table}: deterministic", a == b)
        _check(f"{table}: UUID v5", a.version == 5)
        _check(f"{table}: cross-table unique", a not in seen)
        seen.add(a)


def test_compute_matches_explicit() -> None:
    client_id = "2944c91f-c9be-485b-8bcc-362bc43432dd"
    pairs = [
        (rid.for_client_trips(client_id=client_id, provider_trip_id=42),
         rid.compute("client_trips", client_id=client_id, provider_trip_id=42)),
        (rid.for_client_speeding_notifications(
            client_id=client_id,
            provider_notification_id="12a97845-b46c-4006-8dfc-2863a8df332c"),
         rid.compute("client_speeding_notifications",
                     client_id=client_id,
                     provider_notification_id="12a97845-b46c-4006-8dfc-2863a8df332c")),
        (rid.for_client_vehicle_daily_fuel(
            client_id=client_id, vehicle_id="V-99", day=date(2026, 4, 27)),
         rid.compute("client_vehicle_daily_fuel",
                     client_id=client_id, vehicle_id="V-99", day=date(2026, 4, 27))),
        (rid.for_client_vehicle_driver_daily_fuel(
            client_id=client_id, vehicle_id="V-99", driver_id="D-11",
            day=date(2026, 4, 27)),
         rid.compute("client_vehicle_driver_daily_fuel",
                     client_id=client_id, vehicle_id="V-99",
                     driver_id="D-11", day=date(2026, 4, 27))),
        # V2 staging dispatch.
        (rid.for_source_trips(client_id=client_id, provider_trip_id=42),
         rid.compute("source_trips",
                     client_id=client_id, provider_trip_id=42)),
        (rid.for_source_notifications(
            client_id=client_id,
            provider_notification_id="12a97845-b46c-4006-8dfc-2863a8df332c"),
         rid.compute("source_notifications",
                     client_id=client_id,
                     provider_notification_id="12a97845-b46c-4006-8dfc-2863a8df332c")),
        (rid.for_source_fuel_observations(
            client_id=client_id, registration_norm="ABC123",
            window_start_ts="2026-04-27T08:00:00Z",
            window_end_ts="2026-04-27T08:30:00Z"),
         rid.compute("source_fuel_observations",
                     client_id=client_id, registration_norm="ABC123",
                     window_start_ts="2026-04-27T08:00:00Z",
                     window_end_ts="2026-04-27T08:30:00Z")),
    ]
    for explicit, dispatched in pairs:
        _check("compute() == for_*()", explicit == dispatched,
               f"{explicit}=={dispatched}")


def test_source_trips_distinct_from_client_trips() -> None:
    """Per-table uniqueness invariant: same business key, different table
    name -> different record_id. Guards against accidental cross-layer
    collision after V2 lands.
    """
    client_id = "2944c91f-c9be-485b-8bcc-362bc43432dd"
    a = rid.for_client_trips(client_id=client_id, provider_trip_id=12345)
    b = rid.for_source_trips(client_id=client_id, provider_trip_id=12345)
    _check("client_trips vs source_trips distinct for same business key",
           a != b, f"client={a}, source={b}")

    n_client = rid.for_client_speeding_notifications(
        client_id=client_id,
        provider_notification_id="12a97845-b46c-4006-8dfc-2863a8df332c")
    n_source = rid.for_source_notifications(
        client_id=client_id,
        provider_notification_id="12a97845-b46c-4006-8dfc-2863a8df332c")
    _check("client_speeding_notifications vs source_notifications distinct",
           n_client != n_source, f"client={n_client}, source={n_source}")


def test_source_fuel_timestamp_normalization() -> None:
    """`window_*_ts` accepts datetime, 'Z'-suffixed string, or '+00:00'
    string equivalently — different shapes for the same UTC second
    collapse to the same record_id. Microseconds are dropped (same UTC
    second wins).
    """
    client_id = "2944c91f-c9be-485b-8bcc-362bc43432dd"
    a = rid.for_source_fuel_observations(
        client_id=client_id, registration_norm="ABC123",
        window_start_ts="2026-04-27T08:00:00Z",
        window_end_ts="2026-04-27T08:30:00Z")
    b = rid.for_source_fuel_observations(
        client_id=client_id, registration_norm="ABC123",
        window_start_ts="2026-04-27T08:00:00+00:00",
        window_end_ts="2026-04-27T08:30:00+00:00")
    c = rid.for_source_fuel_observations(
        client_id=client_id, registration_norm="ABC123",
        window_start_ts=datetime(2026, 4, 27, 8, 0, 0, tzinfo=timezone.utc),
        window_end_ts=datetime(2026, 4, 27, 8, 30, 0, tzinfo=timezone.utc))
    d = rid.for_source_fuel_observations(
        client_id=client_id, registration_norm="ABC123",
        window_start_ts=datetime(2026, 4, 27, 8, 0, 0, 123456,
                                 tzinfo=timezone.utc),
        window_end_ts=datetime(2026, 4, 27, 8, 30, 0, 999999,
                               tzinfo=timezone.utc))
    _check("source_fuel_observations: Z / +00:00 / datetime / microseconds collapse",
           a == b == c == d, f"{a}, {b}, {c}, {d}")


def test_source_fuel_window_changes_id() -> None:
    """Different window edges must produce different record_ids — the
    same registration sampled across two distinct windows is two
    distinct staging rows.
    """
    client_id = "2944c91f-c9be-485b-8bcc-362bc43432dd"
    a = rid.for_source_fuel_observations(
        client_id=client_id, registration_norm="ABC123",
        window_start_ts="2026-04-27T08:00:00Z",
        window_end_ts="2026-04-27T08:30:00Z")
    b = rid.for_source_fuel_observations(
        client_id=client_id, registration_norm="ABC123",
        window_start_ts="2026-04-27T08:00:00Z",
        window_end_ts="2026-04-27T09:00:00Z")  # end different
    _check("source_fuel_observations: different window_end_ts -> different id",
           a != b, f"{a} == {b}")


def test_source_fuel_required_components_and_tz() -> None:
    """Required-component invariants for `for_source_fuel_observations`:
    every component must be present, non-empty, parseable, and tz-aware.
    """
    client_id = "2944c91f-c9be-485b-8bcc-362bc43432dd"
    cases = [
        ("missing client_id",
         dict(client_id=None, registration_norm="ABC123",
              window_start_ts="2026-04-27T08:00:00Z",
              window_end_ts="2026-04-27T08:30:00Z")),
        ("empty registration_norm",
         dict(client_id=client_id, registration_norm="   ",
              window_start_ts="2026-04-27T08:00:00Z",
              window_end_ts="2026-04-27T08:30:00Z")),
        ("missing window_start_ts",
         dict(client_id=client_id, registration_norm="ABC123",
              window_start_ts=None,
              window_end_ts="2026-04-27T08:30:00Z")),
        ("tz-naive window_end_ts as datetime",
         dict(client_id=client_id, registration_norm="ABC123",
              window_start_ts="2026-04-27T08:00:00Z",
              window_end_ts=datetime(2026, 4, 27, 8, 30, 0))),
        ("garbage window_end_ts string",
         dict(client_id=client_id, registration_norm="ABC123",
              window_start_ts="2026-04-27T08:00:00Z",
              window_end_ts="not-a-timestamp")),
    ]
    for label, kwargs in cases:
        try:
            rid.for_source_fuel_observations(**kwargs)
            _check(f"required-component check raises: {label}", False,
                   "no exception raised")
        except ValueError:
            _check(f"required-component check raises: {label}", True)
        except Exception as exc:
            _check(f"required-component check raises: {label}", False,
                   f"unexpected {type(exc).__name__}: {exc}")


def test_date_normalization() -> None:
    """`day` accepts date, datetime, or 'YYYY-MM-DD' string equivalently."""
    client_id = "2944c91f-c9be-485b-8bcc-362bc43432dd"
    a = rid.for_client_vehicle_daily_fuel(
        client_id=client_id, vehicle_id="V-99", day=date(2026, 4, 27))
    b = rid.for_client_vehicle_daily_fuel(
        client_id=client_id, vehicle_id="V-99", day="2026-04-27")
    c = rid.for_client_vehicle_daily_fuel(
        client_id=client_id, vehicle_id="V-99",
        day=datetime(2026, 4, 27, 12, 30, tzinfo=timezone.utc))
    _check("date / 'YYYY-MM-DD' / datetime collapse to same record_id",
           a == b == c, f"{a}, {b}, {c}")


def test_int_vs_str_business_key() -> None:
    """provider_trip_id is normalized via str(int(...)) — int/str stable."""
    client_id = "2944c91f-c9be-485b-8bcc-362bc43432dd"
    a = rid.for_client_trips(client_id=client_id, provider_trip_id=12345)
    b = rid.for_client_trips(client_id=client_id, provider_trip_id="12345")
    _check("provider_trip_id int vs '12345' equal", a == b)


def test_required_components() -> None:
    client_id = "2944c91f-c9be-485b-8bcc-362bc43432dd"
    cases = [
        ("client_trips no client_id",
         dict(client_id=None, provider_trip_id=1)),
        ("client_trips no provider_trip_id",
         dict(client_id=client_id, provider_trip_id=None)),
        ("vehicle_daily_fuel no day",
         dict(client_id=client_id, vehicle_id="V-99", day=None)),
        ("vehicle_daily_fuel empty client_id",
         dict(client_id="   ", vehicle_id="V-99", day="2026-04-27")),
    ]
    for label, kwargs in cases:
        try:
            if "provider_trip_id" in kwargs:
                rid.for_client_trips(**kwargs)
            else:
                rid.for_client_vehicle_daily_fuel(**kwargs)
            _check(f"required-component check raises: {label}", False,
                   "no exception raised")
        except ValueError:
            _check(f"required-component check raises: {label}", True)
        except Exception as exc:
            _check(f"required-component check raises: {label}", False,
                   f"unexpected {type(exc).__name__}: {exc}")


def main() -> int:
    test_namespace_frozen()
    test_determinism_and_version()
    test_compute_matches_explicit()
    test_date_normalization()
    test_int_vs_str_business_key()
    test_required_components()
    test_source_trips_distinct_from_client_trips()
    test_source_fuel_timestamp_normalization()
    test_source_fuel_window_changes_id()
    test_source_fuel_required_components_and_tz()

    print("")
    if FAILURES:
        print(f"FAIL — {len(FAILURES)} check(s) failed:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("OK — all record_id checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
