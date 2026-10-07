#!/usr/bin/env python3
"""Phase 2C tests: set-based effective-access helpers and the `/user/database`
list switch.

These tests reuse the in-memory FakeModel from the Phase 1/2A harness (which
encodes the additive direct-OR-active-group semantics) and add a set-query-aware
fake cursor that interprets the two new set helpers' SQL. They prove:

* the set-based client/dataset helpers match the established direct/group/
  additive-OR/inactive behavior,
* the `/user/database` list output is unchanged after switching to the set
  helper, with no N+1 (exactly one query),
* multi-database `database_name` and dataset capability flags are preserved,
* assignment eligibility behavior is unchanged.

Run:

    cd /opt/log-platform
    env PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_platform_access_helper_rollout_phase2c.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_platform_canonical_login_and_access_helpers import (  # noqa: E402
    CLIENT_CAPS,
    DATASET_CAPS,
    FakeModel,
    _caps,
    _patch,
    _restore,
    api_main,
    C,
    D,
    G,
    U,
)


def _norm(sql: str) -> str:
    return " ".join(str(sql).lower().split())


class SetCursor:
    """Interprets the two Phase 2C set-helper queries against a FakeModel."""

    def __init__(self, model: FakeModel, calls: list):
        self.model = model
        self.calls = calls
        self._rows: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        s = _norm(sql)
        p = tuple(params or ())
        m = self.model
        self.calls.append(s)
        self._rows = []

        # Dataset set/list helper.
        if "effective_dataset_access" in s and "order by pc.display_name" in s:
            uid = p[0]
            idx = 3
            ds_filter = client_filter = None
            if "pdd.dataset_id = any(%s)" in s:
                ds_filter = {str(x) for x in p[idx]}
                idx += 1
            if "pdd.client_code = any(%s)" in s:
                client_filter = {str(x) for x in p[idx]}
                idx += 1
            ordered = []
            for did, ds in m.datasets.items():
                if not m._dataset_accessible(uid, did, require_user_active=False):
                    continue
                if ds_filter is not None and did not in ds_filter:
                    continue
                if client_filter is not None and str(ds.get("client_code")) not in client_filter:
                    continue
                eff = m._eff_dataset(uid, did)
                direct = m._direct_dataset_caps(uid, did)
                group = m._group_dataset_caps(uid, did)
                row = m._dataset_row(did)
                row["can_view_rows"] = eff["can_view_rows"]
                row["can_filter_rows"] = eff["can_filter_rows"]
                row["can_export_rows"] = eff["can_export_rows"]
                row["client_database_name"] = ds.get("client_database_name")
                row["source_direct"] = any(bool(direct.get(c)) for c in DATASET_CAPS)
                row["source_group"] = any(bool(group.get(c)) for c in DATASET_CAPS)
                ordered.append((str(ds.get("client_code")), str(row.get("dataset_name")), row))
            ordered.sort(key=lambda t: (t[0], t[1]))
            self._rows = [r for _, _, r in ordered]
            return

        # Client set helper.
        if (
            "effective_client_access" in s
            and "join portal_clients pc" in s
            and "effective_dataset_access" not in s
            and "select 1" not in s
        ):
            uid = p[0]
            client_filter = None
            if "client_code = any(%s)" in s:
                client_filter = {str(x) for x in p[1]}
            rows = []
            for code, client in m.clients.items():
                direct = m._direct_client_caps(uid, code)
                group = m._group_client_caps(uid, code)
                via_direct = any(bool(direct.get(c)) for c in CLIENT_CAPS)
                via_group = any(bool(group.get(c)) for c in CLIENT_CAPS)
                if not (via_direct or via_group):
                    continue
                if client_filter is not None and code not in client_filter:
                    continue
                eff = m._eff_client(uid, code)
                rows.append({
                    "client_code": code,
                    "client_name": code,
                    "client_active": client["is_active"],
                    "can_view_reports": eff["can_view_reports"],
                    "can_view_database": eff["can_view_database"],
                    "can_export_database": eff["can_export_database"],
                    "source_direct": via_direct,
                    "source_group": via_group,
                })
            rows.sort(key=lambda r: (str(r["client_name"]), str(r["client_code"])))
            self._rows = rows
            return

        raise AssertionError(f"SetCursor received an unrecognized query:\n{s}")

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class SetConn:
    def __init__(self, model: FakeModel, calls: list):
        self.model = model
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return SetCursor(self.model, self.calls)


def _run(model: FakeModel, fn):
    calls: list = []
    old = _patch("db_conn", lambda: SetConn(model, calls))
    try:
        return fn(), calls
    finally:
        api_main.db_conn = old


# --------------------------------------------------------------------------
# Model builders
# --------------------------------------------------------------------------
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


def _ds_model(*, client_db_direct=False, client_db_group=False, group_active=True,
              rows_direct=None, rows_group=None, dataset_active=True, client_active=True,
              user_active=True, database_name="client_alpha_db") -> FakeModel:
    m = FakeModel()
    m.clients[C] = {"is_active": client_active}
    m.users[U] = {"is_active": user_active}
    m.datasets[D] = {"client_code": C, "is_active": dataset_active, "dataset_name": "Trips", "slug": "trips", "client_database_name": database_name}

    def _group():
        if G not in m.groups:
            m.groups[G] = {"is_active": group_active}
        if not any(gu["group_id"] == G and gu["user_id"] == U for gu in m.group_users):
            m.group_users.append({"group_id": G, "user_id": U})

    if client_db_direct:
        m.user_clients.append({"user_id": U, "client_code": C, **_caps(view_database=True)})
    if client_db_group:
        _group()
        m.group_clients.append({"group_id": G, "client_code": C, **_caps(view_database=True)})
    if rows_direct is not None:
        m.dataset_users.append({"user_id": U, "dataset_id": D, **rows_direct})
    if rows_group is not None:
        _group()
        m.dataset_groups.append({"group_id": G, "dataset_id": D, **rows_group})
    return m


def _ds_caps(view=False, filt=False, export=False):
    return {"can_view_rows": view, "can_filter_rows": filt, "can_export_rows": export}


# --------------------------------------------------------------------------
# 1-5: set-based client helper
# --------------------------------------------------------------------------
def _test_client_set_direct() -> None:
    m = _client_model(direct=_caps(view_database=True))
    res, _ = _run(m, lambda: api_main._list_effective_client_access_for_user(U))
    assert C in res and res[C]["can_view_database"] is True
    assert res[C]["source_direct"] is True and res[C]["source_group"] is False
    print("PASS: set-based client helper resolves direct access")


def _test_client_set_group() -> None:
    m = _client_model(group=_caps(view_database=True))
    res, _ = _run(m, lambda: api_main._list_effective_client_access_for_user(U))
    assert res[C]["can_view_database"] is True
    assert res[C]["source_group"] is True and res[C]["source_direct"] is False
    print("PASS: set-based client helper resolves active-group access")


def _test_client_set_additive_or() -> None:
    m = _client_model(direct=_caps(view_reports=True), group=_caps(view_database=True))
    res, _ = _run(m, lambda: api_main._list_effective_client_access_for_user(U, [C]))
    assert res[C]["can_view_reports"] is True and res[C]["can_view_database"] is True
    assert res[C]["source_direct"] is True and res[C]["source_group"] is True
    print("PASS: set-based client helper keeps additive OR across direct+group")


def _test_client_set_inactive_group_ignored() -> None:
    m = _client_model(group=_caps(view_database=True), group_active=False)
    res, _ = _run(m, lambda: api_main._list_effective_client_access_for_user(U))
    # inactive group contributes nothing -> user has no grant -> client absent
    assert C not in res, res
    print("PASS: set-based client helper ignores inactive groups")


def _test_client_set_inactive_client_flag() -> None:
    m = _client_model(direct=_caps(view_database=True), client_active=False)
    res, _ = _run(m, lambda: api_main._list_effective_client_access_for_user(U))
    assert res[C]["client_active"] is False, res
    print("PASS: set-based client helper reports inactive client via client_active flag")


# --------------------------------------------------------------------------
# 6-12: set-based dataset helper
# --------------------------------------------------------------------------
def _test_dataset_set_direct() -> None:
    m = _ds_model(client_db_direct=True, rows_direct=_ds_caps(view=True, filt=True, export=False))
    res, _ = _run(m, lambda: api_main._list_effective_dataset_access_for_user(U))
    assert D in res and res[D]["can_view_rows"] is True
    assert res[D]["source_direct"] is True
    print("PASS: set-based dataset helper resolves direct dataset access")


def _test_dataset_set_group() -> None:
    m = _ds_model(client_db_group=True, rows_group=_ds_caps(view=True, filt=False, export=True))
    res, _ = _run(m, lambda: api_main._list_effective_dataset_access_for_user(U))
    assert D in res and res[D]["source_group"] is True
    print("PASS: set-based dataset helper resolves active-group dataset access")


def _test_dataset_set_missing_client_db() -> None:
    m = _ds_model(client_db_direct=False, rows_direct=_ds_caps(view=True))
    res, _ = _run(m, lambda: api_main._list_effective_dataset_access_for_user(U))
    assert D not in res, "missing client can_view_database must hide the dataset"
    print("PASS: set-based dataset helper still requires client can_view_database")


def _test_dataset_set_missing_view_rows() -> None:
    m = _ds_model(client_db_direct=True)  # client ok, no dataset grant
    res, _ = _run(m, lambda: api_main._list_effective_dataset_access_for_user(U))
    assert D not in res, "missing can_view_rows must hide the dataset"
    print("PASS: set-based dataset helper still requires dataset can_view_rows")


def _test_dataset_set_capabilities_preserved() -> None:
    m = _ds_model(
        client_db_direct=True,
        rows_direct=_ds_caps(view=True, filt=True, export=False),
        rows_group=_ds_caps(view=True, filt=False, export=True),
    )
    res, _ = _run(m, lambda: api_main._list_effective_dataset_access_for_user(U))
    assert res[D]["can_filter_rows"] is True, "filter from direct grant"
    assert res[D]["can_export_rows"] is True, "export from group grant (additive OR)"
    print("PASS: set-based dataset helper preserves filter/export additive OR")


def _test_dataset_set_inactive_dataset() -> None:
    m = _ds_model(client_db_direct=True, rows_direct=_ds_caps(view=True), dataset_active=False)
    res, _ = _run(m, lambda: api_main._list_effective_dataset_access_for_user(U))
    assert D not in res, "inactive dataset must be hidden"
    print("PASS: set-based dataset helper hides inactive datasets")


def _test_dataset_set_inactive_user() -> None:
    # The list query does not gate on the user row directly; visibility is driven
    # by grants. Mirror the previous list behavior exactly: an inactive user with
    # valid grants still appears in this query (the route layer enforces login).
    m_active = _ds_model(client_db_direct=True, rows_direct=_ds_caps(view=True), user_active=True)
    m_inactive = _ds_model(client_db_direct=True, rows_direct=_ds_caps(view=True), user_active=False)
    res_active, _ = _run(m_active, lambda: api_main._list_effective_dataset_access_for_user(U))
    res_inactive, _ = _run(m_inactive, lambda: api_main._list_effective_dataset_access_for_user(U))
    assert (D in res_active) == (D in res_inactive), "list visibility must match prior behavior regardless of user flag"
    print("PASS: set-based dataset helper preserves prior list visibility semantics")


# --------------------------------------------------------------------------
# 13-15: /user/database list output + N+1 + multi-database fields
# --------------------------------------------------------------------------
def _test_user_database_list_output_and_no_n_plus_one() -> None:
    m = _ds_model(client_db_direct=True, rows_direct=_ds_caps(view=True, filt=True, export=True))
    listing, calls = _run(m, lambda: api_main._list_accessible_portal_database_datasets_for_user(U))
    assert any(str(row.get("dataset_id")) == D for row in listing), (listing, "dataset must remain in list")
    row = next(r for r in listing if str(r.get("dataset_id")) == D)
    # output shape preserved by _portal_database_dataset_row
    for key in ("dataset_id", "client_code", "client_database_name", "dataset_name", "can_view_rows", "can_filter_rows", "can_export_rows", "visible_columns"):
        assert key in row, (key, row)
    assert row["client_database_name"] == "client_alpha_db", row
    # exactly one access query for the whole list -> no N+1
    assert len(calls) == 1, (len(calls), "list path must issue exactly one set query")
    print("PASS: /user/database list output preserved with a single query (no N+1)")


def _test_list_preserves_multi_db_and_order() -> None:
    m = _ds_model(client_db_direct=True, rows_direct=_ds_caps(view=True))
    # add a second dataset for the same client to check ordering survives
    m.datasets["dddddddd-dddd-dddd-dddd-aaaaaaaaaaaa"] = {"client_code": C, "is_active": True, "dataset_name": "Alarms", "slug": "alarms", "client_database_name": "client_alpha_db"}
    m.dataset_users.append({"user_id": U, "dataset_id": "dddddddd-dddd-dddd-dddd-aaaaaaaaaaaa", **_ds_caps(view=True)})
    listing, _ = _run(m, lambda: api_main._list_accessible_portal_database_datasets_for_user(U))
    names = [r.get("dataset_name") for r in listing]
    assert names == sorted(names), (names, "datasets must be ordered by name within a client")
    assert all(r.get("client_database_name") == "client_alpha_db" for r in listing), listing
    print("PASS: list preserves multi-database fields and display ordering")


# --------------------------------------------------------------------------
# 16: assignment eligibility behavior preserved (builders unchanged)
# --------------------------------------------------------------------------
def _test_assignment_eligibility_preserved() -> None:
    seen = {}

    def _fake_users(code):
        seen["user_code"] = code
        return [{"user_id": U}]

    def _fake_groups(code):
        seen["group_code"] = code
        return [{"group_id": G}]

    patches = [
        ("_get_portal_database_dataset", _patch("_get_portal_database_dataset", lambda dataset_id: {"client_code": C} if dataset_id == D else None)),
        ("_list_users_with_database_access_for_client", _patch("_list_users_with_database_access_for_client", _fake_users)),
        ("_list_groups_with_database_access_for_client", _patch("_list_groups_with_database_access_for_client", _fake_groups)),
    ]
    try:
        users = api_main._list_users_eligible_for_dataset_assignment(D)
        groups = api_main._list_groups_eligible_for_dataset_assignment(D)
        empty = api_main._list_users_eligible_for_dataset_assignment("missing")
    finally:
        _restore(patches)
    assert seen.get("user_code") == C and seen.get("group_code") == C, seen
    assert users == [{"user_id": U}] and groups == [{"group_id": G}]
    assert empty == [], "unknown dataset yields no eligible users"
    print("PASS: dataset assignment eligibility behavior unchanged")


# --------------------------------------------------------------------------
# 17: no DSN/secret/raw SQL/client row values exposed by the helpers
# --------------------------------------------------------------------------
def _test_no_secret_exposure() -> None:
    m = _ds_model(client_db_direct=True, rows_direct=_ds_caps(view=True))
    res, _ = _run(m, lambda: api_main._list_effective_dataset_access_for_user(U))
    row = res[D]
    forbidden = ("dsn", "password", "token", "secret", "storage_key", "connection_string", "sql")
    for key in row.keys():
        assert str(key).lower() not in forbidden, (key, "no secret/SQL keys in helper output")
    blob = " ".join(str(v) for v in row.values()).lower()
    for needle in ("postgres://", "password=", "api_write_token", "dbname=", "host="):
        assert needle not in blob, (needle, "no DSN/secret material in helper output")
    print("PASS: set helpers expose no DSN/secret/raw SQL/client row values")


def main() -> None:
    _test_client_set_direct()
    _test_client_set_group()
    _test_client_set_additive_or()
    _test_client_set_inactive_group_ignored()
    _test_client_set_inactive_client_flag()

    _test_dataset_set_direct()
    _test_dataset_set_group()
    _test_dataset_set_missing_client_db()
    _test_dataset_set_missing_view_rows()
    _test_dataset_set_capabilities_preserved()
    _test_dataset_set_inactive_dataset()
    _test_dataset_set_inactive_user()

    _test_user_database_list_output_and_no_n_plus_one()
    _test_list_preserves_multi_db_and_order()
    _test_assignment_eligibility_preserved()
    _test_no_secret_exposure()

    print("\nALL PASS: Phase 2C set-based access helpers (behavior preserved)")


if __name__ == "__main__":
    main()
