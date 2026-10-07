#!/usr/bin/env python3
"""Manual checks for new-client onboarding without trip-level fuel columns.

Run from repo root:

    PYTHONDONTWRITEBYTECODE=1 python3 ops/tests_manual/test_workflow_a_onboarding_no_trip_fuel_columns.py
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
OLD_DDL = REPO_ROOT / "db" / "client_business" / "011_extend_client_trips.sql"
NEW_DDL = REPO_ROOT / "db" / "client_business" / "020_client_trips_final_schema.sql"
TRIP_MODE_DDL = REPO_ROOT / "db" / "client_business" / "021_add_trip_mode_to_client_trips.sql"
DRIVER_RESTRICTIONS_DDL = (
    REPO_ROOT / "db" / "client_business" / "026_add_driver_restrictions_to_client_trips.sql"
)
ONBOARD = REPO_ROOT / "scripts" / "onboard_workflow_a_client.py"

LOCATION_COLUMNS = {
    "start_location",
    "start_latitude",
    "start_longitude",
    "end_location",
    "end_latitude",
    "end_longitude",
}
DEPRECATED_FUEL_COLUMNS = {"fuel_consumed_liters", "avg_fuel_l_per_100km"}

FAILURES: list[str] = []


def _check(label: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    line = f"[{status}] {label}"
    if detail:
        line += f"\n        {detail}"
    print(line)
    if not ok:
        FAILURES.append(label)


def _ddl_file_names_from_onboarding() -> list[str]:
    tree = ast.parse(ONBOARD.read_text(encoding="utf-8"), filename=str(ONBOARD))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "CLIENT_BUSINESS_DDL_FILES"
                   for t in node.targets):
            continue
        return [
            s.value for s in ast.walk(node.value)
            if isinstance(s, ast.Constant)
            and isinstance(s.value, str)
            and s.value.endswith(".sql")
        ]
    return []


def test_old_ddl_still_documents_deprecated_columns() -> None:
    _check("old 011 DDL file remains present", OLD_DDL.exists(), f"path={OLD_DDL}")
    if not OLD_DDL.exists():
        return
    old_sql = OLD_DDL.read_text(encoding="utf-8")
    for col in DEPRECATED_FUEL_COLUMNS:
        _check(f"old 011 DDL still contains deprecated `{col}`",
               col in old_sql)


def test_new_ddl_shape() -> None:
    _check("new 020 DDL file is present", NEW_DDL.exists(), f"path={NEW_DDL}")
    if not NEW_DDL.exists():
        return
    new_sql = NEW_DDL.read_text(encoding="utf-8")
    _check("new 020 DDL creates/rebuilds public.client_trips",
           "CREATE TABLE IF NOT EXISTS public.client_trips" in new_sql
           and "client_trips_rebuilt_020" in new_sql)
    _check("new 020 DDL has no DROP COLUMN",
           "DROP COLUMN" not in new_sql.upper())
    for col in sorted(LOCATION_COLUMNS):
        _check(f"new 020 DDL keeps `{col}`", col in new_sql)
    for col in sorted(DEPRECATED_FUEL_COLUMNS):
        _check(f"new 020 DDL omits deprecated `{col}` from final table writes",
               f"{col} DOUBLE PRECISION" not in new_sql)
    _check("021 trip_mode DDL file is present", TRIP_MODE_DDL.exists(), f"path={TRIP_MODE_DDL}")
    if TRIP_MODE_DDL.exists():
        trip_mode_sql = TRIP_MODE_DDL.read_text(encoding="utf-8")
        _check("021 DDL rebuilds client_trips with trip_mode before start_timestamp",
               "trip_mode TEXT NULL" in trip_mode_sql
               and trip_mode_sql.index("trip_mode TEXT NULL") < trip_mode_sql.index("start_timestamp TIMESTAMPTZ NULL"))
    _check("026 Driver_Restrictions DDL file is present",
           DRIVER_RESTRICTIONS_DDL.exists(), f"path={DRIVER_RESTRICTIONS_DDL}")
    if DRIVER_RESTRICTIONS_DDL.exists():
        driver_restrictions_sql = DRIVER_RESTRICTIONS_DDL.read_text(encoding="utf-8")
        _check("026 DDL additively adds nullable Driver_Restrictions",
               'ADD COLUMN IF NOT EXISTS "Driver_Restrictions" TEXT NULL' in driver_restrictions_sql
               and "client_trips_rebuilt" not in driver_restrictions_sql)


def test_onboarding_uses_new_ddl_not_old_ddl() -> None:
    _check("onboarding script is present", ONBOARD.exists(), f"path={ONBOARD}")
    if not ONBOARD.exists():
        return
    ddl_names = _ddl_file_names_from_onboarding()
    _check("CLIENT_BUSINESS_DDL_FILES parsed", bool(ddl_names),
           f"got={ddl_names}")
    _check("new onboarding includes 020_client_trips_final_schema.sql",
           "020_client_trips_final_schema.sql" in ddl_names,
           f"got={ddl_names}")
    _check("new onboarding includes 021_add_trip_mode_to_client_trips.sql",
           "021_add_trip_mode_to_client_trips.sql" in ddl_names,
           f"got={ddl_names}")
    _check("new onboarding includes 026_add_driver_restrictions_to_client_trips.sql",
           "026_add_driver_restrictions_to_client_trips.sql" in ddl_names,
           f"got={ddl_names}")
    _check("new onboarding does not apply 009 old base client_trips DDL",
           "009_workflow_a_client_business.sql" not in ddl_names,
           f"got={ddl_names}")
    _check("new onboarding no longer applies 011_extend_client_trips.sql",
           "011_extend_client_trips.sql" not in ddl_names,
           f"got={ddl_names}")


def main() -> int:
    test_old_ddl_still_documents_deprecated_columns()
    test_new_ddl_shape()
    test_onboarding_uses_new_ddl_not_old_ddl()
    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} check(s) failed:")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("\nOK - new-client onboarding omits deprecated trip-level fuel columns.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
