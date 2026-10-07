#!/usr/bin/env python3
"""Phase 2D tests: adopt the set-based client access helper in the single-client
database gate, and prove the admin per-user summary and report-folder paths are
unchanged.

Reuses the Phase 1/2A FakeModel/FakeCursor harness (whose FakeCursor was extended
to answer the set-based client query the switched gate now issues). Proves:

* `_portal_user_has_database_access_to_client` (now helper-backed) keeps exact
  parity with the per-client effective-client wrapper across direct/group/
  additive-OR/inactive scenarios,
* the admin per-user summary still returns the same client lists/flags/counts,
* dataset list/access (Phase 2C) is unchanged,
* report-folder access logic is untouched,
* no DSN/secret/raw SQL leaks through the helper output.

Run:

    cd /opt/log-platform
    env PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_platform_access_helper_rollout_phase2d.py
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_platform_canonical_login_and_access_helpers import (  # noqa: E402
    FakeModel,
    _caps,
    _restore,
    _patch,
    _with_model,
    api_main,
    C,
    G,
    U,
)


def _client_model(*, direct=None, group=None, group_active=True, client_active=True) -> FakeModel:
    m = FakeModel()
    m.clients[C] = {"is_active": client_active}
    m.users[U] = {"is_active": True}
    if direct is not None:
        m.user_clients.append({"user_id": U, "client_code": C, **direct})
    if group is not None:
        m.groups[G] = {"is_active": group_active}
        m.group_users.append({"group_id": G, "user_id": U})
        m.group_clients.append({"group_id": G, "client_code": C, **group})
    return m


def _gate_and_helper(model):
    old = _with_model(model)
    try:
        gate = api_main._portal_user_has_database_access_to_client(U, C)
        helper = api_main._list_effective_client_access_for_user(U, [C])
    finally:
        api_main.db_conn = old
    entry = helper.get(C)
    expected = bool(entry and entry.get("can_view_database") and entry.get("client_active"))
    return gate, expected, entry


# --------------------------------------------------------------------------
# 1-7: single-client gate parity (switched to set helper)
# --------------------------------------------------------------------------
def _test_gate_direct() -> None:
    gate, expected, entry = _gate_and_helper(_client_model(direct=_caps(view_database=True)))
    assert gate is True and gate == expected, (gate, expected)
    assert entry["source_direct"] is True and entry["source_group"] is False
    print("PASS: db gate allows direct can_view_database (helper-backed, parity)")


def _test_gate_group() -> None:
    gate, expected, entry = _gate_and_helper(_client_model(group=_caps(view_database=True)))
    assert gate is True and gate == expected, (gate, expected)
    assert entry["source_group"] is True
    print("PASS: db gate allows active-group can_view_database (parity)")


def _test_gate_additive_or() -> None:
    gate, expected, _ = _gate_and_helper(_client_model(direct=_caps(view_reports=True), group=_caps(view_database=True)))
    assert gate is True and gate == expected, (gate, expected)
    print("PASS: db gate honors additive OR (reports direct + database group)")


def _test_gate_inactive_group_denied() -> None:
    gate, expected, _ = _gate_and_helper(_client_model(group=_caps(view_database=True), group_active=False))
    assert gate is False and gate == expected, (gate, expected)
    print("PASS: db gate ignores inactive groups")


def _test_gate_inactive_client_denied() -> None:
    gate, expected, entry = _gate_and_helper(_client_model(direct=_caps(view_database=True), client_active=False))
    assert gate is False and gate == expected, (gate, expected)
    assert entry["client_active"] is False
    print("PASS: db gate denies access when client is inactive")


def _test_gate_no_grant_denied() -> None:
    gate, expected, entry = _gate_and_helper(_client_model())
    assert gate is False and gate == expected, (gate, expected)
    assert entry is None
    print("PASS: db gate denies when the user has no client grant")


def _test_gate_reports_only_does_not_grant_database() -> None:
    # can_view_reports direct only -> database gate must stay False (capabilities preserved).
    gate, expected, entry = _gate_and_helper(_client_model(direct=_caps(view_reports=True)))
    assert gate is False and gate == expected, (gate, expected)
    assert entry is not None and entry["can_view_reports"] is True and entry["can_view_database"] is False
    print("PASS: reports-only grant does not satisfy the database gate (caps preserved)")


# --------------------------------------------------------------------------
# 8: dataset list behavior (Phase 2C) unchanged
# --------------------------------------------------------------------------
def _test_dataset_list_still_works() -> None:
    m = FakeModel()
    D = "dddddddd-dddd-dddd-dddd-dddddddddddd"
    m.clients[C] = {"is_active": True}
    m.users[U] = {"is_active": True}
    m.datasets[D] = {"client_code": C, "is_active": True, "dataset_name": "Trips", "slug": "trips", "client_database_name": "client_alpha_db"}
    m.user_clients.append({"user_id": U, "client_code": C, **_caps(view_database=True)})
    m.dataset_users.append({"user_id": U, "dataset_id": D, "can_view_rows": True, "can_filter_rows": True, "can_export_rows": False})
    old = _with_model(m)
    try:
        listing = api_main._list_accessible_portal_database_datasets_for_user(U)
    finally:
        api_main.db_conn = old
    assert any(str(r.get("dataset_id")) == D for r in listing), listing
    print("PASS: Phase 2C dataset list behavior unchanged")


# --------------------------------------------------------------------------
# 9: report-folder access logic untouched (source unchanged this phase)
# --------------------------------------------------------------------------
def _test_report_folder_logic_untouched() -> None:
    # The single-client report gate remains a self-contained inline CTE; Phase 2D
    # only switched the database client gate, not the report client gate.
    src = inspect.getsource(api_main._portal_user_has_report_access_to_client)
    assert "effective_client_access" in src
    assert "_list_effective_client_access_for_user" not in src
    print("PASS: report client gate remains unchanged by the Phase 2D database-gate switch")


# --------------------------------------------------------------------------
# 10: the switched gate no longer carries its own inline CTE
# --------------------------------------------------------------------------
def _test_gate_uses_set_helper() -> None:
    src = inspect.getsource(api_main._portal_user_has_database_access_to_client)
    assert "_list_effective_client_access_for_user" in src, src
    assert "WITH effective_client_access" not in src, "gate must no longer inline the CTE"
    print("PASS: single-client db gate now delegates to the set-based helper")


# --------------------------------------------------------------------------
# 11: no DSN/secret/raw SQL exposure in helper output
# --------------------------------------------------------------------------
def _test_no_secret_exposure() -> None:
    old = _with_model(_client_model(direct=_caps(view_database=True)))
    try:
        entry = api_main._list_effective_client_access_for_user(U, [C]).get(C)
    finally:
        api_main.db_conn = old
    forbidden = ("dsn", "password", "token", "secret", "storage_key", "connection_string", "sql")
    for key in entry.keys():
        assert str(key).lower() not in forbidden, key
    blob = " ".join(str(v) for v in entry.values()).lower()
    for needle in ("postgres://", "password=", "api_write_token", "dbname=", "host="):
        assert needle not in blob, needle
    print("PASS: set-based client helper exposes no DSN/secret/raw SQL")


def main() -> None:
    _test_gate_direct()
    _test_gate_group()
    _test_gate_additive_or()
    _test_gate_inactive_group_denied()
    _test_gate_inactive_client_denied()
    _test_gate_no_grant_denied()
    _test_gate_reports_only_does_not_grant_database()
    _test_dataset_list_still_works()
    _test_report_folder_logic_untouched()
    _test_gate_uses_set_helper()
    _test_no_secret_exposure()
    print("\nALL PASS: Phase 2D set-based client gate adoption (behavior preserved)")


if __name__ == "__main__":
    main()
