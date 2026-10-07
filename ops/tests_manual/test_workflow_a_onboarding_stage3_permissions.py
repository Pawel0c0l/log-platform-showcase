#!/usr/bin/env python3
"""Manual checks for Stage 3 permission bootstrap in Workflow A onboarding.

Run:

    PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_workflow_a_onboarding_stage3_permissions.py
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


def _cfg() -> onboard.OnboardConfig:
    return onboard.OnboardConfig(
        client_name="Test",
        client_key="TEST",
        client_code="TEST00001",
        enabled=True,
        provider_type="telematics",
        provider_base_url="https://fleet.example.test",
        client_db_host="127.0.0.1",
        client_db_port=5432,
        client_db_name="test_main",
        client_db_schema="public",
        speed_trigger_filter_text="SPEEDING",
        trip_metrics_population_source=TRIP_METRICS_SOURCE_API,
        api_username_env="TEST_API_USERNAME",
        api_key_env="TEST_API_KEY",
        db_username_env="TEST_DB_USERNAME",
        db_key_env="TEST_DB_KEY",
        api_username="api-user",
        api_key="api-key",
        db_username="test_user",
        db_key="db-secret",
    )


class FakeConn:
    def close(self):
        pass


class FakePsycopg:
    def __init__(self):
        self.calls = []

    def connect(self, dsn, autocommit=False):
        self.calls.append({"dsn": dsn, "autocommit": autocommit})
        return FakeConn()


def test_onboarding_dry_run_prints_stage3_grants_without_connecting() -> None:
    fake = FakePsycopg()
    original_require = onboard._require

    def fake_require(module_name: str, pip_name: str | None = None):
        if module_name == "psycopg":
            return fake
        return original_require(module_name, pip_name)

    onboard._require = fake_require
    try:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            onboard.apply_workflow_b_stage3_permissions(_cfg(), apply=False)
    finally:
        onboard._require = original_require

    text = out.getvalue()
    assert 'GRANT CONNECT ON DATABASE "test_main" TO "workflow_b_stage3_loader";' in text
    assert fake.calls == []
    print("PASS: onboarding dry-run prints Stage 3 grants without applying them")


def test_onboarding_apply_calls_stage3_permission_bootstrap() -> None:
    fake = FakePsycopg()
    calls = []
    original_require = onboard._require

    def fake_require(module_name: str, pip_name: str | None = None):
        if module_name == "psycopg":
            return fake
        return original_require(module_name, pip_name)

    def fake_ensure(conn, client_code, client_db_name, client_db_user):
        calls.append((conn, client_code, client_db_name, client_db_user))

    original_ensure = onboard.stage3_permissions.ensure_stage3_permissions_for_client
    onboard._require = fake_require
    onboard.stage3_permissions.ensure_stage3_permissions_for_client = fake_ensure
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            onboard.apply_workflow_b_stage3_permissions(_cfg(), apply=True)
    finally:
        onboard._require = original_require
        onboard.stage3_permissions.ensure_stage3_permissions_for_client = original_ensure

    assert len(calls) == 1
    _conn, client_code, client_db_name, client_db_user = calls[0]
    assert client_code == "TEST00001"
    assert client_db_name == "test_main"
    assert client_db_user == "test_user"
    assert fake.calls and "dbname=postgres" in fake.calls[0]["dsn"]
    print("PASS: onboarding apply calls shared Stage 3 permission bootstrap")


def test_onboarding_flow_contains_stage3_permission_step() -> None:
    source = (REPO_ROOT / "scripts/onboard_workflow_a_client.py").read_text(encoding="utf-8")
    assert "apply_workflow_b_stage3_permissions(cfg, apply=False)" in source
    assert "apply_workflow_b_stage3_permissions(cfg, apply=True)" in source
    print("PASS: onboarding main flow includes Stage 3 permission bootstrap")


def main() -> int:
    test_onboarding_dry_run_prints_stage3_grants_without_connecting()
    test_onboarding_apply_calls_stage3_permission_bootstrap()
    test_onboarding_flow_contains_stage3_permission_step()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
