#!/usr/bin/env python3
"""Phase 2F tests: per-source client access helper and the client-level switch
of the admin effective access summary.

Proves that `_list_client_access_sources_for_user` reproduces the exact
per-source client lists the summary used to build inline:

* direct list from `portal_user_clients` (active client, view_db OR view_reports),
* group list from active `portal_group_users` + active `portal_groups` +
  `portal_group_clients` (NOT deduplicated across groups),
* additive-OR merged/effective view,
* inactive groups / inactive clients ignored,
* capability flags preserved, export-only clients excluded (as before),

and that `_portal_effective_access_summary_for_user` keeps its exact output
shape (direct/group separation, `{client_code, display_name, source}` rows,
unchanged folder/dataset counts) and never leaks DSN/secret/raw SQL.

Run:

    cd /opt/log-platform
    env PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_platform_client_access_sources_phase2f.py
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
    _patch,
    api_main,
    U,
    C,
    G,
)

C2 = "BCME_02"
G2 = "hhhhhhhh-hhhh-hhhh-hhhh-hhhhhhhhhhhh"
FOLDER = "ffffffff-ffff-ffff-ffff-ffffffffffff"
DATASET = "dddddddd-dddd-dddd-dddd-dddddddddddd"


def _norm(sql: str) -> str:
    return " ".join(str(sql).lower().split())


class SourcesModel(FakeModel):
    """Shared FakeModel + report-folder/dataset tables for summary counts.

    Adds display names and group names so the per-source rows can be checked.
    """

    def __init__(self):
        super().__init__()
        self.client_names = {}   # client_code -> display_name
        self.group_names = {}    # group_id -> group_name
        self.folders = {}        # folder_id -> {client_code, is_active}
        self.folder_users = []   # {user_id, folder_id}
        self.folder_groups = []  # {group_id, folder_id}

    # ---- per-source direct/group client rows (mirror the helper SQL) ----
    def direct_client_rows(self, uid, client_filter=None):
        rows = []
        for r in self.user_clients:
            if r["user_id"] != uid:
                continue
            code = r["client_code"]
            client = self.clients.get(code)
            if not (client and client.get("is_active")):
                continue
            if not (bool(r.get("can_view_database")) or bool(r.get("can_view_reports"))):
                continue
            if client_filter is not None and code not in client_filter:
                continue
            rows.append({
                "client_code": code,
                "display_name": self.client_names.get(code, code),
                "can_view_reports": bool(r.get("can_view_reports")),
                "can_view_database": bool(r.get("can_view_database")),
                "can_export_database": bool(r.get("can_export_database")),
            })
        rows.sort(key=lambda x: x["client_code"])
        return rows

    def group_client_rows(self, uid, client_filter=None):
        rows = []
        for gu in self.group_users:
            if gu["user_id"] != uid:
                continue
            grp = self.groups.get(gu["group_id"])
            if not grp or not grp.get("is_active"):
                continue
            for gc in self.group_clients:
                if gc["group_id"] != gu["group_id"]:
                    continue
                code = gc["client_code"]
                client = self.clients.get(code)
                if not (client and client.get("is_active")):
                    continue
                if not (bool(gc.get("can_view_database")) or bool(gc.get("can_view_reports"))):
                    continue
                if client_filter is not None and code not in client_filter:
                    continue
                rows.append({
                    "client_code": code,
                    "display_name": self.client_names.get(code, code),
                    "group_id": gu["group_id"],
                    "group_name": self.group_names.get(gu["group_id"], gu["group_id"]),
                    "can_view_reports": bool(gc.get("can_view_reports")),
                    "can_view_database": bool(gc.get("can_view_database")),
                    "can_export_database": bool(gc.get("can_export_database")),
                })
        rows.sort(key=lambda x: (x["client_code"], x["group_name"]))
        return rows

    # ---- summary count helpers ----
    def _folder_accessible(self, uid, fid):
        folder = self.folders.get(fid)
        if not folder:
            return False
        client = self.clients.get(folder.get("client_code"))
        direct = any(fu["user_id"] == uid and fu["folder_id"] == fid for fu in self.folder_users)
        group = False
        for fg in self.folder_groups:
            if fg["folder_id"] != fid:
                continue
            grp = self.groups.get(fg["group_id"])
            if grp and grp.get("is_active") and any(
                gu["group_id"] == fg["group_id"] and gu["user_id"] == uid for gu in self.group_users
            ):
                group = True
        return bool(
            folder.get("is_active")
            and client and client.get("is_active")
            and self._eff_client(uid, folder.get("client_code"))["can_view_reports"]
            and (direct or group)
        )

    def _dataset_accessible_count(self, uid, did):
        ds = self.datasets.get(did)
        if not ds:
            return False
        client = self.clients.get(ds.get("client_code"))
        direct = any(du["user_id"] == uid and du["dataset_id"] == did for du in self.dataset_users)
        group = False
        for dg in self.dataset_groups:
            if dg["dataset_id"] != did:
                continue
            grp = self.groups.get(dg["group_id"])
            if grp and grp.get("is_active") and any(
                gu["group_id"] == dg["group_id"] and gu["user_id"] == uid for gu in self.group_users
            ):
                group = True
        return bool(
            ds.get("is_active")
            and client and client.get("is_active")
            and self._eff_client(uid, ds.get("client_code"))["can_view_database"]
            and (direct or group)
        )

    def folder_count(self, uid):
        return sum(1 for fid in self.folders if self._folder_accessible(uid, fid))

    def dataset_count(self, uid):
        return sum(1 for did in self.datasets if self._dataset_accessible_count(uid, did))


class SourcesCursor:
    def __init__(self, model: SourcesModel, calls: list):
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

        # Summary folder count (inline CTE — must match before helper branches).
        if "count(distinct prf.folder_id)" in s:
            self._rows = [{"count": m.folder_count(p[0])}]
            return
        # Summary dataset count (inline CTE).
        if "count(distinct pdd.dataset_id)" in s:
            self._rows = [{"count": m.dataset_count(p[0])}]
            return
        # Per-source helper: direct client rows.
        if "from portal_user_clients puc" in s and "bool_or" not in s and "count(" not in s:
            uid = p[0]
            cf = {str(x) for x in p[1]} if "client_code = any(%s)" in s else None
            self._rows = m.direct_client_rows(uid, cf)
            return
        # Per-source helper: group client rows.
        if "from portal_group_users pgu" in s and "portal_group_clients pgc" in s and "count(" not in s and "bool_or" not in s:
            uid = p[0]
            cf = {str(x) for x in p[1]} if "client_code = any(%s)" in s else None
            self._rows = m.group_client_rows(uid, cf)
            return
        raise AssertionError(f"SourcesCursor received an unrecognized query:\n{s}")

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class SourcesConn:
    def __init__(self, model, calls):
        self.model = model
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return SourcesCursor(self.model, self.calls)


def _run(model, fn):
    calls: list = []
    old = _patch("db_conn", lambda: SourcesConn(model, calls))
    try:
        return fn(), calls
    finally:
        api_main.db_conn = old


def _model() -> SourcesModel:
    m = SourcesModel()
    m.clients[C] = {"is_active": True}
    m.clients[C2] = {"is_active": True}
    m.client_names[C] = "Acme"
    m.client_names[C2] = "Bravo"
    m.users[U] = {"is_active": True}
    return m


def _add_group(m, gid, *, active=True, name=None):
    m.groups[gid] = {"is_active": active}
    m.group_names[gid] = name or gid
    if not any(gu["group_id"] == gid and gu["user_id"] == U for gu in m.group_users):
        m.group_users.append({"group_id": gid, "user_id": U})


# --------------------------------------------------------------------------
# 1-2: direct / group lists preserved
# --------------------------------------------------------------------------
def _test_direct_list_preserved() -> None:
    m = _model()
    m.user_clients.append({"user_id": U, "client_code": C, **_caps(view_database=True)})
    res, _ = _run(m, lambda: api_main._list_client_access_sources_for_user(U))
    assert [r["client_code"] for r in res["direct_clients"]] == [C], res
    assert res["direct_clients"][0]["source"] == "direct"
    assert res["direct_clients"][0]["client_name"] == "Acme"
    assert res["group_clients"] == []
    print("PASS: direct client list preserved")


def _test_group_list_preserved() -> None:
    m = _model()
    _add_group(m, G, name="Ops")
    m.group_clients.append({"group_id": G, "client_code": C, **_caps(view_reports=True)})
    res, _ = _run(m, lambda: api_main._list_client_access_sources_for_user(U))
    assert [r["client_code"] for r in res["group_clients"]] == [C], res
    row = res["group_clients"][0]
    assert row["source"] == "group" and row["group_id"] == G and row["group_name"] == "Ops"
    assert res["direct_clients"] == []
    print("PASS: group-derived client list preserved")


# --------------------------------------------------------------------------
# 3-4: separation + multiple group rows not deduplicated
# --------------------------------------------------------------------------
def _test_direct_and_group_separate() -> None:
    m = _model()
    m.user_clients.append({"user_id": U, "client_code": C, **_caps(view_database=True)})
    _add_group(m, G, name="Ops")
    m.group_clients.append({"group_id": G, "client_code": C, **_caps(view_reports=True)})
    res, _ = _run(m, lambda: api_main._list_client_access_sources_for_user(U))
    assert len(res["direct_clients"]) == 1 and len(res["group_clients"]) == 1
    assert res["direct_clients"][0]["source"] == "direct"
    assert res["group_clients"][0]["source"] == "group"
    print("PASS: direct and group rows remain separate (same client in both)")


def _test_multiple_group_rows_not_deduplicated() -> None:
    m = _model()
    _add_group(m, G, name="Ops")
    _add_group(m, G2, name="Finance")
    m.group_clients.append({"group_id": G, "client_code": C, **_caps(view_reports=True)})
    m.group_clients.append({"group_id": G2, "client_code": C, **_caps(view_database=True)})
    res, _ = _run(m, lambda: api_main._list_client_access_sources_for_user(U))
    same_client = [r for r in res["group_clients"] if r["client_code"] == C]
    assert len(same_client) == 2, same_client
    assert {r["group_id"] for r in same_client} == {G, G2}
    print("PASS: multiple group rows for one client are NOT deduplicated")


# --------------------------------------------------------------------------
# 5: additive OR merged behavior
# --------------------------------------------------------------------------
def _test_merged_additive_or() -> None:
    m = _model()
    m.user_clients.append({"user_id": U, "client_code": C, **_caps(view_reports=True)})
    _add_group(m, G, name="Ops")
    m.group_clients.append({"group_id": G, "client_code": C, **_caps(view_database=True, export_database=True)})
    res, _ = _run(m, lambda: api_main._list_client_access_sources_for_user(U))
    eff = res["effective_clients"][C]
    assert eff["source_direct"] is True and eff["source_group"] is True
    assert eff["can_view_reports"] is True and eff["can_view_database"] is True and eff["can_export_database"] is True
    print("PASS: merged effective access is additive OR across direct + group")


# --------------------------------------------------------------------------
# 6-7: inactive groups / inactive clients ignored
# --------------------------------------------------------------------------
def _test_inactive_group_ignored() -> None:
    m = _model()
    _add_group(m, G, active=False, name="Ops")
    m.group_clients.append({"group_id": G, "client_code": C, **_caps(view_reports=True)})
    res, _ = _run(m, lambda: api_main._list_client_access_sources_for_user(U))
    assert res["group_clients"] == [], res
    assert C not in res["effective_clients"]
    print("PASS: inactive groups ignored")


def _test_inactive_client_ignored() -> None:
    m = _model()
    m.clients[C] = {"is_active": False}
    m.user_clients.append({"user_id": U, "client_code": C, **_caps(view_database=True)})
    _add_group(m, G, name="Ops")
    m.group_clients.append({"group_id": G, "client_code": C, **_caps(view_reports=True)})
    res, _ = _run(m, lambda: api_main._list_client_access_sources_for_user(U))
    assert res["direct_clients"] == [] and res["group_clients"] == [], res
    print("PASS: inactive clients ignored in per-source lists")


# --------------------------------------------------------------------------
# 8-9: flags preserved + export-only excluded (gating preserved)
# --------------------------------------------------------------------------
def _test_flags_preserved() -> None:
    m = _model()
    m.user_clients.append({"user_id": U, "client_code": C, **_caps(view_reports=True, view_database=True, export_database=True)})
    res, _ = _run(m, lambda: api_main._list_client_access_sources_for_user(U))
    row = res["direct_clients"][0]
    assert row["can_view_reports"] is True and row["can_view_database"] is True and row["can_export_database"] is True
    print("PASS: reports/database/export flags preserved on direct rows")


def _test_export_only_excluded() -> None:
    # A grant with ONLY can_export_database (no view) must NOT appear, matching
    # the old summary's (can_view_database OR can_view_reports) gate.
    m = _model()
    m.user_clients.append({"user_id": U, "client_code": C, **_caps(export_database=True)})
    _add_group(m, G, name="Ops")
    m.group_clients.append({"group_id": G, "client_code": C2, **_caps(export_database=True)})
    res, _ = _run(m, lambda: api_main._list_client_access_sources_for_user(U))
    assert res["direct_clients"] == [], res
    assert res["group_clients"] == [], res
    print("PASS: export-only clients excluded exactly as before")


# --------------------------------------------------------------------------
# 10-12: summary output shape + folder/dataset counts unchanged
# --------------------------------------------------------------------------
def _summary(m):
    old = _patch("_list_portal_user_groups", lambda uid: [{"group_id": G, "group_name": "Ops"}])
    try:
        return _run(m, lambda: api_main._portal_effective_access_summary_for_user(U))
    finally:
        api_main._list_portal_user_groups = old


def _test_summary_shape_unchanged() -> None:
    m = _model()
    m.user_clients.append({"user_id": U, "client_code": C2, **_caps(view_database=True)})
    _add_group(m, G, name="Ops")
    m.group_clients.append({"group_id": G, "client_code": C, **_caps(view_reports=True)})
    (summary, _) = _summary(m)
    assert set(summary.keys()) == {"groups", "direct_clients", "group_clients", "report_folder_count", "database_dataset_count"}, summary
    for row in summary["direct_clients"] + summary["group_clients"]:
        assert set(row.keys()) == {"client_code", "display_name", "source"}, row
    assert [r["client_code"] for r in summary["direct_clients"]] == [C2]
    assert summary["direct_clients"][0]["source"] == "direct"
    assert [r["client_code"] for r in summary["group_clients"]] == [C]
    assert summary["group_clients"][0]["source"] == "group"
    assert summary["group_clients"][0]["display_name"] == "Acme"
    print("PASS: summary output shape unchanged (direct/group separation + exact row keys)")


def _test_summary_counts_unchanged() -> None:
    m = _model()
    m.user_clients.append({"user_id": U, "client_code": C, **_caps(view_reports=True, view_database=True)})
    # one accessible folder + one accessible dataset under client C
    m.folders[FOLDER] = {"client_code": C, "is_active": True}
    m.folder_users.append({"user_id": U, "folder_id": FOLDER})
    m.datasets[DATASET] = {"client_code": C, "is_active": True}
    m.dataset_users.append({"user_id": U, "dataset_id": DATASET})
    (summary, _) = _summary(m)
    assert summary["report_folder_count"] == 1, summary
    assert summary["database_dataset_count"] == 1, summary
    # No reports access -> folder count drops to 0 (count logic untouched).
    m2 = _model()
    m2.user_clients.append({"user_id": U, "client_code": C, **_caps(view_database=True)})
    m2.folders[FOLDER] = {"client_code": C, "is_active": True}
    m2.folder_users.append({"user_id": U, "folder_id": FOLDER})
    (summary2, _) = _summary(m2)
    assert summary2["report_folder_count"] == 0, summary2
    print("PASS: folder/dataset counts unchanged (inline count logic preserved)")


# --------------------------------------------------------------------------
# 13: Phase 2C/2D/2E helpers unchanged
# --------------------------------------------------------------------------
def _test_prior_helpers_unchanged() -> None:
    for name in (
        "_list_effective_client_access_for_user",
        "_list_effective_dataset_access_for_user",
        "_list_effective_report_folder_access_for_user",
        "_portal_user_has_database_access_to_client",
    ):
        assert callable(getattr(api_main, name)), name
    # The merged set helper still has its own effective_client_access CTE
    # (Part C leaves it untouched).
    merged = inspect.getsource(api_main._list_effective_client_access_for_user)
    assert "WITH effective_client_access" in merged, merged
    assert "source_direct" in merged and "source_group" in merged
    print("PASS: Phase 2C/2D/2E helpers remain unchanged")


# --------------------------------------------------------------------------
# 14: no secret / DSN / raw SQL / client row values exposure
# --------------------------------------------------------------------------
def _test_no_secret_exposure() -> None:
    m = _model()
    m.user_clients.append({"user_id": U, "client_code": C, **_caps(view_database=True)})
    _add_group(m, G, name="Ops")
    m.group_clients.append({"group_id": G, "client_code": C, **_caps(view_reports=True)})
    res, _ = _run(m, lambda: api_main._list_client_access_sources_for_user(U))
    forbidden_keys = ("dsn", "password", "token", "secret", "storage_key", "connection_string", "sql")
    rows = res["direct_clients"] + res["group_clients"] + list(res["effective_clients"].values())
    for row in rows:
        for key in row.keys():
            assert str(key).lower() not in forbidden_keys, key
        blob = " ".join(str(v) for v in row.values()).lower()
        for needle in ("postgres://", "password=", "api_write_token", "storage_key", "dbname=", "select "):
            assert needle not in blob, needle
    print("PASS: per-source helper exposes no DSN/secret/storage-key/raw SQL/client row values")


def main() -> None:
    _test_direct_list_preserved()
    _test_group_list_preserved()
    _test_direct_and_group_separate()
    _test_multiple_group_rows_not_deduplicated()
    _test_merged_additive_or()
    _test_inactive_group_ignored()
    _test_inactive_client_ignored()
    _test_flags_preserved()
    _test_export_only_excluded()
    _test_summary_shape_unchanged()
    _test_summary_counts_unchanged()
    _test_prior_helpers_unchanged()
    _test_no_secret_exposure()
    print("\nALL PASS: Phase 2F per-source client access helper + summary client-level switch (behavior preserved)")


if __name__ == "__main__":
    main()
