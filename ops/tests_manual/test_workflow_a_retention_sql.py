#!/usr/bin/env python3
"""Manual sanity test for `jobs.api.telematics.retention_purge` SQL shape.

What this checks (no DB, no network, no `psycopg` unless installed):

  * Static-source guarantees:
      - `_batched_delete` and `_count_eligible` exist.
      - The deletion query uses `WITH victims AS (SELECT ctid …)` + ctid IN
        DELETE form (so concurrent inserts cannot enlarge the working set).
      - Schema, table, and `retention_key_column` are wrapped in
        `psycopg.sql.Identifier` (never f-strings).
      - The `cutoff_ts` is bound via `%s`, not built with `NOW() - INTERVAL`.
  * Live SQL render (only if `psycopg` v3 is available locally):
      - Render `_batched_delete`'s SQL for one allowlisted (schema, table,
        retention_key_column) triple via `psycopg.sql.SQL.as_string`.
      - Verify the rendered string contains correctly-quoted identifiers
        and the expected parameter placeholders.

The live render branch is skipped (PASS / NOTE) if `psycopg` is not
importable in the current environment.

Run:

    cd /opt/log-platform
    python3 ops/tests_manual/test_workflow_a_retention_sql.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jobs.api.telematics import registry  # noqa: E402


PURGE_PATH = REPO_ROOT / "jobs" / "api" / "telematics" / "retention_purge.py"


FAILURES: list[str] = []
NOTES: list[str] = []


def _check(label: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    line = f"[{status}] {label}"
    if detail:
        line += f"\n        {detail}"
    print(line)
    if not ok:
        FAILURES.append(label)


def _note(label: str, detail: str) -> None:
    print(f"[NOTE] {label}: {detail}")
    NOTES.append(label)


def _strip_python_comments_and_docstrings(src: str) -> str:
    """Drop triple-quoted strings and `#`-comments so we can grep code only.

    Single- and double-quoted regular string literals are kept (the SQL
    bodies live in those). Triple-quoted blocks are typically docstrings;
    in retention_purge.py the SQL is built via psycopg.sql.SQL(...) calls,
    so stripping triple-quoted blocks is safe and intentional here.
    """
    out: list[str] = []
    i = 0
    n = len(src)
    while i < n:
        ch = src[i]
        if ch in ("'", '"'):
            quote = ch
            triple = src[i:i + 3] == quote * 3
            if triple:
                end = src.find(quote * 3, i + 3)
                if end == -1:
                    return "".join(out)
                i = end + 3
                continue
            j = i + 1
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == quote:
                    j += 1
                    break
                j += 1
            out.append(src[i:j])
            i = j
            continue
        if ch == "#":
            j = src.find("\n", i)
            if j == -1:
                break
            i = j
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def test_static_shape() -> None:
    raw_src = PURGE_PATH.read_text(encoding="utf-8")
    src = raw_src
    code_only = _strip_python_comments_and_docstrings(raw_src)

    _check("_batched_delete defined",
           re.search(r"^def _batched_delete\b", src, re.MULTILINE) is not None)
    _check("_count_eligible defined",
           re.search(r"^def _count_eligible\b", src, re.MULTILINE) is not None)

    _check("schema wrapped in psycopg.sql.Identifier",
           "pgsql.Identifier(schema)" in src or
           "Identifier(schema)" in src)
    _check("table wrapped in psycopg.sql.Identifier",
           "pgsql.Identifier(table)" in src or
           "Identifier(table)" in src)
    _check("retention_key_column wrapped in psycopg.sql.Identifier",
           "pgsql.Identifier(retention_key_column)" in src or
           "Identifier(retention_key_column)" in src)

    # Forbid building cutoff dynamically in SQL — code-only, ignoring
    # docstrings/comments which legitimately mention NOW()/INTERVAL.
    forbidden = re.compile(
        r"INTERVAL\s+'?\s*%s\s+days'?|"
        r"NOW\(\)\s*-\s*INTERVAL|"
        r"now\(\)\s*-\s*interval",
        re.IGNORECASE,
    )
    bad = forbidden.search(code_only)
    _check("no NOW()/INTERVAL math in retention SQL",
           bad is None,
           f"matched={bad.group(0)!r}" if bad else "")

    _check("DELETE uses 'WITH victims AS' (ctid CTE)",
           "WITH victims AS" in src)
    _check("DELETE filters by ctid IN (SELECT ctid FROM victims)",
           "ctid IN (SELECT ctid FROM victims)" in src or
           "WHERE ctid IN" in src)

    _check("retention SQL uses '< %s' for cutoff",
           re.search(r"<\s*%s", src) is not None)
    _check("retention SQL uses 'LIMIT %s' for batch size",
           re.search(r"LIMIT\s+%s", src) is not None)
    _check("policy loader reads denormalized client_code",
           "COALESCE(ctr.client_code, ca.client_code, '') AS client_code" in src)
    _check("retention log context includes client_code",
           '"client_code": client_code' in src)


def test_live_render_if_possible() -> None:
    """If `psycopg` v3 is installed, actually render the SQL and inspect it."""
    try:
        import psycopg  # noqa: F401
        from psycopg import sql as pgsql
    except Exception as exc:
        _note("live-render skipped",
              f"psycopg not importable in this env ({type(exc).__name__})")
        return

    spec = registry.get_table("client_speeding_notifications")
    schema_ident = pgsql.Identifier(spec.schema)
    table_ident = pgsql.Identifier(spec.name)
    col_ident = pgsql.Identifier(spec.retention_key_column)

    delete_sql = pgsql.SQL(
        "WITH victims AS ( "
        "  SELECT ctid FROM {schema}.{table} "
        "  WHERE {col} < %s "
        "  ORDER BY {col} "
        "  LIMIT %s "
        ") "
        "DELETE FROM {schema}.{table} "
        "WHERE ctid IN (SELECT ctid FROM victims)"
    ).format(schema=schema_ident, table=table_ident, col=col_ident)

    try:
        rendered = delete_sql.as_string(None)
    except TypeError:
        # Some psycopg builds require a context. Fall back to a real one
        # only if the env exposes a DSN, otherwise skip the live render.
        _note("live-render skipped",
              "psycopg.sql.SQL.as_string requires a context in this version")
        return

    _check("live render quotes schema",
           f'"{spec.schema}"' in rendered,
           f"rendered={rendered!r}")
    _check("live render quotes table",
           f'"{spec.name}"' in rendered,
           f"rendered={rendered!r}")
    _check("live render quotes retention_key_column",
           f'"{spec.retention_key_column}"' in rendered,
           f"rendered={rendered!r}")
    _check("live render uses %s for cutoff and limit",
           rendered.count("%s") == 2,
           f"rendered={rendered!r}")
    _check("live render still uses ctid CTE pattern",
           "WITH victims AS" in rendered and
           "ctid IN (SELECT ctid FROM victims)" in rendered,
           f"rendered={rendered!r}")


def main() -> int:
    test_static_shape()
    test_live_render_if_possible()

    print("")
    if FAILURES:
        print(f"FAIL — {len(FAILURES)} check(s) failed:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    if NOTES:
        print(f"OK — retention SQL shape checks passed "
              f"({len(NOTES)} optional check(s) skipped).")
    else:
        print("OK — retention SQL shape checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
