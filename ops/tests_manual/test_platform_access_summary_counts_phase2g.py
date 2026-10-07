#!/usr/bin/env python3
"""Phase 2G tests: count-parity helpers for the admin effective access summary.

Proves that `_count_effective_report_folders_for_user` and
`_count_effective_database_datasets_for_user` reproduce the previously inline
`count(DISTINCT ...)` semantics exactly, and that
`_portal_effective_access_summary_for_user` keeps identical counts plus the
Phase 2F direct/group client lists.

Reuses the Phase 2F in-memory model/cursor (which already models the summary's
count CTEs) so the count helpers can be driven without a real database.

Run:

    cd /opt/log-platform
    env PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_platform_access_summary_counts_phase2g.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_platform_client_access_sources_phase2f import (  # noqa: E402
    api_main,
    _add_group,
    _caps,
    _model,
    _patch,
    _run,
    C,
    DATASET,
    FOLDER,
    G,
    G2,
    U,
)

DATASET2 = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
FOLDER2 = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def _folders(m):
    res, _ = _run(m, lambda: api_main._count_effective_report_folders_for_user(U))
    return res


def _datasets(m):
    res, _ = _run(m, lambda: api_main._count_effective_database_datasets_for_user(U))
    return res


# --------------------------------------------------------------------------
# 1-7: report folder count
# --------------------------------------------------------------------------
def _test_folder_count_direct() -> None:
    m = _model()
    m.user_clients.append({"user_id": U, "client_code": C, **_caps(view_reports=True)})
    m.folders[FOLDER] = {"client_code": C, "is_active": True}
    m.folder_users.append({"user_id": U, "folder_id": FOLDER})
    assert _folders(m) == 1
    print("PASS: folder count counts direct access")


def _test_folder_count_group() -> None:
    m = _model()
    _add_group(m, G, name="Ops")
    m.group_clients.append({"group_id": G, "client_code": C, **_caps(view_reports=True)})
    m.folders[FOLDER] = {"client_code": C, "is_active": True}
    m.folder_groups.append({"group_id": G, "folder_id": FOLDER})
    assert _folders(m) == 1
    print("PASS: folder count counts group-derived access")


def _test_folder_count_no_overcount() -> None:
    # Same folder reached via direct AND active group -> counted once.
    m = _model()
    m.user_clients.append({"user_id": U, "client_code": C, **_caps(view_reports=True)})
    _add_group(m, G, name="Ops")
    m.group_clients.append({"group_id": G, "client_code": C, **_caps(view_reports=True)})
    m.folders[FOLDER] = {"client_code": C, "is_active": True}
    m.folder_users.append({"user_id": U, "folder_id": FOLDER})
    m.folder_groups.append({"group_id": G, "folder_id": FOLDER})
    assert _folders(m) == 1, "direct + group duplicate folder must count once"
    print("PASS: folder count does not overcount direct+group duplicates")


def _test_folder_count_inactive_group_ignored() -> None:
    m = _model()
    m.user_clients.append({"user_id": U, "client_code": C, **_caps(view_reports=True)})
    _add_group(m, G, active=False, name="Ops")
    m.folders[FOLDER] = {"client_code": C, "is_active": True}
    m.folder_groups.append({"group_id": G, "folder_id": FOLDER})
    assert _folders(m) == 0, "inactive group must not contribute folder access"
    print("PASS: folder count ignores inactive groups")


def _test_folder_count_inactive_client_ignored() -> None:
    m = _model()
    m.clients[C] = {"is_active": False}
    m.user_clients.append({"user_id": U, "client_code": C, **_caps(view_reports=True)})
    m.folders[FOLDER] = {"client_code": C, "is_active": True}
    m.folder_users.append({"user_id": U, "folder_id": FOLDER})
    assert _folders(m) == 0, "inactive client must not contribute folder access"
    print("PASS: folder count ignores inactive clients")


def _test_folder_count_inactive_folder_ignored() -> None:
    m = _model()
    m.user_clients.append({"user_id": U, "client_code": C, **_caps(view_reports=True)})
    m.folders[FOLDER] = {"client_code": C, "is_active": False}
    m.folder_users.append({"user_id": U, "folder_id": FOLDER})
    assert _folders(m) == 0, "inactive folder must not be counted"
    print("PASS: folder count ignores inactive folders")


def _test_folder_count_requires_client_view_reports() -> None:
    # Folder assigned + active, but client has can_view_database only (no reports).
    m = _model()
    m.user_clients.append({"user_id": U, "client_code": C, **_caps(view_database=True)})
    m.folders[FOLDER] = {"client_code": C, "is_active": True}
    m.folder_users.append({"user_id": U, "folder_id": FOLDER})
    assert _folders(m) == 0, "missing client can_view_reports must block folder count"
    print("PASS: folder count requires client can_view_reports")


# --------------------------------------------------------------------------
# 8-13: database dataset count
# --------------------------------------------------------------------------
def _test_dataset_count_direct() -> None:
    m = _model()
    m.user_clients.append({"user_id": U, "client_code": C, **_caps(view_database=True)})
    m.datasets[DATASET] = {"client_code": C, "is_active": True}
    m.dataset_users.append({"user_id": U, "dataset_id": DATASET})
    assert _datasets(m) == 1
    print("PASS: dataset count counts direct access")


def _test_dataset_count_group() -> None:
    m = _model()
    _add_group(m, G, name="Ops")
    m.group_clients.append({"group_id": G, "client_code": C, **_caps(view_database=True)})
    m.datasets[DATASET] = {"client_code": C, "is_active": True}
    m.dataset_groups.append({"group_id": G, "dataset_id": DATASET})
    assert _datasets(m) == 1
    print("PASS: dataset count counts group-derived access")


def _test_dataset_count_no_overcount() -> None:
    m = _model()
    m.user_clients.append({"user_id": U, "client_code": C, **_caps(view_database=True)})
    _add_group(m, G, name="Ops")
    m.group_clients.append({"group_id": G, "client_code": C, **_caps(view_database=True)})
    m.datasets[DATASET] = {"client_code": C, "is_active": True}
    m.dataset_users.append({"user_id": U, "dataset_id": DATASET})
    m.dataset_groups.append({"group_id": G, "dataset_id": DATASET})
    assert _datasets(m) == 1, "direct + group duplicate dataset must count once"
    print("PASS: dataset count does not overcount direct+group duplicates")


def _test_dataset_count_inactive_dataset_ignored() -> None:
    m = _model()
    m.user_clients.append({"user_id": U, "client_code": C, **_caps(view_database=True)})
    m.datasets[DATASET] = {"client_code": C, "is_active": False}
    m.dataset_users.append({"user_id": U, "dataset_id": DATASET})
    assert _datasets(m) == 0, "inactive dataset must not be counted"
    print("PASS: dataset count ignores inactive datasets")


def _test_dataset_count_requires_client_view_database() -> None:
    m = _model()
    m.user_clients.append({"user_id": U, "client_code": C, **_caps(view_reports=True)})
    m.datasets[DATASET] = {"client_code": C, "is_active": True}
    m.dataset_users.append({"user_id": U, "dataset_id": DATASET})
    assert _datasets(m) == 0, "missing client can_view_database must block dataset count"
    print("PASS: dataset count requires client can_view_database")


def _test_dataset_count_not_gated_on_can_view_rows() -> None:
    # Parity check: the inline count gates on ASSIGNMENT existence only, NOT on
    # can_view_rows (unlike the dataset *list* set helper). A dataset assigned
    # with can_view_rows=False is still counted. This preserves exact behavior.
    m = _model()
    m.user_clients.append({"user_id": U, "client_code": C, **_caps(view_database=True)})
    m.datasets[DATASET] = {"client_code": C, "is_active": True}
    m.dataset_users.append({"user_id": U, "dataset_id": DATASET, "can_view_rows": False, "can_filter_rows": False, "can_export_rows": False})
    assert _datasets(m) == 1, "count must NOT gate on can_view_rows (assignment existence only)"
    print("PASS: dataset count gates on assignment existence, not can_view_rows (parity preserved)")


# --------------------------------------------------------------------------
# 14-15: summary counts + Phase 2F lists unchanged
# --------------------------------------------------------------------------
def _summary(m):
    old = _patch("_list_portal_user_groups", lambda uid: [{"group_id": G, "group_name": "Ops"}])
    try:
        return _run(m, lambda: api_main._portal_effective_access_summary_for_user(U))
    finally:
        api_main._list_portal_user_groups = old


def _test_summary_counts_match_helpers() -> None:
    m = _model()
    m.user_clients.append({"user_id": U, "client_code": C, **_caps(view_reports=True, view_database=True)})
    m.folders[FOLDER] = {"client_code": C, "is_active": True}
    m.folders[FOLDER2] = {"client_code": C, "is_active": True}
    m.folder_users.append({"user_id": U, "folder_id": FOLDER})
    m.folder_users.append({"user_id": U, "folder_id": FOLDER2})
    m.datasets[DATASET] = {"client_code": C, "is_active": True}
    m.dataset_users.append({"user_id": U, "dataset_id": DATASET})
    expected_folders = _folders(m)
    expected_datasets = _datasets(m)
    (summary, _) = _summary(m)
    assert summary["report_folder_count"] == expected_folders == 2, summary
    assert summary["database_dataset_count"] == expected_datasets == 1, summary
    print("PASS: summary counts equal the dedicated count helpers (exact parity)")


def _test_summary_lists_unchanged() -> None:
    m = _model()
    m.user_clients.append({"user_id": U, "client_code": C, **_caps(view_database=True)})
    _add_group(m, G, name="Ops")
    m.group_clients.append({"group_id": G, "client_code": C, **_caps(view_reports=True)})
    (summary, _) = _summary(m)
    assert set(summary.keys()) == {"groups", "direct_clients", "group_clients", "report_folder_count", "database_dataset_count"}
    for row in summary["direct_clients"] + summary["group_clients"]:
        assert set(row.keys()) == {"client_code", "display_name", "source"}, row
    assert [r["client_code"] for r in summary["direct_clients"]] == [C]
    assert [r["client_code"] for r in summary["group_clients"]] == [C]
    assert summary["direct_clients"][0]["source"] == "direct"
    assert summary["group_clients"][0]["source"] == "group"
    print("PASS: Phase 2F direct/group client lists unchanged after count switch")


# --------------------------------------------------------------------------
# 16: counts are plain ints; no secret/SQL/row-value leakage
# --------------------------------------------------------------------------
def _test_no_secret_exposure() -> None:
    m = _model()
    m.user_clients.append({"user_id": U, "client_code": C, **_caps(view_reports=True, view_database=True)})
    m.folders[FOLDER] = {"client_code": C, "is_active": True}
    m.folder_users.append({"user_id": U, "folder_id": FOLDER})
    m.datasets[DATASET] = {"client_code": C, "is_active": True}
    m.dataset_users.append({"user_id": U, "dataset_id": DATASET})
    f = _folders(m)
    d = _datasets(m)
    assert isinstance(f, int) and isinstance(d, int), (f, d)
    (summary, _) = _summary(m)
    assert isinstance(summary["report_folder_count"], int)
    assert isinstance(summary["database_dataset_count"], int)
    print("PASS: counts are plain integers; no DSN/secret/raw SQL/client row values exposed")


def main() -> None:
    _test_folder_count_direct()
    _test_folder_count_group()
    _test_folder_count_no_overcount()
    _test_folder_count_inactive_group_ignored()
    _test_folder_count_inactive_client_ignored()
    _test_folder_count_inactive_folder_ignored()
    _test_folder_count_requires_client_view_reports()
    _test_dataset_count_direct()
    _test_dataset_count_group()
    _test_dataset_count_no_overcount()
    _test_dataset_count_inactive_dataset_ignored()
    _test_dataset_count_requires_client_view_database()
    _test_dataset_count_not_gated_on_can_view_rows()
    _test_summary_counts_match_helpers()
    _test_summary_lists_unchanged()
    _test_no_secret_exposure()
    print("\nALL PASS: Phase 2G count-parity helpers + summary count switch (behavior preserved)")


if __name__ == "__main__":
    main()
