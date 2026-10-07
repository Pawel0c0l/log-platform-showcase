#!/usr/bin/env python3
"""Manual tests for Telematics fleet-wide raw vehicle events support.

No DB and no network. The test replaces the client's requests session with a
small fake and verifies bounded pagination, `limit=1000`, and normalized
response data for `GET /vehicles/events`.

Run from repo root:

    PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_workflow_a_vehicle_events.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from requests.exceptions import HTTPError as RequestsHTTPError
from requests.exceptions import Timeout as RequestsTimeout


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.api.telematics.provider_client import (  # noqa: E402
    TELEMATICS_PROVIDER_PAGE_LIMIT_ENV,
    TelematicsFleetProviderClient,
    provider_page_limit_from_env,
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


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


class FakeResponse:
    def __init__(
        self,
        payload: dict,
        *,
        status_code: int = 200,
        raise_http: bool = False,
        text: str | None = None,
        headers: dict | None = None,
    ):
        self.payload = payload
        self.status_code = status_code
        self.raise_http = raise_http
        self.text = text if text is not None else str(payload)
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        if self.raise_http:
            raise RequestsHTTPError(response=self)
        return None

    def json(self) -> dict:
        return self.payload


class FakeSession:
    def __init__(self, payloads_by_page: dict[int, dict]):
        self.payloads_by_page = payloads_by_page
        self.calls: list[dict] = []
        self.auth = None

    def get(self, url: str, *, params: dict, timeout: int):
        self.calls.append({"url": url, "params": dict(params), "timeout": timeout})
        payload = self.payloads_by_page[params["page"]]
        if isinstance(payload, list):
            payload = payload.pop(0)
        if isinstance(payload, BaseException):
            raise payload
        if isinstance(payload, FakeResponse):
            return payload
        return FakeResponse(payload)


def _client_with_payloads(
    payloads_by_page: dict[int, dict],
    **client_kwargs,
) -> tuple[TelematicsFleetProviderClient, FakeSession]:
    page_limit = client_kwargs.pop("page_limit", 2)
    client = TelematicsFleetProviderClient(
        base_url="https://fleet.example.test",
        basic_auth_username="user",
        basic_auth_password="secret",
        timeout_s=12,
        page_limit=page_limit,
        **client_kwargs,
    )
    fake = FakeSession(payloads_by_page)
    client._session = fake
    return client, fake


def _client_with_default_page_limit(
    payloads_by_page: dict[int, dict],
    **client_kwargs,
) -> tuple[TelematicsFleetProviderClient, FakeSession]:
    client = TelematicsFleetProviderClient(
        base_url="https://fleet.example.test",
        basic_auth_username="user",
        basic_auth_password="secret",
        timeout_s=12,
        **client_kwargs,
    )
    fake = FakeSession(payloads_by_page)
    client._session = fake
    return client, fake


def test_provider_page_limit_default_and_env_override() -> None:
    previous = os.environ.pop(TELEMATICS_PROVIDER_PAGE_LIMIT_ENV, None)
    try:
        _check("provider page-limit env helper defaults to 1000",
               provider_page_limit_from_env() == 1000,
               f"default={provider_page_limit_from_env()!r}")
        os.environ[TELEMATICS_PROVIDER_PAGE_LIMIT_ENV] = "321"
        _check("provider page-limit env override wins",
               provider_page_limit_from_env() == 321,
               f"override={provider_page_limit_from_env()!r}")
    finally:
        if previous is None:
            os.environ.pop(TELEMATICS_PROVIDER_PAGE_LIMIT_ENV, None)
        else:
            os.environ[TELEMATICS_PROVIDER_PAGE_LIMIT_ENV] = previous


def test_fetch_trips_uses_default_page_limit_1000() -> None:
    client, fake = _client_with_default_page_limit({
        1: {
            "data": [{"trip_id": 125, "registration": "REG-C"}],
            "meta": {"current_page": 1, "last_page": 1},
        },
    })
    result = client.fetch_trips(
        window_start_ts=_ts("2026-04-01T08:00:00Z"),
        window_end_ts=_ts("2026-04-01T09:00:00Z"),
    )
    _check("/trips uses default provider page limit 1000",
           len(result) == 1 and fake.calls[0]["params"].get("limit") == 1000,
           f"calls={fake.calls!r}")


def test_fetch_vehicles_uses_default_page_limit_1000() -> None:
    client, fake = _client_with_default_page_limit({
        1: {
            "data": [{"vehicle_id": 501, "registration": "REG-501"}],
            "meta": {"current_page": 1, "last_page": 1, "total": 1},
        },
    })
    result = client.fetch_vehicles_fleet(sub_window_label="vehicle-default-limit")
    _check("/vehicles uses default provider page limit 1000",
           len(result) == 1 and fake.calls[0]["params"] == {"page": 1, "limit": 1000},
           f"calls={fake.calls!r}, result={result!r}")


def test_stops_at_last_page_and_normalizes() -> None:
    payloads = {
        1: {
            "data": [{
                "event_id": 101,
                "registration": " ABC123 ",
                "vehicle_id": 42,
                "event_ts": "2026-04-01 08:00:01+00",
                "speed": "141",
                "road_speed": "90",
                "road_speeding": "true",
                "rpm": "2100",
                "latitude": "52.1",
                "longitude": "21.2",
                "odometer": "123456",
            }],
            "meta": {"current_page": 1, "last_page": 2},
        },
        2: {
            "data": [{
                "event_id": 102,
                "registration": "ABC123",
                "vehicle_id": 42,
                "event_ts": "2026-04-01 08:00:02+00",
                "speed": 171,
                "road_speed": None,
                "road_speeding": False,
                "rpm": None,
                "latitude": None,
                "longitude": None,
                "odometer": None,
            }],
            "meta": {"current_page": 2, "last_page": 2},
        },
    }
    client, fake = _client_with_payloads(payloads)
    result = client.fetch_vehicle_events_fleet(
        start_timestamp=_ts("2026-04-01T08:00:00Z"),
        end_timestamp=_ts("2026-04-01T09:00:00Z"),
        sub_window_label="vehicle-events-test",
        limit=1000,
        max_pages=500,
    )

    _check("two paginated GETs were issued",
           len(fake.calls) == 2,
           f"calls={fake.calls!r}")
    _check("GET endpoint is fleet-wide vehicle events",
           fake.calls[0]["url"] == "https://fleet.example.test/vehicles/events",
           f"url={fake.calls[0]['url']!r}")
    _check("GET params use provider datetime format, page, and limit=1000",
           fake.calls[0]["params"]["page"] == 1
           and fake.calls[0]["params"]["limit"] == 1000
           and fake.calls[1]["params"]["page"] == 2,
           f"calls={fake.calls!r}")
    # `/vehicles/events` addresses events by Europe/Warsaw wall-clock — proven
    # by the DELTA00001 GET-only probe on 2026-08-10 (docs/18 §2). 2026-04-01 is
    # CEST, so the intended UTC window 08:00–09:00 goes on the wire as
    # 10:00–11:00. Response `event_ts` stays UTC and is asserted below.
    # Full contract coverage:
    # `ops/tests_manual/test_telematics_vehicle_events_wire_time_contract.py`.
    _check("fleet events send the Warsaw wall-clock window, not the UTC projection",
           fake.calls[0]["params"]["start_timestamp"] == "2026-04-01 10:00:00"
           and fake.calls[0]["params"]["end_timestamp"] == "2026-04-01 11:00:00"
           and fake.calls[1]["params"]["start_timestamp"] == "2026-04-01 10:00:00",
           f"calls={fake.calls!r}")
    _check("response is normalized to expected vehicle event fields",
           result[0]["registration"] == "ABC123"
           and result[0]["vehicle_id"] == 42
           and result[0]["event_ts"] == _ts("2026-04-01T08:00:01Z")
           and result[0]["speed"] == 141.0
           and result[0]["road_speed"] == 90.0
           and result[0]["road_speeding"] is True
           and result[0]["rpm"] == 2100
           and result[0]["latitude"] == 52.1
           and result[0]["longitude"] == 21.2
           and result[0]["odometer"] == 123456
           and result[1]["speed"] == 171.0,
           f"result={result!r}")


def test_vehicle_events_limit_is_capped_at_1000_and_stats_available() -> None:
    client, fake = _client_with_payloads({
        1: {
            "data": [{
                "event_id": 151,
                "registration": "REG-A",
                "event_ts": "2026-04-01 08:00:01+00",
                "speed": 141,
            }],
            "meta": {"current_page": 1, "last_page": 1, "total": 1},
        },
    })
    result, stats = client.fetch_vehicle_events_fleet(
        start_timestamp=_ts("2026-04-01T08:00:00Z"),
        end_timestamp=_ts("2026-04-01T09:00:00Z"),
        sub_window_label="limit-cap",
        limit=2000,
        max_pages=500,
        return_stats=True,
    )
    _check("vehicle events provider limit is capped at 1000",
           fake.calls[0]["params"]["limit"] == 1000,
           f"calls={fake.calls!r}")
    _check("vehicle events stats are returned when requested",
           len(result) == 1
           and stats["pages_fetched"] == 1
           and stats["records_fetched"] == 1
           and stats["limit"] == 1000,
           f"result={result!r}, stats={stats!r}")


def test_provider_retry_backoff_on_timeout() -> None:
    sleeps: list[float] = []
    client, fake = _client_with_payloads({
        1: [
            RequestsTimeout("read timed out"),
            {
                "data": [{
                    "event_id": 161,
                    "registration": "REG-A",
                    "event_ts": "2026-04-01 08:00:01+00",
                    "speed": 141,
                }],
                "meta": {"current_page": 1, "last_page": 1},
            },
        ],
    }, sleep_fn=sleeps.append)
    result = client.fetch_vehicle_events_fleet(
        start_timestamp=_ts("2026-04-01T08:00:00Z"),
        end_timestamp=_ts("2026-04-01T09:00:00Z"),
        sub_window_label="retry-timeout",
        limit=1000,
        max_pages=500,
    )
    _check("timeout retry succeeds after provider backoff",
           len(fake.calls) == 2 and len(result) == 1 and 5 in sleeps,
           f"calls={fake.calls!r}, sleeps={sleeps!r}, result={result!r}")


def test_provider_rate_limit_sleep_between_requests() -> None:
    sleeps: list[float] = []
    client, fake = _client_with_payloads({
        1: {
            "data": [{
                "event_id": 171,
                "registration": "REG-A",
                "event_ts": "2026-04-01 08:00:01+00",
                "speed": 141,
            }],
            "meta": {"current_page": 1, "last_page": 2},
        },
        2: {
            "data": [{
                "event_id": 172,
                "registration": "REG-A",
                "event_ts": "2026-04-01 08:00:02+00",
                "speed": 142,
            }],
            "meta": {"current_page": 2, "last_page": 2},
        },
    }, rate_limit_rps=2.5, sleep_fn=sleeps.append)
    result = client.fetch_vehicle_events_fleet(
        start_timestamp=_ts("2026-04-01T08:00:00Z"),
        end_timestamp=_ts("2026-04-01T09:00:00Z"),
        sub_window_label="rate-limit",
        limit=1000,
        max_pages=500,
    )
    _check("rate limiter sleeps between provider requests",
           len(fake.calls) == 2 and len(result) == 2 and any(s >= 0.3 for s in sleeps),
           f"calls={fake.calls!r}, sleeps={sleeps!r}")


def test_stops_on_empty_data() -> None:
    payloads = {
        1: {
            "data": [{
                "event_id": 201,
                "registration": "REG-A",
                "event_ts": "2026-04-01 08:00:01+00",
                "speed": 140,
            }],
            "meta": {"current_page": 1, "last_page": 3},
        },
        2: {
            "data": [],
            "meta": {"current_page": 2, "last_page": 3},
        },
        3: {
            "data": [{
                "event_id": 203,
                "registration": "REG-A",
                "event_ts": "2026-04-01 08:00:03+00",
                "speed": 170,
            }],
            "meta": {"current_page": 3, "last_page": 3},
        },
    }
    client, fake = _client_with_payloads(payloads)
    result = client.fetch_vehicle_events_fleet(
        start_timestamp=_ts("2026-04-01T08:00:00Z"),
        end_timestamp=_ts("2026-04-01T09:00:00Z"),
        sub_window_label="empty-stop",
        limit=1000,
        max_pages=500,
    )
    _check("empty data stops pagination",
           len(fake.calls) == 2 and len(result) == 1,
           f"calls={fake.calls!r}, result={result!r}")


def test_keeps_duplicate_vehicle_event_rows() -> None:
    duplicate = {
        "event_id": 250,
        "registration": "REG-A",
        "event_ts": "2026-04-01 08:00:01+00",
        "speed": 145,
    }
    client, fake = _client_with_payloads({
        1: {
            "data": [dict(duplicate), dict(duplicate), dict(duplicate)],
            "meta": {"current_page": 1, "last_page": 1},
        },
    })
    result = client.fetch_vehicle_events_fleet(
        start_timestamp=_ts("2026-04-01T08:00:00Z"),
        end_timestamp=_ts("2026-04-01T09:00:00Z"),
        sub_window_label="duplicate-rows",
        limit=1000,
        max_pages=500,
    )
    _check("duplicate /vehicles/events rows are preserved",
           len(result) == 3 and len(fake.calls) == 1,
           f"result={result!r}, calls={fake.calls!r}")


def test_stops_at_max_pages() -> None:
    payloads = {
        1: {
            "data": [{
                "event_id": 301,
                "registration": "REG-A",
                "event_ts": "2026-04-01 08:00:01+00",
                "speed": 140,
            }],
            "meta": {"current_page": 1, "last_page": 5, "total": 5},
        },
        2: {
            "data": [{
                "event_id": 302,
                "registration": "REG-A",
                "event_ts": "2026-04-01 08:00:02+00",
                "speed": 150,
            }],
            "meta": {"current_page": 2, "last_page": 5, "total": 5},
        },
        3: {
            "data": [{
                "event_id": 303,
                "registration": "REG-A",
                "event_ts": "2026-04-01 08:00:03+00",
                "speed": 160,
            }],
            "meta": {"current_page": 3, "last_page": 5, "total": 5},
        },
    }
    client, fake = _client_with_payloads(payloads)
    result = client.fetch_vehicle_events_fleet(
        start_timestamp=_ts("2026-04-01T08:00:00Z"),
        end_timestamp=_ts("2026-04-01T09:00:00Z"),
        sub_window_label="max-pages-stop",
        limit=1000,
        max_pages=2,
    )
    _check("max_pages stops before requesting page 3",
           len(fake.calls) == 2 and len(result) == 2,
           f"calls={fake.calls!r}, result={result!r}")


def test_http_422_context_includes_request_params_body_and_safe_headers() -> None:
    client, fake = _client_with_payloads({
        1: FakeResponse(
            {"error": "bad window"},
            status_code=422,
            raise_http=True,
            text='{"message":"validation failed","field":"limit"}',
            headers={
                "Content-Type": "application/json",
                "X-Request-Id": "req-123",
                "Set-Cookie": "session=secret",
            },
        ),
    })
    try:
        client.fetch_vehicle_events_fleet(
            start_timestamp=_ts("2026-04-26T00:00:00Z"),
            end_timestamp=_ts("2026-04-26T03:59:59Z"),
            sub_window_label="http-422",
            limit=1000,
            max_pages=500,
        )
        _check("HTTP 422 raises provider safety error", False)
    except Exception as e:
        ctx = getattr(e, "context", {})
        _check("HTTP 422 raises provider safety error",
               getattr(e, "code", None) == "HTTP_ERROR",
               f"error={e!r}")
        _check("HTTP 422 context includes endpoint params",
               ctx.get("status_code") == 422
               and ctx.get("params", {}).get("start_timestamp") == "2026-04-26 02:00:00"
               and ctx.get("params", {}).get("end_timestamp") == "2026-04-26 05:59:59"
               and ctx.get("params", {}).get("page") == 1
               and ctx.get("params", {}).get("limit") == 1000,
               f"context={ctx!r}, calls={fake.calls!r}")
        _check("HTTP 422 context includes response body text",
               "validation failed" in str(ctx.get("response_body_text")),
               f"context={ctx!r}")
        headers = ctx.get("response_headers") or {}
        _check("HTTP 422 context includes safe headers only",
               headers.get("content-type") == "application/json"
               and headers.get("x-request-id") == "req-123"
               and "set-cookie" not in headers,
               f"headers={headers!r}")


def test_no_per_registration_vehicle_events_endpoint() -> None:
    provider_src = (REPO_ROOT / "jobs/api/telematics/provider_client.py").read_text()
    sync_src = (REPO_ROOT / "jobs/api/telematics/sync_trips_and_speeding.py").read_text()
    combined = provider_src + "\n" + sync_src
    forbidden_endpoint = "/vehicles/" + "{registration}" + "/events"
    _check("per-registration vehicle events endpoint is not present",
           forbidden_endpoint not in combined,
           "")


def test_fetch_vehicle_events_registration_uses_registration_query_param() -> None:
    client, fake = _client_with_payloads({
        1: {
            "data": [{
                "event_id": 431,
                "event_ts": "2026-04-01 08:00:01+00",
                "speed": 145,
            }],
            "meta": {"current_page": 1, "last_page": 1},
        },
    })
    result = client.fetch_vehicle_events_registration(
        registration=" REG-A ",
        start_timestamp=_ts("2026-04-01T08:00:00Z"),
        end_timestamp=_ts("2026-04-01T08:05:00Z"),
        sub_window_label="registration-events-test",
        limit=1000,
        max_pages=500,
    )
    params = fake.calls[0]["params"]
    _check("per-registration fallback uses fleet /vehicles/events endpoint",
           fake.calls[0]["url"] == "https://fleet.example.test/vehicles/events",
           f"calls={fake.calls!r}")
    _check("per-registration fallback sends registration query param",
           params.get("registration") == "REG-A",
           f"params={params!r}")
    # The registration fallback runs precisely when the fleet path is failing;
    # leaving it on UTC serialization would silently change which events a
    # degraded run collects. Same Warsaw wall-clock contract as the fleet path.
    _check("per-registration fallback sends the Warsaw wall-clock window",
           params.get("start_timestamp") == "2026-04-01 10:00:00"
           and params.get("end_timestamp") == "2026-04-01 10:05:00",
           f"params={params!r}")
    _check("per-registration fallback fills missing event registration from request",
           result[0]["registration"] == "REG-A"
           and result[0]["speed"] == 145.0,
           f"result={result!r}")


def test_fetch_trips_includes_private_by_default() -> None:
    client, fake = _client_with_payloads({
        1: {
            "data": [{
                "trip_id": 123,
                "registration": "REG-A",
                "is_private": True,
            }],
            "meta": {"current_page": 1, "last_page": 1},
        },
    })
    result = client.fetch_trips(
        window_start_ts=_ts("2026-04-01T08:00:00Z"),
        window_end_ts=_ts("2026-04-01T09:00:00Z"),
    )
    params = fake.calls[0]["params"]

    _check("trips fetch uses /trips",
           fake.calls[0]["url"] == "https://fleet.example.test/trips",
           f"calls={fake.calls!r}")
    _check("trips fetch includes private trips by default",
           params.get("incl_private") == "true"
           and params.get("page") == 1
           and params.get("limit") == 2
           and result == [{"trip_id": 123, "registration": "REG-A", "is_private": True}],
           f"params={params!r}, result={result!r}")

    # `/trips` addresses trips by Europe/Warsaw wall-clock, not by the UTC
    # projection — confirmed by live GET-only probes on 2026-08-10 and
    # implemented in `provider_client.trips_wire_window`. 2026-04-01 is CEST,
    # so the UTC window 08:00-09:00 must go on the wire as 10:00-11:00.
    # The full contract, including both DST transitions, is covered by
    # `ops/tests_manual/test_telematics_trips_wire_time_contract.py`.
    _check("trips fetch sends the Warsaw wall-clock window, not the UTC projection",
           params.get("start_timestamp") == "2026-04-01 10:00:00"
           and params.get("end_timestamp") == "2026-04-01 11:00:00",
           f"params={params!r}")


def test_provider_metrics_snapshot_tracks_request_and_parse_timing() -> None:
    client, fake = _client_with_payloads({
        1: {
            "data": [{
                "trip_id": 124,
                "registration": "REG-B",
                "is_private": False,
            }],
            "meta": {"current_page": 1, "last_page": 1},
        },
    })
    before = client.metrics_snapshot()
    result = client.fetch_trips(
        window_start_ts=_ts("2026-04-01T08:00:00Z"),
        window_end_ts=_ts("2026-04-01T09:00:00Z"),
    )
    after = client.metrics_snapshot()

    _check("provider metrics count /trips requests",
           len(fake.calls) == 1
           and len(result) == 1
           and before["total_requests"] == 0
           and after["total_requests"] == 1
           and after["request_count_by_endpoint"].get("/trips") == 1,
           f"before={before!r}, after={after!r}, calls={fake.calls!r}")
    _check("provider metrics track response parse timing for /trips",
           after["response_parse_count_by_endpoint"].get("/trips") == 1
           and after["request_elapsed_seconds_by_endpoint"].get("/trips", -1) >= 0
           and after["response_parse_elapsed_seconds_by_endpoint"].get("/trips", -1) >= 0,
           f"after={after!r}")

    after["request_count_by_endpoint"]["/trips"] = 999
    fresh = client.metrics_snapshot()
    _check("provider metrics snapshot is a defensive copy",
           fresh["request_count_by_endpoint"].get("/trips") == 1,
           f"fresh={fresh!r}")


def test_fetch_notifications_uses_documented_alerts_query_params() -> None:
    client, fake = _client_with_payloads({
        1: {
            "data": [],
            "meta": {"current_page": 1, "last_page": 1},
        },
    })
    result = client.fetch_notifications(
        window_start_ts=_ts("2026-04-01T08:00:00Z"),
        window_end_ts=_ts("2026-04-01T09:00:00Z"),
    )
    params = fake.calls[0]["params"]
    _check("notifications fetch uses /alerts/notifications",
           fake.calls[0]["url"] == "https://fleet.example.test/alerts/notifications",
           f"calls={fake.calls!r}")
    _check("notifications fetch uses documented filter date params",
           params.get("filter[date_from]") == "2026-04-01 08:00:00"
           and params.get("filter[date_to]") == "2026-04-01 09:00:00"
           and "date_from" not in params
           and "date_to" not in params,
           f"params={params!r}")
    _check("notifications fetch includes page and limit params",
           params.get("page") == 1
           and params.get("limit") == 2
           and result == [],
           f"params={params!r}, result={result!r}")


def test_fetch_vehicles_fleet_paginates_and_normalizes_metadata() -> None:
    client, fake = _client_with_payloads({
        1: {
            "data": [{
                "vehicle_id": 42,
                "registration": " ABC123 ",
                "vehicle_name": " Big car ",
                "client_vehicle_description": " The oldest car ",
                "client_vehicle_description2": " ignored 2 ",
                "client_vehicle_description3": " ignored 3 ",
                "chassis_number": " CH-1 ",
            }],
            "meta": {"current_page": 1, "last_page": 2, "total": 2},
        },
        2: {
            "data": [{
                "vehicle_id": 43,
                "registration": "XYZ987",
                "vehicle_name": None,
                "client_vehicle_description": "",
                "chassis_number": None,
            }],
            "meta": {"current_page": 2, "last_page": 2, "total": 2},
        },
    })
    result = client.fetch_vehicles_fleet(max_pages=10, max_records=10, sub_window_label="vehicle-inventory-test")

    _check("GET /vehicles inventory paginates",
           len(fake.calls) == 2
           and fake.calls[0]["url"] == "https://fleet.example.test/vehicles"
           and fake.calls[0]["params"] == {"page": 1, "limit": 2}
           and fake.calls[1]["params"] == {"page": 2, "limit": 2},
           f"calls={fake.calls!r}")
    _check("GET /vehicles inventory normalizes metadata fields",
           result == [
               {
                   "vehicle_id": 42,
                   "registration": "ABC123",
                   "vehicle_name": "Big car",
                   "vehicle_description": "The oldest car",
                   "chassis_number": "CH-1",
                   "raw": {
                       "vehicle_id": 42,
                       "registration": " ABC123 ",
                       "vehicle_name": " Big car ",
                       "client_vehicle_description": " The oldest car ",
                       "client_vehicle_description2": " ignored 2 ",
                       "client_vehicle_description3": " ignored 3 ",
                       "chassis_number": " CH-1 ",
                   },
               },
               {
                   "vehicle_id": 43,
                   "registration": "XYZ987",
                   "vehicle_name": None,
                   "vehicle_description": None,
                   "chassis_number": None,
                   "raw": {
                       "vehicle_id": 43,
                       "registration": "XYZ987",
                       "vehicle_name": None,
                       "client_vehicle_description": "",
                       "chassis_number": None,
                   },
               },
           ],
           f"result={result!r}")


def test_fetch_vehicles_fleet_enforces_max_records_without_per_vehicle_calls() -> None:
    client, fake = _client_with_payloads({
        1: {
            "data": [
                {"vehicle_id": 1, "registration": "REG-1", "vehicle_name": "One"},
                {"vehicle_id": 2, "registration": "REG-2", "vehicle_name": "Two"},
            ],
            "meta": {"current_page": 1, "last_page": 2, "total": 4},
        },
        2: {
            "data": [
                {"vehicle_id": 3, "registration": "REG-3", "vehicle_name": "Three"},
                {"vehicle_id": 4, "registration": "REG-4", "vehicle_name": "Four"},
            ],
            "meta": {"current_page": 2, "last_page": 2, "total": 4},
        },
    })
    result = client.fetch_vehicles_fleet(max_pages=10, max_records=3, sub_window_label="vehicle-inventory-cap")

    _check("GET /vehicles max_records cap truncates safely",
           len(result) == 3 and [r["vehicle_id"] for r in result] == [1, 2, 3],
           f"result={result!r}")
    _check("GET /vehicles inventory uses only fleet endpoint",
           all(call["url"] == "https://fleet.example.test/vehicles" for call in fake.calls),
           f"calls={fake.calls!r}")


def test_fetch_drivers_fleet_paginates_and_normalizes_restrictions() -> None:
    client, fake = _client_with_payloads({
        1: {
            "data": [{
                "driver_id": " DRV-1 ",
                "first_name": " Ann ",
                "last_name": " Driver ",
                "identification_tag_id": " TAG-1 ",
                "license_driver_restrictions": " Weekdays only ",
            }],
            "meta": {"current_page": 1, "last_page": 2, "total": 2},
        },
        2: {
            "data": [{
                "driver_id": "DRV-2",
                "first_name": "No",
                "last_name": "Restriction",
                "last_identification_tag_id": "TAG-2",
                "license_driver_restrictions": "",
            }],
            "meta": {"current_page": 2, "last_page": 2, "total": 2},
        },
    })
    result = client.fetch_drivers_fleet(max_pages=10, max_records=10, sub_window_label="driver-inventory-test")

    _check("GET /drivers inventory paginates",
           len(fake.calls) == 2
           and fake.calls[0]["url"] == "https://fleet.example.test/drivers"
           and fake.calls[0]["params"] == {"page": 1, "limit": 2}
           and fake.calls[1]["params"] == {"page": 2, "limit": 2},
           f"calls={fake.calls!r}")
    _check("GET /drivers inventory normalizes license restrictions",
           result == [
               {
                   "driver_id": "DRV-1",
                   "first_name": "Ann",
                   "last_name": "Driver",
                   "identification_tag_id": "TAG-1",
                   "license_driver_restrictions": "Weekdays only",
                   "raw": {
                       "driver_id": " DRV-1 ",
                       "first_name": " Ann ",
                       "last_name": " Driver ",
                       "identification_tag_id": " TAG-1 ",
                       "license_driver_restrictions": " Weekdays only ",
                   },
               },
               {
                   "driver_id": "DRV-2",
                   "first_name": "No",
                   "last_name": "Restriction",
                   "identification_tag_id": "TAG-2",
                   "license_driver_restrictions": None,
                   "raw": {
                       "driver_id": "DRV-2",
                       "first_name": "No",
                       "last_name": "Restriction",
                       "last_identification_tag_id": "TAG-2",
                       "license_driver_restrictions": "",
                   },
               },
           ],
           f"result={result!r}")


def main() -> int:
    test_provider_page_limit_default_and_env_override()
    test_fetch_trips_uses_default_page_limit_1000()
    test_fetch_vehicles_uses_default_page_limit_1000()
    test_stops_at_last_page_and_normalizes()
    test_vehicle_events_limit_is_capped_at_1000_and_stats_available()
    test_provider_retry_backoff_on_timeout()
    test_provider_rate_limit_sleep_between_requests()
    test_stops_on_empty_data()
    test_keeps_duplicate_vehicle_event_rows()
    test_stops_at_max_pages()
    test_http_422_context_includes_request_params_body_and_safe_headers()
    test_no_per_registration_vehicle_events_endpoint()
    test_fetch_vehicle_events_registration_uses_registration_query_param()
    test_fetch_trips_includes_private_by_default()
    test_provider_metrics_snapshot_tracks_request_and_parse_timing()
    test_fetch_notifications_uses_documented_alerts_query_params()
    test_fetch_vehicles_fleet_paginates_and_normalizes_metadata()
    test_fetch_vehicles_fleet_enforces_max_records_without_per_vehicle_calls()
    test_fetch_drivers_fleet_paginates_and_normalizes_restrictions()
    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} check(s) failed:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("\nOK - fetch_vehicle_events_fleet manual tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
