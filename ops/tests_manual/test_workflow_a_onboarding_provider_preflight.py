#!/usr/bin/env python3
"""Manual tests for Workflow A onboarding provider auth preflight.

Run from repo root:

    PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_workflow_a_onboarding_provider_preflight.py
"""
from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import onboard_workflow_a_client as onboard  # noqa: E402
from jobs.trip_metrics_population_source import TRIP_METRICS_SOURCE_API  # noqa: E402


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
    def __init__(self, payload: object, *, status_code: int = 200, text: str = ""):
        self.payload = payload
        self.status_code = status_code
        self.text = text or str(payload)

    def json(self) -> object:
        return self.payload


class FakeRequests:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.calls: list[dict] = []

    def get(self, url: str, *, params: dict, auth: tuple[str, str], timeout: int):
        self.calls.append({
            "url": url,
            "params": dict(params),
            "auth": auth,
            "timeout": timeout,
        })
        return self.response


def _cfg() -> onboard.OnboardConfig:
    return onboard.OnboardConfig(
        client_name="Test Client",
        client_key="TEST",
        client_code="TEST00001",
        enabled=True,
        provider_type="telematics",
        provider_base_url="https://fleet.example.test",
        client_db_host="127.0.0.1",
        client_db_port=5432,
        client_db_name="client_test",
        client_db_schema="public",
        speed_trigger_filter_text="SPEEDING",
        trip_metrics_population_source=TRIP_METRICS_SOURCE_API,
        api_username_env="TEST_API_USERNAME",
        api_key_env="TEST_API_KEY",
        db_username_env="TEST_DB_USERNAME",
        db_key_env="TEST_DB_KEY",
        api_username="api-user",
        api_key="api-key",
        db_username="db-user",
        db_key="db-key",
    )


@contextlib.contextmanager
def _patched_requests(fake: FakeRequests):
    original_require = onboard._require

    def fake_require(module_name: str, pip_name: str | None = None):
        if module_name == "requests":
            return fake
        return original_require(module_name, pip_name)

    onboard._require = fake_require
    try:
        yield
    finally:
        onboard._require = original_require


def test_preflight_uses_lightweight_vehicles_endpoint() -> None:
    fake = FakeRequests(FakeResponse({"data": []}, status_code=200))
    out = io.StringIO()
    with _patched_requests(fake), contextlib.redirect_stdout(out):
        onboard.provider_auth_preflight(_cfg())

    _check("provider preflight issued one request",
           len(fake.calls) == 1,
           f"calls={fake.calls!r}")
    call = fake.calls[0]
    _check("provider preflight uses GET /vehicles",
           call["url"] == "https://fleet.example.test/vehicles",
           f"call={call!r}")
    _check("provider preflight uses only lightweight pagination params",
           call["params"] == {"limit": 1, "page": 1},
           f"params={call['params']!r}")
    _check("provider preflight does not request /trips or incl_private",
           "/trips" not in call["url"] and "incl_private" not in call["params"],
           f"call={call!r}")
    _check("provider preflight uses configured credentials and reasonable timeout",
           call["auth"] == ("api-user", "api-key")
           and call["timeout"] == onboard.PROVIDER_AUTH_PREFLIGHT_TIMEOUT_S
           and 15 <= call["timeout"] <= 30,
           f"call={call!r}")
    _check("provider preflight log mentions lightweight auth check",
           "Lightweight auth check" in out.getvalue()
           and "GET /vehicles" in out.getvalue(),
           out.getvalue())


def test_preflight_success_requires_minimal_json_shape() -> None:
    fake = FakeRequests(FakeResponse({"data": [{"vehicle_id": 1}]}, status_code=200))
    with _patched_requests(fake), contextlib.redirect_stdout(io.StringIO()):
        onboard.provider_auth_preflight(_cfg())
    _check("successful /vehicles JSON response passes onboarding preflight",
           len(fake.calls) == 1)


def test_preflight_auth_failure_raises() -> None:
    fake = FakeRequests(FakeResponse({"message": "unauthorized"}, status_code=401, text="unauthorized"))
    try:
        with _patched_requests(fake), contextlib.redirect_stdout(io.StringIO()):
            onboard.provider_auth_preflight(_cfg())
        _check("auth failure raises OnboardError", False)
    except onboard.OnboardError as exc:
        _check("auth failure raises OnboardError",
               "Provider auth FAILED (HTTP 401)" in str(exc),
               f"error={exc!r}")


def test_seed_sql_includes_client_code() -> None:
    src = (REPO_ROOT / "scripts" / "onboard_workflow_a_client.py").read_text(encoding="utf-8")
    _check("schedule seed INSERT includes client_code",
           "client_id, client_code, dataset_name, enabled" in src)
    _check("retention seed INSERT includes client_code",
           "client_id, client_code, table_name, enabled, retention_days" in src)
    _check("seed function accepts client_code",
           "def seed_dataset_schedule_and_retention(\n    client_id: str, client_code: Optional[str]" in src)


def main() -> int:
    test_preflight_uses_lightweight_vehicles_endpoint()
    test_preflight_success_requires_minimal_json_shape()
    test_preflight_auth_failure_raises()
    test_seed_sql_includes_client_code()
    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} check(s) failed:")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("\nOK - onboarding provider preflight checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
