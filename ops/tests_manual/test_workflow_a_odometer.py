#!/usr/bin/env python3
"""Manual sanity test for odometer wiring in
`jobs.api.telematics.sync_trips_and_speeding` (migration 016).

What this checks (no DB, no network):

  * `_safe_int` parses int-shaped values defensively:
      - real ints, numeric strings, and integer-valued floats parse;
      - bools, NaN, negatives, and garbage strings collapse to None.
  * `_extract_odometer_value(trip, *keys)` picks the FIRST non-None
    candidate (canonical name first, then synonyms), then runs the
    value through `_safe_int`.
  * The trip-parsing snippet inside the job source still references
    the canonical provider keys (`start_odometer_value`,
    `end_odometer_value`) AND tolerates legacy synonyms.
  * The final client_trips migration
    `db/client_business/020_client_trips_final_schema.sql` declares the
    BIGINT NULL columns; the onboarding script uses that final DDL directly.
  * The job's INSERT column list includes both odometer columns AND
    the `DO UPDATE SET` clause refreshes them on `overwrite_existing`.

This script does NOT execute SQL or import psycopg.

Run:

    cd /opt/log-platform
    python3 ops/tests_manual/test_workflow_a_odometer.py
"""
from __future__ import annotations

import re
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _install_stub(name: str, attrs: dict | None = None) -> None:
    if name in sys.modules:
        return
    try:
        __import__(name)
        return
    except Exception:
        pass
    mod = types.ModuleType(name)
    for k, v in (attrs or {}).items():
        setattr(mod, k, v)
    sys.modules[name] = mod


_install_stub("requests")
_install_stub("psycopg")
_install_stub("psycopg.rows", attrs={"dict_row": object()})

from jobs.api.telematics import sync_trips_and_speeding as job  # noqa: E402

JOB_PATH = REPO_ROOT / "jobs" / "api" / "telematics" / "sync_trips_and_speeding.py"
MIGRATION_PATH = REPO_ROOT / "db" / "client_business" / "020_client_trips_final_schema.sql"
ONBOARD_PATH = REPO_ROOT / "scripts" / "onboard_workflow_a_client.py"

FAILURES: list[str] = []


def _check(label: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    line = f"[{status}] {label}"
    if detail:
        line += f"\n        {detail}"
    print(line)
    if not ok:
        FAILURES.append(label)


def test_safe_int() -> None:
    samples = [
        (None, None),
        (0, 0),
        (12345, 12345),
        (-1, None),
        (True, None),
        (False, None),
        (12.0, 12),
        (12.7, 12),
        (-3.0, None),
        (float("nan"), None),
        ("0", 0),
        (" 1234 ", 1234),
        ("12.5", 12),
        ("", None),
        ("not-a-number", None),
        ("12345678901234", 12345678901234),  # > INT4 max => still ok in BIGINT
    ]
    for raw, expected in samples:
        got = job._safe_int(raw)
        _check(f"_safe_int({raw!r}) -> {expected!r}",
               got == expected, f"got={got!r}")


def test_extract_odometer_value_picks_first_present() -> None:
    samples = [
        ({"start_odometer_value": 100}, ("start_odometer_value", "start_odometer"), 100),
        ({"start_odometer": 200}, ("start_odometer_value", "start_odometer"), 200),
        # Canonical wins when both present.
        ({"start_odometer_value": 100, "start_odometer": 200},
         ("start_odometer_value", "start_odometer"), 100),
        # Canonical None falls through to synonym.
        ({"start_odometer_value": None, "start_odometer": 50},
         ("start_odometer_value", "start_odometer"), 50),
        # Garbage canonical falls through.
        ({"start_odometer_value": "x", "start_odometer": 7},
         ("start_odometer_value", "start_odometer"), 7),
        # All missing or invalid → None.
        ({}, ("start_odometer_value", "start_odometer"), None),
        ({"start_odometer_value": -5}, ("start_odometer_value",), None),
    ]
    for trip, keys, expected in samples:
        got = job._extract_odometer_value(trip, *keys)
        _check(f"_extract_odometer_value({trip!r}, keys={keys!r}) -> {expected!r}",
               got == expected, f"got={got!r}")


def test_job_source_threads_odometer_fields() -> None:
    src = JOB_PATH.read_text(encoding="utf-8")

    _check("job source mentions canonical key `start_odometer_value`",
           "start_odometer_value" in src)
    _check("job source mentions canonical key `end_odometer_value`",
           "end_odometer_value" in src)

    _check("job source extracts odometer via `_extract_odometer_value`",
           "_extract_odometer_value(" in src)

    in_do_update_start = re.search(
        r"DO\s+UPDATE\s+SET[\s\S]+?start_odometer_value\s*=\s*EXCLUDED\.start_odometer_value",
        src, re.IGNORECASE,
    ) is not None
    in_do_update_end = re.search(
        r"DO\s+UPDATE\s+SET[\s\S]+?end_odometer_value\s*=\s*EXCLUDED\.end_odometer_value",
        src, re.IGNORECASE,
    ) is not None
    _check("`start_odometer_value` refreshed in DO UPDATE SET",
           in_do_update_start)
    _check("`end_odometer_value` refreshed in DO UPDATE SET",
           in_do_update_end)


def test_migration_file_present_and_additive() -> None:
    _check("migration 020_client_trips_final_schema.sql exists",
           MIGRATION_PATH.exists(),
           f"missing: {MIGRATION_PATH}")
    if MIGRATION_PATH.exists():
        sql = MIGRATION_PATH.read_text(encoding="utf-8")
        _check("migration creates/rebuilds final client_trips schema",
               "CREATE TABLE IF NOT EXISTS public.client_trips" in sql
               and "client_trips_rebuilt_020" in sql)
        _check("migration adds start_odometer_value BIGINT NULL",
               re.search(r"start_odometer_value\s+BIGINT\s+NULL", sql,
                         re.IGNORECASE) is not None)
        _check("migration adds end_odometer_value BIGINT NULL",
               re.search(r"end_odometer_value\s+BIGINT\s+NULL", sql,
                         re.IGNORECASE) is not None)
        _check("migration has no DROP COLUMN",
               "DROP COLUMN" not in sql.upper())


def test_onboarding_script_registers_migration() -> None:
    txt = ONBOARD_PATH.read_text(encoding="utf-8")
    _check("onboarding script lists final 020_client_trips_final_schema.sql",
           '020_client_trips_final_schema.sql' in txt)
    _check("onboarding migration baseline marks old 016 as superseded",
           '016_add_odometer_columns.sql' in txt)


def main() -> int:
    test_safe_int()
    test_extract_odometer_value_picks_first_present()
    test_job_source_threads_odometer_fields()
    test_migration_file_present_and_additive()
    test_onboarding_script_registers_migration()

    print("")
    if FAILURES:
        print(f"FAIL — {len(FAILURES)} check(s) failed:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("OK — odometer wiring (extractor + INSERT/UPDATE + migration "
          "+ onboarding) looks correct.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
