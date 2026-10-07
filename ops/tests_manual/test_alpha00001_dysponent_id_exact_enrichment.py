#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.reports.postprocess import (  # noqa: E402
    job_alpha00001_dysponent_id_exact_enrichment as job,
)


def test_exact_parameters() -> None:
    request = job._parse_request(
        {
            "client_code": "ALPHA00001",
            "client_id": "client-1",
            "provider_trip_ids": [3, 1, 2],
        }
    )
    assert request.provider_trip_ids == (3, 1, 2)
    assert request.dry_run is True
    assert request.strict is True


def test_real_run_requires_expected_count() -> None:
    try:
        job._parse_request(
            {
                "client_code": "ALPHA00001",
                "provider_trip_ids": [1],
                "dry_run": False,
            }
        )
    except ValueError as exc:
        assert "expected_update_count" in str(exc)
    else:
        raise AssertionError("expected real run without expected_update_count to fail")


def test_provider_ids_are_fail_closed() -> None:
    for value in ([], [1, 1], [True], [0], ["bad"]):
        try:
            job._parse_request({"provider_trip_ids": value})
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected invalid provider_trip_ids to fail: {value!r}")


class UpdateCursor:
    def __init__(self, existing: set[int]) -> None:
        self.existing = existing
        self.current = None
        self.sql: list[str] = []

    def execute(self, sql, params) -> None:
        self.sql.append(sql)
        provider_trip_id = int(params[2])
        if provider_trip_id in self.existing:
            self.current = {"provider_trip_id": provider_trip_id}
            self.existing.remove(provider_trip_id)
        else:
            self.current = None

    def fetchone(self):
        return self.current


def test_updates_touch_only_dysponent_id_and_exact_keys() -> None:
    cursor = UpdateCursor({10, 20})
    updated = job._apply_exact_updates(
        cursor,
        client_id="client-1",
        proposals=[
            {"provider_trip_id": 10, "dysponent_id": "A"},
            {"provider_trip_id": 20, "dysponent_id": "B"},
        ],
    )
    sql = "\n".join(cursor.sql)
    assert updated == [10, 20]
    assert 'SET "Dysponent_ID" = %s' in sql
    assert "provider_trip_id = %s" in sql
    assert "speeding_" not in sql
    assert "high_rpm" not in sql
    assert "overrev" not in sql


def test_strict_default_is_fail_closed() -> None:
    request = job._parse_request({"provider_trip_ids": [1], "strict": False})
    assert request.strict is False
    try:
        job._parse_request({"provider_trip_ids": [1], "expected_update_count": -1})
    except ValueError as exc:
        assert "non-negative" in str(exc)
    else:
        raise AssertionError("expected negative expected_update_count to fail")



def test_strict_history_selection_rejects_competing_ids() -> None:
    rows = [
        {"source_id": "A"},
        {"source_id": "B"},
    ]
    selected, distinct_ids, error = job._select_history_row(rows, strict=True)
    assert selected is None
    assert distinct_ids == ["A", "B"]
    assert error == "AMBIGUOUS_ASSIGNMENT_HISTORY"

    selected, distinct_ids, error = job._select_history_row(rows, strict=False)
    assert selected is None
    assert distinct_ids == ["A", "B"]
    assert error == "AMBIGUOUS_ASSIGNMENT_HISTORY"


def main() -> None:
    test_exact_parameters()
    test_real_run_requires_expected_count()
    test_provider_ids_are_fail_closed()
    test_updates_touch_only_dysponent_id_and_exact_keys()
    test_strict_default_is_fail_closed()
    test_strict_history_selection_rejects_competing_ids()
    print("OK - exact ALPHA00001 Dysponent_ID enrichment tests passed")


if __name__ == "__main__":
    main()
