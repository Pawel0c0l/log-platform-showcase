#!/usr/bin/env python3
"""Manual sanity test for the Workflow B report type registry migrations.

The Stage 2 runtime registry remains `jobs.reports.stage2.registry`. Migration
`019_workflow_b_report_type_registry.sql` seeds a database read model for
operator inspection. Migration
`020_workflow_b_report_registry_detection_contract.sql` adds the Phase 1
machine-readable detection contract for future DB-driven detection. Migration
`021_workflow_b_report_207_registry.sql` adds report_207 to the control-plane
registry. This test compares the seeds to the Python registry without requiring
Postgres, and can optionally run live DB checks after migrations.

Run offline checks:

    cd /opt/log-platform
    python3 ops/tests_manual/test_workflow_b_report_registry.py

Run live DB checks after `bash ops/db_migrate.sh`:

    PYTHONPATH="$PWD" python3 ops/tests_manual/test_workflow_b_report_registry.py --db

Reapply the migration twice before live checks to verify idempotence:

    PYTHONPATH="$PWD" python3 ops/tests_manual/test_workflow_b_report_registry.py --db --apply-sql
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.reports.stage2.detector import DETECT_THRESHOLD  # noqa: E402
from jobs.reports.stage2.registry import REGISTERED_REPORTS  # noqa: E402


MIGRATION_PATH = REPO_ROOT / "db" / "migrations" / "019_workflow_b_report_type_registry.sql"
CONTRACT_MIGRATION_PATH = (
    REPO_ROOT / "db" / "migrations" / "020_workflow_b_report_registry_detection_contract.sql"
)
REPORT_207_MIGRATION_PATH = (
    REPO_ROOT / "db" / "migrations" / "021_workflow_b_report_207_registry.sql"
)
ALPHA_GPS_REGISTRY_MIGRATION_PATH = (
    REPO_ROOT / "db" / "migrations" / "030_workflow_b_alpha_gps_baza_log_registry.sql"
)
D105_2_ECODRIVING_REGISTRY_MIGRATION_PATH = (
    REPO_ROOT / "db" / "migrations" / "041_workflow_b_d105_2_ecodriving_registry.sql"
)
ADDITIVE_REGISTRY_MIGRATION_PATHS = (
    REPORT_207_MIGRATION_PATH,
    ALPHA_GPS_REGISTRY_MIGRATION_PATH,
    D105_2_ECODRIVING_REGISTRY_MIGRATION_PATH,
)
TABLE_FQN = "workflow_b_control.report_type_registry"
ALLOWED_STATUSES = {"implemented", "partial", "todo", "deprecated"}
ECO_TYPES = {"eco_driving_driver", "eco_driving_vehicle"}
JSON_ARRAY_RULE_KEYS = {
    "required_anywhere_strings",
    "required_header_labels",
    "optional_header_labels",
    "forbidden_anywhere_strings",
    "filename_hints",
}
LEGACY_OPTIONAL_DETECTION_RULE_REPORT_TYPES = {"Alpha_GPS_Baza_LOG"}

FAILURES: list[str] = []


def _check(label: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    line = f"[{status}] {label}"
    if detail:
        line += f"\n        {detail}"
    print(line)
    if not ok:
        FAILURES.append(label)


def _split_top_level_tuples(values_block: str) -> list[str]:
    out: list[str] = []
    depth = 0
    in_str = False
    start: int | None = None
    i = 0
    while i < len(values_block):
        ch = values_block[i]
        if in_str:
            if ch == "'":
                if i + 1 < len(values_block) and values_block[i + 1] == "'":
                    i += 2
                    continue
                in_str = False
        else:
            if ch == "'":
                in_str = True
            elif ch == "(":
                if depth == 0:
                    start = i + 1
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and start is not None:
                    out.append(values_block[start:i])
                    start = None
        i += 1
    return out


def _parse_tuple_values(tuple_body: str) -> list[str | None]:
    parts: list[str | None] = []
    cur: list[str] = []
    in_str = False
    quoted_token = False
    i = 0
    while i < len(tuple_body):
        ch = tuple_body[i]
        if in_str:
            if ch == "'":
                if i + 1 < len(tuple_body) and tuple_body[i + 1] == "'":
                    cur.append("'")
                    i += 2
                    continue
                in_str = False
            else:
                cur.append(ch)
        else:
            if ch == "'":
                in_str = True
                quoted_token = True
            elif ch == ",":
                parts.append(_normalize_sql_value("".join(cur).strip(), quoted_token))
                cur = []
                quoted_token = False
            else:
                cur.append(ch)
        i += 1
    tail = "".join(cur).strip()
    if tail or quoted_token:
        parts.append(_normalize_sql_value(tail, quoted_token))
    return parts


def _normalize_sql_value(value: str, quoted_token: bool) -> str | None:
    value = re.sub(r"::[a-zA-Z0-9_]+$", "", value.strip())
    if not quoted_token and value.upper() == "NULL":
        return None
    return value


def _extract_seed_rows(sql_text: str) -> list[list[str | None]]:
    pattern = re.compile(
        r"INSERT\s+INTO\s+" + re.escape(TABLE_FQN) + r"\s*\((.*?)\)\s*VALUES\s*",
        re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(sql_text)
    if not match:
        return []
    after = sql_text[match.end():]
    values_region = re.split(r"\bON\s+CONFLICT\b", after, maxsplit=1, flags=re.IGNORECASE)[0]
    return [_parse_tuple_values(t) for t in _split_top_level_tuples(values_region)]


def _extract_contract_seed_rows(sql_text: str) -> list[list[str | None]]:
    pattern = re.compile(
        r"WITH\s+contract_rows\s*\((.*?)\)\s+AS\s*\(\s*VALUES\s*",
        re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(sql_text)
    if not match:
        return []
    after = sql_text[match.end():]
    values_region = re.split(
        r"\n\)\s*UPDATE\s+" + re.escape(TABLE_FQN),
        after,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    return [_parse_tuple_values(t) for t in _split_top_level_tuples(values_region)]


def _load_seed() -> dict[str, dict[str, Any]]:
    sql_text = MIGRATION_PATH.read_text(encoding="utf-8")
    _check("migration file exists", MIGRATION_PATH.exists(), str(MIGRATION_PATH))
    _check("schema DDL present", "CREATE SCHEMA IF NOT EXISTS workflow_b_control" in sql_text)
    _check("table DDL present", f"CREATE TABLE IF NOT EXISTS {TABLE_FQN}" in sql_text)
    _check("implementation_status CHECK present", "ck_report_type_registry_implementation_status" in sql_text)
    for status in sorted(ALLOWED_STATUSES):
        _check(f"implementation_status allows {status}", f"'{status}'" in sql_text)
    _check("seed uses ON CONFLICT", "ON CONFLICT (report_type) DO UPDATE" in sql_text)

    seed: dict[str, dict[str, Any]] = {}
    rows = _extract_seed_rows(sql_text)
    for row in rows:
        if len(row) != 13:
            _check("seed row has expected column count", False, f"row={row}")
            continue
        report_type = str(row[0])
        if report_type in seed:
            _check(f"duplicate seed row for {report_type}", False)
        seed[report_type] = {
            "display_name": row[1],
            "enabled": row[2],
            "cleaner_module": row[3],
            "cleaner_function": row[4],
            "detection_module": row[5],
            "detection_rules": json.loads(str(row[6])),
            "required_columns": json.loads(str(row[7])),
            "optional_columns": json.loads(str(row[8])),
            "min_detection_score": float(str(row[9])),
            "priority": int(str(row[10])),
            "implementation_status": row[11],
            "notes": row[12],
        }
    return seed


def _parse_bool(value: str | None) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise ValueError(f"expected SQL boolean literal, got {value!r}")


def _expected_column_types(cls: type) -> dict[str, str]:
    registry_column_types = getattr(cls, "REGISTRY_COLUMN_TYPES", None)
    if registry_column_types is not None:
        return dict(registry_column_types)
    mapped: dict[str, str] = {}
    for column_name, raw_type in getattr(cls, "COLUMN_TYPES", {}).items():
        mapped[str(column_name)] = {"float": "numeric"}.get(str(raw_type), str(raw_type))
    return mapped


def _parse_full_registry_row(row: list[str | None]) -> tuple[dict[str, Any], dict[str, Any]]:
    if len(row) < 17:
        raise ValueError(f"expected at least 17 columns, got {len(row)}: {row}")
    row = row[:17]
    seed_row = {
        "display_name": row[1],
        "enabled": row[2],
        "cleaner_module": row[3],
        "cleaner_function": row[4],
        "detection_module": row[5],
        "detection_rules": json.loads(str(row[6])),
        "required_columns": json.loads(str(row[7])),
        "optional_columns": json.loads(str(row[8])),
        "min_detection_score": float(str(row[9])),
        "priority": int(str(row[10])),
        "implementation_status": row[11],
        "notes": row[12],
    }
    contract_row = {
        "detection_rules_schema_version": int(str(row[13])),
        "column_types": json.loads(str(row[14])),
        "multi_table": _parse_bool(row[15]),
        "cleaner_entrypoint": row[16],
        "detection_rules": json.loads(str(row[6])),
        "notes": row[12],
    }
    return seed_row, contract_row


def _load_contract_seed() -> dict[str, dict[str, Any]]:
    sql_text = CONTRACT_MIGRATION_PATH.read_text(encoding="utf-8")
    _check("contract migration file exists", CONTRACT_MIGRATION_PATH.exists(), str(CONTRACT_MIGRATION_PATH))
    for ddl_token in (
        "detection_rules_schema_version",
        "column_types",
        "multi_table",
        "cleaner_entrypoint",
        "ck_report_type_registry_detection_rules_object",
        "ck_report_type_registry_column_types_object",
        "ck_report_type_registry_required_columns_array",
        "ck_report_type_registry_optional_columns_array",
    ):
        _check(f"contract migration contains {ddl_token}", ddl_token in sql_text)
    _check("contract seed uses UPDATE FROM contract rows",
           "UPDATE workflow_b_control.report_type_registry AS registry" in sql_text)

    seed: dict[str, dict[str, Any]] = {}
    rows = _extract_contract_seed_rows(sql_text)
    for row in rows:
        if len(row) != 7:
            _check("contract seed row has expected column count", False, f"row={row}")
            continue
        report_type = str(row[0])
        if report_type in seed:
            _check(f"duplicate contract seed row for {report_type}", False)
        seed[report_type] = {
            "detection_rules_schema_version": int(str(row[1])),
            "column_types": json.loads(str(row[2])),
            "multi_table": _parse_bool(row[3]),
            "cleaner_entrypoint": row[4],
            "detection_rules": json.loads(str(row[5])),
            "notes": row[6],
        }
    return seed


def _load_additive_registry_seeds() -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    seed: dict[str, dict[str, Any]] = {}
    contract_seed: dict[str, dict[str, Any]] = {}
    for migration_path in ADDITIVE_REGISTRY_MIGRATION_PATHS:
        sql_text = migration_path.read_text(encoding="utf-8")
        _check(f"additive registry migration exists: {migration_path.name}", migration_path.exists(), str(migration_path))
        _check(f"{migration_path.name} uses ON CONFLICT", "ON CONFLICT" in sql_text and "report_type" in sql_text)
        if migration_path == REPORT_207_MIGRATION_PATH:
            _check("report_207 migration mentions Python runtime remains source",
                   "Stage 2 runtime remains Python-registry driven" in sql_text)

        rows = _extract_seed_rows(sql_text)
        for row in rows:
            report_type = str(row[0]) if row else ""
            if report_type in seed:
                _check(f"duplicate additive seed row for {report_type}", False, migration_path.name)
            try:
                seed_row, contract_row = _parse_full_registry_row(row)
            except ValueError as exc:
                _check(f"{migration_path.name} seed row has expected column count", False, str(exc))
                continue
            seed[report_type] = seed_row
            contract_seed[report_type] = contract_row
    _check(
        "additive registry migrations seed expected report types",
        {"report_207", "Alpha_GPS_Baza_LOG", "report_d105_2_ecodriving"}.issubset(seed),
        f"seed={sorted(seed)}",
    )
    return seed, contract_seed


def _offline_checks(seed: dict[str, dict[str, Any]], contract_seed: dict[str, dict[str, Any]]) -> None:
    py_types = {cls.TYPE: cls for cls in REGISTERED_REPORTS}
    _check("every registered Stage 2 type has one seed row",
           set(seed) == set(py_types),
           f"sql={sorted(seed)}, py={sorted(py_types)}")
    _check("seed row count matches Python registry",
           len(seed) == len(py_types),
           f"sql={len(seed)}, py={len(py_types)}")

    replay_once = dict(seed)
    replay_twice = dict(replay_once)
    replay_twice.update(seed)
    _check("seed merge is idempotent by report_type",
           len(replay_twice) == len(seed),
           f"once={len(seed)}, twice={len(replay_twice)}")
    _check("contract seed covers every registered Stage 2 type",
           set(contract_seed) == set(py_types),
           f"sql={sorted(contract_seed)}, py={sorted(py_types)}")
    contract_replay_once = dict(contract_seed)
    contract_replay_twice = dict(contract_replay_once)
    contract_replay_twice.update(contract_seed)
    _check("contract seed merge is idempotent by report_type",
           len(contract_replay_twice) == len(contract_seed),
           f"once={len(contract_seed)}, twice={len(contract_replay_twice)}")

    for report_type, cls in py_types.items():
        row = seed.get(report_type)
        contract_row = contract_seed.get(report_type)
        if row is None:
            continue
        expected_cleaner = f"{cls.__name__}.clean"
        expected_detection = f"{cls.__module__}.{cls.__name__}.detect"
        _check(f"{report_type}.cleaner_module matches Python",
               row["cleaner_module"] == cls.__module__,
               f"sql={row['cleaner_module']!r}, py={cls.__module__!r}")
        _check(f"{report_type}.cleaner_function matches Python",
               row["cleaner_function"] == expected_cleaner,
               f"sql={row['cleaner_function']!r}, py={expected_cleaner!r}")
        _check(f"{report_type}.detection_module matches Python",
               row["detection_module"] == expected_detection,
               f"sql={row['detection_module']!r}, py={expected_detection!r}")
        _check(f"{report_type}.required_columns matches Python",
               sorted(row["required_columns"]) == sorted(getattr(cls, "REQUIRED_COLUMNS", set())),
               f"sql={row['required_columns']}, py={sorted(getattr(cls, 'REQUIRED_COLUMNS', set()))}")
        _check(f"{report_type}.optional_columns matches Python",
               sorted(row["optional_columns"]) == sorted(getattr(cls, "OPTIONAL_COLUMNS", set())),
               f"sql={row['optional_columns']}, py={sorted(getattr(cls, 'OPTIONAL_COLUMNS', set()))}")
        _check(f"{report_type}.min_detection_score matches global threshold",
               row["min_detection_score"] == float(DETECT_THRESHOLD),
               f"sql={row['min_detection_score']}, py={DETECT_THRESHOLD}")
        _check(f"{report_type}.detection_rules is object",
               isinstance(row["detection_rules"], dict))
        _check(f"{report_type}.implementation_status is allowed",
               row["implementation_status"] in ALLOWED_STATUSES,
               str(row["implementation_status"]))
        if contract_row is None:
            continue
        expected_entrypoint = f"{cls.__module__}:{cls.__name__}.clean"
        _check(f"{report_type}.detection_rules_schema_version is 1",
               contract_row["detection_rules_schema_version"] == 1,
               str(contract_row["detection_rules_schema_version"]))
        _check(f"{report_type}.column_types matches Python",
               contract_row["column_types"] == _expected_column_types(cls),
               f"sql={contract_row['column_types']}, py={_expected_column_types(cls)}")
        _check(f"{report_type}.multi_table matches Python",
               contract_row["multi_table"] == bool(getattr(cls, "MULTI_TABLE", False)),
               f"sql={contract_row['multi_table']}, py={bool(getattr(cls, 'MULTI_TABLE', False))}")
        _check(f"{report_type}.cleaner_entrypoint matches Python",
               contract_row["cleaner_entrypoint"] == expected_entrypoint,
               f"sql={contract_row['cleaner_entrypoint']!r}, py={expected_entrypoint!r}")
        detection_rules = contract_row["detection_rules"]
        _check(f"{report_type}.contract detection_rules is object",
               isinstance(detection_rules, dict))
        _check(f"{report_type}.contract detection_rules schema_version is 1",
               detection_rules.get("schema_version") == 1,
               str(detection_rules.get("schema_version")))
        legacy_optional_rules = report_type in LEGACY_OPTIONAL_DETECTION_RULE_REPORT_TYPES
        _check(
            f"{report_type}.contract score_weights is object",
            isinstance(detection_rules.get("score_weights"), dict)
            or (legacy_optional_rules and "score_weights" not in detection_rules),
            repr(detection_rules.get("score_weights")),
        )
        _check(f"{report_type}.contract term_groups is array",
               isinstance(detection_rules.get("term_groups"), list))
        for key in sorted(JSON_ARRAY_RULE_KEYS):
            _check(
                f"{report_type}.contract {key} is array",
                isinstance(detection_rules.get(key), list)
                or (legacy_optional_rules and key not in detection_rules),
                repr(detection_rules.get(key)),
            )

    for report_type in ECO_TYPES:
        row = seed.get(report_type)
        contract_row = contract_seed.get(report_type)
        cls = py_types.get(report_type)
        _check(f"{report_type} marked partial/todo",
               row is not None and row["implementation_status"] in {"partial", "todo"},
               str(row["implementation_status"] if row else None))
        _check(f"{report_type} contract remains multi-table",
               contract_row is not None and contract_row["multi_table"] is True,
               str(contract_row["multi_table"] if contract_row else None))
        if cls is None:
            continue
        try:
            cls.clean([])
        except NotImplementedError:
            _check(f"{report_type}.clean raises NotImplementedError", True)
        except Exception as exc:
            _check(f"{report_type}.clean raises NotImplementedError", False, repr(exc))
        else:
            _check(f"{report_type}.clean raises NotImplementedError", False)


def _dsn() -> str:
    return (
        f"host={os.getenv('POSTGRES_HOST', '127.0.0.1')} "
        f"port={os.getenv('POSTGRES_PORT', '5432')} "
        f"dbname={os.getenv('POSTGRES_DB', 'logdb')} "
        f"user={os.getenv('POSTGRES_USER', 'loguser')} "
        f"password={os.getenv('POSTGRES_PASSWORD', '')}"
    )


def _live_db_checks(
    seed: dict[str, dict[str, Any]],
    contract_seed: dict[str, dict[str, Any]],
    *,
    apply_sql: bool,
) -> None:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        _check("psycopg available for live DB checks", False, repr(exc))
        return

    try:
        conn = psycopg.connect(_dsn(), row_factory=dict_row)
    except Exception as exc:
        _check("connect to live Postgres for registry checks", False, repr(exc))
        return

    with conn:
        if apply_sql:
            migration_sql = MIGRATION_PATH.read_text(encoding="utf-8")
            contract_sql = CONTRACT_MIGRATION_PATH.read_text(encoding="utf-8")
            additive_sql = [path.read_text(encoding="utf-8") for path in ADDITIVE_REGISTRY_MIGRATION_PATHS]
            with conn.cursor() as cur:
                cur.execute(migration_sql)
                cur.execute(contract_sql)
                for sql_text in additive_sql:
                    cur.execute(sql_text)
                cur.execute(migration_sql)
                cur.execute(contract_sql)
                for sql_text in additive_sql:
                    cur.execute(sql_text)
            conn.commit()
            _check("migration SQL reapplied twice", True)

        with conn.cursor() as cur:
            cur.execute("SELECT to_regnamespace('workflow_b_control') AS exists")
            _check("schema exists in DB", cur.fetchone()["exists"] == "workflow_b_control")

            cur.execute("SELECT to_regclass('workflow_b_control.report_type_registry') AS exists")
            _check("table exists in DB", cur.fetchone()["exists"] == TABLE_FQN)

            cur.execute(
                """
                SELECT report_type, count(*) AS row_count
                FROM workflow_b_control.report_type_registry
                GROUP BY report_type
                """
            )
            db_counts = {row["report_type"]: int(row["row_count"]) for row in cur.fetchall()}
            _check("DB rows match registered Stage 2 types",
                   set(db_counts) == set(seed),
                   f"db={sorted(db_counts)}, seed={sorted(seed)}")
            for report_type in sorted(seed):
                _check(f"DB has exactly one row for {report_type}",
                       db_counts.get(report_type) == 1,
                       f"count={db_counts.get(report_type)}")

            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'workflow_b_control'
                  AND table_name = 'report_type_registry'
                  AND column_name IN (
                    'detection_rules_schema_version',
                    'column_types',
                    'multi_table',
                    'cleaner_entrypoint'
                  )
                """
            )
            db_contract_columns = {row["column_name"] for row in cur.fetchall()}
            _check("DB has Phase 1 contract columns",
                   db_contract_columns == {
                       "detection_rules_schema_version",
                       "column_types",
                       "multi_table",
                       "cleaner_entrypoint",
                   },
                   f"columns={sorted(db_contract_columns)}")

            cur.execute(
                """
                SELECT
                  count(*) FILTER (WHERE detection_rules_schema_version = 1) AS version_one,
                  count(*) FILTER (WHERE jsonb_typeof(detection_rules) = 'object') AS detection_objects,
                  count(*) FILTER (WHERE jsonb_typeof(column_types) = 'object') AS column_type_objects,
                  count(*) FILTER (WHERE jsonb_typeof(required_columns) = 'array') AS required_arrays,
                  count(*) FILTER (WHERE jsonb_typeof(optional_columns) = 'array') AS optional_arrays
                FROM workflow_b_control.report_type_registry
                """
            )
            shape_counts = cur.fetchone()
            expected_count = len(contract_seed)
            for label, value in shape_counts.items():
                _check(f"DB {label} count matches registry row count",
                       int(value) == expected_count,
                       f"count={value}, expected={expected_count}")

            cur.execute(
                """
                SELECT report_type, implementation_status, multi_table
                FROM workflow_b_control.report_type_registry
                WHERE report_type IN ('eco_driving_driver', 'eco_driving_vehicle')
                """
            )
            for row in cur.fetchall():
                _check(f"DB {row['report_type']} marked partial/todo",
                       row["implementation_status"] in {"partial", "todo"},
                       row["implementation_status"])
                _check(f"DB {row['report_type']} remains multi-table",
                       row["multi_table"] is True,
                       str(row["multi_table"]))

            try:
                cur.execute(
                    """
                    INSERT INTO workflow_b_control.report_type_registry (
                      report_type, display_name, cleaner_module, cleaner_function,
                      implementation_status
                    )
                    VALUES ('__invalid_status_test__', 'Invalid status test',
                            'jobs.reports.stage2.types.invalid', 'Invalid.clean',
                            'invalid')
                    """
                )
            except Exception:
                conn.rollback()
                _check("invalid implementation_status rejected by DB", True)
            else:
                conn.rollback()
                _check("invalid implementation_status rejected by DB", False)


def _summary() -> int:
    print("")
    if FAILURES:
        print(f"FAIL - {len(FAILURES)} check(s) failed:")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("OK - Workflow B report registry migrations match the Stage 2 Python registry.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", action="store_true", help="Run live Postgres checks.")
    parser.add_argument("--apply-sql", action="store_true", help="Reapply migration SQL twice before live checks.")
    args = parser.parse_args()

    seed = _load_seed()
    contract_seed = _load_contract_seed()
    additive_seed, additive_contract_seed = _load_additive_registry_seeds()
    seed.update(additive_seed)
    contract_seed.update(additive_contract_seed)
    _offline_checks(seed, contract_seed)
    if args.db or args.apply_sql:
        _live_db_checks(seed, contract_seed, apply_sql=args.apply_sql)
    return _summary()


if __name__ == "__main__":
    raise SystemExit(main())
