#!/usr/bin/env python3
"""Manual regression tests for Workflow B Stage 2 report_207 support.

Run:

    cd /opt/log-platform
    python3 ops/tests_manual/test_stage2_report_207.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.reports.stage2 import job_stage2  # noqa: E402
from jobs.reports.stage2.detector import DETECT_THRESHOLD, detect_report_type  # noqa: E402
from jobs.reports.stage2.registry import REGISTERED_REPORTS, get_report_cls  # noqa: E402
from jobs.reports.stage2.types.report_207 import Report207  # noqa: E402
from jobs.reports.stage2.validation import validate  # noqa: E402


EXPECTED_COLUMNS = [
    "Data i czas",
    "Nr rejestracyjny",
    "Prędkość",
    "Ograniczenie prędkości drogowej",
    "Lokalizacja",
]


def _df(rows: list[list[str]]) -> pd.DataFrame:
    return pd.DataFrame(rows, dtype=str)


class FakeClient:
    def __init__(self) -> None:
        self.logs = []

    def log(self, *args, **kwargs):
        self.logs.append((args, kwargs))


def _valid_report_rows(*, include_title: bool = True) -> list[list[str]]:
    rows: list[list[str]] = []
    if include_title:
        rows.append([" 207   Raport przekroczeń limitów prędkości drogowej ", "", "", "", ""])
        rows.append(["", "", "", "", ""])
    rows.extend(
        [
            ["Data i czas", "Nr rejestracyjny", "Prędkość", "Ograniczenie prędkości drogowej", "Lokalizacja"],
            ["2026-05-12 08:30:00", "WR12345", "91", "50", "Warszawa, ul. Prosta 1"],
            ["2026-05-12 08:31:00", "", "88", "50", "empty registration"],
            ["2026-05-12 08:32:00", "   ", "88", "50", "whitespace registration"],
            ["Data i czas", "Nr rejestracyjny", "Prędkość", "Ograniczenie prędkości drogowej", "Lokalizacja"],
            ["2026-05-12 08:33:00", "WR67890", "110", "70", "Łódź, Piotrkowska 1"],
        ]
    )
    return rows


def _test_detection_positive() -> None:
    detected = detect_report_type([_df(_valid_report_rows(include_title=True))])
    assert detected.report_type == "report_207", detected
    assert detected.detect_score >= DETECT_THRESHOLD, detected
    assert detected.detect_score == 1.0, detected
    print("PASS: report_207 title + header detects above threshold")


def _test_detection_header_only() -> None:
    detected = detect_report_type([_df(_valid_report_rows(include_title=False))])
    assert detected.report_type == "report_207", detected
    assert detected.detect_score >= DETECT_THRESHOLD, detected
    assert detected.detect_score == 0.7, detected
    print("PASS: report_207 header-only detects at threshold")


def _test_detection_title_only_low_confidence() -> None:
    df = _df([
        ["207 Raport przekroczeń limitów prędkości drogowej", "", "", "", ""],
        ["not", "the", "expected", "header", "row"],
    ])
    direct_score = Report207.detect([df])
    detected = detect_report_type([df])
    assert direct_score == 0.3, direct_score
    assert detected.detect_score < DETECT_THRESHOLD, detected
    assert detected.pending_reason == "low_detection_confidence", detected
    print("PASS: report_207 title-only stays below threshold")


def _test_cleaning() -> None:
    cleaned = Report207.clean(_df(_valid_report_rows(include_title=True)))
    assert cleaned.df.columns.tolist() == EXPECTED_COLUMNS, cleaned.df.columns.tolist()
    assert len(cleaned.df) == 2, cleaned.df
    assert cleaned.df["Nr rejestracyjny"].tolist() == ["WR12345", "WR67890"], cleaned.df

    validation = validate(cleaned, Report207)
    assert validation.is_valid, validation.schema_diff
    assert validation.schema_diff["missing_required"] == [], validation.schema_diff
    assert validation.schema_diff["extra_columns"] == [], validation.schema_diff
    print("PASS: report_207 cleaner removes blank registrations and repeated headers")

def _test_excel_serial_datetime_validation() -> None:
    rows = [
        ["207 Raport przekroczeń limitów prędkości drogowej", "", "", "", ""],
        ["Data i czas", "Nr rejestracyjny", "Prędkość", "Ograniczenie prędkości drogowej", "Lokalizacja"],
        ["46164.509224537", "WD2467V", "54", "50", "ulica Jesienna 122"],
    ]
    cleaned = Report207.clean(_df(rows))
    validation = validate(cleaned, Report207)

    assert validation.is_valid, validation.schema_diff
    stats = validation.schema_diff["type_parse_stats"]["Data i czas"]
    assert stats == {"non_null": 1, "fails": 0, "fail_rate": 0.0}, validation.schema_diff
    assert "Data i czas" not in validation.schema_diff.get("type_parse_blocking_cols", []), validation.schema_diff
    print("PASS: report_207 accepts Excel serial datetime in Data i czas")


def _test_invalid_datetime_data_still_blocks() -> None:
    rows = [
        ["207 Raport przekroczeń limitów prędkości drogowej", "", "", "", ""],
        ["Data i czas", "Nr rejestracyjny", "Prędkość", "Ograniczenie prędkości drogowej", "Lokalizacja"],
    ]
    rows.extend([
        ["not-a-date", f"WD{i:04d}", "54", "50", "ulica Jesienna 122"]
        for i in range(12)
    ])
    cleaned = Report207.clean(_df(rows))
    validation = validate(cleaned, Report207)

    assert not validation.is_valid, validation.schema_diff
    assert "Data i czas" in validation.schema_diff.get("type_parse_blocking_cols", []), validation.schema_diff
    assert any(error.startswith("type_parse_blocking:Data i czas") for error in validation.errors), validation.errors
    print("PASS: report_207 invalid datetime values still block")


def _test_column_count_mismatch() -> None:
    df = _df([
        ["207 Raport przekroczeń limitów prędkości drogowej", "", "", "", "", ""],
        [
            "Data i czas",
            "Nr rejestracyjny",
            "Prędkość",
            "Ograniczenie prędkości drogowej",
            "Lokalizacja",
            "Extra",
        ],
        ["2026-05-12 08:30:00", "WR12345", "91", "50", "Warszawa", "unexpected"],
    ])
    assert Report207.detect([df]) >= DETECT_THRESHOLD
    try:
        Report207.clean(df)
    except ValueError as exc:
        message = str(exc)
        assert "report_type=report_207" in message, message
        assert "expected_column_count=5" in message, message
        assert "actual_column_count=6" in message, message
    else:
        raise AssertionError("Expected report_207 column-count mismatch error")
    print("PASS: report_207 cleaner raises clear column-count mismatch")


def _test_registry_parity() -> None:
    registered_types = {cls.TYPE for cls in REGISTERED_REPORTS}
    assert "report_207" in registered_types, sorted(registered_types)
    assert get_report_cls("report_207") is Report207
    print("PASS: Python registry includes report_207")


def _test_normalized_layout_like_stage1_export() -> None:
    """Rows shaped like normalized Stage 1 CSV: preamble, title row, meta, then header + data."""
    rows = [
        ["", "", "", "", ""],
        ["", "", "", "", ""],
        ["207 Raport przekroczeń limitów prędkości drogowej", "", "", "", ""],
        ["Data początek:", "", "2026-05-04 00:00:00+0200", "", ""],
        ["Nr rejestracyjny:", "", "WD2467V", "", ""],
        [
            "Data i czas",
            "Nr rejestracyjny",
            "Prędkość",
            "Ograniczenie prędkości drogowej",
            "Lokalizacja",
        ],
        ["2026-05-04 07:24:03", "WD2467V", "54", "50", "ulica Jesienna 122"],
        ["2026-05-04 07:24:07", "WD2467V", "53", "50", "Częstochowa"],
    ]
    df = _df(rows)
    detected = detect_report_type([df])
    assert detected.report_type == "report_207", detected
    assert detected.detect_score >= DETECT_THRESHOLD, detected
    cleaned = Report207.clean(df)
    assert cleaned.df.columns.tolist() == EXPECTED_COLUMNS
    assert len(cleaned.df) == 2, cleaned.df
    assert cleaned.df["Nr rejestracyjny"].tolist() == ["WD2467V", "WD2467V"]
    print("PASS: report_207 handles Stage-1-like normalized preamble + table")


def _test_report_207_record_id_business_key_and_duplicate_ordinal() -> None:
    rows = [
        ["01.06.2026 10:00", "WX12345", "90", "50", "Location A"],
        ["01.06.2026 10:00", "WX12345", "91", "50", "Location A"],
        ["01.06.2026 10:00", "WX12345", "90", "60", "Location A"],
        ["01.06.2026 10:00", "WX12345", "90", "50", "Location B"],
        ["01.06.2026 11:00", "WX99999", "80", "50", "Location C"],
        ["01.06.2026 11:00", "WX99999", "80", "50", "Location C"],
    ]
    cleaned = pd.DataFrame(rows, columns=EXPECTED_COLUMNS, dtype=str)
    first = job_stage2._add_record_id_column(
        cleaned,
        record_id_ingredients="Data i czas,Nr rejestracyjny",
        client=FakeClient(),
        run_id="run-1",
        context={"report_type": "report_207"},
    )
    second = job_stage2._add_record_id_column(
        cleaned,
        record_id_ingredients="Data i czas,Nr rejestracyjny",
        client=FakeClient(),
        run_id="run-2",
        context={"report_type": "report_207"},
    )

    record_ids = first["record_id"].tolist()
    assert len(set(record_ids[:4])) == 4, record_ids
    assert record_ids[0] != record_ids[1], "different speed must change record_id"
    assert record_ids[0] != record_ids[2], "different speed limit must change record_id"
    assert record_ids[0] != record_ids[3], "different location must change record_id"
    assert record_ids[4] != record_ids[5], "identical visible rows need duplicate ordinals"
    assert record_ids == second["record_id"].tolist(), "same ordered source must be stable"
    assert first.columns.tolist() == EXPECTED_COLUMNS + ["record_id"], first.columns.tolist()
    print("PASS: report_207 record_id uses full business key plus stable duplicate ordinal")


def _test_non_report_207_record_id_still_uses_configured_ingredients() -> None:
    cleaned = pd.DataFrame(
        [
            {"A": "same", "B": "one"},
            {"A": "same", "B": "two"},
            {"A": "other", "B": "two"},
        ],
        dtype=str,
    )
    finalized = job_stage2._add_record_id_column(
        cleaned,
        record_id_ingredients="A",
        client=FakeClient(),
        run_id="run-1",
        context={"report_type": "other_report"},
    )
    record_ids = finalized["record_id"].tolist()
    assert record_ids[0] == record_ids[1], record_ids
    assert record_ids[0] != record_ids[2], record_ids
    print("PASS: non-report_207 record_id still follows configured record_id_ingredients")


def main() -> None:
    _test_detection_positive()
    _test_detection_header_only()
    _test_detection_title_only_low_confidence()
    _test_cleaning()
    _test_excel_serial_datetime_validation()
    _test_invalid_datetime_data_still_blocks()
    _test_column_count_mismatch()
    _test_registry_parity()
    _test_normalized_layout_like_stage1_export()
    _test_report_207_record_id_business_key_and_duplicate_ordinal()
    _test_non_report_207_record_id_still_uses_configured_ingredients()


if __name__ == "__main__":
    main()
