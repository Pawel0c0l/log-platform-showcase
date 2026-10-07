#!/usr/bin/env python3
"""Manual checks for fuel-level based daily aggregation logic.

No DB and no network. These checks cover the pure aggregation rules and the
provider client's normalized GET /fuel/level/{registration} request.

Run from repo root:

    PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_workflow_a_fuel_level_daily.py
"""
from __future__ import annotations

import ast
import sys
import types
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

psycopg = types.ModuleType("psycopg")
rows = types.ModuleType("psycopg.rows")
rows.dict_row = object()
sys.modules.setdefault("psycopg", psycopg)
sys.modules.setdefault("psycopg.rows", rows)

from jobs.api.telematics import aggregate_trip_fuel_daily as aggregate  # noqa: E402
from jobs.api.telematics.provider_client import TelematicsFleetProviderClient  # noqa: E402


AGGREGATE_JOB = REPO_ROOT / "jobs" / "api" / "telematics" / "aggregate_trip_fuel_daily.py"
FAILURES: list[str] = []


def _check(label: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    line = f"[{status}] {label}"
    if detail:
        line += f"\n        {detail}"
    print(line)
    if not ok:
        FAILURES.append(label)


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


class FakeSession:
    def __init__(self, payload: dict):
        self.payload = payload
        self.calls: list[dict] = []
        self.auth = None

    def get(self, url: str, *, params: dict, timeout: int):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        return FakeResponse(self.payload)


def test_aggregate_job_syntax_and_endpoint_usage() -> None:
    source = AGGREGATE_JOB.read_text(encoding="utf-8")
    ast.parse(source)
    _check("aggregate_trip_fuel_daily.py parses", True)
    _check(
        "aggregate job calls fetch_fuel_level",
        "fetch_fuel_level(" in source,
    )
    _check(
        "aggregate job does not call fetch_fuel_consumed_batch",
        "fetch_fuel_consumed_batch" not in source,
    )
    _check(
        "aggregate job does not call legacy fetch_fuel_consumed",
        "fetch_fuel_consumed(" not in source,
    )


def test_fuel_level_provider_normalization() -> None:
    payload = {
        "data": {
            "calibrated": True,
            "start_period": {"accurate": True, "liters": "15.53"},
            "end_period": {"accurate": False, "liters": 13.38},
            "estimated_fuel_used": "2.16",
        }
    }
    client = TelematicsFleetProviderClient(
        base_url="https://fleet.example.test",
        basic_auth_username="user",
        basic_auth_password="secret",
        timeout_s=12,
    )
    fake = FakeSession(payload)
    client._session = fake

    warsaw = ZoneInfo("Europe/Warsaw")
    result = client.fetch_fuel_level(
        " EL5JT28 ",
        datetime(2026, 4, 26, 0, 0, tzinfo=warsaw),
        datetime(2026, 4, 27, 0, 0, tzinfo=warsaw),
        "fuel-level-test",
    )

    _check("one GET was issued", len(fake.calls) == 1, f"calls={fake.calls!r}")
    call = fake.calls[0]
    _check(
        "GET endpoint is /fuel/level/{registration}",
        call["url"] == "https://fleet.example.test/fuel/level/EL5JT28",
        f"url={call['url']!r}",
    )
    _check(
        "GET params preserve local calendar day wall-clock timestamps",
        call["params"]["start_timestamp"] == "2026-04-26 00:00:00"
        and call["params"]["end_timestamp"] == "2026-04-27 00:00:00",
        f"params={call['params']!r}",
    )
    _check(
        "fuel level response is normalized",
        result == {
            "registration": "EL5JT28",
            "start_liters": 15.53,
            "end_liters": 13.38,
            "start_accurate": True,
            "end_accurate": False,
            "calibrated": True,
            "estimated_fuel_used": 2.16,
        },
        f"result={result!r}",
    )


def test_fuel_used_rules() -> None:
    valid, reason = aggregate._fuel_used_liters_from_level({
        "start_liters": 15.5,
        "end_liters": 13.0,
        "start_accurate": True,
        "end_accurate": True,
        "calibrated": True,
    })
    _check("fuel_used_liters = start_liters - end_liters", valid == 2.5 and reason is None)

    negative, reason = aggregate._fuel_used_liters_from_level({
        "start_liters": 10,
        "end_liters": 12,
        "start_accurate": True,
        "end_accurate": True,
        "calibrated": True,
    })
    _check("negative fuel delta returns NULL", negative is None and reason == "negative_fuel_delta")

    missing, reason = aggregate._fuel_used_liters_from_level({
        "start_liters": None,
        "end_liters": 12,
        "start_accurate": True,
        "end_accurate": True,
        "calibrated": True,
    })
    _check("missing start/end returns NULL", missing is None and reason == "missing_start_or_end_liters")

    uncalibrated, reason = aggregate._fuel_used_liters_from_level({
        "start_liters": 15,
        "end_liters": 12,
        "start_accurate": True,
        "end_accurate": True,
        "calibrated": False,
    })
    _check("calibrated=false returns NULL", uncalibrated is None and reason == "not_calibrated")

    inaccurate, reason = aggregate._fuel_used_liters_from_level({
        "start_liters": 15,
        "end_liters": 12,
        "start_accurate": False,
        "end_accurate": True,
        "calibrated": True,
    })
    _check("inaccurate start/end returns NULL", inaccurate is None and reason == "inaccurate_start_or_end")


def test_avg_fuel_and_driver_allocation() -> None:
    _check(
        "avg fuel is computed from daily fuel and distance",
        aggregate._avg_fuel_l_per_100km(10.0, 50_000) == 20.0,
    )
    _check(
        "avg fuel is NULL when fuel is NULL",
        aggregate._avg_fuel_l_per_100km(None, 50_000) is None,
    )
    _check(
        "avg fuel is NULL when distance is zero",
        aggregate._avg_fuel_l_per_100km(10.0, 0) is None,
    )
    _check(
        "driver fuel is proportional to driver distance",
        aggregate._allocated_driver_fuel_liters(
            vehicle_fuel_liters=12.0,
            driver_distance_meters=25_000,
            vehicle_distance_meters=100_000,
        ) == 3.0,
    )
    _check(
        "driver fuel is NULL when vehicle fuel is NULL",
        aggregate._allocated_driver_fuel_liters(
            vehicle_fuel_liters=None,
            driver_distance_meters=25_000,
            vehicle_distance_meters=100_000,
        ) is None,
    )
    _check(
        "driver fuel is NULL when vehicle distance is zero",
        aggregate._allocated_driver_fuel_liters(
            vehicle_fuel_liters=12.0,
            driver_distance_meters=25_000,
            vehicle_distance_meters=0,
        ) is None,
    )
    _check(
        "vehicle fuel combines all registrations for a vehicle/day",
        aggregate._combined_registration_fuel_liters(
            {("2026-04-26", "REG1"): 3.0, ("2026-04-26", "REG2"): 4.0},
            day="2026-04-26",
            registration_keys={"REG1", "REG2"},
        ) == 7.0,
    )
    _check(
        "combined vehicle fuel is NULL if any registration fuel is missing",
        aggregate._combined_registration_fuel_liters(
            {("2026-04-26", "REG1"): 3.0, ("2026-04-26", "REG2"): None},
            day="2026-04-26",
            registration_keys={"REG1", "REG2"},
        ) is None,
    )


def main() -> int:
    test_aggregate_job_syntax_and_endpoint_usage()
    test_fuel_level_provider_normalization()
    test_fuel_used_rules()
    test_avg_fuel_and_driver_allocation()
    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} check(s) failed:")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("\nOK - fuel-level daily aggregation checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
