#!/usr/bin/env python3
"""Focused checks for Eco Driving Person physical-person identity."""

from __future__ import annotations

from pathlib import Path
import csv
import io
import sys
import tempfile

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.ecodriving_person.normalization import (  # noqa: E402
    canonicalize_person_name,
    normalize_person_source_identity,
    person_name_group_key,
)
from jobs.ecodriving_person.physical_person_import import (  # noqa: E402
    EXPECTED_COLUMNS,
    inspect_and_validate_csv,
)

CLIENT_ID = "6018be20-5faa-41b6-89c9-fe2b54a8283e"


def _write_csv(rows: list[dict[str, str]], *, encoding: str = "cp1250") -> Path:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=EXPECTED_COLUMNS, delimiter=";")
    writer.writeheader()
    writer.writerows(rows)
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=".csv")
    handle.write(output.getvalue().encode(encoding))
    handle.close()
    return Path(handle.name)


def _row(**overrides: str) -> dict[str, str]:
    row = {
        "client_id": CLIENT_ID,
        "person_id": "Driver 1-business",
        "person_name": "Żaneta Żółta",
        "email": "zaneta@example.test",
        "ranking_included": "true",
        "is_active": "true",
        "metadata_json": "",
        "created_at": "",
        "updated_at": "",
    }
    row.update(overrides)
    return row


def test_source_identity_normalization() -> None:
    expected = "driver1business"
    for value in (
        "Driver 1-business",
        " driver_1 business ",
        "DRIVER.1/BUSINESS",
        "Driver\t1\nbusiness",
    ):
        assert normalize_person_source_identity(value) == expected

    assert normalize_person_source_identity("Lukasz") != normalize_person_source_identity("Łukasz")
    assert normalize_person_source_identity("Zolty") != normalize_person_source_identity("Żółty")
    assert normalize_person_source_identity("Kierowca 123") == "kierowca123"
    assert normalize_person_source_identity("ŻÓŁTY") == "żółty"
    assert normalize_person_source_identity(" !\t— ") is None


def test_physical_person_grouping() -> None:
    assert canonicalize_person_name("  Anna\tOszmiańczuk\n") == "Anna Oszmiańczuk"
    assert person_name_group_key(" JAN   Kowalski ") == "jan kowalski"
    assert person_name_group_key("Jan Kowalski") == person_name_group_key("jan  kowalski")
    assert person_name_group_key("Lukasz") != person_name_group_key("Łukasz")
    assert person_name_group_key("Żółty") != person_name_group_key("Zolty")
    assert canonicalize_person_name(" \t ") is None


def test_cp1250_import_and_exact_duplicate_handling() -> None:
    source = _row()
    path = _write_csv([source, source, _row(person_id="Driver 1-private")])
    rows, report = inspect_and_validate_csv(path, expected_client_id=CLIENT_ID)
    assert report["encoding"] == "cp1250"
    assert report["delimiter"] == ";"
    assert report["raw_rows"] == 3
    assert report["duplicate_input_rows"] == 1
    assert report["duplicate_source_row_numbers"] == [
        {"retained_row": 2, "duplicate_row": 3}
    ]
    assert report["logical_rows"] == 2
    assert report["physical_person_groups"] == 1
    assert not report["blocking_errors"]
    assert rows[0]["metadata_json"] == {}
    path.unlink()


def test_explicit_utf8_bom_compatibility() -> None:
    path = _write_csv([_row()], encoding="utf-8-sig")
    rows, report = inspect_and_validate_csv(
        path,
        expected_client_id=CLIENT_ID,
        encoding="utf-8-sig",
    )
    assert len(rows) == 1
    assert report["encoding"] == "utf-8-sig"
    assert not report["blocking_errors"]
    path.unlink()


def test_import_validation_blocks_collisions_and_group_conflicts() -> None:
    rows = [
        _row(person_id="Driver 1-business"),
        _row(person_id=" driver_1 business ", email="other@example.test"),
        _row(
            person_id="",
            person_name="",
            email="invalid",
            metadata_json="{",
            ranking_included="maybe",
            is_active="unknown",
        ),
        _row(
            person_id="Driver 2",
            person_name="Żaneta  Żółta",
            ranking_included="false",
            is_active="false",
        ),
    ]
    path = _write_csv(rows)
    _prepared, report = inspect_and_validate_csv(path, expected_client_id=CLIENT_ID)
    codes = {
        error["code"]
        for error in report["blocking_errors"]
        if "code" in error
    }
    row_codes = {
        code
        for error in report["blocking_errors"]
        for code in error.get("codes", [])
    }
    assert "NORMALIZED_SOURCE_IDENTITY_COLLISION" in codes
    assert "PHYSICAL_PERSON_GROUP_CONFLICT" in codes
    assert {
        "blank_person_id",
        "blank_person_name",
        "invalid_email",
        "invalid_metadata_json",
        "invalid_ranking_included",
        "invalid_is_active",
    } <= row_codes
    assert report["group_conflicts"][0]["fields"] == [
        "email",
        "ranking_included",
        "is_active",
    ]
    path.unlink()


def main() -> None:
    test_source_identity_normalization()
    test_physical_person_grouping()
    test_cp1250_import_and_exact_duplicate_handling()
    test_explicit_utf8_bom_compatibility()
    test_import_validation_blocks_collisions_and_group_conflicts()
    print("OK - Eco Person physical identity normalization checks passed")


if __name__ == "__main__":
    main()
