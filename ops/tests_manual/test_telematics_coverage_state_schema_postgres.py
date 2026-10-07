#!/usr/bin/env python3
"""Focused C3 tests for the inert Telematics stabilized coverage schema.

Static migration-text checks always run. Set
TELEMATICS_COVERAGE_STATE_TEST_DSN only to a *disposable* PostgreSQL 16
database — never logdb — for the schema, constraint, partial-state and
inertness checks:

  docker run -d --rm --name c3-coverage-pg -e POSTGRES_PASSWORD=... \
      -e POSTGRES_USER=loguser -e POSTGRES_DB=c3_coverage_test \
      -p 55703:5432 postgres:16
  TELEMATICS_COVERAGE_STATE_TEST_DSN='postgresql://loguser:...@127.0.0.1:55703/c3_coverage_test' \
      .venv/bin/python ops/tests_manual/test_telematics_coverage_state_schema_postgres.py

This suite also owns the runtime-scope guard for the coverage vocabulary. It is
*narrowed*, never deleted, as delivery commits land:

  * C3 — no production Python may reference the new objects at all;
  * C4 — the pure helper may use the four arithmetic field names;
  * C5 — `coverage_windows.py` and `dispatcher.py` are the only production
    modules allowed to name coverage state or import the helper;
  * C6 — the pure helper gains only two plain carrier fields, while coverage
    locks and the two approved UPDATE shapes are allowed only inside the
    dispatcher's named compatibility finalizers;
  * C10/bootstrap tooling — the read-only audit may only `SELECT` coverage, and
    exactly one named function in `ops/bootstrap_telematics_trips_coverage.py`
    may `INSERT` one initial row. `UPDATE`, `DELETE`, upsert and coverage locks
    stay forbidden in both tools, so the bootstrap writer cannot grow into a
    second mutation surface beside C6.
  * cold start — a client that has never run cannot enter the evidence-based
    C10 path, so it gets its own reviewed pair of tools. The rule is repeated,
    not relaxed: `ops/audit_telematics_cold_start.py` may only `SELECT` coverage,
    and exactly one named function in
    `ops/bootstrap_telematics_cold_start_coverage.py` may `INSERT` one zero-width
    baseline row, in the *same* approved shape. `UPDATE`, `DELETE`, upsert and
    coverage locks stay forbidden in both, so a second seeding tool cannot
    become a second mutation surface either. Each seeding module authorizes
    exactly one function name, and neither module's name is accepted in the
    other. `ops/activate_telematics_trips_schedule.py` and
    `ops/telematics_cold_start_chain.py` speak coverage *vocabulary* only: like
    the manual recovery CLI they read state through the shared C11 module and
    may never name the coverage table.

The guard imports no job module and makes no provider request; it reads tracked
sources as text.
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ops.tests_manual.postgres_dsn_safety import (  # noqa: E402
    require_loopback_dsn_or_exit,
)

MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"
MIGRATION_NAME = "057_workflow_a_trips_coverage_state.sql"
MIGRATION_PATH = MIGRATIONS_DIR / MIGRATION_NAME

# Minimum prerequisite chain that reproduces the authoritative account,
# schedule and schedule-history schema this migration binds to.
PREREQUISITE_MIGRATIONS = (
    "008_workflow_a_control_plane.sql",
    "010_add_client_code.sql",
    "011_workflow_a_dataset_registry.sql",
    "012_workflow_a_client_dataset_schedule.sql",
    "013_workflow_a_client_table_retention.sql",
    "014_workflow_a_dispatcher_v1.sql",
    "017_workflow_a_add_client_code_to_control_tables.sql",
    "018_workflow_a_schedule_event_enrichment_mode.sql",
    "055_workflow_a_trips_pagination_mode.sql",
    "056_workflow_a_trips_stabilization_config.sql",
)

CLIENT_ID = "bd7662a5-eeb4-4614-8720-d477abfcb227"
CLIENT_CODE = "TST00001"
SCHEDULE_A = "7cac378a-5787-4d62-85d1-282bed208c8c"
SCHEDULE_B = "9c9c9261-2cf7-4b6f-8955-b515814be2f7"
ABSENT_SCHEDULE = "f25c8a6c-7ca5-4899-8a16-2490d9e5e241"

COVERAGE_COLUMNS = {
    "schedule_id": ("uuid", "NO", None),
    "client_id": ("uuid", "NO", None),
    "client_code": ("text", "YES", None),
    "dataset_name": ("text", "NO", None),
    "coverage_start_ts": ("timestamp with time zone", "YES", None),
    "covered_through_ts": ("timestamp with time zone", "YES", None),
    "bootstrap_status": ("text", "NO", "'UNINITIALIZED'::text"),
    "bootstrap_evidence_ref": ("text", "YES", None),
    "seeded_at": ("timestamp with time zone", "YES", None),
    "seeded_by": ("text", "YES", None),
    "covered_through_source": ("text", "NO", "'bootstrap'::text"),
    "last_gap_detected_ts": ("timestamp with time zone", "YES", None),
    "updated_at": ("timestamp with time zone", "NO", "now()"),
}

HISTORY_EVIDENCE_COLUMNS = {
    "nominal_window_start_ts": "timestamp with time zone",
    "nominal_window_end_ts": "timestamp with time zone",
    "stabilization_delay_seconds": "integer",
    "overlap_seconds": "integer",
    "trips_pagination_mode": "text",
}

PURE_C4_MODULE = Path("jobs/api/telematics/coverage_windows.py")
DISPATCHER_MODULE = Path("jobs/api/telematics/dispatcher.py")
COVERAGE_FINALIZATION_MODULE = Path("jobs/api/telematics/coverage_finalization.py")
AUDIT_MODULE = Path("ops/audit_telematics_coverage_bootstrap.py")
BOOTSTRAP_WRITER_MODULE = Path("ops/bootstrap_telematics_trips_coverage.py")
RECOVERY_TOOL_MODULE = Path("ops/recover_telematics_trips_window.py")
COLD_START_AUDIT_MODULE = Path("ops/audit_telematics_cold_start.py")
COLD_START_WRITER_MODULE = Path("ops/bootstrap_telematics_cold_start_coverage.py")
COLD_START_CHAIN_MODULE = Path("ops/telematics_cold_start_chain.py")
ACTIVATION_TOOL_MODULE = Path("ops/activate_telematics_trips_schedule.py")
ONBOARDING_MODULE = Path("scripts/onboard_workflow_a_client.py")
# The one function permitted to hold a coverage INSERT in the historical C10
# writer, and the one permitted in the cold-start writer. Each name is
# authorized in exactly one module; see
# COVERAGE_INSERT_AUTHORIZED_FUNCTION_BY_MODULE.
APPROVED_INSERT_FUNCTION = "_insert_initial_coverage_row"
APPROVED_COLD_START_INSERT_FUNCTION = "_insert_baseline_row"
# The one function permitted to hold a coverage advancement UPDATE, anywhere.
APPROVED_ADVANCE_FUNCTION = "advance_covered_through_cas"
APPROVED_COVERAGE_LOCK_FUNCTION = "lock_coverage_row_for_update"
TEST_ONLY_PYTHON_PREFIXES = (("ops", "tests_manual"),)
C3_RUNTIME_IDENTIFIERS = (
    "client_dataset_coverage",
    "coverage_start_ts",
    "covered_through_ts",
    "bootstrap_status",
    "bootstrap_evidence_ref",
    "covered_through_source",
    "last_gap_detected_ts",
    "nominal_window_start_ts",
    "nominal_window_end_ts",
)

# C5 authorizes exactly two production modules to speak coverage vocabulary:
# the pure gate/arithmetic helper and the dispatcher that integrates it. Every
# other production file — provider client, provider safety, the sync job,
# artifacts, API routes, unrelated jobs, scripts and any future top-level
# package — stays forbidden by default, because discovery enumerates Git's whole
# tracked Python surface rather than a hand-written list.
#
# `covered_through_source` is intentionally absent from BOTH allowlists: C5
# never records watermark provenance, so the guard proves that statically.
PURE_C5_ALLOWED_IDENTIFIERS = frozenset({
    "coverage_start_ts",
    "covered_through_ts",
    "nominal_window_start_ts",
    "nominal_window_end_ts",
    "bootstrap_status",
    "bootstrap_evidence_ref",
    "covered_through_source",
    "last_gap_detected_ts",
})
# The dispatcher owns the read SELECT and the two function-scoped C6 writers.
# Every other production module remains forbidden by default.
DISPATCHER_C5_ALLOWED_IDENTIFIERS = PURE_C5_ALLOWED_IDENTIFIERS | frozenset({
    "client_dataset_coverage",
})
# The two bootstrap tools speak the same vocabulary: the audit reads it and the
# writer seeds it once. Neither may import the pure runtime helper — bootstrap
# is an operator gate, not a second consumer of the dispatcher's window
# arithmetic.
BOOTSTRAP_TOOL_ALLOWED_IDENTIFIERS = DISPATCHER_C5_ALLOWED_IDENTIFIERS
# C11 adds the narrowly shared advancement CAS. It names the coverage table
# because it holds the one authorized lock and the one authorized advancement
# UPDATE; the function-scoped rules below are what actually confine it.
COVERAGE_FINALIZATION_ALLOWED_IDENTIFIERS = DISPATCHER_C5_ALLOWED_IDENTIFIERS
# The manual recovery CLI speaks coverage *vocabulary* — it prints a snapshot,
# validates the expected watermark and fingerprints the row — but it must never
# name the coverage table, because every statement it needs belongs to the
# shared finalizer. Omitting `client_dataset_coverage` from its allowlist proves
# that statically, independently of the SQL rules below.
RECOVERY_TOOL_ALLOWED_IDENTIFIERS = PURE_C5_ALLOWED_IDENTIFIERS
# The cold-start tools are registered with their *exact* vocabulary, not with a
# shared bootstrap set, because each one's omissions are load-bearing.
#
# The cold-start audit proves a client is provably empty. It counts coverage
# rows and reports the baseline bounds the writer will need, so it names the
# table and the two bounds — and nothing else. `bootstrap_status` in particular
# stays forbidden: reading a status would presuppose a coverage row, which
# contradicts the zero-state gate this tool exists to prove.
COLD_START_AUDIT_ALLOWED_IDENTIFIERS = frozenset({
    "client_dataset_coverage",
    "coverage_start_ts",
    "covered_through_ts",
})
# The cold-start writer seeds one zero-width `A == W` baseline and reads it
# back byte-for-byte, so it names every column it writes plus
# `last_gap_detected_ts`, which it asserts stayed NULL. The two
# `nominal_window_*` names stay forbidden: they are schedule-history claim-time
# evidence and this tool writes no history.
COLD_START_WRITER_ALLOWED_IDENTIFIERS = frozenset({
    "client_dataset_coverage",
    "coverage_start_ts",
    "covered_through_ts",
    "bootstrap_status",
    "bootstrap_evidence_ref",
    "covered_through_source",
    "last_gap_detected_ts",
})
# Activation verifies coverage read-only and then flips one schedule boolean.
# Like the recovery CLI it reads through `coverage_finalization.read_coverage_row`,
# so `client_dataset_coverage` is omitted on purpose and that omission is what
# proves statically that it holds no coverage statement of its own.
ACTIVATION_TOOL_ALLOWED_IDENTIFIERS = frozenset({
    "coverage_start_ts",
    "covered_through_ts",
    "bootstrap_status",
    "covered_through_source",
})
# The pure chain helper opens no connection and issues no SQL. It carries the
# current watermark as one argument name and nothing else.
COLD_START_CHAIN_ALLOWED_IDENTIFIERS = frozenset({
    "covered_through_ts",
})
# Onboarding names the coverage *table* and nothing else, in exactly one
# read-only `SELECT count(*)`: a coverage row is one of the four kinds of
# progress that must refuse a fresh-client creation. It reads no coverage
# column, holds no lock, and issues no INSERT, UPDATE or DELETE — the AST rules
# below still apply to it unchanged, so an onboarding coverage *statement* would
# be a guard failure exactly as it is anywhere else.
ONBOARDING_ALLOWED_IDENTIFIERS = frozenset({
    "client_dataset_coverage",
})
C5_ALLOWED_IDENTIFIERS_BY_MODULE = {
    PURE_C4_MODULE: PURE_C5_ALLOWED_IDENTIFIERS,
    DISPATCHER_MODULE: DISPATCHER_C5_ALLOWED_IDENTIFIERS,
    COVERAGE_FINALIZATION_MODULE: COVERAGE_FINALIZATION_ALLOWED_IDENTIFIERS,
    AUDIT_MODULE: BOOTSTRAP_TOOL_ALLOWED_IDENTIFIERS,
    BOOTSTRAP_WRITER_MODULE: BOOTSTRAP_TOOL_ALLOWED_IDENTIFIERS,
    RECOVERY_TOOL_MODULE: RECOVERY_TOOL_ALLOWED_IDENTIFIERS,
    COLD_START_AUDIT_MODULE: COLD_START_AUDIT_ALLOWED_IDENTIFIERS,
    COLD_START_WRITER_MODULE: COLD_START_WRITER_ALLOWED_IDENTIFIERS,
    ACTIVATION_TOOL_MODULE: ACTIVATION_TOOL_ALLOWED_IDENTIFIERS,
    COLD_START_CHAIN_MODULE: COLD_START_CHAIN_ALLOWED_IDENTIFIERS,
    ONBOARDING_MODULE: ONBOARDING_ALLOWED_IDENTIFIERS,
}
# Exactly two modules may hold a coverage INSERT, each confined to exactly one
# named function and to the one approved one-shot shape. The mapping is by exact
# path — there is no directory, prefix or wildcard form — and a function name
# authorized in one module is not authorized in the other.
COVERAGE_INSERT_AUTHORIZED_FUNCTION_BY_MODULE = {
    BOOTSTRAP_WRITER_MODULE: APPROVED_INSERT_FUNCTION,
    COLD_START_WRITER_MODULE: APPROVED_COLD_START_INSERT_FUNCTION,
}
COVERAGE_HELPER_IMPORT_AUTHORIZED = frozenset({PURE_C4_MODULE, DISPATCHER_MODULE})

# Coverage locks and coverage UPDATEs are confined to these exact functions.
# The scheduled success path no longer holds either: it delegates to the shared
# module, so `_finalize_compat_success` is deliberately absent here and a
# coverage statement reappearing in it is a guard failure.
COVERAGE_SQL_AUTHORIZED_FUNCTIONS = {
    DISPATCHER_MODULE: frozenset({"_finalize_compat_gap"}),
    COVERAGE_FINALIZATION_MODULE: frozenset({
        APPROVED_COVERAGE_LOCK_FUNCTION,
        APPROVED_ADVANCE_FUNCTION,
    }),
}
# The advancement UPDATE must bind its provenance, never inline it: a literal
# source would let one caller silently write the other caller's provenance.
FORBIDDEN_INLINE_SOURCE_LITERALS = (
    "'BOOTSTRAP'", "'SCHEDULED_RUN'", "'OPERATOR'", "'MANUAL_RECOVERY'",
)

COVERAGE_INSERT_PATTERN = (
    r"\bINSERT\s+INTO\s+(?:WORKFLOW_A_CONTROL\.)?CLIENT_DATASET_COVERAGE\b"
)
COVERAGE_DELETE_PATTERN = (
    r"\bDELETE\s+FROM\s+(?:WORKFLOW_A_CONTROL\.)?CLIENT_DATASET_COVERAGE\b"
)
# The exact approved initial-insert shape. Columns that must be present, and
# constructs that must never appear: an upsert would turn the one-shot bootstrap
# into a silent repair path, and writing `last_gap_detected_ts` at bootstrap
# would fabricate a gap observation that no run ever made.
APPROVED_INSERT_REQUIRED = (
    "SCHEDULE_ID",
    "CLIENT_ID",
    "DATASET_NAME",
    "COVERAGE_START_TS",
    "COVERED_THROUGH_TS",
    "BOOTSTRAP_STATUS",
    "BOOTSTRAP_EVIDENCE_REF",
    "SEEDED_AT",
    "SEEDED_BY",
    "COVERED_THROUGH_SOURCE",
)
APPROVED_INSERT_FORBIDDEN = (
    "ON CONFLICT",
    "DO UPDATE",
    "DO NOTHING",
    "LAST_GAP_DETECTED_TS",
)


def _is_approved_bootstrap_insert(sql_upper: str) -> bool:
    if any(token not in sql_upper for token in APPROVED_INSERT_REQUIRED):
        return False
    return not any(token in sql_upper for token in APPROVED_INSERT_FORBIDDEN)



# ---------------------------------------------------------------------------
# Static contract — runs without a database
# ---------------------------------------------------------------------------

def test_migration_file_contract() -> None:
    migration_files = sorted(path.name for path in MIGRATIONS_DIR.glob("*.sql"))
    assert MIGRATION_NAME in migration_files
    assert migration_files.index(MIGRATION_NAME) == (
        migration_files.index("056_workflow_a_trips_stabilization_config.sql") + 1
    ), "057 must be the next ordinal after 056"
    assert not [
        name for name in migration_files
        if name.startswith("057_") and name != MIGRATION_NAME
    ], "ordinal 057 must be occupied by exactly one migration"

    sql = MIGRATION_PATH.read_text(encoding="utf-8")
    for fragment in (
        "CREATE TABLE IF NOT EXISTS workflow_a_control.client_dataset_coverage",
        "pk_client_dataset_coverage PRIMARY KEY (schedule_id)",
        "fk_client_dataset_coverage_schedule",
        "ON DELETE CASCADE",
        "ck_client_dataset_coverage_bootstrap_status",
        "'UNINITIALIZED', 'READY', 'GAP_DETECTED', 'RESEED_REQUIRED'",
        "ck_client_dataset_coverage_covered_through_source",
        "ck_client_dataset_coverage_bounds_order",
        "ck_client_dataset_coverage_ready_complete",
        "idx_client_dataset_coverage_attention",
        "ADD COLUMN IF NOT EXISTS nominal_window_start_ts TIMESTAMPTZ",
        "ADD COLUMN IF NOT EXISTS nominal_window_end_ts TIMESTAMPTZ",
        "ADD COLUMN IF NOT EXISTS stabilization_delay_seconds INTEGER",
        "ADD COLUMN IF NOT EXISTS overlap_seconds INTEGER",
        "ADD COLUMN IF NOT EXISTS trips_pagination_mode TEXT",
        "ck_run_history_nominal_window_order",
        "ck_run_history_stabilization_delay_seconds",
        "ck_run_history_overlap_seconds",
        "ck_run_history_trips_pagination_mode",
        "a.atthasdef",
        "expected no default for claim-time evidence",
    ):
        assert fragment in sql, fragment

    # The repository migration runner does not wrap a whole file in one
    # transaction. Existing history columns therefore have to be validated
    # before any C3 object can be created or altered.
    history_preflight = sql.index("a.atthasdef")
    assert history_preflight < sql.index(
        "CREATE TABLE IF NOT EXISTS workflow_a_control.client_dataset_coverage"
    )
    assert history_preflight < sql.index(
        "ALTER TABLE workflow_a_control.client_schedule_run_history"
    )

    upper = sql.upper()
    for forbidden in (
        "DROP TABLE",
        "DROP COLUMN",
        "TRUNCATE",
        "DELETE FROM",
        "CREATE TRIGGER",
        "CREATE OR REPLACE FUNCTION",
        "CREATE FUNCTION",
        "CREATE PROCEDURE",
        "INSERT INTO",
        "GRANT ",
        "OWNER TO",
        # C3 must not touch the C1/C2 client configuration surface.
        "ALTER TABLE WORKFLOW_A_CONTROL.CLIENT_ACCOUNT",
    ):
        assert forbidden not in upper, forbidden

    # No existing history or coverage row may be rewritten by this migration.
    assert "UPDATE WORKFLOW_A_CONTROL" not in upper
    # No client is enabled for compatibility mode by C3.
    assert "SET TRIPS_PAGINATION_MODE" not in upper
    # The maximum-recovery-span field is C2 configuration, not C3 evidence.
    assert "trips_max_recovery_span_seconds" not in sql

    # Migrations 054-056 must be byte-unchanged; assert they are not referenced
    # for modification here.
    for earlier in (
        "054_environment_identity_resume_contract.sql",
        "055_workflow_a_trips_pagination_mode.sql",
        "056_workflow_a_trips_stabilization_config.sql",
    ):
        assert (MIGRATIONS_DIR / earlier).exists()


def _is_test_only_python_path(relative: Path) -> bool:
    return any(
        relative.parts[:len(prefix)] == prefix
        for prefix in TEST_ONLY_PYTHON_PREFIXES
    )


def _tracked_production_python_paths() -> tuple[Path, ...]:
    """Return every tracked runtime-capable Python path in the repository.

    Enumerating Git's tracked surface makes a new top-level production package
    guarded by default. C5 can authorize integration by changing the small
    explicit identifier/import allowlists, without replacing discovery.
    """
    completed = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.py"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        check=True,
    )
    paths = (
        Path(raw.decode("utf-8"))
        for raw in completed.stdout.split(b"\0")
        if raw
    )
    return tuple(path for path in paths if not _is_test_only_python_path(path))


def _scan_runtime_python_sources(
    sources: dict[Path, str],
) -> tuple[list[str], list[str], list[str]]:
    """Reject coverage SQL except the two named dispatcher finalizers."""
    offenders: list[str] = []
    importers: list[str] = []
    advancement: list[str] = []
    for relative, text in sources.items():
        if _is_test_only_python_path(relative):
            continue
        allowed = C5_ALLOWED_IDENTIFIERS_BY_MODULE.get(relative, frozenset())
        for name in C3_RUNTIME_IDENTIFIERS:
            if name in text and name not in allowed:
                offenders.append(f"{relative}:{name}")
        if (
            relative not in COVERAGE_HELPER_IMPORT_AUTHORIZED
            and "coverage_windows" in text
        ):
            importers.append(str(relative))

        upper = text.upper()
        names_coverage = "CLIENT_DATASET_COVERAGE" in upper
        # `DELETE` has no authorized surface anywhere. `INSERT` has exactly two
        # modules, each narrowed to one function and one shape by the AST pass
        # below.
        if re.search(COVERAGE_DELETE_PATTERN, upper):
            advancement.append(f"{relative}:coverage_delete")
        if (
            re.search(COVERAGE_INSERT_PATTERN, upper)
            and relative not in COVERAGE_INSERT_AUTHORIZED_FUNCTION_BY_MODULE
        ):
            advancement.append(f"{relative}:coverage_insert")

        if relative == PURE_C4_MODULE:
            if names_coverage:
                advancement.append(f"{relative}:pure_helper_io")
            continue

        try:
            tree = ast.parse(text)
        except SyntaxError:
            if names_coverage and re.search(r"\bUPDATE\b|\bFOR\s+UPDATE\b", upper):
                advancement.append(f"{relative}:coverage_sql_outside_finalizer")
            continue

        # A coverage SQL literal that sits outside every function body would
        # escape the function-scoped rules below, so it is checked explicitly.
        function_scoped: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for child in ast.walk(node):
                    if isinstance(child, ast.Constant):
                        function_scoped.add(id(child))
        for child in ast.walk(tree):
            if (
                isinstance(child, ast.Constant)
                and isinstance(child.value, str)
                and "client_dataset_coverage" in child.value
                and id(child) not in function_scoped
                and re.search(COVERAGE_INSERT_PATTERN, child.value.upper())
            ):
                advancement.append(f"{relative}:coverage_insert_at_module_scope")

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            sql_literals = [
                child.value
                for child in ast.walk(node)
                if isinstance(child, ast.Constant)
                and isinstance(child.value, str)
                and "client_dataset_coverage" in child.value
            ]
            for sql in sql_literals:
                sql_upper = sql.upper()
                is_lock = bool(re.search(r"\bFOR\s+UPDATE\b", sql_upper))
                is_update = bool(re.search(
                    r"\bUPDATE\s+WORKFLOW_A_CONTROL\.CLIENT_DATASET_COVERAGE\b",
                    sql_upper,
                ))
                is_insert = bool(re.search(COVERAGE_INSERT_PATTERN, sql_upper))
                if is_insert:
                    if COVERAGE_INSERT_AUTHORIZED_FUNCTION_BY_MODULE.get(
                        relative
                    ) != node.name:
                        advancement.append(
                            f"{relative}:coverage_insert_outside_approved_writer"
                            f":{node.name}"
                        )
                    elif not _is_approved_bootstrap_insert(sql_upper):
                        advancement.append(
                            f"{relative}:invalid_bootstrap_coverage_insert"
                        )
                    continue
                if not (is_lock or is_update):
                    continue
                authorized = COVERAGE_SQL_AUTHORIZED_FUNCTIONS.get(
                    relative, frozenset()
                )
                if node.name not in authorized:
                    advancement.append(
                        f"{relative}:coverage_sql_outside_finalizer:{node.name}"
                    )
                    continue
                if is_update and node.name == APPROVED_ADVANCE_FUNCTION:
                    if (
                        "SET COVERED_THROUGH_TS" not in sql_upper
                        or "COVERED_THROUGH_SOURCE = %(NEW_COVERED_THROUGH_SOURCE)S"
                        not in sql_upper
                        or "SET COVERAGE_START_TS" in sql_upper
                        or "SET BOOTSTRAP_STATUS" in sql_upper
                        or any(
                            literal in sql_upper
                            for literal in FORBIDDEN_INLINE_SOURCE_LITERALS
                        )
                    ):
                        advancement.append(
                            f"{relative}:invalid_advance_coverage_update"
                        )
                if is_update and node.name == APPROVED_COVERAGE_LOCK_FUNCTION:
                    advancement.append(
                        f"{relative}:coverage_update_in_lock_helper"
                    )
                if is_update and node.name == "_finalize_compat_gap":
                    if (
                        "SET BOOTSTRAP_STATUS = 'GAP_DETECTED'" not in sql_upper
                        or "LAST_GAP_DETECTED_TS =" not in sql_upper
                        or "SET COVERED_THROUGH_TS" in sql_upper
                        or "SET COVERAGE_START_TS" in sql_upper
                    ):
                        advancement.append(
                            f"{relative}:invalid_gap_coverage_update"
                        )
    return offenders, importers, advancement


def test_no_runtime_reference_to_new_objects() -> None:
    """C6 keeps coverage access confined to the pure helper and dispatcher."""
    paths = _tracked_production_python_paths()
    assert Path("artifacts/layout.py") in paths
    assert PURE_C4_MODULE in paths
    assert DISPATCHER_MODULE in paths
    sources = {
        relative: (REPO_ROOT / relative).read_text(
            encoding="utf-8", errors="replace"
        )
        for relative in paths
    }
    offenders, importers, advancement = _scan_runtime_python_sources(sources)
    assert not offenders, f"unauthorized coverage vocabulary: {offenders}"
    assert not importers, f"unauthorized coverage_windows users: {importers}"
    assert not advancement, f"unauthorized coverage SQL: {advancement}"

    dispatcher_text = sources[DISPATCHER_MODULE]
    assert "client_dataset_coverage" in dispatcher_text
    assert "coverage_windows" in dispatcher_text
    assert "def _finalize_compat_success(" in dispatcher_text
    assert "def _finalize_compat_gap(" in dispatcher_text

    # C11 — the shared advancement CAS and its two reviewed callers.
    assert COVERAGE_FINALIZATION_MODULE in paths
    assert RECOVERY_TOOL_MODULE in paths
    finalization_text = sources[COVERAGE_FINALIZATION_MODULE]
    recovery_text = sources[RECOVERY_TOOL_MODULE]
    assert f"def {APPROVED_ADVANCE_FUNCTION}(" in finalization_text
    assert f"def {APPROVED_COVERAGE_LOCK_FUNCTION}(" in finalization_text
    # The shared module must not become a second consumer of the pure helper.
    assert "coverage_windows" not in finalization_text
    # The recovery CLI never names the coverage table and holds no coverage SQL.
    assert "client_dataset_coverage" not in recovery_text
    assert APPROVED_ADVANCE_FUNCTION in recovery_text
    # The recovery CLI never touches schedule history beyond reading it.
    recovery_upper = recovery_text.upper()
    for forbidden in (
        "UPDATE WORKFLOW_A_CONTROL.CLIENT_SCHEDULE_RUN_HISTORY",
        "INSERT INTO WORKFLOW_A_CONTROL.CLIENT_SCHEDULE_RUN_HISTORY",
        "DELETE FROM WORKFLOW_A_CONTROL.CLIENT_SCHEDULE_RUN_HISTORY",
        "UPDATE WORKFLOW_A_CONTROL.CLIENT_ACCOUNT",
        "UPDATE WORKFLOW_A_CONTROL.CLIENT_DATASET_SCHEDULE",
    ):
        assert forbidden not in recovery_upper, forbidden

    pure_text = sources[PURE_C4_MODULE]
    assert "covered_through_source" in pure_text
    assert "last_gap_detected_ts" in pure_text
    assert "client_dataset_coverage" not in pure_text

    # The bootstrap tooling exists, and only the writer holds an INSERT.
    assert AUDIT_MODULE in paths and BOOTSTRAP_WRITER_MODULE in paths
    audit_text = sources[AUDIT_MODULE]
    writer_text = sources[BOOTSTRAP_WRITER_MODULE]
    assert not re.search(COVERAGE_INSERT_PATTERN, audit_text.upper()), (
        "the read-only audit must never contain a coverage INSERT")
    assert "--execute" not in audit_text, (
        "the audit tool must expose no execution switch")
    assert f"def {APPROVED_INSERT_FUNCTION}(" in writer_text
    assert re.search(COVERAGE_INSERT_PATTERN, writer_text.upper())
    assert "coverage_windows" not in audit_text
    assert "coverage_windows" not in writer_text

    # Cold start — the same two-tool shape, proved against the real sources.
    for relative in (
        COLD_START_AUDIT_MODULE,
        COLD_START_WRITER_MODULE,
        ACTIVATION_TOOL_MODULE,
        COLD_START_CHAIN_MODULE,
    ):
        assert relative in paths, relative
        # None of them may become a second consumer of the window arithmetic.
        assert "coverage_windows" not in sources[relative], relative

    cold_audit_text = sources[COLD_START_AUDIT_MODULE]
    cold_writer_text = sources[COLD_START_WRITER_MODULE]
    activation_text = sources[ACTIVATION_TOOL_MODULE]
    chain_text = sources[COLD_START_CHAIN_MODULE]

    # The cold-start audit is permanently read-only: it names the coverage table
    # but holds no INSERT and exposes no execution switch at all.
    assert "client_dataset_coverage" in cold_audit_text
    assert not re.search(COVERAGE_INSERT_PATTERN, cold_audit_text.upper()), (
        "the cold-start audit must never contain a coverage INSERT")
    assert "--execute" not in cold_audit_text, (
        "the cold-start audit must expose no execution switch")

    # The cold-start writer holds exactly one INSERT, in its one named function.
    assert f"def {APPROVED_COLD_START_INSERT_FUNCTION}(" in cold_writer_text
    assert re.search(COVERAGE_INSERT_PATTERN, cold_writer_text.upper())
    # The two seeding writers stay distinct surfaces: neither carries the
    # other's approved function name.
    assert APPROVED_INSERT_FUNCTION not in cold_writer_text
    assert APPROVED_COLD_START_INSERT_FUNCTION not in writer_text

    # Activation verifies coverage through the shared C11 reader, never by
    # naming the table, and it changes exactly one schedule field.
    assert "client_dataset_coverage" not in activation_text
    assert "read_coverage_row" in activation_text
    activation_upper = activation_text.upper()
    for forbidden in (
        "UPDATE WORKFLOW_A_CONTROL.CLIENT_ACCOUNT",
        "UPDATE WORKFLOW_A_CONTROL.CLIENT_SCHEDULE_RUN_HISTORY",
        "INSERT INTO WORKFLOW_A_CONTROL.CLIENT_SCHEDULE_RUN_HISTORY",
        "DELETE FROM WORKFLOW_A_CONTROL.CLIENT_SCHEDULE_RUN_HISTORY",
    ):
        assert forbidden not in activation_upper, forbidden

    # The chain helper is pure: no table name and no SQL of any kind.
    assert "client_dataset_coverage" not in chain_text
    chain_upper = chain_text.upper()
    for forbidden in ("INSERT INTO", "DELETE FROM", "FOR UPDATE", "PSYCOPG"):
        assert forbidden not in chain_upper, forbidden


def test_runtime_inertness_guard_synthetic_cases() -> None:
    """Prove approved finalizer SQL passes and equivalent writers fail."""
    pure = (
        "covered_through_source = None\n"
        "last_gap_detected_ts = None\n"
    )
    _, _, violations = _scan_runtime_python_sources({PURE_C4_MODULE: pure})
    assert not violations
    _, _, violations = _scan_runtime_python_sources({
        PURE_C4_MODULE: "sql = 'UPDATE client_dataset_coverage SET x = 1'\n",
    })
    assert violations

    advance_sql = """
def advance_covered_through_cas():
    write = '''UPDATE workflow_a_control.client_dataset_coverage
                  SET covered_through_ts = %(new_covered_through_ts)s,
                      covered_through_source = %(new_covered_through_source)s,
                      updated_at = %(mutation_ts)s'''
"""
    lock_sql = """
def lock_coverage_row_for_update():
    lock = '''SELECT * FROM workflow_a_control.client_dataset_coverage
              FOR UPDATE'''
"""
    gap_sql = """
def _finalize_compat_gap():
    lock = '''SELECT * FROM workflow_a_control.client_dataset_coverage
              FOR UPDATE'''
    write = '''UPDATE workflow_a_control.client_dataset_coverage
                  SET bootstrap_status = 'GAP_DETECTED',
                      last_gap_detected_ts = %s,
                      updated_at = %s'''
"""
    for approved in (advance_sql, lock_sql):
        forbidden, importers, violations = _scan_runtime_python_sources({
            COVERAGE_FINALIZATION_MODULE: approved,
        })
        assert not forbidden and not importers and not violations, violations
    forbidden, importers, violations = _scan_runtime_python_sources({
        DISPATCHER_MODULE: gap_sql,
    })
    assert not forbidden and not importers and not violations, violations

    # The scheduled success finalizer must delegate: coverage SQL reappearing
    # in `_finalize_compat_success` is now itself a violation.
    for reintroduced in (
        advance_sql.replace(
            APPROVED_ADVANCE_FUNCTION, "_finalize_compat_success",
        ),
        lock_sql.replace(
            APPROVED_COVERAGE_LOCK_FUNCTION, "_finalize_compat_success",
        ),
    ):
        _, _, violations = _scan_runtime_python_sources({
            DISPATCHER_MODULE: reintroduced,
        })
        assert violations

    # Right module, wrong function.
    _, _, violations = _scan_runtime_python_sources({
        COVERAGE_FINALIZATION_MODULE: advance_sql.replace(
            APPROVED_ADVANCE_FUNCTION, "_bump_watermark",
        ),
    })
    assert violations

    # Right module and function, wrong shape.
    for mutated, label in (
        (
            advance_sql.replace(
                "covered_through_source = %(new_covered_through_source)s",
                "covered_through_source = 'manual_recovery'",
            ),
            "inlined provenance literal",
        ),
        (
            advance_sql.replace(
                "SET covered_through_ts", "SET coverage_start_ts",
            ),
            "moves the lower bound",
        ),
        (
            advance_sql.replace(
                "SET covered_through_ts = %(new_covered_through_ts)s,",
                "SET bootstrap_status = 'READY',\n"
                "                      covered_through_ts"
                " = %(new_covered_through_ts)s,",
            ),
            "forces a bootstrap status",
        ),
    ):
        _, _, violations = _scan_runtime_python_sources({
            COVERAGE_FINALIZATION_MODULE: mutated,
        })
        assert violations, label

    # The lock helper may never carry an UPDATE.
    _, _, violations = _scan_runtime_python_sources({
        COVERAGE_FINALIZATION_MODULE:
            f"def {APPROVED_COVERAGE_LOCK_FUNCTION}(cur):\n"
            "    cur.execute('UPDATE workflow_a_control.client_dataset_coverage"
            " SET covered_through_ts = %(new_covered_through_ts)s')\n",
    })
    assert violations

    # The approved advancement is rejected in the recovery CLI itself, which
    # must call the shared function rather than hold the statement.
    for relative in (RECOVERY_TOOL_MODULE, AUDIT_MODULE, BOOTSTRAP_WRITER_MODULE):
        _, _, violations = _scan_runtime_python_sources({relative: advance_sql})
        assert violations, relative
    forbidden, _, _ = _scan_runtime_python_sources({
        RECOVERY_TOOL_MODULE: "table = 'client_dataset_coverage'\n",
    })
    assert forbidden, "the recovery CLI must not name the coverage table"

    writer = """
def write_coverage():
    sql = '''UPDATE workflow_a_control.client_dataset_coverage
                SET covered_through_ts = %s'''
"""
    for relative in (
        Path("jobs/api/telematics/provider_client.py"),
        Path("jobs/api/telematics/sync_trips_and_speeding.py"),
        Path("future_runtime/coverage_writer.py"),
    ):
        _, _, violations = _scan_runtime_python_sources({relative: writer})
        assert violations, relative

    for statement in (
        "INSERT INTO workflow_a_control.client_dataset_coverage (schedule_id) VALUES (1)",
        "DELETE FROM workflow_a_control.client_dataset_coverage",
    ):
        _, _, violations = _scan_runtime_python_sources({
            DISPATCHER_MODULE: f"sql = {statement!r}\n",
        })
        assert violations

    _, _, violations = _scan_runtime_python_sources({
        Path("ops/suspected_bug_email_worker.py"):
            "def claim():\n    sql = 'FOR UPDATE SKIP LOCKED'\n",
    })
    assert not violations


APPROVED_BOOTSTRAP_INSERT_SOURCE = """
def _insert_initial_coverage_row(cur, params):
    cur.execute('''
        INSERT INTO workflow_a_control.client_dataset_coverage (
            schedule_id, client_id, client_code, dataset_name,
            coverage_start_ts, covered_through_ts,
            bootstrap_status, bootstrap_evidence_ref,
            seeded_at, seeded_by, covered_through_source, updated_at
        ) VALUES (
            %(schedule_id)s, %(client_id)s, %(client_code)s, %(dataset_name)s,
            %(coverage_start_ts)s, %(covered_through_ts)s,
            %(bootstrap_status)s, %(bootstrap_evidence_ref)s,
            %(seeded_at)s, %(seeded_by)s, %(covered_through_source)s,
            %(updated_at)s
        )
    ''', params)
    return cur.rowcount
"""


def test_bootstrap_writer_guard_synthetic_cases() -> None:
    """Exactly one module, one function and one shape may insert coverage."""
    # The approved shape passes in the named writer function.
    forbidden, importers, violations = _scan_runtime_python_sources({
        BOOTSTRAP_WRITER_MODULE: APPROVED_BOOTSTRAP_INSERT_SOURCE,
    })
    assert not forbidden and not importers and not violations, violations

    # The same INSERT is rejected everywhere else — dispatcher, provider, sync,
    # API, the read-only audit and any unknown future package.
    for relative in (
        DISPATCHER_MODULE,
        Path("jobs/api/telematics/provider_client.py"),
        Path("jobs/api/telematics/sync_trips_and_speeding.py"),
        Path("api/main.py"),
        AUDIT_MODULE,
        Path("future_runtime/coverage_seeder.py"),
    ):
        _, _, violations = _scan_runtime_python_sources({
            relative: APPROVED_BOOTSTRAP_INSERT_SOURCE,
        })
        assert violations, relative

    # Right module, wrong function.
    _, _, violations = _scan_runtime_python_sources({
        BOOTSTRAP_WRITER_MODULE: APPROVED_BOOTSTRAP_INSERT_SOURCE.replace(
            "_insert_initial_coverage_row", "_seed_coverage_row",
        ),
    })
    assert violations

    # Right module and function, wrong shape.
    for mutated, label in (
        (
            APPROVED_BOOTSTRAP_INSERT_SOURCE.replace(
                "%(updated_at)s\n        )",
                "%(updated_at)s\n        ) ON CONFLICT (schedule_id) DO UPDATE"
                " SET covered_through_ts = EXCLUDED.covered_through_ts",
            ),
            "upsert",
        ),
        (
            APPROVED_BOOTSTRAP_INSERT_SOURCE.replace(
                "covered_through_source, updated_at",
                "covered_through_source, last_gap_detected_ts",
            ),
            "writes last_gap_detected_ts",
        ),
        (
            APPROVED_BOOTSTRAP_INSERT_SOURCE
            .replace("bootstrap_evidence_ref,\n", "")
            .replace("%(bootstrap_evidence_ref)s,\n", ""),
            "omits the evidence reference",
        ),
    ):
        _, _, violations = _scan_runtime_python_sources({
            BOOTSTRAP_WRITER_MODULE: mutated,
        })
        assert violations, label

    # An INSERT hoisted to module scope cannot escape the function rules.
    _, _, violations = _scan_runtime_python_sources({
        BOOTSTRAP_WRITER_MODULE:
            "SQL = '''INSERT INTO workflow_a_control.client_dataset_coverage "
            "(schedule_id) VALUES (%s)'''\n",
    })
    assert violations

    # The writer may never UPDATE, DELETE or lock coverage, even in its own
    # approved function: every later mutation belongs to C6.
    for statement, label in (
        (
            "UPDATE workflow_a_control.client_dataset_coverage "
            "SET covered_through_ts = %s",
            "update",
        ),
        (
            "DELETE FROM workflow_a_control.client_dataset_coverage "
            "WHERE schedule_id = %s",
            "delete",
        ),
        (
            "SELECT * FROM workflow_a_control.client_dataset_coverage "
            "WHERE schedule_id = %s FOR UPDATE",
            "lock",
        ),
    ):
        _, _, violations = _scan_runtime_python_sources({
            BOOTSTRAP_WRITER_MODULE:
                f"def {APPROVED_INSERT_FUNCTION}(cur):\n"
                f"    cur.execute({statement!r})\n",
        })
        assert violations, label

    # The audit tool may read coverage, and only read it.
    _, _, violations = _scan_runtime_python_sources({
        AUDIT_MODULE:
            "def read_existing_coverage(cur, schedule_id):\n"
            "    cur.execute('SELECT bootstrap_status FROM "
            "workflow_a_control.client_dataset_coverage WHERE schedule_id = %s',"
            " (schedule_id,))\n",
    })
    assert not violations
    for statement in (
        "UPDATE workflow_a_control.client_dataset_coverage SET bootstrap_status = %s",
        "DELETE FROM workflow_a_control.client_dataset_coverage",
        "SELECT 1 FROM workflow_a_control.client_dataset_coverage FOR UPDATE",
    ):
        _, _, violations = _scan_runtime_python_sources({
            AUDIT_MODULE: f"def probe(cur):\n    cur.execute({statement!r})\n",
        })
        assert violations, statement

    # The C6 gap finalizer UPDATE still passes unchanged, and the shared
    # advancement CAS passes in its own module.
    approved_gap_finalizer = """
def _finalize_compat_gap():
    lock = '''SELECT * FROM workflow_a_control.client_dataset_coverage
              FOR UPDATE'''
    write = '''UPDATE workflow_a_control.client_dataset_coverage
                  SET bootstrap_status = 'GAP_DETECTED',
                      last_gap_detected_ts = %s,
                      updated_at = %s'''
"""
    _, _, violations = _scan_runtime_python_sources({
        DISPATCHER_MODULE: approved_gap_finalizer,
    })
    assert not violations

    approved_advance = """
def advance_covered_through_cas():
    write = '''UPDATE workflow_a_control.client_dataset_coverage
                  SET covered_through_ts = %(new_covered_through_ts)s,
                      covered_through_source = %(new_covered_through_source)s,
                      updated_at = %(mutation_ts)s'''
"""
    _, _, violations = _scan_runtime_python_sources({
        COVERAGE_FINALIZATION_MODULE: approved_advance,
    })
    assert not violations

    # The shared module may never insert, delete or seed coverage.
    for statement, label in (
        (
            "INSERT INTO workflow_a_control.client_dataset_coverage "
            "(schedule_id) VALUES (%s)",
            "insert",
        ),
        (
            "DELETE FROM workflow_a_control.client_dataset_coverage "
            "WHERE schedule_id = %s",
            "delete",
        ),
    ):
        _, _, violations = _scan_runtime_python_sources({
            COVERAGE_FINALIZATION_MODULE:
                f"def {APPROVED_ADVANCE_FUNCTION}(cur):\n"
                f"    cur.execute({statement!r})\n",
        })
        assert violations, label


APPROVED_COLD_START_INSERT_SOURCE = APPROVED_BOOTSTRAP_INSERT_SOURCE.replace(
    APPROVED_INSERT_FUNCTION, APPROVED_COLD_START_INSERT_FUNCTION,
)


def _vocabulary_source(identifiers) -> str:
    """A comment-only module naming exactly the given identifiers."""
    return "".join(f"# {name}\n" for name in sorted(identifiers))


def test_cold_start_registration_is_exact() -> None:
    """Each cold-start module is accepted for its exact vocabulary and no more."""
    registered = {
        COLD_START_AUDIT_MODULE: COLD_START_AUDIT_ALLOWED_IDENTIFIERS,
        COLD_START_WRITER_MODULE: COLD_START_WRITER_ALLOWED_IDENTIFIERS,
        ACTIVATION_TOOL_MODULE: ACTIVATION_TOOL_ALLOWED_IDENTIFIERS,
        COLD_START_CHAIN_MODULE: COLD_START_CHAIN_ALLOWED_IDENTIFIERS,
    }
    for relative, allowed in registered.items():
        # The exact authorized vocabulary is accepted.
        offenders, importers, violations = _scan_runtime_python_sources({
            relative: _vocabulary_source(allowed),
        })
        assert not offenders and not importers and not violations, relative

        # Every identifier outside that module's set is still rejected, one at
        # a time, so a single over-broad name cannot be smuggled in.
        for name in C3_RUNTIME_IDENTIFIERS:
            if name in allowed:
                continue
            offenders, _, _ = _scan_runtime_python_sources({
                relative: f"# {name}\n",
            })
            assert offenders == [f"{relative}:{name}"], (relative, name)

        # None of them may import the pure window helper.
        _, importers, _ = _scan_runtime_python_sources({
            relative: "from jobs.api.telematics import coverage_windows\n",
        })
        assert importers == [str(relative)], relative

    # The specific omissions that carry architectural meaning.
    assert "client_dataset_coverage" not in ACTIVATION_TOOL_ALLOWED_IDENTIFIERS
    assert "client_dataset_coverage" not in COLD_START_CHAIN_ALLOWED_IDENTIFIERS
    assert "bootstrap_status" not in COLD_START_AUDIT_ALLOWED_IDENTIFIERS
    for name in ("nominal_window_start_ts", "nominal_window_end_ts"):
        assert name not in COLD_START_WRITER_ALLOWED_IDENTIFIERS


def test_cold_start_deny_by_default_is_preserved() -> None:
    """Registration authorizes exact paths only — never a pattern or a tree."""
    # Every allowlist key is a concrete tracked Python file: no globs, no
    # directories, no prefixes. A pattern key could not be written here without
    # this assertion failing.
    tracked = set(_tracked_production_python_paths())
    for mapping in (
        C5_ALLOWED_IDENTIFIERS_BY_MODULE,
        COVERAGE_INSERT_AUTHORIZED_FUNCTION_BY_MODULE,
        COVERAGE_SQL_AUTHORIZED_FUNCTIONS,
    ):
        for key in mapping:
            assert key in tracked, key
            assert key.suffix == ".py", key
            assert not any(ch in str(key) for ch in "*?["), key

    # Exactly two modules may ever hold a coverage INSERT, each with exactly
    # one distinct function name. Registering a third — or granting one to a
    # read-only tool — is itself the failure, independently of what shape that
    # tool's statement happens to have.
    assert set(COVERAGE_INSERT_AUTHORIZED_FUNCTION_BY_MODULE) == {
        BOOTSTRAP_WRITER_MODULE,
        COLD_START_WRITER_MODULE,
    }
    assert len(set(COVERAGE_INSERT_AUTHORIZED_FUNCTION_BY_MODULE.values())) == 2
    # Coverage locks and coverage UPDATEs stay confined to the C6 dispatcher
    # finalizer and the C11 shared module. No cold-start tool may acquire one.
    assert set(COVERAGE_SQL_AUTHORIZED_FUNCTIONS) == {
        DISPATCHER_MODULE,
        COVERAGE_FINALIZATION_MODULE,
    }
    for read_only in (
        AUDIT_MODULE,
        COLD_START_AUDIT_MODULE,
        ACTIVATION_TOOL_MODULE,
        COLD_START_CHAIN_MODULE,
        RECOVERY_TOOL_MODULE,
        PURE_C4_MODULE,
    ):
        assert read_only not in COVERAGE_INSERT_AUTHORIZED_FUNCTION_BY_MODULE, (
            read_only)
        assert read_only not in COVERAGE_SQL_AUTHORIZED_FUNCTIONS, read_only
    # The seeding writers never acquire a lock or UPDATE surface either.
    for writer in (BOOTSTRAP_WRITER_MODULE, COLD_START_WRITER_MODULE):
        assert writer not in COVERAGE_SQL_AUTHORIZED_FUNCTIONS, writer

    # A directory-shaped or wildcard-shaped key grants nothing, because lookup
    # is by exact path.
    for pseudo_key in (Path("ops"), Path("ops/*.py"), Path("ops/audit_*.py")):
        assert C5_ALLOWED_IDENTIFIERS_BY_MODULE.get(pseudo_key) is None
        assert COVERAGE_INSERT_AUTHORIZED_FUNCTION_BY_MODULE.get(
            pseudo_key) is None

    # Sitting in the same directory as an authorized module authorizes nothing:
    # a new, unregistered cold-start-shaped tool using the very same vocabulary
    # is still an offender.
    for unregistered in (
        Path("ops/audit_telematics_warm_start.py"),
        Path("ops/bootstrap_telematics_warm_start_coverage.py"),
        Path("ops/telematics_warm_start_chain.py"),
        Path("ops/activate_telematics_other_schedule.py"),
        Path("future_runtime/cold_start_writer.py"),
    ):
        assert unregistered not in C5_ALLOWED_IDENTIFIERS_BY_MODULE
        offenders, _, _ = _scan_runtime_python_sources({
            unregistered: _vocabulary_source(COLD_START_WRITER_ALLOWED_IDENTIFIERS),
        })
        assert offenders, unregistered
        # ...and the approved INSERT shape does not travel with the vocabulary.
        _, _, violations = _scan_runtime_python_sources({
            unregistered: APPROVED_COLD_START_INSERT_SOURCE,
        })
        assert violations, unregistered

    # The previously registered modules remain accepted, unchanged.
    for relative, allowed in (
        (PURE_C4_MODULE, PURE_C5_ALLOWED_IDENTIFIERS),
        (DISPATCHER_MODULE, DISPATCHER_C5_ALLOWED_IDENTIFIERS),
        (COVERAGE_FINALIZATION_MODULE, COVERAGE_FINALIZATION_ALLOWED_IDENTIFIERS),
        (AUDIT_MODULE, BOOTSTRAP_TOOL_ALLOWED_IDENTIFIERS),
        (BOOTSTRAP_WRITER_MODULE, BOOTSTRAP_TOOL_ALLOWED_IDENTIFIERS),
        (RECOVERY_TOOL_MODULE, RECOVERY_TOOL_ALLOWED_IDENTIFIERS),
    ):
        offenders, _, _ = _scan_runtime_python_sources({
            relative: _vocabulary_source(
                allowed - {"client_dataset_coverage"}
            ),
        })
        assert not offenders, relative
    forbidden, importers, violations = _scan_runtime_python_sources({
        BOOTSTRAP_WRITER_MODULE: APPROVED_BOOTSTRAP_INSERT_SOURCE,
    })
    assert not forbidden and not importers and not violations


def test_cold_start_read_only_audit_stays_read_only() -> None:
    """The cold-start audit may SELECT coverage and nothing else."""
    _, _, violations = _scan_runtime_python_sources({
        COLD_START_AUDIT_MODULE:
            "def count_coverage(cur, schedule_id):\n"
            "    cur.execute('SELECT count(*) FROM "
            "workflow_a_control.client_dataset_coverage WHERE schedule_id = %s',"
            " (schedule_id,))\n",
    })
    assert not violations

    # Any coverage mutation or lock added to it is a violation.
    for statement, label in (
        (
            "INSERT INTO workflow_a_control.client_dataset_coverage "
            "(schedule_id) VALUES (%s)",
            "insert",
        ),
        (
            "UPDATE workflow_a_control.client_dataset_coverage "
            "SET bootstrap_status = %s",
            "update",
        ),
        (
            "DELETE FROM workflow_a_control.client_dataset_coverage "
            "WHERE schedule_id = %s",
            "delete",
        ),
        (
            "SELECT 1 FROM workflow_a_control.client_dataset_coverage "
            "WHERE schedule_id = %s FOR UPDATE",
            "lock",
        ),
    ):
        _, _, violations = _scan_runtime_python_sources({
            COLD_START_AUDIT_MODULE:
                f"def probe(cur):\n    cur.execute({statement!r})\n",
        })
        assert violations, label

    # Even the fully approved seeding shape is rejected in the audit.
    _, _, violations = _scan_runtime_python_sources({
        COLD_START_AUDIT_MODULE: APPROVED_COLD_START_INSERT_SOURCE,
    })
    assert violations


def test_cold_start_writer_guard_synthetic_cases() -> None:
    """One module, one function and one shape may seed the cold-start baseline."""
    # The approved shape passes in the named cold-start writer function.
    forbidden, importers, violations = _scan_runtime_python_sources({
        COLD_START_WRITER_MODULE: APPROVED_COLD_START_INSERT_SOURCE,
    })
    assert not forbidden and not importers and not violations, violations

    # Right module, wrong function.
    for wrong_name in ("_seed_baseline", "write_coverage", "main"):
        _, _, violations = _scan_runtime_python_sources({
            COLD_START_WRITER_MODULE:
                APPROVED_COLD_START_INSERT_SOURCE.replace(
                    APPROVED_COLD_START_INSERT_FUNCTION, wrong_name,
                ),
        })
        assert violations, wrong_name

    # The two writers' function names do not cross over: each name is
    # authorized in exactly one module.
    _, _, violations = _scan_runtime_python_sources({
        COLD_START_WRITER_MODULE: APPROVED_BOOTSTRAP_INSERT_SOURCE,
    })
    assert violations, "the C10 function name is not authorized here"
    _, _, violations = _scan_runtime_python_sources({
        BOOTSTRAP_WRITER_MODULE: APPROVED_COLD_START_INSERT_SOURCE,
    })
    assert violations, "the cold-start function name is not authorized there"

    # Right module and function, wrong shape — the same three refusals the C10
    # writer is held to.
    for mutated, label in (
        (
            APPROVED_COLD_START_INSERT_SOURCE.replace(
                "%(updated_at)s\n        )",
                "%(updated_at)s\n        ) ON CONFLICT (schedule_id) DO UPDATE"
                " SET covered_through_ts = EXCLUDED.covered_through_ts",
            ),
            "upsert",
        ),
        (
            APPROVED_COLD_START_INSERT_SOURCE.replace(
                "%(updated_at)s\n        )",
                "%(updated_at)s\n        ) ON CONFLICT DO NOTHING",
            ),
            "conflict-tolerant reseed",
        ),
        (
            APPROVED_COLD_START_INSERT_SOURCE.replace(
                "covered_through_source, updated_at",
                "covered_through_source, last_gap_detected_ts",
            ),
            "writes last_gap_detected_ts",
        ),
        (
            APPROVED_COLD_START_INSERT_SOURCE
            .replace("bootstrap_evidence_ref,\n", "")
            .replace("%(bootstrap_evidence_ref)s,\n", ""),
            "omits the evidence reference",
        ),
    ):
        _, _, violations = _scan_runtime_python_sources({
            COLD_START_WRITER_MODULE: mutated,
        })
        assert violations, label

    # An INSERT hoisted to module scope cannot escape the function rules.
    _, _, violations = _scan_runtime_python_sources({
        COLD_START_WRITER_MODULE:
            "SQL = '''INSERT INTO workflow_a_control.client_dataset_coverage "
            "(schedule_id) VALUES (%s)'''\n",
    })
    assert violations

    # The cold-start writer may never UPDATE, DELETE or lock coverage, even
    # inside its own approved function: every later mutation belongs to C6/C11.
    for statement, label in (
        (
            "UPDATE workflow_a_control.client_dataset_coverage "
            "SET covered_through_ts = %s",
            "update",
        ),
        (
            "UPDATE workflow_a_control.client_dataset_coverage "
            "SET bootstrap_status = 'READY'",
            "status update",
        ),
        (
            "DELETE FROM workflow_a_control.client_dataset_coverage "
            "WHERE schedule_id = %s",
            "delete",
        ),
        (
            "SELECT * FROM workflow_a_control.client_dataset_coverage "
            "WHERE schedule_id = %s FOR UPDATE",
            "lock",
        ),
    ):
        _, _, violations = _scan_runtime_python_sources({
            COLD_START_WRITER_MODULE:
                f"def {APPROVED_COLD_START_INSERT_FUNCTION}(cur):\n"
                f"    cur.execute({statement!r})\n",
        })
        assert violations, label

    # Reading the seeded row back is permitted; it is a plain SELECT.
    _, _, violations = _scan_runtime_python_sources({
        COLD_START_WRITER_MODULE:
            "def _read_back(cur, schedule_id):\n"
            "    cur.execute('SELECT bootstrap_status, last_gap_detected_ts "
            "FROM workflow_a_control.client_dataset_coverage "
            "WHERE schedule_id = %s', (schedule_id,))\n",
    })
    assert not violations


def test_cold_start_readers_hold_no_coverage_statement() -> None:
    """Activation and the chain helper read state; they never name the table."""
    for relative in (ACTIVATION_TOOL_MODULE, COLD_START_CHAIN_MODULE):
        forbidden, _, _ = _scan_runtime_python_sources({
            relative: "table = 'client_dataset_coverage'\n",
        })
        assert forbidden, relative
        # The shared C11 advancement statement belongs to its own module.
        _, _, violations = _scan_runtime_python_sources({
            relative: """
def advance_covered_through_cas():
    write = '''UPDATE workflow_a_control.client_dataset_coverage
                  SET covered_through_ts = %(new_covered_through_ts)s,
                      covered_through_source = %(new_covered_through_source)s,
                      updated_at = %(mutation_ts)s'''
""",
        })
        assert violations, relative
        # ...as does the seeding INSERT.
        _, _, violations = _scan_runtime_python_sources({
            relative: APPROVED_COLD_START_INSERT_SOURCE,
        })
        assert violations, relative

    # Locking the rows activation actually owns stays outside the coverage
    # rules, because those statements name no coverage object.
    _, _, violations = _scan_runtime_python_sources({
        ACTIVATION_TOOL_MODULE:
            "def _flip(cur, schedule_id):\n"
            "    cur.execute('SELECT enabled FROM "
            "workflow_a_control.client_dataset_schedule "
            "WHERE schedule_id = %s FOR UPDATE', (schedule_id,))\n"
            "    cur.execute('UPDATE workflow_a_control.client_dataset_schedule"
            " SET enabled = true WHERE schedule_id = %s', (schedule_id,))\n",
    })
    assert not violations


# ---------------------------------------------------------------------------
# Disposable PostgreSQL helpers
# ---------------------------------------------------------------------------

def _sql(name: str) -> str:
    return (MIGRATIONS_DIR / name).read_text(encoding="utf-8")


def _expect_db_error(conn, statement: str) -> str:
    import psycopg

    try:
        with conn.transaction():
            conn.execute(statement)
    except (psycopg.Error, psycopg.DataError) as exc:
        return str(exc)
    raise AssertionError("database accepted an invalid or incompatible state")


def _bootstrap_baseline(conn) -> None:
    """Rebuild the authoritative prerequisite schema plus one seeded schedule."""
    conn.execute("DROP SCHEMA IF EXISTS workflow_a_control CASCADE")
    for name in PREREQUISITE_MIGRATIONS:
        conn.execute(_sql(name))
    conn.execute(
        """
        INSERT INTO workflow_a_control.client_account
          (client_id, client_code, client_name, provider_type, provider_base_url,
           provider_basic_auth_username, provider_basic_auth_password_secret_ref,
           client_db_host, client_db_port, client_db_name, client_db_user,
           client_db_password_secret_ref, speed_trigger_filter_text)
        VALUES (%s, %s, 'Test Client', 'telematics', 'https://provider.invalid',
                'user', 'TEST_PROVIDER_KEY', '127.0.0.1', 5432, 'clientdb',
                'clientuser', 'TEST_DB_KEY', 'SPEEDING')
        """,
        (CLIENT_ID, CLIENT_CODE),
    )
    for schedule_id, dataset in ((SCHEDULE_A, "trips_sync"),
                                 (SCHEDULE_B, "fuel_daily_aggregation")):
        conn.execute(
            """
            INSERT INTO workflow_a_control.client_dataset_schedule
              (schedule_id, client_id, client_code, dataset_name, enabled,
               frequency, run_time, timezone, lookback_days)
            VALUES (%s, %s, %s, %s, true, 'daily', '02:00:00', 'UTC', 1)
            """,
            (schedule_id, CLIENT_ID, CLIENT_CODE, dataset),
        )
    conn.execute(
        """
        INSERT INTO workflow_a_control.client_schedule_run_history
          (schedule_id, client_id, client_code, dataset_name,
           window_start_ts, window_end_ts, scheduled_fire_ts, status)
        VALUES (%s, %s, %s, 'trips_sync',
                '2026-07-29T02:00:00Z', '2026-07-30T02:00:00Z',
                '2026-07-30T02:00:00Z', 'SUCCESS')
        """,
        (SCHEDULE_A, CLIENT_ID, CLIENT_CODE),
    )
    conn.commit()


def _insert_coverage(conn, **overrides) -> None:
    values = {
        "schedule_id": SCHEDULE_A,
        "client_id": CLIENT_ID,
        "client_code": CLIENT_CODE,
        "dataset_name": "trips_sync",
        "coverage_start_ts": None,
        "covered_through_ts": None,
        "bootstrap_status": "UNINITIALIZED",
        "bootstrap_evidence_ref": None,
        "seeded_at": None,
        "seeded_by": None,
        "covered_through_source": "bootstrap",
    }
    values.update(overrides)
    columns = ", ".join(values)
    placeholders = ", ".join(["%s"] * len(values))
    conn.execute(
        f"INSERT INTO workflow_a_control.client_dataset_coverage ({columns}) "
        f"VALUES ({placeholders})",
        tuple(values.values()),
    )


def _coverage_insert_sql(**overrides) -> str:
    values = {
        "schedule_id": f"'{SCHEDULE_A}'",
        "client_id": f"'{CLIENT_ID}'",
        "client_code": f"'{CLIENT_CODE}'",
        "dataset_name": "'trips_sync'",
        "bootstrap_status": "'UNINITIALIZED'",
        "covered_through_source": "'bootstrap'",
    }
    values.update(overrides)
    return (
        "INSERT INTO workflow_a_control.client_dataset_coverage ("
        + ", ".join(values)
        + ") VALUES ("
        + ", ".join(values.values())
        + ")"
    )


# ---------------------------------------------------------------------------
# Coverage table
# ---------------------------------------------------------------------------

def test_coverage_table_shape(conn) -> None:
    _bootstrap_baseline(conn)
    assert conn.execute(
        "SELECT to_regclass('workflow_a_control.client_dataset_coverage') AS v"
    ).fetchone()["v"] is None

    conn.execute(_sql(MIGRATION_NAME))
    conn.commit()

    assert conn.execute(
        "SELECT to_regclass('workflow_a_control.client_dataset_coverage') AS v"
    ).fetchone()["v"] is not None
    assert conn.execute(
        "SELECT count(*) AS n FROM workflow_a_control.client_dataset_coverage"
    ).fetchone()["n"] == 0, "the migration must not seed a coverage row"

    columns = conn.execute(
        """
        SELECT column_name, data_type, is_nullable, column_default
          FROM information_schema.columns
         WHERE table_schema='workflow_a_control'
           AND table_name='client_dataset_coverage'
         ORDER BY ordinal_position
        """
    ).fetchall()
    observed = {
        row["column_name"]: (row["data_type"], row["is_nullable"],
                             row["column_default"])
        for row in columns
    }
    assert observed == COVERAGE_COLUMNS, observed

    # Primary key, foreign key and named CHECK constraints.
    constraints = {
        row["conname"]: row["definition"]
        for row in conn.execute(
            """
            SELECT conname, pg_get_constraintdef(oid) AS definition
              FROM pg_constraint
             WHERE conrelid='workflow_a_control.client_dataset_coverage'::regclass
            """
        ).fetchall()
    }
    assert constraints["pk_client_dataset_coverage"] == "PRIMARY KEY (schedule_id)"
    assert constraints["fk_client_dataset_coverage_schedule"] == (
        "FOREIGN KEY (schedule_id) "
        "REFERENCES workflow_a_control.client_dataset_schedule(schedule_id) "
        "ON DELETE CASCADE"
    )
    for name in (
        "ck_client_dataset_coverage_bootstrap_status",
        "ck_client_dataset_coverage_covered_through_source",
        "ck_client_dataset_coverage_bounds_order",
        "ck_client_dataset_coverage_ready_complete",
    ):
        assert name in constraints, name

    indexes = {
        row["indexname"]: row["indexdef"]
        for row in conn.execute(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE schemaname='workflow_a_control' "
            "AND tablename='client_dataset_coverage'"
        ).fetchall()
    }
    assert "idx_client_dataset_coverage_attention" in indexes
    assert "bootstrap_status <> 'READY'" in \
        indexes["idx_client_dataset_coverage_attention"]

    # No trigger is created on the coverage table.
    assert conn.execute(
        "SELECT count(*) AS n FROM pg_trigger "
        "WHERE tgrelid='workflow_a_control.client_dataset_coverage'::regclass "
        "AND NOT tgisinternal"
    ).fetchone()["n"] == 0


def test_coverage_owner_and_acl(conn) -> None:
    row = conn.execute(
        """
        SELECT pg_get_userbyid(c.relowner) AS owner, c.relacl::text AS acl
          FROM pg_class c
         WHERE c.oid='workflow_a_control.client_dataset_coverage'::regclass
        """
    ).fetchone()
    schedule = conn.execute(
        """
        SELECT pg_get_userbyid(c.relowner) AS owner, c.relacl::text AS acl
          FROM pg_class c
         WHERE c.oid='workflow_a_control.client_dataset_schedule'::regclass
        """
    ).fetchone()
    assert row["owner"] == schedule["owner"], "owner must match the schema convention"
    # Repository convention: no migration issues GRANT, so relacl stays default.
    assert row["acl"] is None and schedule["acl"] is None


def test_coverage_states(conn) -> None:
    # One row per schedule.
    _insert_coverage(conn)
    conn.commit()
    _expect_db_error(conn, _coverage_insert_sql())

    # A second schedule may hold its own row.
    _insert_coverage(conn, schedule_id=SCHEDULE_B,
                     dataset_name="fuel_daily_aggregation")
    conn.commit()
    assert conn.execute(
        "SELECT count(*) AS n FROM workflow_a_control.client_dataset_coverage"
    ).fetchone()["n"] == 2

    # Accepted UNINITIALIZED representation: NULL bounds, no evidence.
    row = conn.execute(
        "SELECT bootstrap_status, coverage_start_ts, covered_through_ts, "
        "bootstrap_evidence_ref FROM workflow_a_control.client_dataset_coverage "
        "WHERE schedule_id=%s", (SCHEDULE_A,)
    ).fetchone()
    assert row["bootstrap_status"] == "UNINITIALIZED"
    assert row["coverage_start_ts"] is None and row["covered_through_ts"] is None

    # docs/13 §5.2 permits an UNINITIALIZED row to carry bounds while an
    # operator prepares a bootstrap, so the accepted transitional form is
    # allowed — but never with reversed bounds.
    conn.execute(
        "UPDATE workflow_a_control.client_dataset_coverage "
        "SET coverage_start_ts='2026-07-01T00:00:00Z', "
        "    covered_through_ts='2026-07-29T02:00:00Z' "
        "WHERE schedule_id=%s", (SCHEDULE_A,))
    conn.commit()
    _expect_db_error(
        conn,
        "UPDATE workflow_a_control.client_dataset_coverage "
        "SET coverage_start_ts='2026-07-30T00:00:00Z', "
        "    covered_through_ts='2026-07-29T02:00:00Z' "
        f"WHERE schedule_id='{SCHEDULE_A}'",
    )
    # An UNINITIALIZED row still cannot masquerade as a usable claim, because
    # the status vocabulary is the gate and READY is unreachable without
    # complete evidence (asserted below).

    conn.execute("DELETE FROM workflow_a_control.client_dataset_coverage")
    conn.commit()

    ready = {
        "coverage_start_ts": "'2026-07-01T00:00:00Z'",
        "covered_through_ts": "'2026-07-29T02:00:00Z'",
        "bootstrap_status": "'READY'",
        "bootstrap_evidence_ref": "'bundle-2026-08-01-a'",
        "seeded_at": "now()",
        "seeded_by": "'operator@example.invalid'",
    }

    # Valid READY row.
    conn.execute(_coverage_insert_sql(**ready))
    conn.commit()
    conn.execute("DELETE FROM workflow_a_control.client_dataset_coverage")
    conn.commit()

    invalid_ready = {
        "READY without start": {"coverage_start_ts": "NULL"},
        "READY without end": {"covered_through_ts": "NULL"},
        "READY without evidence": {"bootstrap_evidence_ref": "NULL"},
        "READY with empty evidence": {"bootstrap_evidence_ref": "'   '"},
        "READY without seeded_at": {"seeded_at": "NULL"},
        "READY without seeded_by": {"seeded_by": "NULL"},
        "READY with empty seeded_by": {"seeded_by": "''"},
        "READY with reversed bounds": {
            "coverage_start_ts": "'2026-07-30T00:00:00Z'"},
    }
    for label, override in invalid_ready.items():
        payload = dict(ready)
        payload.update(override)
        try:
            _expect_db_error(conn, _coverage_insert_sql(**payload))
        except AssertionError as exc:  # pragma: no cover - failure path
            raise AssertionError(f"{label} was accepted") from exc

    # Reversed bounds are rejected in every status.
    _expect_db_error(conn, _coverage_insert_sql(
        coverage_start_ts="'2026-07-30T00:00:00Z'",
        covered_through_ts="'2026-07-29T00:00:00Z'",
    ))

    # GAP_DETECTED and RESEED_REQUIRED preserve the last verified interval.
    for status in ("GAP_DETECTED", "RESEED_REQUIRED"):
        conn.execute(_coverage_insert_sql(
            bootstrap_status=f"'{status}'",
            coverage_start_ts="'2026-07-01T00:00:00Z'",
            covered_through_ts="'2026-07-29T02:00:00Z'",
            bootstrap_evidence_ref="'bundle-2026-08-01-a'",
            last_gap_detected_ts="now()",
        ))
        conn.commit()
        conn.execute("DELETE FROM workflow_a_control.client_dataset_coverage")
        conn.commit()

    # Unknown status and unknown provenance are rejected.
    _expect_db_error(conn, _coverage_insert_sql(bootstrap_status="'ready'"))
    _expect_db_error(conn, _coverage_insert_sql(bootstrap_status="'INITIALIZED'"))
    _expect_db_error(conn, _coverage_insert_sql(covered_through_source="'guess'"))

    # Unknown schedule is rejected by the foreign key.
    _expect_db_error(conn, _coverage_insert_sql(
        schedule_id=f"'{ABSENT_SCHEDULE}'"))


def test_coverage_cascade_and_rerun(conn) -> None:
    conn.execute("DELETE FROM workflow_a_control.client_dataset_coverage")
    _insert_coverage(conn, bootstrap_status="GAP_DETECTED",
                     coverage_start_ts="2026-07-01T00:00:00Z",
                     covered_through_ts="2026-07-29T02:00:00Z")
    conn.commit()

    # Disabling a schedule preserves the row.
    conn.execute("UPDATE workflow_a_control.client_dataset_schedule "
                 "SET enabled=false WHERE schedule_id=%s", (SCHEDULE_A,))
    conn.commit()
    assert conn.execute(
        "SELECT count(*) AS n FROM workflow_a_control.client_dataset_coverage"
    ).fetchone()["n"] == 1

    # Re-running the migration preserves valid rows and creates none.
    before = conn.execute(
        "SELECT * FROM workflow_a_control.client_dataset_coverage"
    ).fetchall()
    conn.execute(_sql(MIGRATION_NAME))
    conn.execute(_sql(MIGRATION_NAME))
    conn.commit()
    after = conn.execute(
        "SELECT * FROM workflow_a_control.client_dataset_coverage"
    ).fetchall()
    assert before == after
    assert len(after) == 1

    # Deleting the schedule cascades the coverage row (docs/14 §6.6).
    conn.execute("DELETE FROM workflow_a_control.client_dataset_schedule "
                 "WHERE schedule_id=%s", (SCHEDULE_A,))
    conn.commit()
    assert conn.execute(
        "SELECT count(*) AS n FROM workflow_a_control.client_dataset_coverage "
        "WHERE schedule_id=%s", (SCHEDULE_A,)
    ).fetchone()["n"] == 0


# ---------------------------------------------------------------------------
# History evidence
# ---------------------------------------------------------------------------

def test_history_evidence_columns(conn) -> None:
    _bootstrap_baseline(conn)
    before = conn.execute(
        "SELECT run_history_id, schedule_id, client_id, client_code, "
        "dataset_name, window_start_ts, window_end_ts, scheduled_fire_ts, "
        "status, error_summary, created_at "
        "FROM workflow_a_control.client_schedule_run_history "
        "ORDER BY run_history_id"
    ).fetchall()
    unique_before = conn.execute(
        "SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint "
        "WHERE conname='uq_run_history_schedule_fire'"
    ).fetchone()["d"]

    conn.execute(_sql(MIGRATION_NAME))
    conn.execute(_sql(MIGRATION_NAME))
    conn.commit()

    after = conn.execute(
        "SELECT run_history_id, schedule_id, client_id, client_code, "
        "dataset_name, window_start_ts, window_end_ts, scheduled_fire_ts, "
        "status, error_summary, created_at "
        "FROM workflow_a_control.client_schedule_run_history "
        "ORDER BY run_history_id"
    ).fetchall()
    assert before == after, "existing history rows must not change"

    unique_after = conn.execute(
        "SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint "
        "WHERE conname='uq_run_history_schedule_fire'"
    ).fetchone()["d"]
    assert unique_after == unique_before == \
        "UNIQUE (schedule_id, scheduled_fire_ts)"

    columns = {
        row["column_name"]: (row["data_type"], row["is_nullable"],
                             row["column_default"])
        for row in conn.execute(
            """
            SELECT column_name, data_type, is_nullable, column_default
              FROM information_schema.columns
             WHERE table_schema='workflow_a_control'
               AND table_name='client_schedule_run_history'
               AND column_name = ANY(%s)
            """,
            (list(HISTORY_EVIDENCE_COLUMNS),),
        ).fetchall()
    }
    assert set(columns) == set(HISTORY_EVIDENCE_COLUMNS)
    for name, expected_type in HISTORY_EVIDENCE_COLUMNS.items():
        assert columns[name] == (expected_type, "YES", None), (name, columns[name])

    # Historical rows keep NULL evidence, and NULL stays acceptable.
    nulls = conn.execute(
        "SELECT count(*) AS n FROM workflow_a_control.client_schedule_run_history "
        "WHERE nominal_window_start_ts IS NULL AND nominal_window_end_ts IS NULL "
        "AND stabilization_delay_seconds IS NULL AND overlap_seconds IS NULL "
        "AND trips_pagination_mode IS NULL"
    ).fetchone()["n"]
    assert nulls == len(after) == 1

    # An ordinary claim insert that omits all five evidence fields must store
    # NULL in every field. Defaults must never manufacture claim-time evidence.
    conn.execute(
        """
        INSERT INTO workflow_a_control.client_schedule_run_history
          (schedule_id, client_id, client_code, dataset_name,
           window_start_ts, window_end_ts, scheduled_fire_ts, status)
        VALUES (%s, %s, %s, 'trips_sync',
                '2026-08-07T02:00:00Z', '2026-08-08T02:00:00Z',
                '2026-08-08T02:00:00Z', 'SUCCESS')
        """,
        (SCHEDULE_A, CLIENT_ID, CLIENT_CODE),
    )
    omitted = conn.execute(
        """
        SELECT nominal_window_start_ts, nominal_window_end_ts,
               stabilization_delay_seconds, overlap_seconds,
               trips_pagination_mode
          FROM workflow_a_control.client_schedule_run_history
         WHERE schedule_id=%s AND scheduled_fire_ts='2026-08-08T02:00:00Z'
        """,
        (SCHEDULE_A,),
    ).fetchone()
    assert omitted == {name: None for name in HISTORY_EVIDENCE_COLUMNS}
    conn.commit()

    # A fully populated, valid evidence row is accepted.
    conn.execute(
        """
        INSERT INTO workflow_a_control.client_schedule_run_history
          (schedule_id, client_id, client_code, dataset_name,
           window_start_ts, window_end_ts, scheduled_fire_ts, status,
           nominal_window_start_ts, nominal_window_end_ts,
           stabilization_delay_seconds, overlap_seconds, trips_pagination_mode)
        VALUES (%s, %s, %s, 'trips_sync',
                '2026-07-29T22:00:00Z', '2026-07-30T22:00:00Z',
                '2026-07-31T02:00:00Z', 'SUCCESS',
                '2026-07-30T02:00:00Z', '2026-07-31T02:00:00Z',
                10800, 3600, 'data_invariants_v1')
        """,
        (SCHEDULE_A, CLIENT_ID, CLIENT_CODE),
    )
    conn.commit()

    base = (
        "INSERT INTO workflow_a_control.client_schedule_run_history "
        "(schedule_id, client_id, client_code, dataset_name, window_start_ts, "
        " window_end_ts, scheduled_fire_ts, status, {columns}) "
        f"VALUES ('{SCHEDULE_A}', '{CLIENT_ID}', '{CLIENT_CODE}', 'trips_sync', "
        "'2026-08-01T00:00:00Z', '2026-08-02T00:00:00Z', "
        "'2026-08-0{day}T02:00:00Z', 'SUCCESS', {values})"
    )
    invalid = (
        ("trips_pagination_mode", "'loose'"),
        ("stabilization_delay_seconds", "-1"),
        ("overlap_seconds", "-1"),
        ("nominal_window_start_ts, nominal_window_end_ts",
         "'2026-07-31T02:00:00Z', '2026-07-30T02:00:00Z'"),
    )
    for day, (columns_sql, values_sql) in enumerate(invalid, start=3):
        _expect_db_error(
            conn,
            base.format(columns=columns_sql, values=values_sql, day=day),
        )

    # Zero delay and zero overlap remain valid.
    conn.execute(base.format(
        columns="stabilization_delay_seconds, overlap_seconds",
        values="0, 0", day=9))
    conn.commit()


# ---------------------------------------------------------------------------
# Partial state
# ---------------------------------------------------------------------------

def test_partial_state_coverage_table(conn) -> None:
    sql = _sql(MIGRATION_NAME)

    # Compatible table partially created and empty converges to the contract.
    _bootstrap_baseline(conn)
    conn.execute(
        "CREATE TABLE workflow_a_control.client_dataset_coverage ("
        " schedule_id UUID, client_id UUID, dataset_name TEXT)"
    )
    conn.commit()
    conn.execute(sql)
    conn.commit()
    assert conn.execute(
        "SELECT count(*) AS n FROM information_schema.columns "
        "WHERE table_schema='workflow_a_control' "
        "AND table_name='client_dataset_coverage'"
    ).fetchone()["n"] == len(COVERAGE_COLUMNS)

    # Incompatible schedule_id type fails visibly.
    _bootstrap_baseline(conn)
    conn.execute(
        "CREATE TABLE workflow_a_control.client_dataset_coverage ("
        " schedule_id TEXT, client_id UUID, dataset_name TEXT)"
    )
    conn.commit()
    message = _expect_db_error(conn, sql)
    assert "incompatible type" in message

    # A required field absent from an already-populated table is a visible stop.
    _bootstrap_baseline(conn)
    conn.execute(
        "CREATE TABLE workflow_a_control.client_dataset_coverage ("
        " schedule_id UUID, client_id UUID, dataset_name TEXT)"
    )
    conn.execute(
        "INSERT INTO workflow_a_control.client_dataset_coverage VALUES "
        f"('{SCHEDULE_A}', '{CLIENT_ID}', 'trips_sync')"
    )
    conn.commit()
    message = _expect_db_error(conn, sql)
    assert "refusing to repair coverage state automatically" in message
    assert conn.execute(
        "SELECT count(*) AS n FROM workflow_a_control.client_dataset_coverage"
    ).fetchone()["n"] == 1, "no pre-existing row may be deleted"

    # A stale same-named constraint is replaced by the canonical definition.
    _bootstrap_baseline(conn)
    conn.execute(sql)
    conn.execute(
        "ALTER TABLE workflow_a_control.client_dataset_coverage "
        "DROP CONSTRAINT ck_client_dataset_coverage_ready_complete")
    conn.execute(
        "ALTER TABLE workflow_a_control.client_dataset_coverage "
        "ADD CONSTRAINT ck_client_dataset_coverage_ready_complete CHECK (true)")
    conn.commit()
    conn.execute(sql)
    conn.commit()
    _expect_db_error(conn, _coverage_insert_sql(bootstrap_status="'READY'"))

    # An invalid pre-existing READY row prevents constraint installation.
    _bootstrap_baseline(conn)
    conn.execute(sql)
    conn.execute(
        "ALTER TABLE workflow_a_control.client_dataset_coverage "
        "DROP CONSTRAINT ck_client_dataset_coverage_ready_complete")
    conn.execute(_coverage_insert_sql(bootstrap_status="'READY'"))
    conn.commit()
    _expect_db_error(conn, sql)
    assert conn.execute(
        "SELECT bootstrap_status AS s "
        "FROM workflow_a_control.client_dataset_coverage"
    ).fetchone()["s"] == "READY", "invalid state must survive, not be rewritten"

    # Invalid bound ordering likewise blocks the constraint.
    _bootstrap_baseline(conn)
    conn.execute(sql)
    conn.execute(
        "ALTER TABLE workflow_a_control.client_dataset_coverage "
        "DROP CONSTRAINT ck_client_dataset_coverage_bounds_order")
    conn.execute(_coverage_insert_sql(
        coverage_start_ts="'2026-07-30T00:00:00Z'",
        covered_through_ts="'2026-07-29T00:00:00Z'"))
    conn.commit()
    _expect_db_error(conn, sql)

    # Duplicate pre-existing schedule rows block the primary key.
    _bootstrap_baseline(conn)
    conn.execute(sql)
    conn.execute(
        "ALTER TABLE workflow_a_control.client_dataset_coverage "
        "DROP CONSTRAINT pk_client_dataset_coverage")
    conn.execute(_coverage_insert_sql())
    conn.execute(_coverage_insert_sql())
    conn.commit()
    _expect_db_error(conn, sql)
    assert conn.execute(
        "SELECT count(*) AS n FROM workflow_a_control.client_dataset_coverage"
    ).fetchone()["n"] == 2

    # A primary key on the wrong grain is a visible stop.
    _bootstrap_baseline(conn)
    conn.execute(
        "CREATE TABLE workflow_a_control.client_dataset_coverage ("
        " schedule_id UUID NOT NULL, client_id UUID NOT NULL,"
        " dataset_name TEXT NOT NULL,"
        " PRIMARY KEY (schedule_id, dataset_name))"
    )
    conn.commit()
    message = _expect_db_error(conn, sql)
    assert "expected (schedule_id)" in message

    # A foreign-key target mismatch is a visible stop.
    _bootstrap_baseline(conn)
    conn.execute(sql)
    conn.execute(
        "ALTER TABLE workflow_a_control.client_dataset_coverage "
        "DROP CONSTRAINT fk_client_dataset_coverage_schedule")
    conn.execute(_coverage_insert_sql(schedule_id=f"'{ABSENT_SCHEDULE}'"))
    conn.commit()
    _expect_db_error(conn, sql)

    # An absent authoritative schedule table is a visible stop.
    conn.execute("DROP SCHEMA IF EXISTS workflow_a_control CASCADE")
    conn.execute("CREATE SCHEMA workflow_a_control")
    conn.commit()
    message = _expect_db_error(conn, sql)
    assert "client_dataset_schedule is absent" in message


def test_partial_state_history_evidence(conn) -> None:
    sql = _sql(MIGRATION_NAME)

    # Some evidence columns already present, compatible: converges.
    _bootstrap_baseline(conn)
    conn.execute(
        "ALTER TABLE workflow_a_control.client_schedule_run_history "
        "ADD COLUMN overlap_seconds INTEGER")
    conn.commit()
    conn.execute(sql)
    conn.commit()
    assert conn.execute(
        "SELECT count(*) AS n FROM information_schema.columns "
        "WHERE table_schema='workflow_a_control' "
        "AND table_name='client_schedule_run_history' "
        "AND column_name = ANY(%s)", (list(HISTORY_EVIDENCE_COLUMNS),)
    ).fetchone()["n"] == len(HISTORY_EVIDENCE_COLUMNS)

    # Incompatible pre-existing evidence type fails visibly.
    _bootstrap_baseline(conn)
    conn.execute(
        "ALTER TABLE workflow_a_control.client_schedule_run_history "
        "ADD COLUMN stabilization_delay_seconds BIGINT")
    conn.commit()
    message = _expect_db_error(conn, sql)
    assert "incompatible type" in message

    # A NOT NULL evidence column is rejected: claim-time evidence stays nullable.
    _bootstrap_baseline(conn)
    conn.execute(
        "ALTER TABLE workflow_a_control.client_schedule_run_history "
        "ADD COLUMN trips_pagination_mode TEXT NOT NULL DEFAULT 'strict_meta'")
    conn.commit()
    message = _expect_db_error(conn, sql)
    assert "must stay nullable" in message

    # Invalid pre-existing non-NULL evidence prevents migration completion and
    # is not rewritten.
    _bootstrap_baseline(conn)
    conn.execute(
        "ALTER TABLE workflow_a_control.client_schedule_run_history "
        "ADD COLUMN overlap_seconds INTEGER")
    conn.execute(
        "UPDATE workflow_a_control.client_schedule_run_history "
        "SET overlap_seconds = -5")
    conn.commit()
    _expect_db_error(conn, sql)
    assert conn.execute(
        "SELECT overlap_seconds AS v "
        "FROM workflow_a_control.client_schedule_run_history"
    ).fetchone()["v"] == -5

    # A same-named CHECK already present is replaced, not duplicated.
    _bootstrap_baseline(conn)
    conn.execute(
        "ALTER TABLE workflow_a_control.client_schedule_run_history "
        "ADD CONSTRAINT ck_run_history_overlap_seconds CHECK (true)")
    conn.commit()
    conn.execute(sql)
    conn.execute(sql)
    conn.commit()
    assert conn.execute(
        "SELECT count(*) AS n FROM pg_constraint "
        "WHERE conname='ck_run_history_overlap_seconds'"
    ).fetchone()["n"] == 1


def test_partial_state_history_defaults(conn) -> None:
    sql = _sql(MIGRATION_NAME)
    default_cases = (
        ("nominal_window_start_ts", "TIMESTAMPTZ", "now()"),
        ("nominal_window_end_ts", "TIMESTAMPTZ",
         "TIMESTAMPTZ '2026-01-01 00:00:00+00'"),
        ("stabilization_delay_seconds", "INTEGER", "0"),
        ("overlap_seconds", "INTEGER", "(0::integer)"),
        ("trips_pagination_mode", "TEXT", "'strict_meta'::text"),
        # Catalog presence, not expression meaning, is authoritative. Even a
        # default expression that currently evaluates to NULL is incompatible.
        ("overlap_seconds", "INTEGER", "NULLIF(0, 0)"),
    )

    for column_name, column_type, default_sql in default_cases:
        _bootstrap_baseline(conn)
        conn.execute(
            "ALTER TABLE workflow_a_control.client_schedule_run_history "
            f"ADD COLUMN {column_name} {column_type} DEFAULT {default_sql}"
        )
        conn.commit()

        before_default = conn.execute(
            """
            SELECT a.atthasdef,
                   pg_get_expr(d.adbin, d.adrelid) AS default_expression
              FROM pg_attribute a
              LEFT JOIN pg_attrdef d
                ON d.adrelid=a.attrelid AND d.adnum=a.attnum
             WHERE a.attrelid=
                   'workflow_a_control.client_schedule_run_history'::regclass
               AND a.attname=%s
               AND a.attnum > 0
               AND NOT a.attisdropped
            """,
            (column_name,),
        ).fetchone()
        before_rows = conn.execute(
            "SELECT run_history_id, schedule_id, client_id, client_code, "
            "dataset_name, window_start_ts, window_end_ts, scheduled_fire_ts, "
            "status, error_summary, created_at, " + column_name + " AS evidence "
            "FROM workflow_a_control.client_schedule_run_history "
            "ORDER BY run_history_id"
        ).fetchall()
        assert before_default["atthasdef"] is True
        assert before_default["default_expression"] is not None
        assert conn.execute(
            "SELECT to_regclass("
            "'workflow_a_control.client_dataset_coverage') AS v"
        ).fetchone()["v"] is None

        message = _expect_db_error(conn, sql)
        assert column_name in message
        assert "expected no default for claim-time evidence" in message

        after_default = conn.execute(
            """
            SELECT a.atthasdef,
                   pg_get_expr(d.adbin, d.adrelid) AS default_expression
              FROM pg_attribute a
              LEFT JOIN pg_attrdef d
                ON d.adrelid=a.attrelid AND d.adnum=a.attnum
             WHERE a.attrelid=
                   'workflow_a_control.client_schedule_run_history'::regclass
               AND a.attname=%s
               AND a.attnum > 0
               AND NOT a.attisdropped
            """,
            (column_name,),
        ).fetchone()
        after_rows = conn.execute(
            "SELECT run_history_id, schedule_id, client_id, client_code, "
            "dataset_name, window_start_ts, window_end_ts, scheduled_fire_ts, "
            "status, error_summary, created_at, " + column_name + " AS evidence "
            "FROM workflow_a_control.client_schedule_run_history "
            "ORDER BY run_history_id"
        ).fetchall()
        assert after_default == before_default, (
            column_name, before_default, after_default)
        assert after_rows == before_rows, column_name
        assert conn.execute(
            "SELECT to_regclass("
            "'workflow_a_control.client_dataset_coverage') AS v"
        ).fetchone()["v"] is None, (
            "history preflight must fail before coverage-table DDL")

        observed_columns = {
            row["column_name"]
            for row in conn.execute(
                """
                SELECT column_name
                  FROM information_schema.columns
                 WHERE table_schema='workflow_a_control'
                   AND table_name='client_schedule_run_history'
                   AND column_name = ANY(%s)
                """,
                (list(HISTORY_EVIDENCE_COLUMNS),),
            ).fetchall()
        }
        assert observed_columns == {column_name}, (
            "migration must not add other evidence columns before failing",
            column_name,
            observed_columns,
        )


# ---------------------------------------------------------------------------
# Inertness
# ---------------------------------------------------------------------------

def test_inertness(conn) -> None:
    _bootstrap_baseline(conn)
    snapshot = {
        "accounts": conn.execute(
            "SELECT client_id, trips_pagination_mode, "
            "trips_stabilization_delay_seconds, trips_overlap_seconds, "
            "trips_max_recovery_span_seconds "
            "FROM workflow_a_control.client_account ORDER BY client_id"
        ).fetchall(),
        "schedules": conn.execute(
            "SELECT * FROM workflow_a_control.client_dataset_schedule "
            "ORDER BY schedule_id"
        ).fetchall(),
        "history": conn.execute(
            "SELECT run_history_id, schedule_id, client_id, client_code, "
            "dataset_name, window_start_ts, window_end_ts, scheduled_fire_ts, "
            "status, platform_run_id, error_summary, started_at, finished_at, "
            "created_at FROM workflow_a_control.client_schedule_run_history "
            "ORDER BY run_history_id"
        ).fetchall(),
    }
    triggers_before = conn.execute(
        "SELECT count(*) AS n FROM pg_trigger t "
        "JOIN pg_class c ON c.oid=t.tgrelid "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='workflow_a_control' AND NOT t.tgisinternal"
    ).fetchone()["n"]

    conn.execute(_sql(MIGRATION_NAME))
    conn.commit()

    assert conn.execute(
        "SELECT client_id, trips_pagination_mode, "
        "trips_stabilization_delay_seconds, trips_overlap_seconds, "
        "trips_max_recovery_span_seconds "
        "FROM workflow_a_control.client_account ORDER BY client_id"
    ).fetchall() == snapshot["accounts"]
    assert conn.execute(
        "SELECT count(*) AS n FROM workflow_a_control.client_account "
        "WHERE trips_pagination_mode <> 'strict_meta'"
    ).fetchone()["n"] == 0, "no client may become data_invariants_v1"
    assert conn.execute(
        "SELECT * FROM workflow_a_control.client_dataset_schedule "
        "ORDER BY schedule_id"
    ).fetchall() == snapshot["schedules"]
    assert conn.execute(
        "SELECT run_history_id, schedule_id, client_id, client_code, "
        "dataset_name, window_start_ts, window_end_ts, scheduled_fire_ts, "
        "status, platform_run_id, error_summary, started_at, finished_at, "
        "created_at FROM workflow_a_control.client_schedule_run_history "
        "ORDER BY run_history_id"
    ).fetchall() == snapshot["history"], "no history status or window may change"
    assert conn.execute(
        "SELECT count(*) AS n FROM workflow_a_control.client_schedule_run_history "
        "WHERE nominal_window_start_ts IS NOT NULL "
        "OR nominal_window_end_ts IS NOT NULL "
        "OR stabilization_delay_seconds IS NOT NULL "
        "OR overlap_seconds IS NOT NULL OR trips_pagination_mode IS NOT NULL"
    ).fetchone()["n"] == 0, "no historical evidence may be invented"
    assert conn.execute(
        "SELECT count(*) AS n FROM workflow_a_control.client_dataset_coverage"
    ).fetchone()["n"] == 0, "no coverage row may be seeded"
    assert conn.execute(
        "SELECT count(*) AS n FROM pg_trigger t "
        "JOIN pg_class c ON c.oid=t.tgrelid "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='workflow_a_control' AND NOT t.tgisinternal"
    ).fetchone()["n"] == triggers_before, "no trigger may be created"


def test_on_disposable_postgres(dsn: str) -> None:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row, autocommit=False) as conn:
        test_coverage_table_shape(conn)
        test_coverage_owner_and_acl(conn)
        test_coverage_states(conn)
        test_coverage_cascade_and_rerun(conn)
        test_history_evidence_columns(conn)
        test_partial_state_coverage_table(conn)
        test_partial_state_history_evidence(conn)
        test_partial_state_history_defaults(conn)
        test_inertness(conn)
        conn.rollback()


def main() -> None:
    test_migration_file_contract()
    test_no_runtime_reference_to_new_objects()
    test_runtime_inertness_guard_synthetic_cases()
    test_bootstrap_writer_guard_synthetic_cases()
    test_cold_start_registration_is_exact()
    test_cold_start_deny_by_default_is_preserved()
    test_cold_start_read_only_audit_stays_read_only()
    test_cold_start_writer_guard_synthetic_cases()
    test_cold_start_readers_hold_no_coverage_statement()
    dsn = os.getenv("TELEMATICS_COVERAGE_STATE_TEST_DSN")
    if dsn:
        require_loopback_dsn_or_exit(
            dsn, label="TELEMATICS_COVERAGE_STATE_TEST_DSN",
        )
        test_on_disposable_postgres(dsn)
        print("PASS: disposable PostgreSQL coverage-schema checks")
    else:
        print("SKIP: set TELEMATICS_COVERAGE_STATE_TEST_DSN for PostgreSQL checks")
    print("OK - Telematics stabilized coverage state schema checks passed")


if __name__ == "__main__":
    main()
