#!/usr/bin/env python3
"""Focused tests for the local/dev-only identity provisioning utility."""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.common import environment_identity
from ops import provision_local_environment_identity as provision


class PatchTarget:
    def __init__(self, target, **attrs):
        self.target = target
        self.attrs = attrs
        self.originals = {}

    def __enter__(self):
        for name, value in self.attrs.items():
            self.originals[name] = getattr(self.target, name)
            setattr(self.target, name, value)

    def __exit__(self, exc_type, exc, tb):
        for name, value in self.originals.items():
            setattr(self.target, name, value)


def runtime(environment: str):
    return environment_identity.RuntimeIdentity(
        environment=environment,
        platform_identity_id="bd7662a5-eeb4-4614-8720-d477abfcb227",
        postgres_host="127.0.0.1",
        postgres_port=5432,
        postgres_db="logdb",
        postgres_user="loguser",
    )


def test_production_target_is_refused_before_database_connection() -> None:
    called = {"connect": 0}

    def fail_connect():
        called["connect"] += 1
        raise AssertionError("production refusal must happen before DB connection")

    with PatchTarget(
        provision.environment_identity,
        load_runtime_identity=lambda: runtime("production"),
    ), PatchTarget(provision, _platform_conn=fail_connect):
        try:
            provision.provision_local_identity(
                client_code="ALPHA00001",
                platform_identity_id="bd7662a5-eeb4-4614-8720-d477abfcb227",
                client_identity_id="b454f82c-5857-4bab-8342-b7258e5cf7de",
                apply=True,
            )
        except environment_identity.EnvironmentIdentityError as exc:
            assert "local/dev-only" in str(exc)
        else:
            raise AssertionError("production provisioning must be refused")
    assert called["connect"] == 0


def test_conflicting_marker_is_never_replaced() -> None:
    existing = {
        "identity_key": "primary",
        "environment": "production",
        "database_identity_id": "f6222a11-06ee-4e4f-8b25-302a9d963cfa",
        "database_role": "platform",
        "database_name": "logdb",
        "client_code": None,
    }
    expected = {
        "identity_key": "primary",
        "environment": "local_dev",
        "database_identity_id": "bd7662a5-eeb4-4614-8720-d477abfcb227",
        "database_role": "platform",
        "database_name": "logdb",
        "client_code": None,
    }
    try:
        provision._require_empty_or_exact(existing, expected, "platform")
    except environment_identity.EnvironmentIdentityError as exc:
        assert exc.code == "PROVISION_MARKER_CONFLICT"
    else:
        raise AssertionError("conflicting marker must be refused")


def test_inspect_decision_is_idempotent() -> None:
    marker = {
        "identity_key": "primary",
        "environment": "local_dev",
        "database_identity_id": "bd7662a5-eeb4-4614-8720-d477abfcb227",
        "database_role": "platform",
        "database_name": "logdb",
        "client_code": None,
    }
    assert provision._require_empty_or_exact(None, marker, "platform") is True
    assert provision._require_empty_or_exact(dict(marker), marker, "platform") is False


def main() -> None:
    test_production_target_is_refused_before_database_connection()
    test_conflicting_marker_is_never_replaced()
    test_inspect_decision_is_idempotent()
    print("OK - local environment identity provisioning regressions passed")


if __name__ == "__main__":
    main()
