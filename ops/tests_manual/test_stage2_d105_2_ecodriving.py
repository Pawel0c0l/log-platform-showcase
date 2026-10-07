#!/usr/bin/env python3
"""Manual regressions for the special D105.2 EcoDriving Stage 2 report type.

Run:

    cd /opt/log-platform
    PYTHONPATH="$PWD" python3 ops/tests_manual/test_stage2_d105_2_ecodriving.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.reports.stage2.detector import DETECT_THRESHOLD, detect_report_type  # noqa: E402
from jobs.reports.stage2.registry import REGISTERED_REPORTS, get_report_cls  # noqa: E402
from jobs.reports.stage2.types.d105_2 import D1052Report  # noqa: E402
from jobs.reports.stage2.types.d105_2_ecodriving import D1052EcoDrivingReport  # noqa: E402
from jobs.reports.stage2.validation import validate  # noqa: E402


EXPECTED_REQUIRED = [
    "Nr Rejestracyjny",
    "Czas rozpoczęcia",
    "Czas zakończenia",
    "przekroczenia obr/min",
    "> 140kmh",
    "> 160kmh",
    "> 170kmh",
]


def _df(rows: list[list[str]]) -> pd.DataFrame:
    return pd.DataFrame(rows, dtype=str)


def _rows(include_title: bool = False, filename_hint: str | None = None) -> list[list[str]]:
    rows: list[list[str]] = []
    if include_title:
        rows.append(["Some title that may change", "", "", "", "", "", "", ""])
        rows.append([filename_hint or "not used", "", "", "", "", "", "", ""])
    rows.extend(
        [
            [
                "Nr Rejestracyjny",
                "Czas rozpoczęcia",
                "Czas zakończenia",
                "przekroczenia obr/min",
                "> 140kmh",
                "> 160kmh",
                "> 170kmh",
                "Dodatkowa kolumna",
            ],
            [" WD12345 ", "2026-05-12 08:30", "2026-05-12 09:00:00", "2", "3", "4", "5", "kept"],
            ["", "2026-05-12 09:00", "2026-05-12 10:00", "0", "0", "0", "0", "missing reg"],
            ["WD99999", "bad-ts", "2026-05-12 10:00", "0", "0", "0", "0", "bad timestamp"],
            ["WD88888", "2026-05-12 10:00", "2026-05-12 11:00", "", "0", "0", "0", "blank metric"],
            ["", "", "", "", "", "", "", ""],
        ]
    )
    return rows


def test_detection_header_structure_only() -> None:
    detected = detect_report_type([_df(_rows(include_title=False))])
    assert detected.report_type == "report_d105_2_ecodriving", detected
    assert detected.detect_score >= DETECT_THRESHOLD, detected
    assert D1052EcoDrivingReport.detect([_df(_rows(include_title=False))]) == 1.0
    print("PASS: D105.2 EcoDriving detects from required header structure only")


def test_detection_ignores_title_and_filename() -> None:
    detected = detect_report_type([_df(_rows(include_title=True, filename_hint="random-name.xlsx"))])
    assert detected.report_type == "report_d105_2_ecodriving", detected
    assert detected.detect_score >= DETECT_THRESHOLD, detected
    print("PASS: D105.2 EcoDriving does not rely on title or filename text")


def test_detection_rejects_missing_required_metric_column() -> None:
    rows = _rows(include_title=False)
    header = rows[0]
    idx = header.index("> 160kmh")
    for row in rows:
        del row[idx]
    direct = D1052EcoDrivingReport.detect([_df(rows)])
    detected = detect_report_type([_df(rows)])
    assert direct == 0.0, direct
    assert detected.report_type != "report_d105_2_ecodriving" or detected.detect_score < DETECT_THRESHOLD, detected
    print("PASS: D105.2 EcoDriving rejects files missing a required metric/matching column")


def test_not_misclassified_as_generic_d105_2() -> None:
    detected = detect_report_type([_df(_rows(include_title=False))])
    candidates = {item["report_type"]: item["score"] for item in detected.candidates_top3}
    assert detected.report_type == "report_d105_2_ecodriving", detected
    assert candidates["report_d105_2_ecodriving"] > candidates.get("d105_2", -1), detected.candidates_top3
    assert REGISTERED_REPORTS.index(D1052EcoDrivingReport) < REGISTERED_REPORTS.index(D1052Report)
    print("PASS: D105.2 EcoDriving is more specific than generic d105_2 in detection")


def test_cleaning_preserves_required_and_extra_columns() -> None:
    cleaned = D1052EcoDrivingReport.clean(_df(_rows(include_title=False)))
    assert cleaned.df.columns.tolist() == EXPECTED_REQUIRED + ["Dodatkowa kolumna"], cleaned.df.columns.tolist()
    assert cleaned.df.loc[0, "Nr Rejestracyjny"] == "WD12345"
    assert cleaned.df.loc[0, "Czas rozpoczęcia"] == "2026-05-12 08:30:00"
    assert cleaned.df.loc[0, "Czas zakończenia"] == "2026-05-12 09:00:00"
    assert cleaned.df.loc[0, "przekroczenia obr/min"] == "2"
    assert cleaned.df.loc[0, "> 140kmh"] == "3"
    assert cleaned.df.loc[0, "> 160kmh"] == "4"
    assert cleaned.df.loc[0, "> 170kmh"] == "5"
    assert cleaned.metadata["invalid_registration_rows"] == 1, cleaned.metadata
    assert cleaned.metadata["invalid_timestamp_rows"] == 1, cleaned.metadata
    assert cleaned.metadata["invalid_metric_rows"] == 1, cleaned.metadata
    validation = validate(cleaned, D1052EcoDrivingReport)
    assert validation.schema_diff["missing_required"] == [], validation.schema_diff
    assert validation.schema_diff["type_parse_stats"]["Czas rozpoczęcia"]["fails"] == 1, validation.schema_diff
    print("PASS: D105.2 EcoDriving cleaner normalizes required columns and marks invalid rows")



def _split_timestamp_rows_with_whitespace() -> list[list[str]]:
    return [
        ["FleetWeb title that must not matter", "", "", "", "", "", "", "", "", "", ""],
        [
            "Nr Rejestracyjny",
            "Marka",
            "Model",
            "Dysponent ID",
            "Data rozpoczęcia",
            "Data ukończenia",
            " Czas rozpoczęcia",
            "Czas zakończenia ",
            " przekroczenia obr/min",
            " >140kmh",
            ">160kmh",
            " >170kmh",
        ],
        ["WD12345", "Ford", "Transit", "D1", "2026-05-12", "2026-05-12", "08:30", "09:05:00", "2", "3", "4", "5"],
        ["WD22222", "Ford", "Transit", "D1", "12.05.2026", "12.05.2026", "10:00", "10:45", "0", "0", "0", "0"],
    ]


def test_split_timestamp_header_whitespace_detection() -> None:
    df = _df(_split_timestamp_rows_with_whitespace())
    detected = detect_report_type([df])
    assert detected.report_type == "report_d105_2_ecodriving", detected
    assert detected.detect_score >= DETECT_THRESHOLD, detected
    assert D1052EcoDrivingReport.detect([df]) == 1.0
    assert D1052Report.detect([df]) < DETECT_THRESHOLD
    print("PASS: D105.2 EcoDriving detects split timestamp headers with leading whitespace")


def test_split_timestamp_cleaning_composes_datetimes() -> None:
    cleaned = D1052EcoDrivingReport.clean(_df(_split_timestamp_rows_with_whitespace()))
    assert cleaned.metadata["split_timestamp_columns"] is True, cleaned.metadata
    assert cleaned.df.loc[0, "Czas rozpoczęcia"] == "2026-05-12 08:30:00", cleaned.df.iloc[0].to_dict()
    assert cleaned.df.loc[0, "Czas zakończenia"] == "2026-05-12 09:05:00", cleaned.df.iloc[0].to_dict()
    assert cleaned.df.loc[1, "Czas rozpoczęcia"] == "2026-05-12 10:00:00", cleaned.df.iloc[1].to_dict()
    assert cleaned.df.loc[1, "Czas zakończenia"] == "2026-05-12 10:45:00", cleaned.df.iloc[1].to_dict()
    assert cleaned.df.loc[0, "przekroczenia obr/min"] == "2"
    assert cleaned.df.loc[0, "> 140kmh"] == "3"
    validation = validate(cleaned, D1052EcoDrivingReport)
    assert validation.schema_diff["missing_required"] == [], validation.schema_diff
    assert validation.schema_diff["type_parse_stats"]["Czas rozpoczęcia"]["fails"] == 0, validation.schema_diff
    print("PASS: D105.2 EcoDriving composes Data+Czas timestamps for migration columns")

def test_registry_parity() -> None:
    assert get_report_cls("report_d105_2_ecodriving") is D1052EcoDrivingReport
    assert D1052EcoDrivingReport in REGISTERED_REPORTS
    print("PASS: Python registry includes report_d105_2_ecodriving")


def main() -> None:
    test_detection_header_structure_only()
    test_detection_ignores_title_and_filename()
    test_detection_rejects_missing_required_metric_column()
    test_not_misclassified_as_generic_d105_2()
    test_cleaning_preserves_required_and_extra_columns()
    test_split_timestamp_header_whitespace_detection()
    test_split_timestamp_cleaning_composes_datetimes()
    test_registry_parity()
    print("OK - D105.2 EcoDriving Stage 2 regressions passed")


if __name__ == "__main__":
    main()
