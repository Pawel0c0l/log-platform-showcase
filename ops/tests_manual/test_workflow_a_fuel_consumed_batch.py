#!/usr/bin/env python3
"""Manual tests for Telematics batch fuel-consumed provider client support.

No DB and no network. The test replaces the client's requests session with a
small fake and verifies validation, POST body shape, and normalized response
data for `fetch_fuel_consumed_batch`.

Run from repo root:

    PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_workflow_a_fuel_consumed_batch.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.api.telematics.provider_client import TelematicsFleetProviderClient  # noqa: E402


FAILURES: list[str] = []


def _check(label: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    line = f"[{status}] {label}"
    if detail:
        line += f"\n        {detail}"
    print(line)
    if not ok:
        FAILURES.append(label)


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s).astimezone(timezone.utc)


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

    def post(self, url: str, *, json: dict, timeout: int):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        return FakeResponse(self.payload)


def _client_with_payload(payload: dict) -> tuple[TelematicsFleetProviderClient, FakeSession]:
    client = TelematicsFleetProviderClient(
        base_url="https://fleet.example.test",
        basic_auth_username="user",
        basic_auth_password="secret",
        timeout_s=12,
    )
    fake = FakeSession(payload)
    client._session = fake
    return client, fake


def test_post_body_and_normalization() -> None:
    payload = {
        "data": [
            {
                "registration": " ABC123 ",
                "vehicle_id": 42,
                "fuel_consumed_start": "100.5",
                "fuel_consumed_end": 140,
                "fuel_consumed": "39.5",
                "extra": "ignored",
            }
        ],
        "meta": {"current_page": 1, "last_page": 1},
    }
    client, fake = _client_with_payload(payload)
    result = client.fetch_fuel_consumed_batch(
        registrations=[" ABC123 "],
        start_timestamp=_ts("2026-04-01T00:00:00+00:00"),
        end_timestamp=_ts("2026-04-01T12:00:00+00:00"),
        sub_window_label="fuel-batch-test",
    )

    _check("one POST was issued", len(fake.calls) == 1, f"calls={fake.calls!r}")
    call = fake.calls[0]
    _check("POST endpoint is /fuel/consumed",
           call["url"] == "https://fleet.example.test/fuel/consumed",
           f"url={call['url']!r}")
    _check("POST body uses provider datetime format",
           call["json"]["start_timestamp"] == "2026-04-01 00:00:00"
           and call["json"]["end_timestamp"] == "2026-04-01 12:00:00",
           f"body={call['json']!r}")
    _check("POST body includes page=1 and limit=100",
           call["json"]["page"] == 1 and call["json"]["limit"] == 100,
           f"body={call['json']!r}")
    _check("registrations are stripped before POST",
           call["json"]["registrations"] == ["ABC123"],
           f"registrations={call['json']['registrations']!r}")
    _check("response is normalized to the expected fields",
           result == [{
               "registration": "ABC123",
               "vehicle_id": 42,
               "fuel_consumed_start": 100.5,
               "fuel_consumed_end": 140.0,
               "fuel_consumed": 39.5,
           }],
           f"result={result!r}")


def test_validation() -> None:
    client, fake = _client_with_payload({"data": []})
    empty = client.fetch_fuel_consumed_batch(
        registrations=[],
        start_timestamp=_ts("2026-04-01T00:00:00+00:00"),
        end_timestamp=_ts("2026-04-01T01:00:00+00:00"),
        sub_window_label="empty",
    )
    _check("empty registration batch returns [] without POST",
           empty == [] and fake.calls == [],
           f"empty={empty!r}, calls={fake.calls!r}")

    too_many = [f"REG{i}" for i in range(101)]
    try:
        client.fetch_fuel_consumed_batch(
            registrations=too_many,
            start_timestamp=_ts("2026-04-01T00:00:00+00:00"),
            end_timestamp=_ts("2026-04-01T01:00:00+00:00"),
            sub_window_label="too-many",
        )
        _check("max 100 registrations is enforced", False)
    except ValueError as e:
        _check("max 100 registrations is enforced",
               "at most 100" in str(e), f"error={e}")

    try:
        client.fetch_fuel_consumed_batch(
            registrations=["REG1"],
            start_timestamp=_ts("2026-04-01T00:00:00+00:00"),
            end_timestamp=_ts("2026-04-02T00:00:01+00:00"),
            sub_window_label="too-wide",
        )
        _check("max 24h window is enforced", False)
    except ValueError as e:
        _check("max 24h window is enforced",
               "<= 24 hours" in str(e), f"error={e}")


def main() -> int:
    test_post_body_and_normalization()
    test_validation()
    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} check(s) failed:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("\nOK - fetch_fuel_consumed_batch manual tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
