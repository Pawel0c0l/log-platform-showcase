#!/usr/bin/env python3
"""Manual regression tests for artifact metadata backfill inference."""
from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ops.backfill_artifact_metadata import infer_updates  # noqa: E402


SHA = "46ceaade3f32197efe1703deed403fa932ea2930ee2649b397f35c85b1ef64fc"
RAW_FILE_ID = "07032a42-9069-4ef8-8770-33ab6106c7dc"


def _row(**overrides):
    row = {
        "artifact_id": "22222222-2222-2222-2222-222222222222",
        "run_id": "33333333-3333-3333-3333-333333333333",
        "filename": f"{SHA}__report_207.csv",
        "storage_key": f"2026/05/13/22222222-2222-2222-2222-222222222222/{SHA}__report_207.csv",
        "raw_file_id": None,
        "workflow_name": None,
        "stage_name": None,
        "artifact_role": None,
        "report_type": None,
        "display_filename": None,
        "original_filename": None,
        "file_ext": None,
        "layout_version": 1,
        "metadata_json": {},
        "run_source": "jobs.reports.stage2.job_stage2",
        "raw_original_filename": None,
        "raw_path": None,
        "normalized_csv_path": None,
        "raw_stage2_report_type": None,
        "raw_sha256": None,
    }
    row.update(overrides)
    return row


def _raw_by_sha():
    return {
        SHA: {
            "id": RAW_FILE_ID,
            "sha256": SHA,
            "original_filename": "source.xls",
            "raw_path": f"/reports/raw/{SHA}__source.xls",
            "normalized_csv_path": f"/reports/normalized/{SHA}.csv",
            "stage2_report_type": "report_207",
        }
    }


def _apply(row, updates):
    for key, value in updates.items():
        row[key] = value
    return row


def _test_stage2_high_confidence_backfill_and_idempotence() -> None:
    row = _row()
    result = infer_updates(row, raw_by_sha=_raw_by_sha())
    updates = result.updates
    assert updates["workflow_name"] == "workflow_b", updates
    assert updates["stage_name"] == "stage_2_clean", updates
    assert updates["artifact_role"] == "cleaned", updates
    assert updates["report_type"] == "report_207", updates
    assert updates["raw_file_id"] == RAW_FILE_ID, updates
    assert updates["original_filename"] == "source.xls", updates
    assert updates["file_ext"] == "csv", updates
    assert updates["display_filename"] == f"{SHA}__report_207.csv", updates
    assert "layout_version" not in updates, updates
    assert updates["metadata_json"]["backfilled"] is True, updates

    second = infer_updates(_apply(row, updates), raw_by_sha=_raw_by_sha())
    assert second.updates == {}, second
    print("PASS: backfill infers Stage 2 metadata and second pass is idempotent")


def _test_stage1_run_source_and_raw_role() -> None:
    row = _row(
        filename=f"{SHA}__source.xls",
        storage_key=f"2026/05/13/222/{SHA}__source.xls",
        raw_file_id=RAW_FILE_ID,
        run_source="jobs.mail.fetch_reports",
        raw_original_filename="source.xls",
        raw_path=f"/reports/raw/{SHA}__source.xls",
        normalized_csv_path=f"/reports/normalized/{SHA}.csv",
    )
    updates = infer_updates(row).updates
    assert updates["workflow_name"] == "workflow_b", updates
    assert updates["stage_name"] == "stage_1_fetch", updates
    assert updates["artifact_role"] == "raw", updates
    assert updates["original_filename"] == "source.xls", updates
    assert updates["file_ext"] == "xls", updates
    assert "client_code" not in updates, updates
    print("PASS: backfill infers Stage 1 run source and raw role without client_code")


def _test_ambiguous_artifact_is_skipped() -> None:
    row = _row(
        filename="notes.txt",
        storage_key="2026/05/13/artifact/notes.txt",
        workflow_name=None,
        stage_name=None,
        artifact_role=None,
        report_type=None,
        file_ext="txt",
        display_filename="notes.txt",
        run_source="jobs.reports.demo",
    )
    result = infer_updates(row)
    assert result.updates == {}, result
    assert result.ambiguous_reasons, result

    row = _row(
        filename="notes.txt",
        storage_key="2026/05/13/artifact/notes.txt",
        workflow_name=None,
        stage_name=None,
        artifact_role=None,
        report_type=None,
        file_ext=None,
        display_filename=None,
        run_source="jobs.reports.demo",
    )
    result = infer_updates(row)
    assert result.updates == {}, result
    assert "semantic_identity_unknown" in result.ambiguous_reasons, result
    print("PASS: ambiguous artifact is skipped")


def _test_v2_storage_key_can_fix_layout_version() -> None:
    row = _row(
        storage_key=(
            "workflow_b/stage_1_fetch/yyyy=2026/mm=05/dd=12/"
            f"run_id=run/raw/unknown__20260512T133718Z__{RAW_FILE_ID[:8]}__raw.xls"
        ),
        layout_version=1,
        run_source=None,
    )
    updates = infer_updates(row).updates
    assert updates["workflow_name"] == "workflow_b", updates
    assert updates["stage_name"] == "stage_1_fetch", updates
    assert updates["artifact_role"] == "raw", updates
    assert updates["layout_version"] == 2, updates
    print("PASS: v2 storage key can correct layout_version when the object key is already v2")


def main() -> None:
    _test_stage2_high_confidence_backfill_and_idempotence()
    _test_stage1_run_source_and_raw_role()
    _test_ambiguous_artifact_is_skipped()
    _test_v2_storage_key_can_fix_layout_version()


if __name__ == "__main__":
    main()
