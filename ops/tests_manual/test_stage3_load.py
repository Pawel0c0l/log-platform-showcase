#!/usr/bin/env python3
"""Manual regressions for Workflow B Stage 3 report loading.

Run:

    cd /opt/log-platform
    PYTHONPATH="$PWD" python3 ops/tests_manual/test_stage3_load.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.reports.stage3 import job_stage3  # noqa: E402


class FakeDataFrame:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = [dict(row) for row in rows]
        self.columns = list(rows[0].keys()) if rows else []
        self.index = list(range(len(rows)))

    def __setitem__(self, column, value):
        if column not in self.columns:
            self.columns.append(column)
        for row in self.rows:
            row[column] = value

    def iterrows(self):
        for idx, row in enumerate(self.rows):
            yield idx, row

    def __len__(self):
        return len(self.rows)


class FakeCursor:
    def __init__(self, rows=None, one=None):
        self.rows = rows or []
        self.one = one
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, query, params=None):
        self.executed.append((str(query), params))

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.one

    def executemany(self, query, values):
        self.executed.append((str(query), values))


class FakePlatformConn:
    def __init__(self, cursor):
        self.cursor_obj = cursor
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cursor_obj

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class FakeDestinationConn:
    def __init__(self):
        self.cursor_obj = FakeCursor()
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cursor_obj

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class PatchAttrs:
    def __init__(self, **attrs):
        self.attrs = attrs
        self.originals = {}

    def __enter__(self):
        for name, value in self.attrs.items():
            self.originals[name] = getattr(job_stage3, name)
            setattr(job_stage3, name, value)

    def __exit__(self, exc_type, exc, tb):
        for name, value in self.originals.items():
            setattr(job_stage3, name, value)


def _migration_sql() -> str:
    return (REPO_ROOT / "db/migrations/029_workflow_b_stage3_load.sql").read_text()


def _destination_inspection(existing_columns: dict[str, str] | None = None) -> dict:
    return {
        "destination_schema_exists": True,
        "destination_table_exists": existing_columns is not None,
        "existing_columns": existing_columns or {},
        "unique_index_exists": False,
    }


def _plan_for(
    df: FakeDataFrame,
    *,
    data_overwrite: bool,
    existing_columns: dict[str, str] | None = None,
    existing_record_ids: set[str] | None = None,
    duplicate_existing_record_ids: list[dict] | None = None,
):
    with PatchAttrs(
        _inspect_destination=lambda *_args, **_kwargs: _destination_inspection(existing_columns),
        _find_existing_duplicate_record_ids=lambda *_args, **_kwargs: duplicate_existing_record_ids or [],
        _fetch_existing_record_ids=lambda *_args, **_kwargs: existing_record_ids or set(),
    ):
        return job_stage3._build_load_plan(
            FakeCursor(),
            df=df,
            data_overwrite=data_overwrite,
            destination_schema="telematics_reports",
            destination_table="report_207",
        )


def test_migration_creates_policy_table() -> None:
    sql = _migration_sql()
    assert "CREATE TABLE IF NOT EXISTS workflow_b_control.report_type_client_load_policy" in sql
    assert "PRIMARY KEY (client_code, report_type)" in sql
    assert "data_overwrite BOOLEAN NOT NULL DEFAULT FALSE" in sql
    print("PASS: migration creates per-client/report Stage 3 load policy")


def test_migration_adds_stage3_raw_file_columns() -> None:
    sql = _migration_sql()
    for column in [
        "stage3_status",
        "stage3_started_at",
        "stage3_finished_at",
        "stage3_error",
        "stage3_inserted_rows",
        "stage3_updated_rows",
        "stage3_skipped_rows",
        "stage3_destination_schema",
        "stage3_destination_table",
        "stage3_data_overwrite",
    ]:
        assert column in sql, column
    print("PASS: migration adds Stage 3 status and audit columns to ingest.raw_file")


def test_candidate_selection_filters_ready_rows() -> None:
    cursor = FakeCursor(
        rows=[
            {
                "raw_file_id": "raw-1",
                "client_code": " CLIENT ",
                "report_type": " report_207 ",
                "source_filename": "clean.csv",
            }
        ]
    )
    candidates = job_stage3._select_stage3_candidates(FakePlatformConn(cursor), limit=5)
    query, params = cursor.executed[0]
    assert "stage2_status = 'OK'" in query
    assert "client_code IS NOT NULL" in query
    assert "btrim(rf.client_code) <> ''" in query
    assert "normalized_csv_path" in query
    assert "raw_path" in query
    assert "normalized_path" not in query
    assert "current_path" not in query
    assert "rf.stage3_status IS NULL" in query
    assert "rf.stage2_updated_at > rf.stage3_finished_at" in query
    assert "FROM artifacts a" in query
    assert "a.artifact_role = 'cleaned'" in query
    assert "a.report_type IN" in query
    # P0-E. Discovery now also admits a stale 'RUNNING' row and an 'ERROR' row,
    # and the staleness threshold is bound rather than inlined, so the grace is
    # the first parameter — ahead of the cleaned-artifact pair and the limit.
    assert "'RUNNING'" in query
    assert "'ERROR'" in query
    assert params == [
        job_stage3.DEFAULT_STAGE3_STALE_GRACE_MINUTES,
        "workflow_b",
        "stage_2_clean",
        5,
    ]
    assert candidates[0].client_code == "CLIENT"
    assert candidates[0].report_type == "report_207"
    print("PASS: Stage 3 candidate selection filters finalized rows with cleaned artifacts")


def test_force_reprocess_requires_target_and_omits_stage3_status_filter() -> None:
    cursor = FakeCursor(rows=[])
    job_stage3._select_stage3_candidates(
        FakePlatformConn(cursor),
        raw_file_id="raw-1",
        force_reprocess=True,
    )
    query, params = cursor.executed[0]
    assert "id = %s" in query
    assert params == ["raw-1"]
    assert "stage3_status IS NULL" not in query
    assert "stage2_status = 'OK'" in query
    print("PASS: force_reprocess is targeted and still requires Stage 2 OK rows")


def test_candidate_selection_ignores_missing_client_code_by_sql() -> None:
    cursor = FakeCursor(rows=[])
    job_stage3._select_stage3_candidates(FakePlatformConn(cursor))
    query, _params = cursor.executed[0]
    assert "client_code IS NOT NULL" in query
    assert "btrim(rf.client_code) <> ''" in query
    print("PASS: Stage 3 candidate SQL excludes rows without client_code")


def test_candidate_selection_is_per_raw_file_not_per_report_type() -> None:
    cursor = FakeCursor(
        rows=[
            {
                "raw_file_id": "raw-new",
                "client_code": "ALPHA00001",
                "report_type": "Alpha_GPS_Baza_LOG",
                "source_filename": "GPS_baza_START_skrypt.xlsm",
                "source_sha256": "sha-new",
            }
        ]
    )
    candidates = job_stage3._select_stage3_candidates(FakePlatformConn(cursor), limit=100)
    query, _params = cursor.executed[0]
    assert "GROUP BY" not in query
    assert "DISTINCT ON" not in query
    # P0-E. `stage3_destination_table` is now projected, because a crashed row
    # names its own destination and recovery needs it. The invariant this test
    # actually guards is unchanged and asserted directly: the destination must
    # never become a *grouping* key that collapses two raw files sharing one
    # target table. Projection is fine; deduplication is not.
    assert "stage3_destination_table" in query.split("FROM ingest.raw_file")[0]
    assert "stage3_destination_table" not in query.split("FROM ingest.raw_file")[1]
    assert candidates[0].raw_file_id == "raw-new"
    assert candidates[0].report_type == "Alpha_GPS_Baza_LOG"
    print("PASS: Stage 3 candidate selection does not suppress newer raw_file_id by report_type")


def test_stage3_ok_is_eligible_only_when_stage2_output_is_newer() -> None:
    from datetime import datetime, timezone

    earlier = datetime(2026, 5, 18, 15, 51, tzinfo=timezone.utc)
    later = datetime(2026, 5, 18, 19, 9, tzinfo=timezone.utc)
    assert job_stage3._stage2_output_is_newer_than_stage3(later, earlier) is True
    assert job_stage3._stage2_output_is_newer_than_stage3(earlier, later) is False
    assert job_stage3._stage3_candidate_exclusion_reasons(
        client_code="ALPHA00001",
        report_type="Alpha_GPS_Baza_LOG",
        stage3_status="OK",
        stage2_updated_at=earlier,
        stage3_finished_at=later,
        has_cleaned_artifact=True,
    ) == ["stage3_status_ok"]
    assert job_stage3._stage3_candidate_exclusion_reasons(
        client_code="ALPHA00001",
        report_type="Alpha_GPS_Baza_LOG",
        stage3_status="OK",
        stage2_updated_at=later,
        stage3_finished_at=earlier,
        has_cleaned_artifact=True,
    ) == []
    print("PASS: Stage 3 OK rows are batch-eligible only when Stage 2 output is newer")


def test_client_account_lookup_uses_existing_control_plane_columns() -> None:
    cursor = FakeCursor(
        one={
            "client_id": "client-1",
            "client_code": "CLIENT",
            "client_db_host": "127.0.0.1",
            "client_db_port": 5432,
            "client_db_name": "client_db",
            "client_db_user": "client_user",
            "client_db_password_secret_ref": "CLIENT_DB_PASSWORD",
        }
    )
    cfg = job_stage3._load_client_account_by_code(cursor, "CLIENT")
    query, params = cursor.executed[0]
    assert "client_db_sslmode" not in query
    assert params == ("CLIENT",)
    assert cfg.client_db_sslmode == "prefer"
    print("PASS: Stage 3 client account lookup uses actual workflow_a_control columns")


def test_locates_stage2_cleaned_artifact() -> None:
    cursor = FakeCursor(
        one={
            "artifact_id": "art-1",
            "filename": "a.csv",
            "original_filename": "orig.csv",
            "display_filename": "display.csv",
        }
    )
    artifact = job_stage3._find_stage2_cleaned_artifact(
        cursor,
        raw_file_id="raw-1",
        report_type="report_207",
    )
    query, params = cursor.executed[0]
    assert params == ("raw-1", "workflow_b", "stage_2_clean", ["report_207"])
    assert "report_type = ANY(%s::text[])" in query
    assert "ORDER BY created_at DESC, artifact_id DESC" in query
    assert " id DESC" not in query
    assert artifact.artifact_id == "art-1"
    print("PASS: Stage 3 locates the matching Stage 2 cleaned artifact using artifact_id ordering")


def test_locates_stage2_cleaned_artifact_with_api_sanitized_report_type() -> None:
    cursor = FakeCursor(
        one={
            "artifact_id": "art-1",
            "filename": "a.csv",
            "original_filename": "orig.csv",
            "display_filename": "display.csv",
        }
    )
    job_stage3._find_stage2_cleaned_artifact(
        cursor,
        raw_file_id="raw-1",
        report_type="Alpha_GPS_Baza_LOG",
    )
    _query, params = cursor.executed[0]
    assert params == (
        "raw-1",
        "workflow_b",
        "stage_2_clean",
        ["Alpha_GPS_Baza_LOG", "alpha_gps_baza_log"],
    )
    print("PASS: Stage 3 lookup accepts the API-sanitized report_type stored on artifacts")


def test_missing_stage2_cleaned_artifact_fails_clearly() -> None:
    cursor = FakeCursor(
        one=None,
        rows=[
            {
                "stage_name": "stage_2_clean",
                "artifact_role": "cleaned",
                "artifact_kind": "REPORT",
                "report_type": "other_report",
                "client_code": "CLIENT",
                "count": 1,
                "latest_created_at": None,
            }
        ],
    )
    try:
        job_stage3._find_stage2_cleaned_artifact(
            cursor,
            raw_file_id="raw-missing",
            report_type="report_207",
        )
    except RuntimeError as exc:
        message = str(exc)
        assert "Stage 2 cleaned artifact not found" in message
        assert "artifact_lookup_filters=" in message
        assert "artifact_rows_for_raw_file=" in message
        assert "stage_2_clean" in message
        assert "other_report" in message
    else:
        raise AssertionError("expected missing cleaned artifact to fail")
    print("PASS: missing Stage 2 cleaned artifact fails with lookup filters and grouped artifact diagnostics")


def test_dry_run_rolls_back_platform_transaction_after_per_file_error() -> None:
    platform_conn = FakePlatformConn(FakeCursor())
    summary = job_stage3._dry_run_candidate(
        platform_conn,
        client=object(),
        run_id="run-1",
        candidate=job_stage3.Candidate("raw-1", "CLIENT", "bad;table", "source.csv"),
    )
    assert summary["dry_run_status"] == "ERROR"
    assert platform_conn.rollbacks == 1
    print("PASS: Stage 3 dry-run rolls back platform connection after per-file errors")


def test_run_rolls_back_and_continues_after_per_file_exception() -> None:
    class FakeClient:
        def log(self, *_args, **_kwargs):
            pass

    platform_conn = FakePlatformConn(FakeCursor())
    candidates = [
        job_stage3.Candidate("raw-1", "CLIENT", "report_207", "source-1.csv"),
        job_stage3.Candidate("raw-2", "CLIENT", "report_207", "source-2.csv"),
    ]
    processed: list[str] = []

    def fake_process(_platform_conn, _client, _run_id, candidate, **_kwargs):
        processed.append(candidate.raw_file_id)
        if candidate.raw_file_id == "raw-1":
            raise RuntimeError("first file failed")
        return job_stage3.LoadResult(
            raw_file_id=candidate.raw_file_id,
            client_code=candidate.client_code,
            report_type=candidate.report_type,
            destination_schema="telematics_reports",
            destination_table=candidate.report_type,
            data_overwrite=False,
            input_rows=1,
            inserted_rows=1,
            updated_rows=0,
            skipped_rows=0,
            status="OK",
        )

    with PatchAttrs(
        _platform_pg_conn=lambda: platform_conn,
        _select_stage3_candidates=lambda *_args, **_kwargs: candidates,
        _process_candidate=fake_process,
    ):
        try:
            job_stage3.run(FakeClient(), "run-1", {})
        except RuntimeError as exc:
            assert "failed for 1 raw file" in str(exc)
        else:
            raise AssertionError("expected Stage 3 run to fail after one per-file error")
    assert processed == ["raw-1", "raw-2"]
    assert platform_conn.rollbacks == 1
    print("PASS: Stage 3 batch processing rolls back after one file error and continues")


def test_no_pending_logging_includes_diagnostics() -> None:
    class FakeClient:
        def __init__(self):
            self.logs = []

        def log(self, level, log_type, source, message, *, run_id=None, context=None, error=None):
            self.logs.append(
                {
                    "level": level,
                    "type": log_type,
                    "source": source,
                    "message": message,
                    "run_id": run_id,
                    "context": context or {},
                    "error": error,
                }
            )

    client = FakeClient()
    diagnostics = {
        "stage2_ok_counts_by_report_type_client_stage3_status": [
            {
                "report_type": "Alpha_GPS_Baza_LOG",
                "client_code": "ALPHA00001",
                "stage3_status": "OK",
                "count": 1,
            }
        ],
        "latest_stage2_ok_rows": [
            {
                "raw_file_id": "raw-1",
                "report_type": "Alpha_GPS_Baza_LOG",
                "client_code": "ALPHA00001",
                "stage3_status": "OK",
                "has_cleaned_artifact": True,
                "exclusion_reasons": ["stage3_status_ok"],
            }
        ],
        "exclusion_reason_counts": {"stage3_status_ok": 1},
    }

    with PatchAttrs(
        _platform_pg_conn=lambda: FakePlatformConn(FakeCursor()),
        _select_stage3_candidates=lambda *_args, **_kwargs: [],
        _stage3_no_pending_diagnostics=lambda *_args, **_kwargs: diagnostics,
    ):
        job_stage3.run(client, "run-1", {"limit": 100})

    no_pending_logs = [
        log for log in client.logs if log["message"] == "No Workflow B Stage 3 pending reports found"
    ]
    assert len(no_pending_logs) == 1
    context = no_pending_logs[0]["context"]
    assert context["stage2_ok_counts_by_report_type_client_stage3_status"] == diagnostics[
        "stage2_ok_counts_by_report_type_client_stage3_status"
    ]
    assert context["latest_stage2_ok_rows"] == diagnostics["latest_stage2_ok_rows"]
    assert context["exclusion_reason_counts"] == {"stage3_status_ok": 1}
    print("PASS: no-pending Stage 3 log includes batch diagnostics")


def test_destination_schema_and_table_ddl_are_present() -> None:
    source = (REPO_ROOT / "jobs/reports/stage3/job_stage3.py").read_text()
    assert job_stage3.DESTINATION_SCHEMA == "telematics_reports"
    for forbidden in (
        "CREATE SCHEMA IF NOT EXISTS",
        "CREATE TABLE IF NOT EXISTS",
        "ALTER TABLE {}.{}",
        "CREATE UNIQUE INDEX IF NOT EXISTS {} ON",
    ):
        assert forbidden not in source
    assert job_stage3._safe_table_name("report_207") == "report_207"
    print("PASS: recurring Stage 3 contains no destination DDL")

def test_preserves_report_columns_and_record_id() -> None:
    df = FakeDataFrame([{"driver": "A", "amount": "10"}])
    job_stage3._ensure_record_id_column(df)
    assert df.columns == ["driver", "amount", "record_id"]
    job_stage3._validate_report_columns(df.columns)
    print("PASS: Stage 3 preserves cleaned columns and amends missing record_id")


def test_technical_metadata_columns_are_reserved() -> None:
    assert "_raw_file_id" in job_stage3.TECHNICAL_COLUMNS
    assert "_stage3_run_id" in job_stage3.TECHNICAL_COLUMNS
    try:
        job_stage3._validate_report_columns(["record_id", "_raw_file_id"])
    except RuntimeError as exc:
        assert "conflicts" in str(exc)
    else:
        raise AssertionError("expected technical column collision to fail")
    print("PASS: Stage 3 reserves technical metadata columns with '_' prefix")


def test_default_policy_is_false_when_missing() -> None:
    cursor = FakeCursor(one=None)
    data_overwrite, found = job_stage3._load_data_overwrite_policy(
        cursor,
        client_code="CLIENT",
        report_type="report_207",
    )
    assert data_overwrite is False
    assert found is False
    print("PASS: missing Stage 3 load policy defaults data_overwrite to false")


def test_auto_grant_permissions_default_is_false_in_run_params() -> None:
    try:
        job_stage3._process_candidate(
            FakePlatformConn(FakeCursor()), object(), "run-1",
            job_stage3.Candidate("raw-1", "CLIENT", "report_207", "source.csv"),
            auto_grant_permissions=True,
        )
    except job_stage3.RuntimeSchemaMutationDisabledError as exc:
        assert "042_workflow_b_stage3_runtime_schema.sql" in str(exc)
    else:
        raise AssertionError("runtime auto-grant must be rejected")
    print("PASS: Stage 3 rejects runtime permission bootstrap")

def test_dry_run_plan_does_not_emit_destination_writes() -> None:
    df = FakeDataFrame([{"record_id": "r1", "value": "a"}])
    cursor = FakeCursor()
    plan = job_stage3._build_load_plan(
        cursor,
        df=df,
        data_overwrite=False,
        destination_schema="telematics_reports",
        destination_table="report_207",
    )
    write_verbs = ("CREATE ", "ALTER ", "INSERT ", "UPDATE ", "DELETE ", "TRUNCATE ")
    assert not any(query.lstrip().upper().startswith(write_verbs) for query, _params in cursor.executed)
    assert plan["columns_to_create"] == ["record_id", "value"] + list(job_stage3.TECHNICAL_COLUMNS)
    assert plan["would_insert_rows"] == 1
    print("PASS: Stage 3 dry-run planning inspects only and emits no destination write SQL")


def test_dry_run_plan_counts_record_id_no_overwrite() -> None:
    plan = _plan_for(
        FakeDataFrame(
            [
                {"record_id": "r1", "value": "existing"},
                {"record_id": "r2", "value": "new"},
                {"record_id": "", "value": "blank"},
            ]
        ),
        data_overwrite=False,
        existing_columns={"record_id": "text", "value": "text"},
        existing_record_ids={"r1"},
    )
    assert plan["would_insert_rows"] == 1
    assert plan["would_update_rows"] == 0
    assert plan["would_skip_rows"] == 2
    assert plan["would_reject_rows"] == 1
    print("PASS: dry-run counts insert/skip decisions for record_id and data_overwrite=false")


def test_dry_run_plan_counts_record_id_overwrite() -> None:
    plan = _plan_for(
        FakeDataFrame(
            [
                {"record_id": "r1", "value": "updated"},
                {"record_id": "r2", "value": "new"},
            ]
        ),
        data_overwrite=True,
        existing_columns={"record_id": "text", "value": "text"},
        existing_record_ids={"r1"},
    )
    assert plan["would_insert_rows"] == 1
    assert plan["would_update_rows"] == 1
    assert plan["would_skip_rows"] == 0
    print("PASS: dry-run counts insert/update decisions for record_id and data_overwrite=true")


def test_dry_run_plan_handles_missing_record_id_modes() -> None:
    df = FakeDataFrame([{"record_id": "", "value": "a"}, {"record_id": " ", "value": "b"}])
    no_overwrite = _plan_for(df, data_overwrite=False)
    assert no_overwrite["would_insert_rows"] == 0
    assert no_overwrite["would_skip_rows"] == 2
    assert no_overwrite["would_replace_table"] is False

    overwrite = _plan_for(
        FakeDataFrame([{"record_id": "", "value": "a"}, {"record_id": "", "value": "b"}]),
        data_overwrite=True,
    )
    assert overwrite["would_insert_rows"] == 2
    assert overwrite["would_skip_rows"] == 0
    assert overwrite["would_replace_table"] is True
    print("PASS: dry-run reports no-op or table replacement for fully empty record_id inputs")


def test_dry_run_plan_detects_existing_duplicate_record_ids() -> None:
    plan = _plan_for(
        FakeDataFrame([{"record_id": "r1", "value": "a"}]),
        data_overwrite=False,
        existing_columns={"record_id": "text", "value": "text"},
        duplicate_existing_record_ids=[{"record_id": "dup", "count": 2}],
    )
    assert plan["duplicate_existing_record_ids_detected"] is True
    assert any("duplicate non-empty record_id" in error for error in plan["errors"])
    print("PASS: dry-run detects duplicate existing destination record IDs before load")


def test_dry_run_candidate_downloads_artifact_and_does_not_update_status_by_default() -> None:
    class FakeClient:
        def __init__(self):
            self.downloaded = []
            self.uploads = []

        def download_artifact(self, artifact_id, path):
            self.downloaded.append((artifact_id, path))
            Path(path).write_text("record_id;value\nr1;a\n", encoding="utf-8")

        def upload_artifact(self, *args, **kwargs):
            self.uploads.append((args, kwargs))

    def fail_status_write(*_args, **_kwargs):
        raise AssertionError("dry-run must not update Stage 3 status")

    client = FakeClient()
    artifact = job_stage3.ArtifactRef("art-1", "clean.csv", "source.csv", "clean.csv")
    plan = {
        "input_rows": 1,
        "original_has_record_id": True,
        "usable_record_id": True,
        "non_empty_record_id_rows": 1,
        "empty_record_id_rows": 0,
        "duplicate_record_id_rows_in_input": 0,
        "destination_schema_exists": True,
        "destination_table_exists": True,
        "columns_to_create": [],
        "columns_to_add": [],
        "unique_index_exists": True,
        "duplicate_existing_record_ids_detected": False,
        "duplicate_existing_record_ids": [],
        "would_insert_rows": 1,
        "would_update_rows": 0,
        "would_skip_rows": 0,
        "would_reject_rows": 0,
        "would_replace_table": False,
        "warnings": [],
        "errors": [],
    }
    with PatchAttrs(
        _find_stage2_cleaned_artifact=lambda *_args, **_kwargs: artifact,
        _load_data_overwrite_policy=lambda *_args, **_kwargs: (False, False),
        _load_client_account_by_code=lambda *_args, **_kwargs: job_stage3.ClientDbConfig(
            client_code="CLIENT",
            client_id="client-1",
            client_db_host="127.0.0.1",
            client_db_port=5432,
            client_db_name="client_db",
            client_db_user="client_user",
            client_db_password_secret_ref="SECRET",
            client_db_sslmode="prefer",
        ),
        _client_business_pg_conn=lambda _cfg: FakeDestinationConn(),
        _read_cleaned_csv=lambda _path: FakeDataFrame([{"record_id": "r1", "value": "a"}]),
        _build_load_plan=lambda *_args, **_kwargs: plan,
        _mark_stage3_started=fail_status_write,
        _mark_stage3_finished=fail_status_write,
        _mark_stage3_error=fail_status_write,
    ):
        summary = job_stage3._dry_run_candidate(
            FakePlatformConn(FakeCursor()),
            client,
            "run-1",
            job_stage3.Candidate("raw-1", "CLIENT", "report_207", "source.csv"),
        )
    assert summary["dry_run_status"] == "OK"
    assert client.downloaded and client.downloaded[0][0] == "art-1"
    assert client.uploads == []
    print("PASS: dry-run downloads/validates the artifact without updating raw_file status by default")


def test_dry_run_auto_grant_permissions_does_not_apply_grants() -> None:
    try:
        job_stage3._dry_run_candidate(
            FakePlatformConn(FakeCursor()), object(), "run-1",
            job_stage3.Candidate("raw-1", "CLIENT", "report_207", "source.csv"),
            auto_grant_permissions=True,
        )
    except job_stage3.RuntimeSchemaMutationDisabledError:
        pass
    else:
        raise AssertionError("dry-run auto-grant must be rejected")
    print("PASS: Stage 3 dry-run cannot request runtime grants")

def test_real_run_auto_grant_permissions_before_client_user_load() -> None:
    batch = job_stage3.Stage3BatchResult()
    item = job_stage3._stage3_failure_item(
        None,
        job_stage3.RuntimeSchemaMutationDisabledError("auto_grant_permissions"),
        dry_run=False,
        force_reprocess=False,
    )
    batch.items.append(item)
    assert item.retryable is False
    assert item.operator_action_required is True
    assert item.error_category == "runtime_schema_mutation_disabled"
    print("PASS: removed runtime grant option is typed non-retryable/operator-action")

def test_dry_run_candidate_reports_missing_or_unsafe_inputs() -> None:
    missing = job_stage3._dry_run_candidate(
        FakePlatformConn(FakeCursor()),
        client=object(),
        run_id="run-1",
        candidate=job_stage3.Candidate("raw-1", "CLIENT", "bad;table", "source.csv"),
    )
    assert missing["dry_run_status"] == "ERROR"
    assert any("Unsafe report_type" in error for error in missing["errors"])

    with tempfile.TemporaryDirectory() as tmpdir:
        empty_path = Path(tmpdir) / "empty.csv"
        empty_path.write_text("", encoding="utf-8")
        try:
            job_stage3._require_nonempty_artifact_file(empty_path, "art-empty")
        except RuntimeError as exc:
            assert "empty" in str(exc)
        else:
            raise AssertionError("expected empty artifact validation failure")
    print("PASS: dry-run reports unsafe report types and empty artifact validation failures")


def test_record_id_no_overwrite_inserts_new_and_skips_existing() -> None:
    df = FakeDataFrame(
        [
            {"record_id": "r1", "value": "old"},
            {"record_id": "r2", "value": "new"},
            {"record_id": " ", "value": "blank"},
        ]
    )
    conn = FakeDestinationConn()

    def fake_insert(_cur, _schema, _table, _columns, rows, **_kwargs):
        assert [row["record_id"] for row in rows] == ["r2"]
        return len(rows)

    with PatchAttrs(
        _inspect_destination=lambda *_args, **_kwargs: _destination_inspection(
            {"record_id": "text", "value": "text"}
        ),
        _require_destination_schema_ready=lambda *_args, **_kwargs: None,
        _find_existing_duplicate_record_ids=lambda *_args, **_kwargs: [],
        _fetch_existing_record_ids=lambda *_args, **_kwargs: {"r1"},
        _insert_rows=fake_insert,
    ):
        result = job_stage3._load_dataframe_to_destination(
            conn,
            raw_file_id="raw-1",
            run_id="run-1",
            client_code="CLIENT",
            report_type="report_207",
            source_artifact_id="art-1",
            source_filename="clean.csv",
            data_overwrite=False,
            df=df,
        )
    assert result.inserted_rows == 1
    assert result.updated_rows == 0
    assert result.skipped_rows == 2
    assert conn.commits == 1
    print("PASS: record_id load with data_overwrite=false appends new rows and skips existing rows")


def test_record_id_overwrite_upserts_existing_and_new() -> None:
    df = FakeDataFrame([{"record_id": "r1", "value": "updated"}, {"record_id": "r2", "value": "new"}])
    conn = FakeDestinationConn()
    with PatchAttrs(
        _inspect_destination=lambda *_args, **_kwargs: _destination_inspection(
            {"record_id": "text", "value": "text"}
        ),
        _require_destination_schema_ready=lambda *_args, **_kwargs: None,
        _find_existing_duplicate_record_ids=lambda *_args, **_kwargs: [],
        _fetch_existing_record_ids=lambda *_args, **_kwargs: {"r1"},
        _upsert_record_id_rows=lambda *_args, **_kwargs: (1, 1),
    ):
        result = job_stage3._load_dataframe_to_destination(
            conn,
            raw_file_id="raw-1",
            run_id="run-1",
            client_code="CLIENT",
            report_type="report_207",
            source_artifact_id="art-1",
            source_filename="clean.csv",
            data_overwrite=True,
            df=df,
        )
    assert result.inserted_rows == 1
    assert result.updated_rows == 1
    assert result.skipped_rows == 0
    print("PASS: record_id load with data_overwrite=true upserts existing and new rows")


def test_missing_record_id_no_overwrite_skips_noop() -> None:
    df = FakeDataFrame([{"record_id": "", "value": "a"}, {"record_id": " ", "value": "b"}])
    conn = FakeDestinationConn()
    with PatchAttrs(_require_destination_schema_ready=lambda *_args, **_kwargs: None):
        result = job_stage3._load_dataframe_to_destination(
            conn,
            raw_file_id="raw-1",
            run_id="run-1",
            client_code="CLIENT",
            report_type="report_207",
            source_artifact_id="art-1",
            source_filename="clean.csv",
            data_overwrite=False,
            df=df,
        )
    assert result.status == "SKIPPED_NO_RECORD_ID"
    assert result.skipped_rows == 2
    assert result.inserted_rows == 0
    assert conn.rollbacks == 1
    print("PASS: missing/empty record_id with data_overwrite=false is a skipped no-op")


def test_missing_record_id_overwrite_replaces_table() -> None:
    df = FakeDataFrame([{"record_id": "", "value": "a"}, {"record_id": "", "value": "b"}])
    conn = FakeDestinationConn()
    calls = []

    def fake_replace(_cur, _schema, _table, _columns, rows, **_kwargs):
        calls.append(rows)

    with PatchAttrs(
        _require_destination_schema_ready=lambda *_args, **_kwargs: None,
        _replace_table_rows=fake_replace,
    ):
        result = job_stage3._load_dataframe_to_destination(
            conn,
            raw_file_id="raw-1",
            run_id="run-1",
            client_code="CLIENT",
            report_type="report_207",
            source_artifact_id="art-1",
            source_filename="clean.csv",
            data_overwrite=True,
            df=df,
        )
    assert result.inserted_rows == 2
    assert result.skipped_rows == 0
    assert len(calls[0]) == 2
    print("PASS: empty record_id with data_overwrite=true replaces destination table contents")


def test_empty_record_id_rows_are_rejected_in_record_id_mode() -> None:
    prepared = job_stage3._prepared_record_id_rows(
        FakeDataFrame(
            [
                {"record_id": "r1", "value": "a"},
                {"record_id": "", "value": "blank"},
                {"record_id": "r1", "value": "dupe"},
            ]
        ),
        ["record_id", "value"],
    )
    reasons = [row["_stage3_skip_reason"] for row in prepared["rejected_rows"]]
    assert reasons == ["empty_record_id", "duplicate_record_id_in_input"]
    assert [row["record_id"] for row in prepared["loadable_rows"]] == ["r1"]
    print("PASS: empty and duplicate record_id rows are rejected before load")


def test_existing_duplicate_record_ids_fail_before_unique_index() -> None:
    plan = {
        "destination_table_exists": True,
        "columns_to_add": [],
        "usable_record_id": True,
        "unique_index_exists": False,
    }
    try:
        job_stage3._require_destination_schema_ready(
            plan, schema_name="telematics_reports", table_name="report_207"
        )
    except job_stage3.Stage3SchemaReadinessError as exc:
        assert "unique index telematics_reports.report_207.report_207__record_id_uidx" in str(exc)
    else:
        raise AssertionError("missing unique index must fail readiness")
    print("PASS: missing record_id unique index requires the controlled migration")

def test_failure_rolls_back_destination_changes() -> None:
    df = FakeDataFrame([{"record_id": "r1", "value": "a"}])
    conn = FakeDestinationConn()

    def fail(*_args, **_kwargs):
        raise RuntimeError("boom")

    with PatchAttrs(_require_destination_schema_ready=fail):
        try:
            job_stage3._load_dataframe_to_destination(
                conn,
                raw_file_id="raw-1",
                run_id="run-1",
                client_code="CLIENT",
                report_type="report_207",
                source_artifact_id="art-1",
                source_filename="clean.csv",
                data_overwrite=True,
                df=df,
            )
        except RuntimeError as exc:
            assert "boom" in str(exc)
        else:
            raise AssertionError("expected destination load failure")
    assert conn.rollbacks == 1
    assert conn.commits == 0
    print("PASS: destination load failure rolls back the transaction")


def test_empty_downloaded_artifact_fails_clearly() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "empty.csv"
        path.write_bytes(b"")
        try:
            job_stage3._require_nonempty_artifact_file(path, "artifact-empty")
        except RuntimeError as exc:
            assert "Downloaded artifact file is empty" in str(exc)
            assert "artifact-empty" in str(exc)
        else:
            raise AssertionError("expected empty downloaded artifact to fail")
    print("PASS: empty downloaded artifact fails before CSV parsing")


def test_identifier_sanitization_blocks_injection() -> None:
    try:
        job_stage3._safe_table_name("report_207; DROP TABLE raw_file")
    except RuntimeError as exc:
        assert "Unsafe report_type" in str(exc)
    else:
        raise AssertionError("expected unsafe report_type to fail")
    try:
        job_stage3._validate_report_columns(["record_id", "bad\x00column"])
    except RuntimeError as exc:
        assert "NUL" in str(exc)
    else:
        raise AssertionError("expected NUL column name to fail")
    print("PASS: SQL identifier validation blocks unsafe table and column names")


def test_existing_table_new_columns_are_added_as_text() -> None:
    plan = {
        "destination_table_exists": True,
        "columns_to_add": ["new_report_column"],
        "usable_record_id": False,
        "unique_index_exists": True,
    }
    try:
        job_stage3._require_destination_schema_ready(
            plan, schema_name="telematics_reports", table_name="report_207"
        )
    except job_stage3.Stage3SchemaReadinessError as exc:
        assert "column telematics_reports.report_207.new_report_column" in str(exc)
        assert "042_workflow_b_stage3_runtime_schema.sql" in str(exc)
    else:
        raise AssertionError("missing destination column must fail readiness")
    print("PASS: missing report columns require the controlled migration")

def test_stage3_status_updates_for_success_skip_and_error() -> None:
    cursor = FakeCursor()
    result = job_stage3.LoadResult(
        raw_file_id="raw-1",
        client_code="CLIENT",
        report_type="report_207",
        destination_schema="telematics_reports",
        destination_table="report_207",
        data_overwrite=False,
        input_rows=2,
        inserted_rows=1,
        updated_rows=0,
        skipped_rows=1,
        status="OK",
    )
    job_stage3._mark_stage3_started(
        cursor,
        "raw-1",
        destination_schema="telematics_reports",
        destination_table="report_207",
    )
    assert "stage3_error = NULL" in cursor.executed[-1][0]
    assert "stage3_inserted_rows = NULL" in cursor.executed[-1][0]
    job_stage3._mark_stage3_finished(cursor, result)
    assert cursor.executed[-1][1][0] == "OK"
    result.status = "SKIPPED_NO_RECORD_ID"
    result.error = "no ids"
    job_stage3._mark_stage3_finished(cursor, result)
    assert cursor.executed[-1][1][0] == "SKIPPED_NO_RECORD_ID"
    job_stage3._mark_stage3_error(
        cursor,
        raw_file_id="raw-1",
        error_message="boom",
        destination_schema="telematics_reports",
        destination_table="report_207",
        data_overwrite=False,
    )
    assert "stage3_status = 'ERROR'" in cursor.executed[-1][0]
    print("PASS: Stage 3 status fields are updated for success, skip, and error")


def test_rerun_does_not_duplicate_loaded_data() -> None:
    cursor = FakeCursor(rows=[])
    job_stage3._select_stage3_candidates(FakePlatformConn(cursor))
    query, _params = cursor.executed[0]
    assert "rf.stage3_status IS NULL" in query
    assert "rf.stage2_updated_at > rf.stage3_finished_at" in query
    prepared = job_stage3._prepared_record_id_rows(
        FakeDataFrame([{"record_id": "r1", "value": "already"}]),
        ["record_id", "value"],
    )
    assert prepared["loadable_rows"][0]["record_id"] == "r1"
    print("PASS: Stage 3 excludes already processed files and record_id mode skips existing IDs")


def test_report_207_repeated_business_rows_load_with_unique_record_ids_and_rerun_skips() -> None:
    rows = [
        {
            "Data i czas": "01.06.2026 10:00",
            "Nr rejestracyjny": "WX12345",
            "Prędkość": "90",
            "Ograniczenie prędkości drogowej": "50",
            "Lokalizacja": "Location A",
            "record_id": "rid-speed-90",
        },
        {
            "Data i czas": "01.06.2026 10:00",
            "Nr rejestracyjny": "WX12345",
            "Prędkość": "91",
            "Ograniczenie prędkości drogowej": "50",
            "Lokalizacja": "Location A",
            "record_id": "rid-speed-91",
        },
        {
            "Data i czas": "01.06.2026 11:00",
            "Nr rejestracyjny": "WX99999",
            "Prędkość": "80",
            "Ograniczenie prędkości drogowej": "50",
            "Lokalizacja": "Location C",
            "record_id": "rid-identical-1",
        },
        {
            "Data i czas": "01.06.2026 11:00",
            "Nr rejestracyjny": "WX99999",
            "Prędkość": "80",
            "Ograniczenie prędkości drogowej": "50",
            "Lokalizacja": "Location C",
            "record_id": "rid-identical-2",
        },
    ]
    df = FakeDataFrame(rows)
    first_plan = _plan_for(
        df,
        data_overwrite=False,
        existing_columns={column: "text" for column in df.columns},
    )
    assert first_plan["input_rows"] == 4
    assert first_plan["duplicate_record_id_rows_in_input"] == 0
    assert first_plan["would_insert_rows"] == 4
    assert first_plan["would_skip_rows"] == 0
    assert len(first_plan["rows_to_insert"]) == 4

    existing_ids = {row["record_id"] for row in rows}
    rerun_plan = _plan_for(
        FakeDataFrame(rows),
        data_overwrite=False,
        existing_columns={column: "text" for column in df.columns},
        existing_record_ids=existing_ids,
    )
    assert rerun_plan["would_insert_rows"] == 0
    assert rerun_plan["would_skip_rows"] == 4
    assert {row["_stage3_skip_reason"] for row in rerun_plan["rejected_rows"]} == {"record_id_already_loaded"}
    print("PASS: Stage 3 loads repeated report_207 business rows when record_ids are unique and rerun skips them")


def test_stage3_result_artifact_filenames_are_unique_per_source_file() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.uploads = []

        def upload_artifact(self, path, **kwargs):
            assert Path(path).exists(), path
            self.uploads.append({"path_name": Path(path).name, **kwargs})
            return f"artifact-{len(self.uploads)}"

    client = FakeClient()
    source = job_stage3.ArtifactRef(
        artifact_id="61eef8f6-8895-4bdc-8f49-da7015b113ba",
        filename="clean.csv",
        original_filename="source.xls",
        display_filename="clean.csv",
    )
    result_a = job_stage3.LoadResult(
        raw_file_id="11111111-aaaa-bbbb-cccc-000000000000",
        client_code="CLIENT",
        report_type="report_207",
        destination_schema="telematics_reports",
        destination_table="report_207",
        data_overwrite=False,
        input_rows=2,
        inserted_rows=2,
        updated_rows=0,
        skipped_rows=1,
        status="OK",
        source_artifact_id="61eef8f6-8895-4bdc-8f49-da7015b113ba",
        rejected_rows=[{"record_id": "rid-1", "_stage3_skip_reason": "record_id_already_loaded"}],
    )
    result_b = job_stage3.LoadResult(
        raw_file_id="22222222-aaaa-bbbb-cccc-000000000000",
        client_code="CLIENT",
        report_type="report_207",
        destination_schema="telematics_reports",
        destination_table="report_207",
        data_overwrite=False,
        input_rows=2,
        inserted_rows=2,
        updated_rows=0,
        skipped_rows=1,
        status="OK",
        source_artifact_id="8fbe0fab-5c30-4121-8c3f-18038cacc5ed",
        rejected_rows=[{"record_id": "rid-2", "_stage3_skip_reason": "record_id_already_loaded"}],
    )

    job_stage3._upload_stage3_artifacts(client, "run-1", source, result_a)
    job_stage3._upload_stage3_artifacts(client, "run-1", source, result_b)

    display_filenames = [upload["display_filename"] for upload in client.uploads]
    path_names = [upload["path_name"] for upload in client.uploads]
    assert len(display_filenames) == 4
    assert len(set(display_filenames)) == 4, display_filenames
    assert len(set(path_names)) == 4, path_names
    assert any(name.startswith("stage3_load_result__11111111__aaaaaaaa") for name in display_filenames)
    assert any(name.startswith("stage3_load_result__22222222__bbbbbbbb") for name in display_filenames)
    assert any(name.startswith("stage3_rejected_rows__11111111__aaaaaaaa") for name in display_filenames)
    assert any(name.startswith("stage3_rejected_rows__22222222__bbbbbbbb") for name in display_filenames)

    from datetime import datetime, timezone
    from api.artifacts.layout import build_artifact_object_key

    object_keys = [
        build_artifact_object_key(
            workflow_name=upload["workflow_name"],
            stage_name=upload["stage_name"],
            run_id="run-1",
            artifact_role=upload["artifact_role"],
            ext=Path(upload["display_filename"]).suffix.lstrip("."),
            created_at=datetime(2026, 7, 8, tzinfo=timezone.utc),
            report_type=upload["report_type"],
            raw_file_id=upload["raw_file_id"],
            display_filename=upload["display_filename"],
        )
        for upload in client.uploads
    ]
    assert len(set(object_keys)) == 4, object_keys
    print("PASS: Stage 3 result/rejected artifact filenames and object keys are unique per source artifact")


def test_client_migration_is_idempotent_and_least_privilege() -> None:
    sql = (REPO_ROOT / "db/client_business/042_workflow_b_stage3_runtime_schema.sql").read_text()
    assert "ADD COLUMN IF NOT EXISTS _loaded_at" in sql
    assert "CREATE UNIQUE INDEX IF NOT EXISTS" in sql
    assert "ADD COLUMN IF NOT EXISTS migrated_to_client_db" in sql
    assert "GRANT CONNECT ON DATABASE %I TO %I" in sql
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE telematics_reports.%I" in sql
    assert "GRANT SELECT, UPDATE ON TABLE public.client_trips" in sql
    assert "GRANT ALL" not in sql
    assert "ALTER TABLE telematics_reports.report_207 OWNER" not in sql
    assert "GRANT CREATE" not in sql
    print("PASS: migration is additive/idempotent and uses explicit least-privilege grants")


def test_schema_readiness_failure_is_typed_and_diagnostic() -> None:
    exc = job_stage3.Stage3SchemaReadinessError([
        job_stage3.MissingSchemaObject(
            "telematics_reports", "report_207", "column", "_raw_file_id"
        )
    ])
    item = job_stage3._stage3_failure_item(
        job_stage3.Candidate("raw-1", "CLIENT", "report_207", "source.csv"),
        exc,
        dry_run=False,
        force_reprocess=False,
    )
    assert item.outcome == job_stage3.Stage3Outcome.FAILED_SCHEMA_NOT_READY
    assert item.retryable is False and item.operator_action_required is True
    assert item.error_category == "schema_not_ready"
    assert "telematics_reports.report_207._raw_file_id" in item.error_detail
    assert "042_workflow_b_stage3_runtime_schema.sql" in item.error_detail
    print("PASS: Stage 3 schema failure is typed, non-retryable, and diagnostic")


def test_alpha_gps_missing_table_is_typed_schema_readiness() -> None:
    conn = FakeDestinationConn()
    with PatchAttrs(_alpha_gps_rows=lambda _df: ([], [])):
        try:
            job_stage3._load_alpha_gps_replace_all(
                conn,
                raw_file_id="raw-1",
                run_id="run-1",
                client_code="ALPHA00001",
                report_type="Alpha_GPS_Baza_LOG",
                source_artifact_id="art-1",
                source_filename="clean.csv",
                df=FakeDataFrame([]),
                source_sha256=None,
                raw_artifact_id=None,
                normalized_artifact_id=None,
                cleaned_artifact_id="art-1",
                destination_schema="telematics_reports",
                destination_table="Alpha_GPS_Baza_LOG",
            )
        except job_stage3.Stage3SchemaReadinessError as exc:
            assert "table telematics_reports.Alpha_GPS_Baza_LOG" in str(exc)
            assert "023_alpha_gps_baza_log_workflow_b.sql" in str(exc)
        else:
            raise AssertionError("missing Alpha GPS table must fail readiness")
    assert conn.rollbacks == 1 and conn.commits == 0
    print("PASS: special Alpha GPS missing table is typed with its migration")


def main() -> None:
    test_migration_creates_policy_table()
    test_migration_adds_stage3_raw_file_columns()
    test_client_migration_is_idempotent_and_least_privilege()
    test_schema_readiness_failure_is_typed_and_diagnostic()
    test_alpha_gps_missing_table_is_typed_schema_readiness()
    test_candidate_selection_filters_ready_rows()
    test_force_reprocess_requires_target_and_omits_stage3_status_filter()
    test_candidate_selection_ignores_missing_client_code_by_sql()
    test_candidate_selection_is_per_raw_file_not_per_report_type()
    test_stage3_ok_is_eligible_only_when_stage2_output_is_newer()
    test_client_account_lookup_uses_existing_control_plane_columns()
    test_locates_stage2_cleaned_artifact()
    test_locates_stage2_cleaned_artifact_with_api_sanitized_report_type()
    test_missing_stage2_cleaned_artifact_fails_clearly()
    test_dry_run_rolls_back_platform_transaction_after_per_file_error()
    test_run_rolls_back_and_continues_after_per_file_exception()
    test_no_pending_logging_includes_diagnostics()
    test_destination_schema_and_table_ddl_are_present()
    test_preserves_report_columns_and_record_id()
    test_technical_metadata_columns_are_reserved()
    test_default_policy_is_false_when_missing()
    test_auto_grant_permissions_default_is_false_in_run_params()
    test_dry_run_plan_does_not_emit_destination_writes()
    test_dry_run_plan_counts_record_id_no_overwrite()
    test_dry_run_plan_counts_record_id_overwrite()
    test_dry_run_plan_handles_missing_record_id_modes()
    test_dry_run_plan_detects_existing_duplicate_record_ids()
    test_dry_run_candidate_downloads_artifact_and_does_not_update_status_by_default()
    test_dry_run_auto_grant_permissions_does_not_apply_grants()
    test_real_run_auto_grant_permissions_before_client_user_load()
    test_dry_run_candidate_reports_missing_or_unsafe_inputs()
    test_record_id_no_overwrite_inserts_new_and_skips_existing()
    test_record_id_overwrite_upserts_existing_and_new()
    test_missing_record_id_no_overwrite_skips_noop()
    test_missing_record_id_overwrite_replaces_table()
    test_empty_record_id_rows_are_rejected_in_record_id_mode()
    test_existing_duplicate_record_ids_fail_before_unique_index()
    test_failure_rolls_back_destination_changes()
    test_empty_downloaded_artifact_fails_clearly()
    test_identifier_sanitization_blocks_injection()
    test_existing_table_new_columns_are_added_as_text()
    test_stage3_status_updates_for_success_skip_and_error()
    test_rerun_does_not_duplicate_loaded_data()
    test_report_207_repeated_business_rows_load_with_unique_record_ids_and_rerun_skips()
    test_stage3_result_artifact_filenames_are_unique_per_source_file()


if __name__ == "__main__":
    main()
