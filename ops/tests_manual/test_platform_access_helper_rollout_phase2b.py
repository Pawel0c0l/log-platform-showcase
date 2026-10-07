#!/usr/bin/env python3
"""Phase 2B tests: selected Client Database Explorer access call sites now resolve
their effective row capabilities through the central helper wrapper while the
combined gating query keeps owning visibility, metadata, and client-DB routing.

These tests reuse the in-memory fake database harness from the Phase 1/2A parity
test so the helper-backed `_get_portal_database_dataset_for_user` is exercised on
the same shared model that proves parity. They verify that switching the
capability source did not change any permission behavior, filtering/export
gating, or multi-database routing.

Run:

    cd /opt/log-platform
    env PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_platform_access_helper_rollout_phase2b.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
# Allow importing the sibling Phase 1/2A harness by module name.
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_platform_canonical_login_and_access_helpers import (  # noqa: E402
    FakeModel,
    _caps,
    _restore,
    _with_model,
    api_main,
    C,
    D,
    G,
    U,
)


MAPPED_DB = "client_alpha_db"


class RoutingFakeModel(FakeModel):
    """FakeModel whose combined-query dataset row carries the mapped client DB
    name, so routing-field preservation can be asserted after the overlay."""

    def _dataset_row(self, did):
        row = super()._dataset_row(did)
        ds = self.datasets.get(did, {})
        row["client_database_name"] = ds.get("client_database_name")
        return row


def _ds_model(
    *,
    client_db_direct=False,
    client_db_group=False,
    group_active=True,
    view_rows_direct=None,
    filter_direct=None,
    export_direct=None,
    view_rows_group=None,
    filter_group=None,
    export_group=None,
    dataset_active=True,
    client_active=True,
    user_active=True,
    database_name=MAPPED_DB,
) -> RoutingFakeModel:
    m = RoutingFakeModel()
    m.clients[C] = {"is_active": client_active}
    m.users[U] = {"is_active": user_active}
    m.datasets[D] = {
        "client_code": C,
        "is_active": dataset_active,
        "dataset_name": "Trips",
        "slug": "trips",
        "client_database_name": database_name,
    }

    def _ensure_group_membership():
        if G not in m.groups:
            m.groups[G] = {"is_active": group_active}
        if not any(gu["group_id"] == G and gu["user_id"] == U for gu in m.group_users):
            m.group_users.append({"group_id": G, "user_id": U})

    if client_db_direct:
        m.user_clients.append({"user_id": U, "client_code": C, **_caps(view_database=True)})
    if client_db_group:
        _ensure_group_membership()
        m.group_clients.append({"group_id": G, "client_code": C, **_caps(view_database=True)})
    if any(v is not None for v in (view_rows_direct, filter_direct, export_direct)):
        m.dataset_users.append(
            {
                "user_id": U,
                "dataset_id": D,
                "can_view_rows": bool(view_rows_direct),
                "can_filter_rows": bool(filter_direct),
                "can_export_rows": bool(export_direct),
            }
        )
    if any(v is not None for v in (view_rows_group, filter_group, export_group)):
        _ensure_group_membership()
        m.dataset_groups.append(
            {
                "group_id": G,
                "dataset_id": D,
                "can_view_rows": bool(view_rows_group),
                "can_filter_rows": bool(filter_group),
                "can_export_rows": bool(export_group),
            }
        )
    return m


def _accessor_and_helper(model):
    """Return (dataset_dict_or_None, details_helper_dict) for the shared model."""
    old = _with_model(model)
    try:
        dataset = api_main._get_portal_database_dataset_for_user(D, U)
        details = api_main._get_effective_dataset_access_details_for_user(U, D)
    finally:
        api_main.db_conn = old
    return dataset, details


def _assert_overlay_parity(dataset, details, label):
    """When accessible, the dataset dict's three capability flags must equal the
    central helper's flags (that is the whole point of the switch)."""
    assert dataset is not None, (label, "expected accessible dataset")
    for cap in ("can_view_rows", "can_filter_rows", "can_export_rows"):
        assert bool(dataset.get(cap)) == bool(details.get(cap)), (label, cap, dataset.get(cap), details.get(cap))


# --------------------------------------------------------------------------
# 1-3: direct, group, additive OR
# --------------------------------------------------------------------------
def _test_direct_dataset_access() -> None:
    m = _ds_model(client_db_direct=True, view_rows_direct=True, filter_direct=True, export_direct=False)
    dataset, details = _accessor_and_helper(m)
    assert dataset is not None, "direct access must remain visible"
    assert dataset["can_view_rows"] is True
    assert dataset["can_filter_rows"] is True
    assert dataset["can_export_rows"] is False
    _assert_overlay_parity(dataset, details, "direct")
    # routing fields preserved by the overlay
    assert dataset["client_code"] == C
    assert dataset["client_database_name"] == MAPPED_DB
    print("PASS: direct dataset access preserved; flags come from central helper")


def _test_group_dataset_access() -> None:
    m = _ds_model(client_db_group=True, view_rows_group=True, filter_group=False, export_group=True)
    dataset, details = _accessor_and_helper(m)
    assert dataset is not None, "group-derived access must remain visible"
    assert dataset["can_view_rows"] is True
    assert dataset["can_filter_rows"] is False
    assert dataset["can_export_rows"] is True
    _assert_overlay_parity(dataset, details, "group")
    print("PASS: group-derived dataset access preserved via central helper")


def _test_additive_or() -> None:
    # direct grants view+filter (no export); active group grants export.
    m = _ds_model(
        client_db_direct=True,
        view_rows_direct=True,
        filter_direct=True,
        export_direct=False,
        view_rows_group=True,
        filter_group=False,
        export_group=True,
    )
    dataset, details = _accessor_and_helper(m)
    assert dataset is not None
    assert dataset["can_filter_rows"] is True, "filter from direct grant"
    assert dataset["can_export_rows"] is True, "export from group grant (additive OR)"
    _assert_overlay_parity(dataset, details, "additive-or")
    print("PASS: direct + group remains additive OR for dataset capabilities")


# --------------------------------------------------------------------------
# 4: inactive groups do not grant access
# --------------------------------------------------------------------------
def _test_inactive_group_no_access() -> None:
    # client prerequisite satisfied directly; row access ONLY via an inactive group.
    m = _ds_model(client_db_direct=True, view_rows_group=True, group_active=False)
    dataset, details = _accessor_and_helper(m)
    assert dataset is None, "inactive group must not grant dataset visibility"
    assert details["accessible"] is False, details
    print("PASS: inactive group does not grant dataset access")


# --------------------------------------------------------------------------
# 5: missing client-level can_view_database blocks dataset visibility
# --------------------------------------------------------------------------
def _test_missing_client_db_blocks() -> None:
    m = _ds_model(client_db_direct=False, view_rows_direct=True)
    dataset, details = _accessor_and_helper(m)
    assert dataset is None, "no client can_view_database must block dataset visibility"
    assert details["client_can_view_database"] is False, details
    assert details["accessible"] is False, details
    print("PASS: missing client can_view_database still blocks dataset visibility")


# --------------------------------------------------------------------------
# 6: missing dataset-level can_view_rows blocks dataset visibility
# --------------------------------------------------------------------------
def _test_missing_view_rows_blocks() -> None:
    m = _ds_model(client_db_direct=True)  # client prereq met, but no dataset grant
    dataset, details = _accessor_and_helper(m)
    assert dataset is None, "no dataset can_view_rows must block visibility"
    assert details["accessible"] is False, details
    print("PASS: missing dataset can_view_rows still blocks dataset visibility")


# --------------------------------------------------------------------------
# 7: can_filter_rows=false blocks filter use
# --------------------------------------------------------------------------
def _filter_columns():
    return [
        {"column_name": "trip_date", "display_name": "Trip date", "is_visible": True, "is_filterable": True, "is_sortable": True, "is_default_date_column": False, "data_type": "text", "display_order": 1},
        {"column_name": "driver", "display_name": "Driver", "is_visible": True, "is_filterable": True, "is_sortable": False, "is_default_date_column": False, "data_type": "text", "display_order": 2},
    ]


def _test_filter_gate_blocks_when_disabled() -> None:
    m = _ds_model(client_db_direct=True, view_rows_direct=True, filter_direct=False, export_direct=False)
    dataset, _ = _accessor_and_helper(m)
    assert dataset is not None and dataset["can_filter_rows"] is False
    params = {"filter__driver": ["bob"]}
    conditions, values, active, error, _entries = api_main._build_portal_database_filter_conditions(dataset, _filter_columns(), params)
    assert error and "Filtering is not enabled" in error, (error, "filter must be rejected when can_filter_rows is False")
    print("PASS: can_filter_rows=false still rejects filter use")


def _test_filter_gate_allows_when_enabled() -> None:
    m = _ds_model(client_db_direct=True, view_rows_direct=True, filter_direct=True, export_direct=False)
    dataset, _ = _accessor_and_helper(m)
    assert dataset is not None and dataset["can_filter_rows"] is True
    params = {"filter__driver": ["bob"]}
    conditions, values, active, error, _entries = api_main._build_portal_database_filter_conditions(dataset, _filter_columns(), params)
    assert error is None, (error, "filter must be allowed when can_filter_rows is True")
    assert conditions and values, (conditions, values)
    print("PASS: can_filter_rows=true still allows filter use")


# --------------------------------------------------------------------------
# 8: can_export_rows gate
# --------------------------------------------------------------------------
def _test_export_gate() -> None:
    blocked, _ = _accessor_and_helper(
        _ds_model(client_db_direct=True, view_rows_direct=True, filter_direct=True, export_direct=False)
    )
    allowed, _ = _accessor_and_helper(
        _ds_model(client_db_direct=True, view_rows_direct=True, filter_direct=True, export_direct=True)
    )
    assert blocked is not None and blocked["can_export_rows"] is False, "export must be blocked"
    assert allowed is not None and allowed["can_export_rows"] is True, "export must be allowed"
    print("PASS: export gating driven by helper-resolved can_export_rows")


# --------------------------------------------------------------------------
# 9-11: inactive dataset / client / user
# --------------------------------------------------------------------------
def _test_inactive_dataset_denied() -> None:
    dataset, details = _accessor_and_helper(
        _ds_model(client_db_direct=True, view_rows_direct=True, dataset_active=False)
    )
    assert dataset is None and details["accessible"] is False
    print("PASS: inactive dataset is denied")


def _test_inactive_client_denied() -> None:
    dataset, details = _accessor_and_helper(
        _ds_model(client_db_direct=True, view_rows_direct=True, client_active=False)
    )
    assert dataset is None and details["accessible"] is False
    print("PASS: inactive client is denied")


def _test_inactive_user_denied() -> None:
    dataset, details = _accessor_and_helper(
        _ds_model(client_db_direct=True, view_rows_direct=True, user_active=False)
    )
    assert dataset is None and details["accessible"] is False
    print("PASS: inactive user is denied")


# --------------------------------------------------------------------------
# 12: multi-database routing preserved (mapped client DB, never logdb)
# --------------------------------------------------------------------------
def _test_routing_uses_mapped_client_db() -> None:
    m = _ds_model(client_db_direct=True, view_rows_direct=True, database_name=MAPPED_DB)
    dataset, _ = _accessor_and_helper(m)
    assert dataset is not None
    resolved = api_main._portal_dataset_client_database_name(dataset)
    assert resolved == MAPPED_DB, (resolved, "row browsing/export must target the mapped client DB")

    # And the actual connect path uses that name, not the control-plane db_conn().
    used = []
    old_connect = api_main._connect_portal_client_database

    class _ClientCur:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def execute(self, sql, params=None):
            pass
        def fetchone(self):
            return {"total": 0}
        def fetchall(self):
            return []

    class _ClientConn:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def cursor(self):
            return _ClientCur()

    def _fake_connect(database_name):
        used.append(database_name)
        return _ClientConn()

    api_main._connect_portal_client_database = _fake_connect
    try:
        api_main._count_portal_database_rows(dataset, _filter_columns(), {})
    finally:
        api_main._connect_portal_client_database = old_connect
    assert used == [MAPPED_DB], (used, "count query must connect to the mapped client DB only")
    print("PASS: row counting/export route to the mapped client DB, never logdb")


# --------------------------------------------------------------------------
# 13: no DSN/secrets/raw SQL/client row values exposed in the metadata dict
# --------------------------------------------------------------------------
def _test_no_secret_or_sql_exposure() -> None:
    m = _ds_model(client_db_direct=True, view_rows_direct=True)
    dataset, _ = _accessor_and_helper(m)
    assert dataset is not None
    forbidden_keys = ("dsn", "password", "token", "secret", "storage_key", "connection_string", "sql")
    for key in dataset.keys():
        assert key.lower() not in forbidden_keys, (key, "metadata dict must not expose secrets/SQL")
    blob = " ".join(str(v) for v in dataset.values()).lower()
    for needle in ("postgres://", "password=", "api_write_token", "dbname=", "host="):
        assert needle not in blob, (needle, "no DSN/secret material in dataset metadata")
    print("PASS: dataset metadata exposes no DSN/secret/raw SQL/client row values")


# --------------------------------------------------------------------------
# 14: /user/database list output remains compatible (deferred, set-based query)
# --------------------------------------------------------------------------
def _test_user_database_list_compatible() -> None:
    m = _ds_model(client_db_direct=True, view_rows_direct=True)
    old = _with_model(m)
    try:
        listing = api_main._list_accessible_portal_database_datasets_for_user(U)
    finally:
        api_main.db_conn = old
    assert any(str(row.get("dataset_id")) == D for row in listing), (listing, "dataset must remain in the list")
    print("PASS: /user/database list output remains compatible (still set-based)")


# --------------------------------------------------------------------------
# 15: existing dataset assignment / admin effective wrapper remains compatible
# --------------------------------------------------------------------------
def _test_assignment_workflow_compatible() -> None:
    m = _ds_model(client_db_direct=True, view_rows_direct=True, filter_direct=True, export_direct=True)
    old = _with_model(m)
    try:
        wrapper = api_main._get_effective_dataset_access_for_user(U, D)
        denied = api_main._get_effective_dataset_access_for_user(U, "no-such-dataset")
    finally:
        api_main.db_conn = old
    assert wrapper is not None, "admin effective wrapper must still resolve accessible datasets"
    assert wrapper.get("can_view_rows") is True, wrapper
    assert denied is None, "admin effective wrapper still returns None when not accessible"
    assert callable(api_main._get_portal_database_dataset_assignment_data)
    print("PASS: admin effective wrapper + assignment workflow remain compatible")


def main() -> None:
    _test_direct_dataset_access()
    _test_group_dataset_access()
    _test_additive_or()
    _test_inactive_group_no_access()
    _test_missing_client_db_blocks()
    _test_missing_view_rows_blocks()
    _test_filter_gate_blocks_when_disabled()
    _test_filter_gate_allows_when_enabled()
    _test_export_gate()
    _test_inactive_dataset_denied()
    _test_inactive_client_denied()
    _test_inactive_user_denied()
    _test_routing_uses_mapped_client_db()
    _test_no_secret_or_sql_exposure()
    _test_user_database_list_compatible()
    _test_assignment_workflow_compatible()
    print("\nALL PASS: Phase 2B central access helper rollout (behavior preserved)")


if __name__ == "__main__":
    main()
