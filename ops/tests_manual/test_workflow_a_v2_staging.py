#!/usr/bin/env python3
"""Manual sanity test for Workflow A V2 declared-only staging schema.

What this checks (no DB, no network):

  * `db/client_business/017_v2_staging_tables.sql` declares all three
    `source_*` tables with `IF NOT EXISTS`, idempotent indexes, the
    documented PRIMARY KEYs, and the `record_id` UNIQUE INDEX.
  * The columns the user requested are all present (loose name/type
    sniff — Postgres parser is the real authority at apply time, but
    this catches obvious drift).
  * `db/migrations/015_workflow_a_v2_datasets.sql` previously declared
    V2 registry rows, and `016_workflow_a_disable_declared_v2_registry.sql`
    removes those rows again because the V2 jobs do not exist.
  * `scripts/onboard_workflow_a_client.py:CLIENT_BUSINESS_DDL_FILES`
    does not include `017_v2_staging_tables.sql`, so new clients do not
    receive unused V2 tables by default.
  * V2 dataset schedule rows are NOT seeded by onboarding (Phase 1
    framing — the V2 jobs do not exist yet, so creating disabled rows
    would only create operator footguns).

Phase 1 framing:
  Production behavior is unchanged. `sync_trips_and_speeding` remains
  the only producer of `client_trips` / `client_speeding_notifications`,
  and no Workflow A job in this repo writes the new `source_*` tables or
  appears in the Python runtime registry.

Run:

    cd /opt/log-platform
    python3 ops/tests_manual/test_workflow_a_v2_staging.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


CLIENT_DDL_PATH = REPO_ROOT / "db" / "client_business" / "017_v2_staging_tables.sql"
PLATFORM_MIGRATION_PATH = REPO_ROOT / "db" / "migrations" / "015_workflow_a_v2_datasets.sql"
CORRECTIVE_MIGRATION_PATH = (
    REPO_ROOT / "db" / "migrations" / "016_workflow_a_disable_declared_v2_registry.sql"
)
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


# Required column names per V2 design — light sniff, not a full DDL parser.
SOURCE_TRIPS_REQUIRED = (
    "record_id", "client_id", "client_code",
    "provider_trip_id", "registration", "vehicle_id",
    "driver_id", "driver_name", "driver_surname", "chassis_number",
    "terminal_id", "terminal_serial",
    "start_timestamp", "end_timestamp",
    "trip_duration_seconds", "trip_distance_meters",
    "start_latitude", "start_longitude",
    "end_latitude", "end_longitude",
    "start_geofence_name", "end_geofence_name",
    "start_location", "end_location",
    "harsh_acceleration_events", "harsh_braking_events",
    "harsh_turning_events", "idle_events", "idle_time_seconds",
    "start_odometer_value", "end_odometer_value",
    "raw_payload", "fetched_at", "synced_at", "sync_run_id",
)

SOURCE_NOTIFICATIONS_REQUIRED = (
    "record_id", "client_id", "client_code",
    "provider_notification_id", "type", "type_raw",
    "registration", "registration_norm", "vehicle_id", "event_ts",
    "speed", "trigger_description", "geofence_id",
    "notification_msg", "status",
    "raw_payload", "fetched_at", "synced_at", "sync_run_id",
)

SOURCE_FUEL_REQUIRED = (
    "record_id", "client_id", "client_code",
    "registration_norm", "window_start_ts", "window_end_ts",
    "fuel_consumed_liters", "api_status", "http_status",
    "raw_payload", "fetched_at", "synced_at", "sync_run_id",
)


def _slice_create_table(sql: str, fqn: str) -> str | None:
    """Extract the body of a `CREATE TABLE IF NOT EXISTS <fqn> ( ... );`
    statement. Tracks parenthesis depth on a single statement.
    """
    pattern = re.compile(
        r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+" + re.escape(fqn) + r"\s*\(",
        re.IGNORECASE,
    )
    m = pattern.search(sql)
    if not m:
        return None
    i = m.end()
    depth = 1
    start = i
    while i < len(sql) and depth > 0:
        ch = sql[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return sql[start:i]
        i += 1
    return None


def _strip_sql_line_comments(sql: str) -> str:
    """Drop everything from `--` to end-of-line, preserving newlines.
    Block comments (`/* */`) are not used in our DDL so this is safe.
    """
    cleaned: list[str] = []
    for line in sql.splitlines():
        idx = line.find("--")
        cleaned.append(line[:idx] if idx != -1 else line)
    return "\n".join(cleaned)


def _column_names(table_body: str) -> set[str]:
    """Parse the leading identifier of each top-level comma-separated
    line in a CREATE TABLE body. Skips PRIMARY KEY / CONSTRAINT lines
    and `--` line comments.
    """
    body = _strip_sql_line_comments(table_body)
    out: set[str] = set()
    depth = 0
    cur: list[str] = []
    parts: list[str] = []
    for ch in body:
        if ch == "(":
            depth += 1
            cur.append(ch)
        elif ch == ")":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    tail = "".join(cur).strip()
    if tail:
        parts.append(tail)
    for p in parts:
        s = p.strip()
        if not s:
            continue
        head = s.split()[0].upper() if s.split() else ""
        if head in {"PRIMARY", "CONSTRAINT", "UNIQUE", "FOREIGN", "CHECK"}:
            continue
        ident = s.split()[0].strip(", ")
        if ident:
            out.add(ident)
    return out


def test_client_ddl_idempotent_creates() -> None:
    _check("client DDL file present", CLIENT_DDL_PATH.exists(),
           f"path={CLIENT_DDL_PATH}")
    if not CLIENT_DDL_PATH.exists():
        return
    sql = CLIENT_DDL_PATH.read_text(encoding="utf-8")

    for fqn in (
        "public.source_trips",
        "public.source_notifications",
        "public.source_fuel_observations",
    ):
        _check(f"`CREATE TABLE IF NOT EXISTS {fqn}` present",
               re.search(
                   r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+"
                   + re.escape(fqn),
                   sql, re.IGNORECASE) is not None)

    # Indexes are CREATE INDEX IF NOT EXISTS so file is replay-safe.
    for idx in (
        "uq_source_trips_record_id",
        "idx_source_trips_client_start_ts",
        "idx_source_trips_client_registration_start_ts",
        "uq_source_notifications_record_id",
        "idx_source_notifications_client_vehicle_event_ts",
        "idx_source_notifications_client_reg_norm_event_ts",
        "idx_source_notifications_client_event_ts_hot_types",
        "uq_source_fuel_observations_record_id",
        "idx_source_fuel_observations_client_reg_norm_window_end",
    ):
        _check(f"index `{idx}` present (IF NOT EXISTS)",
               re.search(
                   r"CREATE\s+(UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\s+"
                   + re.escape(idx),
                   sql, re.IGNORECASE) is not None)

    # Partial index for the hot type set.
    _check("hot-types partial index references HIGH_RPM/OVERREV/SPEEDING",
           re.search(
               r"WHERE\s+type\s+IN\s*\(\s*'HIGH_RPM'\s*,\s*'OVERREV'\s*,\s*'SPEEDING'\s*\)",
               sql, re.IGNORECASE) is not None)


def test_required_columns_present() -> None:
    if not CLIENT_DDL_PATH.exists():
        return
    sql = CLIENT_DDL_PATH.read_text(encoding="utf-8")

    for fqn, required in (
        ("public.source_trips", SOURCE_TRIPS_REQUIRED),
        ("public.source_notifications", SOURCE_NOTIFICATIONS_REQUIRED),
        ("public.source_fuel_observations", SOURCE_FUEL_REQUIRED),
    ):
        body = _slice_create_table(sql, fqn)
        _check(f"CREATE TABLE body parsed: {fqn}", body is not None)
        if body is None:
            continue
        cols = _column_names(body)
        missing = sorted(c for c in required if c not in cols)
        _check(f"{fqn}: all required columns present", not missing,
               f"missing={missing} cols={sorted(cols)}")


def test_primary_keys_match_design() -> None:
    if not CLIENT_DDL_PATH.exists():
        return
    sql = CLIENT_DDL_PATH.read_text(encoding="utf-8")

    cases = [
        ("public.source_trips",
         r"PRIMARY\s+KEY\s*\(\s*client_id\s*,\s*provider_trip_id\s*\)"),
        ("public.source_notifications",
         r"PRIMARY\s+KEY\s*\(\s*client_id\s*,\s*provider_notification_id\s*\)"),
        ("public.source_fuel_observations",
         r"PRIMARY\s+KEY\s*\(\s*client_id\s*,\s*registration_norm\s*,"
         r"\s*window_start_ts\s*,\s*window_end_ts\s*\)"),
    ]
    for fqn, pattern in cases:
        body = _slice_create_table(sql, fqn) or ""
        _check(f"{fqn}: PRIMARY KEY shape matches design",
               re.search(pattern, body, re.IGNORECASE) is not None)


def test_no_foreign_keys_to_platform_db() -> None:
    """Client DB must not reference the platform DB — they live in
    separate Postgres instances. The whole staging DDL must be free
    of `REFERENCES workflow_a_control.*` clauses.
    """
    if not CLIENT_DDL_PATH.exists():
        return
    sql = CLIENT_DDL_PATH.read_text(encoding="utf-8")
    _check("no FK from client DDL to platform schema",
           "REFERENCES workflow_a_control" not in sql,
           "client DDL referenced workflow_a_control.* — invalid")


def test_platform_v2_declaration_removed_from_active_registry() -> None:
    _check("platform migration file present", PLATFORM_MIGRATION_PATH.exists(),
           f"path={PLATFORM_MIGRATION_PATH}")
    if not PLATFORM_MIGRATION_PATH.exists():
        return
    sql = PLATFORM_MIGRATION_PATH.read_text(encoding="utf-8")

    for ds in ("trips_ingest", "notifications_ingest",
               "fuel_ingest", "trips_enrichment"):
        _check(f"dataset_registry seed contains {ds!r}",
               f"'{ds}'" in sql)

    for tb in ("source_trips", "source_notifications",
               "source_fuel_observations"):
        _check(f"table_registry seed contains {tb!r}",
               f"'{tb}'" in sql)

    _check("dataset_registry uses ON CONFLICT DO UPDATE",
           re.search(
               r"INSERT\s+INTO\s+workflow_a_control\.dataset_registry"
               r"[\s\S]+?ON\s+CONFLICT\s*\(\s*dataset_name\s*\)\s+DO\s+UPDATE",
               sql, re.IGNORECASE) is not None)
    _check("table_registry uses ON CONFLICT DO UPDATE",
           re.search(
               r"INSERT\s+INTO\s+workflow_a_control\.table_registry"
               r"[\s\S]+?ON\s+CONFLICT\s*\(\s*table_name\s*\)\s+DO\s+UPDATE",
               sql, re.IGNORECASE) is not None)

    # Retention key column is verified against the Python registry by
    # `test_workflow_a_registry_sync.py`; here we only check the
    # canonical literal is mentioned in the seed.
    for col in ("start_timestamp", "event_ts", "window_end_ts"):
        _check(f"table_registry seed mentions retention column {col!r}",
               f"'{col}'" in sql)

    _check("corrective migration file present", CORRECTIVE_MIGRATION_PATH.exists(),
           f"path={CORRECTIVE_MIGRATION_PATH}")
    if not CORRECTIVE_MIGRATION_PATH.exists():
        return
    corrective = CORRECTIVE_MIGRATION_PATH.read_text(encoding="utf-8")
    for ds in ("trips_ingest", "notifications_ingest",
               "fuel_ingest", "trips_enrichment"):
        _check(f"corrective migration deletes dataset {ds!r}",
               re.search(
                   r"DELETE\s+FROM\s+workflow_a_control\.dataset_registry"
                   r"[\s\S]+?" + re.escape(f"'{ds}'"),
                   corrective, re.IGNORECASE) is not None)
    for tb in ("source_trips", "source_notifications",
               "source_fuel_observations"):
        _check(f"corrective migration deletes table {tb!r}",
               re.search(
                   r"DELETE\s+FROM\s+workflow_a_control\.table_registry"
                   r"[\s\S]+?" + re.escape(f"'{tb}'"),
                   corrective, re.IGNORECASE) is not None)


def test_onboarding_excludes_017_ddl() -> None:
    _check("onboard script present", ONBOARD_PATH.exists(),
           f"path={ONBOARD_PATH}")
    if not ONBOARD_PATH.exists():
        return
    src = ONBOARD_PATH.read_text(encoding="utf-8")
    m = re.search(r"CLIENT_BUSINESS_DDL_FILES\s*=\s*\[(.*?)\]", src, re.DOTALL)
    _check("CLIENT_BUSINESS_DDL_FILES literal parsed", m is not None)
    if m is not None:
        block = m.group(1)
        _check("CLIENT_BUSINESS_DDL_FILES does NOT reference 017_v2_staging_tables.sql",
               '"017_v2_staging_tables.sql"' not in block
               and "'017_v2_staging_tables.sql'" not in block)


def test_onboarding_does_not_seed_v2_schedule_rows() -> None:
    """Phase 1 must NOT add the V2 datasets to DEFAULT_DATASETS — until
    the V2 jobs exist, an enabled schedule row would only confuse
    operators (the dispatcher would log ERROR and skip). Same logic
    for source_* in DEFAULT_TABLES.
    """
    if not ONBOARD_PATH.exists():
        return
    src = ONBOARD_PATH.read_text(encoding="utf-8")
    m = re.search(r"DEFAULT_DATASETS\s*=\s*\(([^)]*)\)", src)
    _check("DEFAULT_DATASETS literal parsed", m is not None)
    if m is not None:
        block = m.group(1)
        for ds in ("trips_ingest", "notifications_ingest",
                   "fuel_ingest", "trips_enrichment"):
            _check(f"DEFAULT_DATASETS does NOT include {ds!r}",
                   f"'{ds}'" not in block and f'"{ds}"' not in block)
    m = re.search(r"DEFAULT_TABLES\s*=\s*\(([^)]*)\)", src)
    _check("DEFAULT_TABLES literal parsed", m is not None)
    if m is not None:
        block = m.group(1)
        for tb in ("source_trips", "source_notifications",
                   "source_fuel_observations"):
            _check(f"DEFAULT_TABLES does NOT include {tb!r}",
                   f"'{tb}'" not in block and f'"{tb}"' not in block)


def test_registry_python_excludes_v2() -> None:
    """Ensure the Python runtime registry does not expose V2 entries until
    the job modules exist.
    """
    from jobs.api.telematics import registry  # noqa: WPS433

    for ds in ("trips_ingest", "notifications_ingest",
               "fuel_ingest", "trips_enrichment"):
        _check(f"registry.DATASETS does NOT contain {ds!r}",
               ds not in registry.DATASETS)

    for tb in ("source_trips", "source_notifications",
               "source_fuel_observations"):
        _check(f"registry.TABLES does NOT contain {tb!r}",
               tb not in registry.TABLES)


def main() -> int:
    test_client_ddl_idempotent_creates()
    test_required_columns_present()
    test_primary_keys_match_design()
    test_no_foreign_keys_to_platform_db()
    test_platform_v2_declaration_removed_from_active_registry()
    test_onboarding_excludes_017_ddl()
    test_onboarding_does_not_seed_v2_schedule_rows()
    test_registry_python_excludes_v2()

    print("")
    if FAILURES:
        print(f"FAIL — {len(FAILURES)} check(s) failed:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("OK — V2 declared-only schema checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
