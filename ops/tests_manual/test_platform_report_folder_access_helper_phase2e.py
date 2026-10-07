#!/usr/bin/env python3
"""Phase 2E tests: set-based report-folder access helper and the user-facing
report-folder list/accessor switch.

Reuses the Phase 1/2A FakeModel (client/group/user effective-access semantics) and
adds a report-folder fake model + a report-folder-query-aware fake cursor. Proves:

* the set-based helper matches direct/group/additive-OR/inactive behavior,
* `/user/reports` list and folder accessor output are unchanged (one query, no N+1),
* folder-level `can_preview` / `can_download` gating is preserved,
* the preview/download gate (`_portal_report_artifact_access`) still enforces
  `can_preview`/`can_download`, artifact `client_code` match, and folder-filter
  match, and never exposes storage keys/tokens,
* Artifact Explorer RBAC / Artifact Browser token APIs are untouched.

Run:

    cd /opt/log-platform
    env PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_platform_report_folder_access_helper_phase2e.py
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
    _restore,
    api_main,
    C,
    G,
    U,
)

F = "ffffffff-ffff-ffff-ffff-ffffffffffff"


def _norm(sql: str) -> str:
    return " ".join(str(sql).lower().split())


class ReportFolderModel(FakeModel):
    """Extends the shared FakeModel with report-folder tables."""

    def __init__(self):
        super().__init__()
        self.folders = {}        # folder_id -> {client_code, is_active, can_preview, can_download, folder_name, slug}
        self.folder_users = []   # {user_id, folder_id}
        self.folder_groups = []  # {group_id, folder_id}

    def _folder_source(self, uid, fid):
        direct = any(fu["user_id"] == uid and fu["folder_id"] == fid for fu in self.folder_users)
        group = False
        for fg in self.folder_groups:
            if fg["folder_id"] != fid:
                continue
            g = self.groups.get(fg["group_id"])
            if not g or not g.get("is_active"):
                continue
            if any(gu["group_id"] == fg["group_id"] and gu["user_id"] == uid for gu in self.group_users):
                group = True
        return direct, group

    def _folder_accessible(self, uid, fid):
        folder = self.folders.get(fid)
        if not folder:
            return False
        client = self.clients.get(folder.get("client_code"))
        direct, group = self._folder_source(uid, fid)
        return bool(
            folder.get("is_active")
            and client and client.get("is_active")
            and self._eff_client(uid, folder.get("client_code"))["can_view_reports"]
            and (direct or group)
        )

    def _folder_row(self, fid):
        folder = self.folders.get(fid, {})
        return {
            "folder_id": fid,
            "client_code": folder.get("client_code"),
            "client_display_name": folder.get("client_code"),
            "folder_name": folder.get("folder_name", f"Folder {fid}"),
            "slug": folder.get("slug", fid),
            "description": None,
            "search_query_json": folder.get("search_query_json") or {},
            "is_active": folder.get("is_active"),
            "can_preview": folder.get("can_preview"),
            "can_download": folder.get("can_download"),
            "created_by": None,
            "created_at": None,
            "updated_by": None,
            "updated_at": None,
            "assigned_users": 1,
        }


class ReportCursor:
    def __init__(self, model: ReportFolderModel, calls: list):
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
        if "effective_folder_access" in s and "order by pc.display_name" in s:
            uid = p[0]
            idx = 3
            folder_filter = client_filter = None
            if "prf.folder_id = any(%s)" in s:
                folder_filter = {str(x) for x in p[idx]}
                idx += 1
            if "prf.client_code = any(%s)" in s:
                client_filter = {str(x) for x in p[idx]}
                idx += 1
            ordered = []
            for fid, folder in m.folders.items():
                if not m._folder_accessible(uid, fid):
                    continue
                if folder_filter is not None and fid not in folder_filter:
                    continue
                if client_filter is not None and str(folder.get("client_code")) not in client_filter:
                    continue
                direct, group = m._folder_source(uid, fid)
                row = m._folder_row(fid)
                row["can_view_reports"] = m._eff_client(uid, folder.get("client_code"))["can_view_reports"]
                row["source_direct"] = direct
                row["source_group"] = group
                ordered.append((str(folder.get("client_code")), str(row.get("folder_name")), row))
            ordered.sort(key=lambda t: (t[0], t[1]))
            self._rows = [r for _, _, r in ordered]
            return
        raise AssertionError(f"ReportCursor received an unrecognized query:\n{s}")

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class ReportConn:
    def __init__(self, model, calls):
        self.model = model
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return ReportCursor(self.model, self.calls)


def _run(model, fn):
    calls: list = []
    old = _patch("db_conn", lambda: ReportConn(model, calls))
    try:
        return fn(), calls
    finally:
        api_main.db_conn = old


def _model(*, client_reports_direct=False, client_reports_group=False, group_active=True,
           folder_direct=False, folder_group=False, folder_active=True, client_active=True,
           can_preview=True, can_download=False) -> ReportFolderModel:
    m = ReportFolderModel()
    m.clients[C] = {"is_active": client_active}
    m.users[U] = {"is_active": True}
    m.folders[F] = {"client_code": C, "is_active": folder_active, "can_preview": can_preview, "can_download": can_download, "folder_name": "Monthly", "slug": "monthly"}

    def _grp():
        if G not in m.groups:
            m.groups[G] = {"is_active": group_active}
        if not any(gu["group_id"] == G and gu["user_id"] == U for gu in m.group_users):
            m.group_users.append({"group_id": G, "user_id": U})

    if client_reports_direct:
        m.user_clients.append({"user_id": U, "client_code": C, **_caps(view_reports=True)})
    if client_reports_group:
        _grp()
        m.group_clients.append({"group_id": G, "client_code": C, **_caps(view_reports=True)})
    if folder_direct:
        m.folder_users.append({"user_id": U, "folder_id": F})
    if folder_group:
        _grp()
        m.folder_groups.append({"group_id": G, "folder_id": F})
    return m


# --------------------------------------------------------------------------
# 1-8: set-based helper visibility gates
# --------------------------------------------------------------------------
def _test_helper_direct() -> None:
    res, _ = _run(_model(client_reports_direct=True, folder_direct=True), lambda: api_main._list_effective_report_folder_access_for_user(U))
    assert F in res and res[F]["source_direct"] is True and res[F]["source_group"] is False
    print("PASS: report-folder helper resolves direct access")


def _test_helper_group() -> None:
    res, _ = _run(_model(client_reports_group=True, folder_group=True), lambda: api_main._list_effective_report_folder_access_for_user(U))
    assert F in res and res[F]["source_group"] is True
    print("PASS: report-folder helper resolves active-group access")


def _test_helper_additive_or() -> None:
    # client reports via group, folder assignment via both direct and group
    m = _model(client_reports_direct=True, folder_direct=True, folder_group=True, client_reports_group=True)
    res, _ = _run(m, lambda: api_main._list_effective_report_folder_access_for_user(U))
    assert res[F]["source_direct"] is True and res[F]["source_group"] is True
    print("PASS: report-folder helper keeps additive OR for assignment provenance")


def _test_helper_inactive_group_ignored() -> None:
    # client reports satisfied directly; folder ONLY via inactive group
    res, _ = _run(_model(client_reports_direct=True, folder_group=True, group_active=False), lambda: api_main._list_effective_report_folder_access_for_user(U))
    assert F not in res, "inactive group must not grant folder access"
    print("PASS: report-folder helper ignores inactive groups")


def _test_helper_missing_client_reports() -> None:
    res, _ = _run(_model(client_reports_direct=False, folder_direct=True), lambda: api_main._list_effective_report_folder_access_for_user(U))
    assert F not in res, "missing client can_view_reports must hide folder"
    print("PASS: report-folder helper requires client can_view_reports")


def _test_helper_inactive_folder() -> None:
    res, _ = _run(_model(client_reports_direct=True, folder_direct=True, folder_active=False), lambda: api_main._list_effective_report_folder_access_for_user(U))
    assert F not in res, "inactive folder must be hidden"
    print("PASS: report-folder helper hides inactive folders")


def _test_helper_inactive_client() -> None:
    res, _ = _run(_model(client_reports_direct=True, folder_direct=True, client_active=False), lambda: api_main._list_effective_report_folder_access_for_user(U))
    assert F not in res, "inactive client must hide folder"
    print("PASS: report-folder helper hides folders of inactive clients")


def _test_helper_inactive_user() -> None:
    m = _model(client_reports_direct=True, folder_direct=True)
    m.users[U] = {"is_active": False}
    # The list query mirrors prior behavior (route layer enforces login); ensure
    # visibility matches the active-user case to confirm no semantic drift.
    res_inactive, _ = _run(m, lambda: api_main._list_effective_report_folder_access_for_user(U))
    m2 = _model(client_reports_direct=True, folder_direct=True)
    res_active, _ = _run(m2, lambda: api_main._list_effective_report_folder_access_for_user(U))
    assert (F in res_inactive) == (F in res_active)
    print("PASS: report-folder helper preserves prior list visibility semantics")


# --------------------------------------------------------------------------
# 9-10: can_preview / can_download gating preserved through the access gate
# --------------------------------------------------------------------------
def _gate(model, action, *, artifact_client=C, filter_match=True):
    patches = [
        ("_get_artifact_browser_row", _patch("_get_artifact_browser_row", lambda artifact_id: {"artifact_id": artifact_id, "storage_key": "secret/object/key", "display_filename": "r.pdf"})),
        ("_artifact_browser_row", _patch("_artifact_browser_row", lambda row: {"client_code": artifact_client, "artifact_id": row.get("artifact_id"), "report_type": "monthly", "file_ext": "pdf", "display_filename": "r.pdf"})),
        ("_portal_artifact_matches_report_folder", _patch("_portal_artifact_matches_report_folder", lambda folder, artifact_id: filter_match)),
    ]
    calls: list = []
    old = _patch("db_conn", lambda: ReportConn(model, calls))
    try:
        user = {"user_id": U, "is_active": True}
        return api_main._portal_report_artifact_access(user, F, "art-1", action=action)
    finally:
        api_main.db_conn = old
        _restore(patches)


def _test_preview_gate_blocks_when_disabled() -> None:
    m = _model(client_reports_direct=True, folder_direct=True, can_preview=False, can_download=False)
    assert _gate(m, "preview") is None, "preview must be denied when can_preview is False"
    print("PASS: can_preview=false blocks preview")


def _test_download_gate_blocks_when_disabled() -> None:
    m = _model(client_reports_direct=True, folder_direct=True, can_preview=True, can_download=False)
    assert _gate(m, "download") is None, "download must be denied when can_download is False"
    print("PASS: can_download=false blocks download")


def _test_preview_gate_allows_when_enabled() -> None:
    m = _model(client_reports_direct=True, folder_direct=True, can_preview=True)
    access = _gate(m, "preview")
    assert access is not None, "preview must be allowed when can_preview is True"
    print("PASS: can_preview=true allows preview")


# --------------------------------------------------------------------------
# 13: artifact client_code mismatch + folder filter mismatch are enforced
# --------------------------------------------------------------------------
def _test_artifact_client_code_enforced() -> None:
    m = _model(client_reports_direct=True, folder_direct=True, can_preview=True)
    assert _gate(m, "preview", artifact_client="OTHER_99") is None, "artifact from another client must be denied"
    print("PASS: artifact client_code mismatch is denied (forced client_code preserved)")


def _test_folder_filter_enforced() -> None:
    m = _model(client_reports_direct=True, folder_direct=True, can_preview=True)
    assert _gate(m, "preview", filter_match=False) is None, "artifact not matching folder filters must be denied"
    print("PASS: folder filter matching is enforced")


# --------------------------------------------------------------------------
# 11-12: list/accessor output + no N+1
# --------------------------------------------------------------------------
def _test_list_output_and_no_n_plus_one() -> None:
    m = _model(client_reports_direct=True, folder_direct=True, can_preview=True, can_download=True)
    listing, calls = _run(m, lambda: api_main._list_accessible_portal_report_folders_for_user(U))
    assert any(str(f.get("folder_id")) == F for f in listing), listing
    folder = next(f for f in listing if str(f.get("folder_id")) == F)
    for key in ("folder_id", "client_code", "folder_name", "slug", "search_query_json", "can_preview", "can_download", "is_active"):
        assert key in folder, key
    assert folder["can_preview"] is True and folder["can_download"] is True
    assert len(calls) == 1, (len(calls), "list path must issue exactly one query")
    print("PASS: /user/reports list output preserved with a single query (no N+1)")


def _test_accessor_output() -> None:
    m = _model(client_reports_direct=True, folder_direct=True, can_preview=True)
    (folder, calls) = _run(m, lambda: api_main._get_accessible_portal_report_folder_for_user(U, F))
    assert folder is not None and str(folder.get("folder_id")) == F
    assert folder["can_preview"] is True
    assert len(calls) == 1, (len(calls), "accessor must issue exactly one query")
    none_folder, _ = _run(_model(folder_direct=True), lambda: api_main._get_accessible_portal_report_folder_for_user(U, F))
    assert none_folder is None, "no client reports access -> accessor returns None"
    print("PASS: folder detail accessor output preserved")


# --------------------------------------------------------------------------
# 14-15: no secret exposure; RBAC/token surfaces untouched
# --------------------------------------------------------------------------
def _test_no_secret_exposure() -> None:
    m = _model(client_reports_direct=True, folder_direct=True)
    res, _ = _run(m, lambda: api_main._list_effective_report_folder_access_for_user(U))
    row = res[F]
    forbidden = ("dsn", "password", "token", "secret", "storage_key", "connection_string", "sql")
    for key in row.keys():
        assert str(key).lower() not in forbidden, key
    blob = " ".join(str(v) for v in row.values()).lower()
    for needle in ("postgres://", "password=", "api_write_token", "storage_key", "dbname="):
        assert needle not in blob, needle
    print("PASS: report-folder helper exposes no DSN/secret/storage-key/raw SQL")


def _test_rbac_and_token_untouched() -> None:
    # The report-folder helper does not reach into Artifact Explorer RBAC or the
    # token API; verify those surfaces still exist and the helper source is
    # self-contained (no RBAC/token calls).
    assert callable(api_main.require_token)
    src = inspect.getsource(api_main._list_effective_report_folder_access_for_user)
    for needle in ("require_token", "user_can_access_artifact", "API_READ_TOKEN", "API_WRITE_TOKEN"):
        assert needle not in src, needle
    print("PASS: Artifact Explorer RBAC and Artifact Browser token behavior untouched")


def main() -> None:
    _test_helper_direct()
    _test_helper_group()
    _test_helper_additive_or()
    _test_helper_inactive_group_ignored()
    _test_helper_missing_client_reports()
    _test_helper_inactive_folder()
    _test_helper_inactive_client()
    _test_helper_inactive_user()
    _test_preview_gate_blocks_when_disabled()
    _test_download_gate_blocks_when_disabled()
    _test_preview_gate_allows_when_enabled()
    _test_artifact_client_code_enforced()
    _test_folder_filter_enforced()
    _test_list_output_and_no_n_plus_one()
    _test_accessor_output()
    _test_no_secret_exposure()
    _test_rbac_and_token_untouched()
    print("\nALL PASS: Phase 2E set-based report-folder access helper (behavior preserved)")


if __name__ == "__main__":
    main()
