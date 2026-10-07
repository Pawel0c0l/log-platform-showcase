#!/usr/bin/env python3
"""Manual regression checks for deprecated trip-level fuel writes.

Run from repo root:

    PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_workflow_a_trip_fuel_deprecated.py
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SYNC_JOB = REPO_ROOT / "jobs" / "api" / "telematics" / "sync_trips_and_speeding.py"

FAILURES: list[str] = []


def _check(label: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    line = f"[{status}] {label}"
    if detail:
        line += f"\n        {detail}"
    print(line)
    if not ok:
        FAILURES.append(label)


def _source() -> str:
    return SYNC_JOB.read_text(encoding="utf-8")


def test_sync_job_syntax() -> None:
    source = _source()
    ast.parse(source)
    _check("sync_trips_and_speeding.py parses", True)


def test_no_trip_level_fuel_writes() -> None:
    source = _source()
    _check(
        "client_trips fuel_consumed_liters is not written by sync job",
        "fuel_consumed_liters" not in source,
    )
    _check(
        "client_trips avg_fuel_l_per_100km is not written by sync job",
        "avg_fuel_l_per_100km" not in source,
    )


def test_skip_fuel_is_backward_compatible_noop() -> None:
    source = _source()
    _check(
        "skip_fuel param remains accepted",
        'params.get("skip_fuel", False)' in source,
    )
    _check(
        "skip_fuel no longer triggers provider fuel calls",
        "fetch_fuel_consumed(" not in source and "_fetch_fuel_for_trips" not in source,
    )
    _check(
        "deprecation/no-op log is present",
        "Trip-level fuel enrichment is deprecated and skipped" in source
        and "backward-compatible no-op" in source,
    )


def main() -> int:
    test_sync_job_syntax()
    test_no_trip_level_fuel_writes()
    test_skip_fuel_is_backward_compatible_noop()
    if FAILURES:
        print(f"\n{len(FAILURES)} test(s) failed.")
        return 1
    print("\nOK - trip-level fuel deprecation checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
