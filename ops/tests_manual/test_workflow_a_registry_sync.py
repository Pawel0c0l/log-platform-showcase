#!/usr/bin/env python3
"""Manual sanity test: Python registry vs final platform migration SQL seeds.

The Python module `jobs.api.telematics.registry` is the **source of truth**
for the Workflow A dataset/table catalog. Two platform migrations seed
the same rows into `workflow_a_control.dataset_registry` and
`workflow_a_control.table_registry` so SQL clients and ops dashboards
can join against the catalog directly:

  * `011_workflow_a_dataset_registry.sql` — V1 datasets (`trips_sync`,
    `fuel_daily_aggregation`) and tables (`client_*`).
  * `015_workflow_a_v2_datasets.sql` — previously declared V2 rows.
  * `016_workflow_a_disable_declared_v2_registry.sql` — removes those
    declared-only V2 rows again until real V2 job modules exist.
  * `032_workflow_a_eco_driving_registry.sql` — adds implemented Eco
    Driving dispatcher dataset rows and table registry rows.
  * `046_workflow_a_eco_person_registry.sql` — adds implemented isolated
    Eco Driving Person dataset rows and table registry rows.

This test replays the relevant INSERT/DELETE seed effects in migration
order (later-applied rows win on `ON CONFLICT DO UPDATE`, same as
Postgres at apply time) and compares the final set to the Python
registry, asserting bidirectional equivalence.

If those definitions ever diverge, the dispatcher or retention worker
(which use the **Python** registry as their allowlist) will refuse to act
on rows the DB declared but Python did not, or would treat missing Python
rows as implemented when they are not.

Run:

    cd /opt/log-platform
    python3 ops/tests_manual/test_workflow_a_registry_sync.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.api.telematics import registry  # noqa: E402


MIGRATION_PATHS = [
    REPO_ROOT / "db" / "migrations" / "011_workflow_a_dataset_registry.sql",
    REPO_ROOT / "db" / "migrations" / "015_workflow_a_v2_datasets.sql",
    REPO_ROOT / "db" / "migrations" / "016_workflow_a_disable_declared_v2_registry.sql",
    REPO_ROOT / "db" / "migrations" / "032_workflow_a_eco_driving_registry.sql",
    REPO_ROOT / "db" / "migrations" / "046_workflow_a_eco_person_registry.sql",
]


FAILURES: list[str] = []


def _check(label: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    line = f"[{status}] {label}"
    if detail:
        line += f"\n        {detail}"
    print(line)
    if not ok:
        FAILURES.append(label)


# ---------------------------------------------------------------------------
# Tiny SQL VALUES tuple parser — handles single-quoted string literals only,
# which is exactly the shape our seed uses. Doublequoted '' escapes are
# supported (not currently used).
# ---------------------------------------------------------------------------

def _split_top_level_tuples(values_block: str) -> list[str]:
    """Split a `VALUES (...), (...), ...` block into individual tuple bodies.

    Tracks parentheses depth and respects single-quoted strings.
    """
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


def _parse_tuple_values(tuple_body: str) -> list[str]:
    """Parse a single `(a, b, c)` body into a list of string values.

    Supports only single-quoted SQL strings (which is what our seed uses).
    """
    parts: list[str] = []
    cur = []
    in_str = False
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
            elif ch == ",":
                parts.append("".join(cur).strip())
                cur = []
            else:
                cur.append(ch)
        i += 1
    tail = "".join(cur).strip()
    if tail:
        parts.append(tail)
    return parts


def _extract_insert(sql_text: str, table_fqn: str) -> list[list[str]]:
    """Extract all `(a, b, c)` rows from `INSERT INTO <table_fqn> ... VALUES (...)`.

    Stops at the first terminating `;` or the next `INSERT INTO` to keep this
    simple. Multi-row INSERTs are supported (the seed uses them).
    """
    pattern = re.compile(
        r"INSERT\s+INTO\s+" + re.escape(table_fqn) + r"\b.*?VALUES\s*",
        re.IGNORECASE | re.DOTALL,
    )
    m = pattern.search(sql_text)
    if not m:
        return []
    after = sql_text[m.end():]
    end = re.search(r";\s*(\nINSERT\s+INTO|\nON\s+CONFLICT|$)", after,
                    re.IGNORECASE | re.DOTALL)
    region = after[:end.start()] if end else after
    region = re.split(r"\bON\s+CONFLICT\b", region, maxsplit=1,
                      flags=re.IGNORECASE)[0]
    tuples = _split_top_level_tuples(region)
    return [_parse_tuple_values(t) for t in tuples]


def _extract_delete_keys(sql_text: str, table_fqn: str, key_column: str) -> list[str]:
    """Extract simple `DELETE FROM <table> WHERE <key> IN ('a', 'b')` keys.

    The corrective migration uses this narrow, explicit shape. This is not a
    general SQL parser.
    """
    pattern = re.compile(
        r"DELETE\s+FROM\s+" + re.escape(table_fqn)
        + r"\s+WHERE\s+" + re.escape(key_column)
        + r"\s+IN\s*\((.*?)\)",
        re.IGNORECASE | re.DOTALL,
    )
    out: list[str] = []
    for m in pattern.finditer(sql_text):
        out.extend(re.findall(r"'((?:''|[^'])*)'", m.group(1)))
    return [v.replace("''", "'") for v in out]


def _merge_registry_rows(table_fqn: str, key_column: str) -> list[list[str]]:
    """Read every migration in MIGRATION_PATHS, parse INSERTs into
    `table_fqn`, parse simple DELETEs from it, and merge by primary key
    (first column). Later migrations overwrite earlier ones — mirrors
    `ON CONFLICT DO UPDATE` semantics at apply time. Returns rows as a
    list of lists, in insertion-order of the first appearance.
    """
    merged: dict[str, list[str]] = {}
    order: list[str] = []
    for path in MIGRATION_PATHS:
        if not path.exists():
            _check(f"migration file present: {path.name}", False,
                   f"missing: {path}")
            continue
        sql_text = path.read_text(encoding="utf-8")
        for row in _extract_insert(sql_text, table_fqn):
            if not row:
                continue
            pk = row[0]
            if pk not in merged:
                order.append(pk)
            merged[pk] = row
        for pk in _extract_delete_keys(sql_text, table_fqn, key_column):
            merged.pop(pk, None)
            if pk in order:
                order.remove(pk)
    return [merged[pk] for pk in order]


def main() -> int:
    for path in MIGRATION_PATHS:
        _check(f"migration file present: {path.name}", path.exists(),
               f"path={path}")

    # ---- dataset_registry ----
    dataset_rows = _merge_registry_rows(
        "workflow_a_control.dataset_registry", "dataset_name",
    )
    _check("dataset_registry rows parsed",
           len(dataset_rows) == len(registry.DATASETS),
           f"sql={len(dataset_rows)}, python={len(registry.DATASETS)}")
    sql_datasets = {row[0]: (row[1], row[2]) for row in dataset_rows}
    py_datasets = {k: (v.job_module, v.description)
                   for k, v in registry.DATASETS.items()}
    _check("dataset_registry name set matches",
           set(sql_datasets.keys()) == set(py_datasets.keys()),
           f"sql={sorted(sql_datasets)}, py={sorted(py_datasets)}")
    for name, (job_module, description) in py_datasets.items():
        sql_row = sql_datasets.get(name)
        if sql_row is None:
            _check(f"dataset_registry[{name}] present in SQL", False)
            continue
        _check(f"dataset_registry[{name}].job_module matches",
               sql_row[0] == job_module,
               f"sql={sql_row[0]!r}, py={job_module!r}")
        _check(f"dataset_registry[{name}].description matches",
               sql_row[1] == description,
               f"sql={sql_row[1]!r}, py={description!r}")

    # ---- table_registry ----
    table_rows = _merge_registry_rows(
        "workflow_a_control.table_registry", "table_name",
    )
    _check("table_registry rows parsed",
           len(table_rows) == len(registry.TABLES),
           f"sql={len(table_rows)}, python={len(registry.TABLES)}")
    sql_tables = {row[0]: row for row in table_rows}
    _check("table_registry name set matches",
           set(sql_tables.keys()) == set(registry.TABLES.keys()),
           f"sql={sorted(sql_tables)}, py={sorted(registry.TABLES)}")
    for name, spec in registry.TABLES.items():
        sql_row = sql_tables.get(name)
        if sql_row is None:
            _check(f"table_registry[{name}] present in SQL", False)
            continue
        _check(f"table_registry[{name}].dataset_name matches",
               sql_row[1] == spec.dataset_name,
               f"sql={sql_row[1]!r}, py={spec.dataset_name!r}")
        _check(f"table_registry[{name}].schema matches",
               sql_row[2] == spec.schema,
               f"sql={sql_row[2]!r}, py={spec.schema!r}")
        _check(f"table_registry[{name}].retention_key_column matches",
               sql_row[3] == spec.retention_key_column,
               f"sql={sql_row[3]!r}, py={spec.retention_key_column!r}")
        _check(f"table_registry[{name}].description matches",
               sql_row[4] == spec.description,
               f"sql={sql_row[4]!r}, py={spec.description!r}")

    return _summary()


def _summary() -> int:
    print("")
    if FAILURES:
        print(f"FAIL — {len(FAILURES)} check(s) failed:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("OK — Python registry and final migration registry seeds are in sync.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
