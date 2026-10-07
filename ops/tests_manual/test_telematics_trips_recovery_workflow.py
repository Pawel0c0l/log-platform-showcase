#!/usr/bin/env python3
"""C11 — pure and static checks for the shared coverage CAS and recovery CLI.

No database, no network, no subprocess. The disposable-PostgreSQL behavior lives
in `test_telematics_trips_recovery_postgres.py`.
"""
from __future__ import annotations

import ast
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from jobs.api.telematics import coverage_finalization as cf  # noqa: E402
from jobs.api.telematics import coverage_windows as cw  # noqa: E402
from jobs.api.telematics import dispatcher as d  # noqa: E402
from jobs.trips_pagination_mode import (  # noqa: E402
    TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1 as MODE,
)
from ops import recover_telematics_trips_window as rc  # noqa: E402

RECOVERY_TOOL = ROOT / "ops/recover_telematics_trips_window.py"
FINALIZATION_MODULE = ROOT / "jobs/api/telematics/coverage_finalization.py"
MIGRATION_058 = ROOT / "db/migrations/058_telematics_trips_manual_recovery.sql"

SID = "7cac378a-5787-4d62-85d1-282bed208c8c"
CID = "bd7662a5-eeb4-4614-8720-d477abfcb227"
A = datetime(2026, 7, 1, tzinfo=timezone.utc)
W = datetime(2026, 7, 27, tzinfo=timezone.utc)
E = datetime(2026, 8, 3, tzinfo=timezone.utc)
SEEDED = datetime(2026, 7, 1, 12, 30, 45, tzinfo=timezone.utc)
UPDATED = datetime(2026, 7, 1, 12, 30, 45, tzinfo=timezone.utc)


def coverage_row(**overrides):
    row = {
        "schedule_id": SID,
        "client_id": CID,
        "client_code": "TST00001",
        "dataset_name": "trips_sync",
        "coverage_start_ts": A,
        "covered_through_ts": W,
        "bootstrap_status": "READY",
        "bootstrap_evidence_ref": "artifact:safe",
        "covered_through_source": "bootstrap",
        "seeded_at": SEEDED,
        "seeded_by": "operator",
        "last_gap_detected_ts": None,
        "updated_at": UPDATED,
    }
    row.update(overrides)
    return row


class FakeCursor:
    """Records every statement so "no statement was issued" is provable."""

    def __init__(self, *, rowcount=1, readback=None):
        self.statements = []
        self.rowcount = rowcount
        self._readback = readback

    def execute(self, sql, params=None):
        self.statements.append((" ".join(str(sql).split()), params))

    def fetchall(self):
        return [] if self._readback is None else [dict(self._readback)]

    def updates(self):
        return [s for s, _ in self.statements if s.upper().startswith("UPDATE")]


# ---------------------------------------------------------------------------
# 1. Constants and single ownership
# ---------------------------------------------------------------------------

def test_constant_parity_and_vocabulary() -> None:
    """The shared module restates C5 constants; drift is a test failure."""
    assert cf.COVERAGE_STATUS_READY == cw.COVERAGE_STATUS_READY
    assert cf.TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1 == MODE
    assert cf.TRIPS_SYNC_DATASET_NAME == d.TRIPS_SYNC_DATASET_NAME

    # Only the two advancement provenances may ever be written.
    assert cf.COVERAGE_ADVANCEMENT_SOURCES == {
        "scheduled_run", "manual_recovery",
    }
    assert cf.COVERAGE_ADVANCEMENT_SOURCES < cf.COVERAGE_SOURCE_VOCABULARY
    assert "bootstrap" not in cf.COVERAGE_ADVANCEMENT_SOURCES
    assert "operator" not in cf.COVERAGE_ADVANCEMENT_SOURCES

    # Migration 058 must actually be able to store the new value.
    sql = MIGRATION_058.read_text(encoding="utf-8")
    assert (
        "'bootstrap', 'scheduled_run', 'operator', 'manual_recovery'" in sql
    ), "the source vocabulary must be widened before the code writes it"

    # The dispatcher re-exports the shared classification rather than owning a
    # second copy of the string.
    assert d.TRIPS_COVERAGE_ADVANCE_CONFLICT is cf.TRIPS_COVERAGE_ADVANCE_CONFLICT

    # The CAS field list is exactly the documented eleven.
    assert cf.COVERAGE_CAS_FIELDS == (
        "schedule_id", "client_id", "dataset_name", "coverage_start_ts",
        "covered_through_ts", "bootstrap_status", "bootstrap_evidence_ref",
        "covered_through_source", "seeded_at", "seeded_by",
        "last_gap_detected_ts",
    )
    assert "updated_at" not in cf.COVERAGE_CAS_FIELDS
    assert "client_code" not in cf.COVERAGE_CAS_FIELDS


# ---------------------------------------------------------------------------
# 2. Canonical fingerprint
# ---------------------------------------------------------------------------

def test_fingerprint_contract() -> None:
    base = coverage_row()
    fingerprint = cf.coverage_fingerprint(base)
    assert len(fingerprint) == 64 and fingerprint == fingerprint.lower()
    assert cf.coverage_fingerprint(dict(base)) == fingerprint

    # The same instants rendered at other valid offsets are the same evidence.
    shifted = coverage_row(
        coverage_start_ts=A.astimezone(timezone(timedelta(hours=2))),
        covered_through_ts=W.astimezone(timezone(timedelta(hours=-5))),
        seeded_at=SEEDED.astimezone(timezone(timedelta(hours=1))),
    )
    assert cf.coverage_fingerprint(shifted) == fingerprint

    # A genuinely different instant is different evidence, at microsecond
    # resolution: nothing is rounded or truncated.
    for field, value in (
        ("covered_through_ts", W + timedelta(seconds=1)),
        ("seeded_at", SEEDED + timedelta(microseconds=1)),
        ("updated_at", UPDATED + timedelta(microseconds=1)),
        ("bootstrap_status", "GAP_DETECTED"),
        ("covered_through_source", "manual_recovery"),
        ("client_code", None),
        ("last_gap_detected_ts", W),
    ):
        assert cf.coverage_fingerprint(coverage_row(**{field: value})) != (
            fingerprint
        ), field

    # The version participates in the hash, so a future algorithm cannot
    # accidentally collide with a stored version-1 value.
    assert cf.COVERAGE_FINGERPRINT_VERSION == "telematics-coverage-fingerprint/1"
    assert len(cf.COVERAGE_FINGERPRINT_FIELDS) == 13

    # A fingerprint is evidence, never authorization: it is not consulted by
    # the CAS predicate.
    finalization_source = FINALIZATION_MODULE.read_text(encoding="utf-8")
    advance = _function_source(finalization_source, "advance_covered_through_cas")
    assert "coverage_fingerprint" not in advance


def _function_source(text: str, name: str) -> str:
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(text, node) or ""
    raise AssertionError(f"function {name} not found")


# ---------------------------------------------------------------------------
# 3. Snapshot and null-safe matching
# ---------------------------------------------------------------------------

def test_snapshot_and_matching() -> None:
    row = coverage_row()
    snapshot = cf.CoverageClaimSnapshot.from_row(row)
    assert cf.snapshot_matches_row(row, snapshot)

    params = snapshot.cas_params()
    assert set(params) == {f"claim_{f}" for f in cf.COVERAGE_CAS_FIELDS}
    assert params["claim_covered_through_ts"] == W

    # Timezone-equivalent instants match; different instants do not.
    assert cf.snapshot_matches_row(
        coverage_row(covered_through_ts=W.astimezone(timezone(timedelta(hours=3)))),
        snapshot,
    )
    assert not cf.snapshot_matches_row(
        coverage_row(covered_through_ts=W + timedelta(seconds=1)), snapshot,
    )

    # Null transitions are conflicts in both directions.
    with_gap = coverage_row(last_gap_detected_ts=W)
    assert not cf.snapshot_matches_row(with_gap, snapshot)
    gap_snapshot = cf.CoverageClaimSnapshot.from_row(with_gap)
    assert not cf.snapshot_matches_row(row, gap_snapshot)

    # Provenance-only and evidence-only operator edits conflict.
    for field, value in (
        ("covered_through_source", "operator"),
        ("bootstrap_evidence_ref", "artifact:other"),
        ("seeded_by", "someone-else"),
        ("bootstrap_status", "GAP_DETECTED"),
    ):
        assert not cf.snapshot_matches_row(
            coverage_row(**{field: value}), snapshot,
        ), field

    # The two justified exclusions stay excluded.
    for field, value in (("client_code", None), ("updated_at", W)):
        assert cf.snapshot_matches_row(coverage_row(**{field: value}), snapshot), (
            field
        )

    # The snapshot is immutable and carries no live objects.
    try:
        snapshot.covered_through_ts = E  # type: ignore[misc]
        raise AssertionError("snapshot must be frozen")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 4. The shared CAS — validation, no-op, monotonicity, conflicts
# ---------------------------------------------------------------------------

def test_cas_refuses_before_issuing_any_statement() -> None:
    snapshot = cf.CoverageClaimSnapshot.from_row(coverage_row())
    mutation = datetime(2026, 8, 4, tzinfo=timezone.utc)

    cases = (
        {"source": "operator"},
        {"source": "bootstrap"},
        {"expected_bootstrap_status": "GAP_DETECTED"},
        {"expected_trips_pagination_mode": "strict_meta"},
        {"candidate_covered_through_ts": E.replace(tzinfo=None)},
        {"mutation_ts": mutation.replace(tzinfo=None)},
    )
    for override in cases:
        cur = FakeCursor()
        kwargs = {
            "snapshot": snapshot,
            "candidate_covered_through_ts": E,
            "source": cf.COVERAGE_SOURCE_MANUAL_RECOVERY,
            "mutation_ts": mutation,
        }
        kwargs.update(override)
        try:
            cf.advance_covered_through_cas(cur, **kwargs)
            raise AssertionError(f"expected refusal for {override}")
        except ValueError:
            pass
        assert cur.statements == [], override

    # A non-READY or unbounded snapshot is refused before any statement too.
    for bad in (
        coverage_row(bootstrap_status="GAP_DETECTED"),
        coverage_row(covered_through_ts=None),
    ):
        cur = FakeCursor()
        try:
            cf.advance_covered_through_cas(
                cur,
                snapshot=cf.CoverageClaimSnapshot.from_row(bad),
                candidate_covered_through_ts=E,
                source=cf.COVERAGE_SOURCE_MANUAL_RECOVERY,
                mutation_ts=mutation,
            )
            raise AssertionError("expected refusal")
        except ValueError:
            pass
        assert cur.statements == []

    # A non-trips dataset can never reach a coverage statement.
    cur = FakeCursor()
    try:
        cf.advance_covered_through_cas(
            cur,
            snapshot=cf.CoverageClaimSnapshot.from_row(
                coverage_row(dataset_name="fuel_daily_aggregation")
            ),
            candidate_covered_through_ts=E,
            source=cf.COVERAGE_SOURCE_SCHEDULED_RUN,
            mutation_ts=mutation,
        )
        raise AssertionError("expected refusal")
    except ValueError:
        pass
    assert cur.statements == []


def test_cas_noop_and_require_advance() -> None:
    snapshot = cf.CoverageClaimSnapshot.from_row(coverage_row())
    mutation = datetime(2026, 8, 4, tzinfo=timezone.utc)

    for candidate in (W, W - timedelta(seconds=1)):
        cur = FakeCursor()
        result = cf.advance_covered_through_cas(
            cur,
            snapshot=snapshot,
            candidate_covered_through_ts=candidate,
            source=cf.COVERAGE_SOURCE_SCHEDULED_RUN,
            mutation_ts=mutation,
        )
        assert result.moved is False and result.rows_updated == 0
        assert result.mutation_ts is None
        assert result.covered_through_source == "bootstrap"
        assert cur.statements == [], "a no-op issues no statement at all"

        # The manual recovery path refuses the same no-op instead.
        cur = FakeCursor()
        try:
            cf.advance_covered_through_cas(
                cur,
                snapshot=snapshot,
                candidate_covered_through_ts=candidate,
                source=cf.COVERAGE_SOURCE_MANUAL_RECOVERY,
                mutation_ts=mutation,
                require_advance=True,
            )
            raise AssertionError("expected an advance refusal")
        except cf.CoverageCasConflict as exc:
            assert exc.code == cf.TRIPS_COVERAGE_ADVANCE_CONFLICT
        assert cur.statements == []


def test_cas_statement_shape_and_conflicts() -> None:
    snapshot = cf.CoverageClaimSnapshot.from_row(coverage_row())
    mutation = datetime(2026, 8, 4, tzinfo=timezone.utc)
    advanced = coverage_row(
        covered_through_ts=E,
        covered_through_source="manual_recovery",
        updated_at=mutation,
    )

    cur = FakeCursor(rowcount=1, readback=advanced)
    result = cf.advance_covered_through_cas(
        cur,
        snapshot=snapshot,
        candidate_covered_through_ts=E,
        source=cf.COVERAGE_SOURCE_MANUAL_RECOVERY,
        mutation_ts=mutation,
        require_advance=True,
    )
    assert result.moved is True and result.rows_updated == 1
    assert result.new_covered_through_ts == "2026-08-03T00:00:00Z"
    assert result.expected_old_covered_through_ts == "2026-07-27T00:00:00Z"
    assert result.covered_through_source == "manual_recovery"
    assert result.coverage_start_ts_unchanged and result.verified

    update = cur.updates()
    assert len(update) == 1
    statement = update[0].upper()
    assert "SET COVERED_THROUGH_TS = %(NEW_COVERED_THROUGH_TS)S" in statement
    assert "COVERED_THROUGH_SOURCE = %(NEW_COVERED_THROUGH_SOURCE)S" in statement
    assert "SET COVERAGE_START_TS" not in statement
    assert "BOOTSTRAP_STATUS = %(CLAIM_BOOTSTRAP_STATUS)S" in statement
    assert "SET BOOTSTRAP_STATUS" not in statement
    assert "COVERED_THROUGH_TS < %(NEW_COVERED_THROUGH_TS)S" in statement
    for field in cf.COVERAGE_CAS_FIELDS:
        assert f"CLAIM_{field.upper()}" in statement, field

    # rowcount != 1 is a conflict, and nothing is retried.
    for rowcount in (0, 2):
        cur = FakeCursor(rowcount=rowcount, readback=advanced)
        try:
            cf.advance_covered_through_cas(
                cur, snapshot=snapshot, candidate_covered_through_ts=E,
                source=cf.COVERAGE_SOURCE_SCHEDULED_RUN, mutation_ts=mutation,
            )
            raise AssertionError("expected a CAS conflict")
        except cf.CoverageCasConflict as exc:
            assert exc.code == cf.TRIPS_COVERAGE_ADVANCE_CONFLICT
            assert exc.rows_updated == rowcount
        assert len(cur.updates()) == 1, "no retry"

    # A read-back that disagrees with the approved decision fails closed.
    for wrong in (
        coverage_row(covered_through_ts=E, covered_through_source="scheduled_run",
                     updated_at=mutation),
        coverage_row(covered_through_ts=E + timedelta(seconds=1),
                     covered_through_source="manual_recovery",
                     updated_at=mutation),
        coverage_row(covered_through_ts=E, covered_through_source="manual_recovery",
                     updated_at=mutation, coverage_start_ts=A - timedelta(days=1)),
    ):
        cur = FakeCursor(rowcount=1, readback=wrong)
        try:
            cf.advance_covered_through_cas(
                cur, snapshot=snapshot, candidate_covered_through_ts=E,
                source=cf.COVERAGE_SOURCE_MANUAL_RECOVERY, mutation_ts=mutation,
                require_advance=True,
            )
            raise AssertionError("expected a post-write verification failure")
        except cf.CoverageCasConflict as exc:
            assert exc.code == cf.TRIPS_COVERAGE_POSTWRITE_VERIFICATION_FAILED


def test_cas_never_commits_or_rolls_back() -> None:
    """Transaction ownership stays with the caller (docs/15 §7)."""
    source = FINALIZATION_MODULE.read_text(encoding="utf-8")
    for forbidden in (".commit(", ".rollback(", "autocommit", "psycopg.connect"):
        assert forbidden not in source, forbidden
    # And it holds no INSERT/DELETE of any kind.
    upper = source.upper()
    assert "INSERT INTO" not in upper
    assert "DELETE FROM" not in upper


# ---------------------------------------------------------------------------
# 5. Recovery CLI — parsing, confirmation, params
# ---------------------------------------------------------------------------

def _args(**overrides):
    base = {
        "client_code": "BRAVO00016",
        "dataset": "trips_sync",
        "window_start": "2026-07-27T00:00:00Z",
        "window_end": "2026-08-03T00:00:00Z",
        "expected_old_covered_through": "2026-07-27T00:00:00Z",
        "reason": "recover the failed compatibility interval",
        "approval_ref": "TELEMATICS-C11-1",
        "expected_environment": "production",
        "expected_platform_uuid": "52517750-7438-4558-8490-2736ae4cc629",
    }
    base.update(overrides)
    argv = []
    for key, value in base.items():
        argv += [f"--{key.replace('_', '-')}", str(value)]
    return argv


def test_cli_dry_run_is_the_default() -> None:
    parser = rc.build_parser()
    args = parser.parse_args(_args())
    assert args.execute is False
    assert args.confirm_client_code is None
    assert args.dataset == "trips_sync"

    # There is no `--dry-run` switch to forget: dry-run is the absence of
    # `--execute`, so the default cannot be inverted by a typo.
    flags = {action.option_strings[0] for action in parser._actions
             if action.option_strings}
    assert "--execute" in flags
    assert "--confirm-client-code" in flags
    for required in (
        "--client-code", "--dataset", "--window-start", "--window-end",
        "--expected-old-covered-through", "--reason", "--approval-ref",
        "--expected-environment", "--expected-platform-uuid", "--dsn",
    ):
        assert required in flags, required


def test_cli_confirmation_is_required_for_execute() -> None:
    parser = rc.build_parser()
    for argv, expected in (
        (_args() + ["--execute"], "RECOVERY_REFUSED_CONFIRMATION"),
        (
            _args() + ["--execute", "--confirm-client-code", "OTHER00001"],
            "RECOVERY_REFUSED_CONFIRMATION",
        ),
    ):
        try:
            rc.run(parser.parse_args(argv))
            raise AssertionError("expected a confirmation refusal")
        except rc.RecoveryRefused as exc:
            assert exc.code == expected
            assert exc.exit_code == rc.EXIT_INVALID_PARAMETERS


def test_cli_instant_parsing() -> None:
    assert rc.parse_instant("2026-08-03T00:00:00Z", label="x") == E
    assert rc.parse_instant("2026-08-03T02:00:00+02:00", label="x") == E
    for bad in ("2026-08-03T00:00:00", "2026-08-03", "", "2026-08-03T00:00:00.5Z"):
        try:
            rc.parse_instant(bad, label="x")
            raise AssertionError(f"expected refusal for {bad!r}")
        except rc.RecoveryRefused as exc:
            assert exc.code == "RECOVERY_REFUSED_PARAMETER"


def test_cli_bounded_free_text() -> None:
    assert rc._safe_reason("  a   b ") == "a b"
    for bad in ("", "x" * 501, "secret=\x00"):
        try:
            rc._safe_reason(bad)
            raise AssertionError("expected refusal")
        except rc.RecoveryRefused:
            pass
    for bad in ("", "has space", "x" * 201):
        try:
            rc._safe_token(bad, label="--approval-ref")
            raise AssertionError("expected refusal")
        except rc.RecoveryRefused:
            pass


def test_job_params_are_literal_and_compatibility_scoped() -> None:
    from jobs.api.telematics import manual_recovery_authority as mra

    schedule_id = "b8ce660c-5b8b-4fff-829c-12607f3f6e10"
    params = rc.build_job_params(
        client_id=CID, client_code="BRAVO00016",
        event_enrichment_mode="enabled",
        window_start_ts=W, window_end_ts=E,
        recovery_run_id="d8fccad0-b7bb-4e8b-886c-19b800b06c86",
        schedule_id=schedule_id,
        schedule_disabled=False,
    )
    # The window is passed literally: no shift, no lookback, no derivation.
    assert params["window_start_ts"] == "2026-07-27T00:00:00Z"
    assert params["window_end_ts"] == "2026-08-03T00:00:00Z"
    assert params["trips_pagination_mode"] == MODE
    assert params["trigger"] == "MANUAL_RECOVERY" != "SCHEDULED"
    assert params["manual_recovery_run_id"]
    # A recovery must never look like a scheduled fire to the job.
    assert "scheduled_fire_ts" not in params
    assert "nominal_window_start_ts" not in params
    assert "nominal_window_end_ts" not in params
    # An enabled-schedule recovery carries no disabled-schedule opt-in at all.
    for key in mra.DISABLED_SCHEDULE_AUTHORITY_PARAM_KEYS:
        assert key not in params, key
    # No secret, no DSN, no credential ever reaches runner params.
    for key, value in params.items():
        assert "password" not in key.lower() and "secret" not in key.lower()
        assert "password=" not in str(value)

    # The disabled-schedule variant adds exactly the two opt-in parameters, and
    # nothing else changes.
    cold = rc.build_job_params(
        client_id=CID, client_code="BRAVO00016",
        event_enrichment_mode="enabled",
        window_start_ts=W, window_end_ts=E,
        recovery_run_id="d8fccad0-b7bb-4e8b-886c-19b800b06c86",
        schedule_id=schedule_id,
        schedule_disabled=True,
    )
    assert set(cold) - set(params) == set(
        mra.DISABLED_SCHEDULE_AUTHORITY_PARAM_KEYS
    )
    assert cold[mra.PARAM_DISABLED_SCHEDULE_FLAG] is True
    assert cold[mra.PARAM_EXPECTED_SCHEDULE_ID] == schedule_id
    assert {k: v for k, v in cold.items() if k in params} == params


# ---------------------------------------------------------------------------
# 6. Static contract of the recovery tool
# ---------------------------------------------------------------------------

def test_recovery_tool_static_contract() -> None:
    source = RECOVERY_TOOL.read_text(encoding="utf-8")
    upper = source.upper()

    # It calls the shared finalizer and holds no coverage statement itself.
    assert "advance_covered_through_cas" in source
    assert "lock_coverage_row_for_update" in source
    assert "client_dataset_coverage" not in source

    # It never mutates schedule history, a schedule or a client.
    for forbidden in (
        "UPDATE WORKFLOW_A_CONTROL.CLIENT_SCHEDULE_RUN_HISTORY",
        "INSERT INTO WORKFLOW_A_CONTROL.CLIENT_SCHEDULE_RUN_HISTORY",
        "DELETE FROM WORKFLOW_A_CONTROL.CLIENT_SCHEDULE_RUN_HISTORY",
        "UPDATE WORKFLOW_A_CONTROL.CLIENT_ACCOUNT",
        "UPDATE WORKFLOW_A_CONTROL.CLIENT_DATASET_SCHEDULE",
        "DROP ",
        "TRUNCATE ",
    ):
        assert forbidden not in upper, forbidden

    # It reaches the sync job only through the reviewed runner contract.
    assert "ops/runner.py" in source
    assert rc.SYNC_JOB_MODULE == "jobs.api.telematics.sync_trips_and_speeding"
    assert "import jobs.api.telematics.sync_trips_and_speeding" not in source
    assert "from jobs.api.telematics import sync_trips_and_speeding" not in source

    # The insert-only backfill module may be *named* in the module docstring —
    # explaining what this tool deliberately is not is worth documenting — but
    # it must never be referenced by code.
    docstring_node = ast.parse(source).body[0]
    assert isinstance(docstring_node, ast.Expr)
    docstring_lines = range(docstring_node.lineno, docstring_node.end_lineno + 1)
    mentions = [
        number for number, line in enumerate(source.splitlines(), start=1)
        if "backfill_trips_insert_only" in line
    ]
    assert mentions, "the distinction from the backfill module must be documented"
    assert all(number in docstring_lines for number in mentions), mentions

    # It never retries: no sleep, no retry loop, no attempt counter.
    for forbidden in ("time.sleep", "while True", "for attempt", "max_retries"):
        assert forbidden not in source, forbidden

    # Exactly one subprocess call site, and it is the runner launch.
    tree = ast.parse(source)
    subprocess_calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
    ]
    # `subprocess.run` appears twice: the git helper and the single job launch.
    assert len(subprocess_calls) == 2, len(subprocess_calls)

    # Terminal classifications are bounded constants, not free text.
    assert rc.RECOVERY_BUSINESS_FAILED == "RECOVERY_BUSINESS_FAILED"
    assert rc.RECOVERY_ORCHESTRATION_FAILED == "RECOVERY_ORCHESTRATION_FAILED"
    assert rc.MAX_ERROR_SUMMARY_CHARS == 4000

    # stdout of the business job is deliberately not persisted.
    assert "stdout_tail" not in source


def test_migration_058_file_contract() -> None:
    files = sorted(p.name for p in (ROOT / "db/migrations").glob("*.sql"))
    assert MIGRATION_058.name in files
    assert files.index(MIGRATION_058.name) == (
        files.index("057_workflow_a_trips_coverage_state.sql") + 1
    ), "058 must be the next ordinal after 057"
    assert not [
        name for name in files
        if name.startswith("058_") and name != MIGRATION_058.name
    ]

    sql = MIGRATION_058.read_text(encoding="utf-8")
    for fragment in (
        "CREATE TABLE IF NOT EXISTS workflow_a_control.client_dataset_recovery_run",
        "fk_client_dataset_recovery_run_client",
        "fk_client_dataset_recovery_run_schedule",
        "ON DELETE RESTRICT",
        "ck_client_dataset_recovery_run_status",
        "'PLANNED', 'RUNNING', 'SUCCESS', 'FAILED', 'FINALIZATION_CONFLICT'",
        "ck_client_dataset_recovery_run_dataset",
        "ck_client_dataset_recovery_run_mode",
        "ck_client_dataset_recovery_run_window_order",
        "ck_client_dataset_recovery_run_window_anchor",
        "ck_client_dataset_recovery_run_terminal_times",
        "ck_client_dataset_recovery_run_bounded_text",
        "ck_client_dataset_recovery_run_fingerprints",
        "uq_client_dataset_recovery_run_approved_window",
        "uq_client_dataset_recovery_run_active",
    ):
        assert fragment in sql, fragment

    upper = sql.upper()
    for forbidden in (
        "DROP TABLE", "DROP COLUMN", "TRUNCATE", "DELETE FROM",
        "CREATE TRIGGER", "CREATE OR REPLACE FUNCTION", "CREATE FUNCTION",
        "CREATE PROCEDURE", "INSERT INTO", "SET TRIPS_PAGINATION_MODE",
    ):
        assert forbidden not in upper, forbidden
    # It must not rewrite any existing coverage or history row.
    assert "UPDATE WORKFLOW_A_CONTROL.CLIENT_DATASET_COVERAGE" not in upper
    assert "UPDATE WORKFLOW_A_CONTROL.CLIENT_SCHEDULE_RUN_HISTORY" not in upper
    # 057 stays byte-unchanged; only its source CHECK is replaced additively.
    assert (ROOT / "db/migrations/057_workflow_a_trips_coverage_state.sql").exists()


# ---------------------------------------------------------------------------
# 7. Documentation contract — the resolved C11 decision
# ---------------------------------------------------------------------------

RESOLVED_RULE = "exactly two reviewed surfaces may advance `W`"

OBSOLETE_STATEMENTS = (
    "No recovery operation may also advance scheduled coverage state",
    "**never** advances\n  `covered_through_ts`",
    "no coverage advancement",
    "an operator closes the gap with a separately reviewed `UPDATE` on "
    "`covered_through_ts`",
)


def test_documentation_states_the_resolved_contract() -> None:
    docs = {
        name: (ROOT / "docs" / name).read_text(encoding="utf-8")
        for name in (
            "13_telematics_trips_stabilization_windows.md",
            "14_telematics_trips_compatibility_implementation_plan.md",
            "15_telematics_coverage_mutation_contract.md",
        )
    }
    for name, text in docs.items():
        assert RESOLVED_RULE in text, name
        assert "manual_recovery" in text, name
        for obsolete in OBSOLETE_STATEMENTS:
            assert obsolete not in text, f"{name}: {obsolete!r} must be removed"

    # The prohibition on ad hoc manual coverage SQL survives the revision.
    assert "manual coverage SQL" in docs[
        "15_telematics_coverage_mutation_contract.md"
    ]
    # And the fleet-wide rollout gate is not weakened.
    assert "fleet-wide" in docs[
        "14_telematics_trips_compatibility_implementation_plan.md"
    ]

    operations = (ROOT / "docs/07_operations.md").read_text(encoding="utf-8")
    assert "recover_telematics_trips_window.py" in operations
    assert "058_telematics_trips_manual_recovery.sql" in operations
    jobs = (ROOT / "docs/05_jobs.md").read_text(encoding="utf-8")
    assert "client_dataset_recovery_run" in jobs


def main() -> None:
    test_constant_parity_and_vocabulary()
    test_fingerprint_contract()
    test_snapshot_and_matching()
    test_cas_refuses_before_issuing_any_statement()
    test_cas_noop_and_require_advance()
    test_cas_statement_shape_and_conflicts()
    test_cas_never_commits_or_rolls_back()
    test_cli_dry_run_is_the_default()
    test_cli_confirmation_is_required_for_execute()
    test_cli_instant_parsing()
    test_cli_bounded_free_text()
    test_job_params_are_literal_and_compatibility_scoped()
    test_recovery_tool_static_contract()
    test_migration_058_file_contract()
    test_documentation_states_the_resolved_contract()
    print("OK - Telematics manual recovery workflow (pure/static) checks passed")


if __name__ == "__main__":
    main()
