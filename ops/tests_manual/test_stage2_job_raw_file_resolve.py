#!/usr/bin/env python3
"""Regression: Stage 2 job resolves raw_file id using mapping-style PG rows.

psycopg 3 defaults to tuple rows; ``row['id']`` raises TypeError without
``row_factory=dict_row``. Production saw: tuple indices must be integers or slices, not str.

Run:

    cd /opt/log-platform
    .venv/bin/python3 ops/tests_manual/test_stage2_job_raw_file_resolve.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.reports.stage2.job_stage2 import _get_raw_file_id  # noqa: E402


def _test_tuple_row_string_index_fails_like_production() -> None:
    """What happens when fetchone() returns a tuple (no dict_row)."""
    row = ("11e44cf1-7421-47b6-8d5e-f75675b37d88",)
    try:
        _ = row["id"]  # type: ignore[index]
    except TypeError as exc:
        assert "str" in str(exc)
    else:
        raise AssertionError("expected TypeError for tuple row['id']")
    print("PASS: tuple row + string index matches production failure mode")


def _test_get_raw_file_id_accepts_mapping_row() -> None:
    cur = MagicMock()
    cur.fetchone.return_value = {"id": "11e44cf1-7421-47b6-8d5e-f75675b37d88"}
    path = Path("/home/logplatform/reports-data/normalized/2026/05/12/x.csv")
    assert _get_raw_file_id(cur, path) == "11e44cf1-7421-47b6-8d5e-f75675b37d88"
    print("PASS: _get_raw_file_id works with dict-like row")


def main() -> None:
    _test_tuple_row_string_index_fails_like_production()
    _test_get_raw_file_id_accepts_mapping_row()


if __name__ == "__main__":
    main()
