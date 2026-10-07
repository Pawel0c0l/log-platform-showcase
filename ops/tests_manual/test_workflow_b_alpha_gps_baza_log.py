#!/usr/bin/env python3
"""Manual regressions for Workflow B Alpha GPS XLSM LOG import.

Run:

    cd /opt/log-platform
    PYTHONPATH="$PWD" python3 ops/tests_manual/test_workflow_b_alpha_gps_baza_log.py
"""
from __future__ import annotations

import csv
import sys
import tempfile
from pathlib import Path

import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.mail import fetch_reports  # noqa: E402
from jobs.alpha import import_gps_baza_log_xlsm  # noqa: E402
from jobs.reports.stage2.detector import detect_report_type  # noqa: E402
from jobs.reports.stage2 import job_stage2  # noqa: E402
from jobs.reports.stage2.registry import REGISTERED_REPORTS  # noqa: E402
from jobs.reports.stage2.types.alpha_gps_baza_log import AlphaGPSBazaLog  # noqa: E402
from jobs.reports.stage3 import job_stage3  # noqa: E402
from ops import diagnose_workflow_b_file_lineage  # noqa: E402


class FakeDataFrame:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = [dict(row) for row in rows]
        self.columns = list(rows[0].keys()) if rows else []
        self.index = list(range(len(rows)))

    def iterrows(self):
        for idx, row in enumerate(self.rows):
            yield idx, row

    def __len__(self):
        return len(self.rows)


class FakeCursor:
    def __init__(self):
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, query, params=None):
        self.executed.append((str(query), params))

    def executemany(self, query, values):
        self.executed.append((str(query), values))


class FakeDestinationConn:
    def __init__(self):
        self.cursor_obj = FakeCursor()
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cursor_obj

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


def _sample_xlsm_bytes() -> bytes:
    """Legacy single-sheet fixture: every header row lives inside ``LOG``."""

    from openpyxl import Workbook

    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "GPS_baza_START_skrypt.xlsm"
        wb = Workbook()
        ws = wb.active
        ws.title = "Other"
        ws.append(["not", "the", "sheet"])
        log = wb.create_sheet("LOG")
        log.append(["intro"])
        log.append(["ID", "Nr rejestracyjny", "Data przydziału", "RFID", "PRYW", "EDYS", "OPTIMA", "OTK"])
        log.append(["noise", "", "", "", "", "", "", ""])
        log.append(["ID", "Nr rejestracyjny", "Data przydziału", "Nazwa Pliku csv"])
        log.append(["1", " wx 12345 ", "01.05.2026", "a.csv"])
        log.append(["", "", "", ""])
        log.append(["2", "WX 99999", 45000, "b.csv"])
        log.append(["ID", "Data przydziału", "PRYW stary", "PRYW aktualny"])
        log.append(["3", "2026-05-03", "old", "new"])
        wb.save(path)
        wb.close()
        return path.read_bytes()


def _real_alpha_gps_xlsm_bytes() -> bytes:
    """Multi-sheet fixture mirroring the production ALPHA00001 workbook.

    The detection header lives on ``GPS_baza_START``, the cleaning-output
    header lives on ``LOG``, and the cleaning-stop header lives on
    ``Status_Prywatnosci`` — exactly like the email-delivered workbook.
    """

    from openpyxl import Workbook

    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "GPS_baza_START_skrypt.xlsm"
        wb = Workbook()
        gps = wb.active
        gps.title = "GPS_baza_START"
        gps.append(["ID", "Nr rejestracyjny", "Data przydziału", "RFID", "PRYW", "EDYS", "OPTIMA", "OTK"])
        gps.append(["551659", "WE3H229", "2025-11-24 00:00:00", 1, 0, 0, 0, 0])
        gps.append(["999998", "WN5412R", "2025-11-24 00:00:00", 1, 0, 0, 0, 0])

        log = wb.create_sheet("LOG")
        log.append(["ID", "Nr rejestracyjny", "Data przydziału", "Nazwa Pliku csv"])
        log.append(["1", " wx 12345 ", "01.05.2026", "a.csv"])
        log.append(["", "", "", ""])
        log.append(["2", "WX 99999", 45000, "b.csv"])

        priv = wb.create_sheet("Status_Prywatnosci")
        priv.append(["ID", "Data przydziału", "PRYW stary", "PRYW aktualny"])
        priv.append(["3", "2026-05-03", "old", "new"])

        wb.save(path)
        wb.close()
        return path.read_bytes()


def test_stage1_accepts_xlsm_and_normalizes_log_sheet() -> None:
    """Single-sheet legacy fixture: all rows still reach the canonical CSV."""

    assert ".xlsm" in fetch_reports.ALLOWED_EXTENSIONS
    assert fetch_reports._extension_from_content_type(
        "application/vnd.ms-excel.sheet.macroenabled.12"
    ) == ".xlsm"
    assert fetch_reports.DEFAULT_SENDER_FILTERS == ()

    raw = _sample_xlsm_bytes()
    with tempfile.TemporaryDirectory() as tmpdir:
        out = Path(tmpdir) / "normalized.csv"
        fetch_reports._convert_to_canonical_csv(raw, ".xlsm", out)
        rows = list(csv.reader(out.open(encoding="utf-8-sig"), delimiter=";"))
    flattened = "\n".join(";".join(row) for row in rows)
    assert "ID;Nr rejestracyjny;Data przydziału;RFID;PRYW;EDYS;OPTIMA;OTK" in flattened
    assert "ID;Nr rejestracyjny;Data przydziału;Nazwa Pliku csv" in flattened
    assert "ID;Data przydziału;PRYW stary;PRYW aktualny" in flattened
    print("PASS: Workflow B Stage 1 accepts .xlsm and normalizes every worksheet, preserving each header row")


def test_stage1_emits_all_sheets_for_multi_sheet_alpha_gps_workbook() -> None:
    """Multi-sheet workbook (real ALPHA00001 shape) is concatenated into one CSV."""

    raw = _real_alpha_gps_xlsm_bytes()
    with tempfile.TemporaryDirectory() as tmpdir:
        out = Path(tmpdir) / "normalized.csv"
        fetch_reports._convert_to_canonical_csv(raw, ".xlsm", out)
        rows = list(csv.reader(out.open(encoding="utf-8-sig"), delimiter=";"))
    flattened = "\n".join(";".join(row) for row in rows)
    assert "ID;Nr rejestracyjny;Data przydziału;RFID;PRYW;EDYS;OPTIMA;OTK" in flattened, (
        "GPS_baza_START detection header must reach normalized CSV"
    )
    assert "ID;Nr rejestracyjny;Data przydziału;Nazwa Pliku csv" in flattened, (
        "LOG output header must reach normalized CSV"
    )
    assert "ID;Data przydziału;PRYW stary;PRYW aktualny" in flattened, (
        "Status_Prywatnosci stop header must reach normalized CSV"
    )
    detection_idx = flattened.find("ID;Nr rejestracyjny;Data przydziału;RFID")
    log_idx = flattened.find("ID;Nr rejestracyjny;Data przydziału;Nazwa Pliku csv")
    stop_idx = flattened.find("ID;Data przydziału;PRYW stary;PRYW aktualny")
    assert detection_idx < log_idx < stop_idx, "sheet order must be preserved"
    print("PASS: Workflow B Stage 1 emits all sheets of a multi-sheet Alpha GPS XLSM into one canonical CSV")


def test_stage2_detects_and_cleans_alpha_gps_log() -> None:
    assert AlphaGPSBazaLog in REGISTERED_REPORTS
    assert "Alpha_GPS_Baza_LOG" not in Path(job_stage2.__file__).read_text()

    df = pd.DataFrame(
        [
            ["intro", "", "", "", "", "", "", ""],
            ["ID", "Nr rejestracyjny", "Data przydziału", "RFID", "PRYW", "EDYS", "OPTIMA", "OTK"],
            ["noise", "", "", "", "", "", "", ""],
            ["ID", "Nr rejestracyjny", "Data przydziału", "Nazwa Pliku csv", "", "", "", ""],
            ["1", " wx 12345 ", "01.05.2026", "a.csv", "", "", "", ""],
            ["", "", "", "", "", "", "", ""],
            ["2", "WX 99999", "2026-05-02", "b.csv", "", "", "", ""],
            ["ID", "Data przydziału", "PRYW stary", "PRYW aktualny", "", "", "", ""],
            ["3", "2026-05-03", "old", "new", "", "", "", ""],
        ]
    )
    detection = detect_report_type([df])
    assert detection.report_type == "Alpha_GPS_Baza_LOG"
    assert detection.pending_reason is None

    cleaned = AlphaGPSBazaLog.clean([df])
    assert cleaned.report_type == "Alpha_GPS_Baza_LOG"
    assert list(cleaned.df.columns) == [
        "ID",
        "Nr rejestracyjny",
        "Data przydziału",
        "Nazwa Pliku csv",
    ]
    assert cleaned.df.to_dict("records") == [
        {
            "ID": "1",
            "Nr rejestracyjny": "wx 12345",
            "Data przydziału": "2026-05-01",
            "Nazwa Pliku csv": "a.csv",
        },
        {
            "ID": "2",
            "Nr rejestracyjny": "WX 99999",
            "Data przydziału": "2026-05-02",
            "Nazwa Pliku csv": "b.csv",
        },
    ]
    assert cleaned.metadata["header_row_number"] == 4
    assert cleaned.metadata["stop_row_number"] == 8
    print("PASS: Stage 2 detects through the registry, starts at LOG output header, stops before PRYW delta header, and trims output")


def test_stage1_to_stage2_pipeline_detects_real_xlsm_shape() -> None:
    """End-to-end smoke: a real-shape XLSM normalizes and detects as Alpha_GPS_Baza_LOG."""

    from jobs.reports.stage2.io import read_csv_loose, split_into_tables  # noqa: E402

    raw = _real_alpha_gps_xlsm_bytes()
    with tempfile.TemporaryDirectory() as tmpdir:
        out = Path(tmpdir) / "normalized.csv"
        fetch_reports._convert_to_canonical_csv(raw, ".xlsm", out)
        raw_df = read_csv_loose(str(out))
        tables = split_into_tables(raw_df)
        detection = detect_report_type(tables)
        assert detection.report_type == "Alpha_GPS_Baza_LOG", detection
        assert detection.detect_score == 1.0
        assert detection.pending_reason is None
        cleaned = AlphaGPSBazaLog.clean(tables[0])
        assert list(cleaned.df.columns) == [
            "ID",
            "Nr rejestracyjny",
            "Data przydziału",
            "Nazwa Pliku csv",
        ]
        assert len(cleaned.df) >= 2
        assert cleaned.metadata["stop_row_number"] is not None
    print("PASS: real-shape multi-sheet XLSM round-trips through Stage 1 + Stage 2 detection and cleaning")


def test_stage2_invalid_assignment_date_fails_fast() -> None:
    df = pd.DataFrame(
        [
            ["ID", "Nr rejestracyjny", "Data przydziału", "RFID", "PRYW", "EDYS", "OPTIMA", "OTK"],
            ["ID", "Nr rejestracyjny", "Data przydziału", "Nazwa Pliku csv", "", "", "", ""],
            ["1", "WX 12345", "not-a-date", "a.csv", "", "", "", ""],
        ]
    )
    try:
        AlphaGPSBazaLog.clean([df])
    except ValueError as exc:
        assert "Invalid Data przydziału" in str(exc)
    else:
        raise AssertionError("expected invalid Alpha GPS assignment date to fail")
    print("PASS: Stage 2 fail-fast validation rejects invalid Data przydziału")


def test_stage3_alpha_target_and_replace_all_transaction() -> None:
    assert job_stage3._destination_for_report_type("Alpha_GPS_Baza_LOG") == (
        "telematics_reports",
        "Alpha_GPS_Baza_LOG",
    )
    assert job_stage3._load_data_overwrite_policy(
        type("Cursor", (), {"execute": lambda *_args, **_kwargs: None, "fetchone": lambda *_args: None})(),
        client_code="ALPHA00001",
        report_type="Alpha_GPS_Baza_LOG",
    ) == (True, True)

    df = FakeDataFrame(
        [
            {
                "ID": "1",
                "Nr rejestracyjny": " wx 12345 ",
                "Data przydziału": "2026-05-01",
                "Nazwa Pliku csv": "a.csv",
            }
        ]
    )
    conn = FakeDestinationConn()
    replaced = []

    def fake_replace(_cur, schema, table, rows, **kwargs):
        replaced.append((schema, table, rows, kwargs))

    with PatchAttrs(
        _inspect_destination=lambda *_args, **_kwargs: {
            "destination_schema_exists": True,
            "destination_table_exists": True,
            "existing_columns": {},
            "unique_index_exists": False,
        },
        _replace_alpha_gps_rows=fake_replace,
    ):
        result = job_stage3._load_dataframe_to_destination(
            conn,
            raw_file_id="11111111-1111-1111-1111-111111111111",
            run_id="22222222-2222-2222-2222-222222222222",
            client_code="ALPHA00001",
            report_type="Alpha_GPS_Baza_LOG",
            source_artifact_id="33333333-3333-3333-3333-333333333333",
            source_filename="clean.csv",
            data_overwrite=False,
            df=df,
            source_sha256="sha",
            raw_artifact_id="44444444-4444-4444-4444-444444444444",
            normalized_artifact_id="55555555-5555-5555-5555-555555555555",
            cleaned_artifact_id="33333333-3333-3333-3333-333333333333",
        )
    assert result.destination_schema == "telematics_reports"
    assert result.destination_table == "Alpha_GPS_Baza_LOG"
    assert result.inserted_rows == 1
    assert result.data_overwrite is True
    assert conn.commits == 1
    assert replaced[0][0:2] == ("telematics_reports", "Alpha_GPS_Baza_LOG")
    assert replaced[0][2][0]["registration"] == "WX 12345"
    assert replaced[0][3]["source_sha256"] == "sha"
    print("PASS: Stage 3 maps Alpha GPS rows and commits replace-all load for the exact target table")


def test_stage3_alpha_failure_rolls_back_previous_contents() -> None:
    df = FakeDataFrame(
        [
            {
                "ID": "1",
                "Nr rejestracyjny": "WX 12345",
                "Data przydziału": "2026-05-01",
                "Nazwa Pliku csv": "a.csv",
            }
        ]
    )
    conn = FakeDestinationConn()

    def fail_replace(*_args, **_kwargs):
        raise RuntimeError("insert failed")

    with PatchAttrs(
        _inspect_destination=lambda *_args, **_kwargs: {
            "destination_schema_exists": True,
            "destination_table_exists": True,
            "existing_columns": {},
            "unique_index_exists": False,
        },
        _replace_alpha_gps_rows=fail_replace,
    ):
        try:
            job_stage3._load_dataframe_to_destination(
                conn,
                raw_file_id="11111111-1111-1111-1111-111111111111",
                run_id="22222222-2222-2222-2222-222222222222",
                client_code="ALPHA00001",
                report_type="Alpha_GPS_Baza_LOG",
                source_artifact_id="33333333-3333-3333-3333-333333333333",
                source_filename="clean.csv",
                data_overwrite=True,
                df=df,
            )
        except RuntimeError as exc:
            assert "insert failed" in str(exc)
        else:
            raise AssertionError("expected Alpha GPS replace-all load failure")
    assert conn.rollbacks == 1
    assert conn.commits == 0
    print("PASS: failed Alpha GPS Stage 3 load rolls back the destination transaction")


def test_stage2_cleaned_artifact_metadata_is_discoverable_by_stage3() -> None:
    source = Path(job_stage2.__file__).read_text(encoding="utf-8")
    assert '"workflow_name": "workflow_b"' in source
    assert '"stage_name": "stage_2_clean"' in source
    assert '"artifact_role": "cleaned"' in source
    assert '"raw_file_id": raw_file_id' in source
    assert '"report_type": detection.report_type' in source
    assert '"client_code": resolved_client_code' in source

    artifacts = [
        {
            "artifact_id": "art-cleaned",
            "raw_file_id": "raw-1",
            "workflow_name": "workflow_b",
            "stage_name": "stage_2_clean",
            "artifact_role": "cleaned",
            "artifact_kind": "REPORT",
            "report_type": "alpha_gps_baza_log",
            "client_code": "ALPHA00001",
            "storage_key": "workflow_b/stage_2_clean/report_type=alpha_gps_baza_log/cleaned/file.csv",
            "filename": "file.csv",
            "display_filename": "file.csv",
        }
    ]
    analysis = diagnose_workflow_b_file_lineage.analyze_stage3_lookup(
        artifacts,
        raw_file_id="raw-1",
        report_type="Alpha_GPS_Baza_LOG",
        client_code="ALPHA00001",
    )
    assert analysis["found"] is True
    assert analysis["matching_artifact_ids"] == ["art-cleaned"]
    assert analysis["filters"]["report_type_any"] == [
        "Alpha_GPS_Baza_LOG",
        "alpha_gps_baza_log",
    ]
    print("PASS: Stage 2 cleaned artifact metadata is discoverable by Stage 3 despite API report_type sanitization")


def test_stage3_missing_artifact_error_includes_lookup_filters() -> None:
    class Cursor:
        def __init__(self):
            self.executed = []

        def execute(self, query, params=None):
            self.executed.append((str(query), params))

        def fetchone(self):
            return None

        def fetchall(self):
            return [
                {
                    "stage_name": "stage_2_clean",
                    "artifact_role": "debug_sample",
                    "artifact_kind": "REPORT",
                    "report_type": "alpha_gps_baza_log",
                    "client_code": "ALPHA00001",
                    "count": 1,
                    "latest_created_at": None,
                }
            ]

    try:
        job_stage3._find_stage2_cleaned_artifact(
            Cursor(),
            raw_file_id="raw-1",
            report_type="Alpha_GPS_Baza_LOG",
        )
    except RuntimeError as exc:
        message = str(exc)
        assert "artifact_lookup_filters=" in message
        assert "artifact_rows_for_raw_file=" in message
        assert '"artifact_role": "cleaned"' in message
        assert "debug_sample" in message
    else:
        raise AssertionError("expected missing cleaned artifact lookup to fail")
    print("PASS: missing cleaned artifact errors include Stage 3 lookup filters and grouped artifact rows")


def test_diagnostic_script_reports_lookup_failure_reasons() -> None:
    artifacts = [
        {
            "artifact_id": "art-normalized",
            "raw_file_id": "raw-1",
            "workflow_name": "workflow_b",
            "stage_name": "stage_1_fetch",
            "artifact_role": "normalized",
            "artifact_kind": "REPORT",
            "report_type": "unknown",
            "client_code": None,
        }
    ]
    analysis = diagnose_workflow_b_file_lineage.analyze_stage3_lookup(
        artifacts,
        raw_file_id="raw-1",
        report_type="Alpha_GPS_Baza_LOG",
        client_code="ALPHA00001",
    )
    assert analysis["found"] is False
    assert "wrong or missing stage_name" in analysis["failure_reasons"]
    assert "wrong or missing artifact_role" in analysis["failure_reasons"]
    assert "wrong or missing report_type" in analysis["failure_reasons"]
    assert "wrong or missing client_code" in analysis["failure_reasons"]
    print("PASS: diagnostic lineage helper explains why Stage 3 lookup would not find a cleaned artifact")


def test_stage2_docs_use_persisted_identity_for_path_params() -> None:
    source = Path(job_stage2.__file__).read_text(encoding="utf-8")
    docs = (REPO_ROOT / "docs/05_jobs.md").read_text(encoding="utf-8")
    assert "input_files mode does not prepare Stage 3" in source
    assert "REPORTS_DATA_DIR" in docs and "normalized" in docs
    assert "input_files" in docs and "input_dir" in docs
    assert "raw_file_id" in docs
    print("PASS: Stage 2 docs retain path params but require persisted raw-file identity")


def test_migrations_and_deprecated_direct_job_are_present() -> None:
    platform_sql = (REPO_ROOT / "db/migrations/030_workflow_b_alpha_gps_baza_log_registry.sql").read_text()
    ensure_sql = (REPO_ROOT / "db/migrations/031_workflow_b_ensure_alpha_gps_baza_log_registry.sql").read_text()
    client_sql = (REPO_ROOT / "db/client_business/023_alpha_gps_baza_log_workflow_b.sql").read_text()
    assert "Alpha_GPS_Baza_LOG" in platform_sql
    assert "data_overwrite" in platform_sql
    assert "INSERT INTO workflow_b_control.report_type_registry" in ensure_sql
    assert "ON CONFLICT (report_type)" in ensure_sql
    assert "enabled" in ensure_sql
    assert "'implemented'" in ensure_sql
    assert '"required_header_labels"' in ensure_sql
    assert '"ID"' in ensure_sql
    assert '"Nr rejestracyjny"' in ensure_sql
    assert '"Data przydziału"' in ensure_sql
    assert '"RFID"' in ensure_sql
    assert '"PRYW"' in ensure_sql
    assert '"EDYS"' in ensure_sql
    assert '"OPTIMA"' in ensure_sql
    assert '"OTK"' in ensure_sql
    assert '"output_header_labels"' in ensure_sql
    assert '"Nazwa Pliku csv"' in ensure_sql
    assert '"stop_before_header_labels"' in ensure_sql
    assert '"PRYW stary"' in ensure_sql
    assert '"PRYW aktualny"' in ensure_sql
    assert '"target_client_code": "ALPHA00001"' in ensure_sql
    assert '"target_database": "alpha_main"' in ensure_sql
    assert '"target_schema": "telematics_reports"' in ensure_sql
    assert '"target_table": "Alpha_GPS_Baza_LOG"' in ensure_sql
    assert "INSERT INTO workflow_b_control.report_type_client_load_policy" in ensure_sql
    assert "'ALPHA00001'" in ensure_sql
    assert "true" in ensure_sql
    assert 'telematics_reports."Alpha_GPS_Baza_LOG"' in client_sql
    assert 'telematics_reports."Alpha_GPS_Baza_LOG"' not in client_sql
    assert import_gps_baza_log_xlsm.DEPRECATED is True
    print("PASS: migrations register/repair Alpha_GPS_Baza_LOG and old source_path job is clearly deprecated")


def main() -> None:
    test_stage1_accepts_xlsm_and_normalizes_log_sheet()
    test_stage1_emits_all_sheets_for_multi_sheet_alpha_gps_workbook()
    test_stage1_to_stage2_pipeline_detects_real_xlsm_shape()
    test_stage2_detects_and_cleans_alpha_gps_log()
    test_stage2_invalid_assignment_date_fails_fast()
    test_stage3_alpha_target_and_replace_all_transaction()
    test_stage3_alpha_failure_rolls_back_previous_contents()
    test_stage2_cleaned_artifact_metadata_is_discoverable_by_stage3()
    test_stage3_missing_artifact_error_includes_lookup_filters()
    test_diagnostic_script_reports_lookup_failure_reasons()
    test_stage2_docs_use_persisted_identity_for_path_params()
    test_migrations_and_deprecated_direct_job_are_present()


if __name__ == "__main__":
    main()
