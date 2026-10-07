#!/usr/bin/env python3
"""Focused tests for the dry-run-first Telematics coverage bootstrap writer.

Every check here needs a *disposable* PostgreSQL 16 database — never logdb —
because the writer's dry-run path is itself a live preflight:

  docker run -d --rm --name c10-bootstrap-pg -e POSTGRES_PASSWORD=... \\
      -e POSTGRES_USER=loguser -e POSTGRES_DB=c10_bootstrap_test \\
      -p 55707:5432 postgres:16
  TELEMATICS_BOOTSTRAP_WRITER_TEST_DSN='postgresql://loguser:...@127.0.0.1:55707/c10_bootstrap_test' \\
      .venv/bin/python ops/tests_manual/test_telematics_coverage_bootstrap_writer_postgres.py

The suite proves the writer inserts exactly one approved row, refuses every
ambiguous or racing state, and never mutates anything else.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ops import audit_telematics_coverage_bootstrap as audit  # noqa: E402
from ops import bootstrap_telematics_trips_coverage as writer  # noqa: E402
from ops.tests_manual.postgres_dsn_safety import (  # noqa: E402
    require_loopback_dsn_or_exit,
)
from ops.tests_manual.test_telematics_coverage_bootstrap_audit import (  # noqa: E402
    CLIENT_CODE,
    CLIENT_ID,
    ENVIRONMENT,
    PLATFORM_UUID,
    SCHEDULE_TRIPS,
    build_fixture,
)

UTC = timezone.utc

# A fully proven week, then a two-day hole, then a proven tail. The hole is what
# every interval-selection case below is measured against.
HISTORY = (
    ("2026-07-20T02:00:00Z", "SUCCESS"),
    ("2026-07-21T02:00:00Z", "SUCCESS"),
    ("2026-07-22T02:00:00Z", "SUCCESS"),
    ("2026-07-23T02:00:00Z", "SUCCESS"),
    ("2026-07-24T02:00:00Z", "SUCCESS"),
    ("2026-07-25T02:00:00Z", "SUCCESS"),
    ("2026-07-26T02:00:00Z", "SUCCESS"),
    ("2026-07-27T02:00:00Z", "SUCCESS"),
    # 2026-07-28 and 2026-07-29 produced no row at all.
    ("2026-07-30T02:00:00Z", "SUCCESS"),
    ("2026-07-31T02:00:00Z", "SUCCESS"),
    ("2026-08-01T02:00:00Z", "SUCCESS"),
)
# The missing fires imply unresolved nominal windows of
# [2026-07-27T02:00Z, 2026-07-28T02:00Z] and [2026-07-28T02:00Z, 2026-07-29T02:00Z].
GAP_START = "2026-07-27T02:00:00Z"
GAP_END = "2026-07-29T02:00:00Z"

# Selected entirely after the hole: the honest narrow claim of docs/13 §13.3.
GOOD_A = "2026-07-30T00:00:00Z"
GOOD_W = "2026-08-01T02:00:00Z"

SEEDED_BY = "operator@example.invalid"
APPROVAL_REF = "OPS-1234"


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _generate_bundle(dsn: str, tmp: Path, **overrides) -> tuple[dict, Path]:
    args = Namespace(
        client_code=CLIENT_CODE,
        dataset="trips_sync",
        output=str(tmp / "bundle.json"),
        expected_environment=ENVIRONMENT,
        expected_platform_uuid=PLATFORM_UUID,
        range_start="2026-07-20T00:00:00Z",
        range_end="2026-08-01T12:00:00Z",
        dsn=dsn,
        **overrides,
    )
    bundle, path = audit.run_audit(args)
    return bundle, path


def _rewrite_bundle(path: Path, bundle: dict, **changes) -> str:
    """Rewrite a bundle with a recomputed self-hash. Returns the new hash."""
    mutated = {**bundle, **changes}
    mutated["bundle_sha256"] = audit.bundle_sha256(mutated)
    path.write_text(audit.canonical_json(mutated) + "\n", encoding="utf-8")
    return mutated["bundle_sha256"]


def _writer_args(dsn: str, bundle_path: Path, digest: str, **overrides) -> Namespace:
    values = {
        "client_code": CLIENT_CODE,
        "dataset": "trips_sync",
        "evidence_file": str(bundle_path),
        "evidence_sha256": digest,
        "coverage_start_ts": GOOD_A,
        "covered_through_ts": GOOD_W,
        "seeded_by": SEEDED_BY,
        "approval_ref": APPROVAL_REF,
        "expected_environment": ENVIRONMENT,
        "expected_platform_uuid": PLATFORM_UUID,
        "execute": False,
        "confirm_client_code": None,
        "dsn": dsn,
    }
    values.update(overrides)
    return Namespace(**values)


def _snapshot(conn) -> dict:
    tables = (
        "workflow_a_control.client_account",
        "workflow_a_control.client_dataset_schedule",
        "workflow_a_control.client_schedule_run_history",
        "workflow_a_control.client_dataset_coverage",
    )
    return {
        table: conn.execute(f"SELECT * FROM {table}").fetchall()
        for table in tables
    }


def _expect_refusal(dsn, bundle_path, digest, expected_code, **overrides) -> None:
    try:
        writer.run(_writer_args(dsn, bundle_path, digest, **overrides))
    except writer.BootstrapRefused as exc:
        assert exc.code == expected_code, (overrides, exc.code, str(exc))
        return
    raise AssertionError(f"{overrides or expected_code} was accepted")


# ---------------------------------------------------------------------------
# Pure — interval selection
# ---------------------------------------------------------------------------

def _bundle_with_gap() -> dict:
    return {
        "missing_or_unproven_intervals": [
            {
                "kind": "MISSING_FIRE",
                "scheduled_fire_ts": GAP_END,
                "interval_start_ts": GAP_START,
                "interval_end_ts": GAP_END,
            },
        ],
    }


def _instant(text: str) -> datetime:
    return audit.parse_iso_utc(text, label="test")


def test_gap_before_a_does_not_block() -> None:
    blocking = writer.find_blocking_interval(
        bundle=_bundle_with_gap(),
        coverage_start_ts=_instant("2026-07-29T02:00:01Z"),
        covered_through_ts=_instant("2026-08-01T02:00:00Z"),
    )
    assert blocking is None


def test_gap_after_w_does_not_block() -> None:
    blocking = writer.find_blocking_interval(
        bundle=_bundle_with_gap(),
        coverage_start_ts=_instant("2026-07-20T02:00:00Z"),
        covered_through_ts=_instant("2026-07-27T01:59:59Z"),
    )
    assert blocking is None


def test_gap_intersecting_the_selection_blocks() -> None:
    for start, end in (
        ("2026-07-20T02:00:00Z", "2026-08-01T02:00:00Z"),   # fully contains
        ("2026-07-28T00:00:00Z", "2026-07-28T12:00:00Z"),   # inside the gap
        ("2026-07-26T00:00:00Z", "2026-07-27T02:00:00Z"),   # touches the start
        ("2026-07-29T02:00:00Z", "2026-07-31T00:00:00Z"),   # touches the end
    ):
        blocking = writer.find_blocking_interval(
            bundle=_bundle_with_gap(),
            coverage_start_ts=_instant(start),
            covered_through_ts=_instant(end),
        )
        assert blocking is not None, (start, end)
        assert blocking["kind"] == "MISSING_FIRE"


def test_unbounded_unresolved_entry_blocks() -> None:
    bundle = {"missing_or_unproven_intervals": [
        {"kind": "MISSING_FIRE", "scheduled_fire_ts": GAP_END,
         "interval_start_ts": None, "interval_end_ts": None},
    ]}
    assert writer.find_blocking_interval(
        bundle=bundle,
        coverage_start_ts=_instant("2030-01-01T00:00:00Z"),
        covered_through_ts=_instant("2030-01-02T00:00:00Z"),
    ) is not None


def test_bounds_ordering_and_future_w() -> None:
    now = _instant("2026-08-02T00:00:00Z")
    # W == A is a valid degenerate closed interval.
    writer.validate_selected_interval(
        bundle={"missing_or_unproven_intervals": []},
        coverage_start_ts=_instant("2026-08-01T00:00:00Z"),
        covered_through_ts=_instant("2026-08-01T00:00:00Z"),
        now_utc=now,
    )
    for start, end in (
        ("2026-08-01T00:00:01Z", "2026-08-01T00:00:00Z"),
        ("2026-08-01T00:00:00Z", "2026-08-03T00:00:00Z"),
    ):
        try:
            writer.validate_selected_interval(
                bundle={"missing_or_unproven_intervals": []},
                coverage_start_ts=_instant(start),
                covered_through_ts=_instant(end),
                now_utc=now,
            )
        except writer.BootstrapRefused as exc:
            assert exc.code == "BOOTSTRAP_REFUSED_INTERVAL"
            continue
        raise AssertionError(f"({start}, {end}) was accepted")


def test_timezone_equivalent_instants_are_equal() -> None:
    assert writer.parse_bound("2026-08-01T02:00:00+02:00", label="x") == \
        writer.parse_bound("2026-08-01T00:00:00Z", label="x")
    for bad in ("2026-08-01T00:00:00", "2026-08-01T00:00:00.500Z", "nonsense"):
        try:
            writer.parse_bound(bad, label="x")
        except writer.BootstrapRefused as exc:
            assert exc.code == "BOOTSTRAP_REFUSED_PARAMETER"
            continue
        raise AssertionError(f"{bad!r} was accepted")


def test_evidence_ref_is_safe_and_carries_no_content() -> None:
    ref = writer.build_evidence_ref(
        evidence_sha256="a" * 64, approval_ref=APPROVAL_REF
    )
    assert ref == (
        f"telematics-coverage-bootstrap/1:sha256={'a' * 64}:approval={APPROVAL_REF}"
    )
    assert len(ref) < 200


def test_writer_module_launches_nothing() -> None:
    text = (REPO_ROOT / "ops" / "bootstrap_telematics_trips_coverage.py").read_text(
        encoding="utf-8"
    )
    for forbidden in (
        "import requests", "provider_client", "import subprocess",
        "subprocess.run", "subprocess.Popen", "Popen(",
        "os.system", "os.exec", "runner.py", "resolve_secret",
        # No enablement, no schedule edit, no history row: the writer's only
        # mutation is the single coverage INSERT.
        "SET trips_pagination_mode",
        "UPDATE workflow_a_control.client_account",
        "UPDATE workflow_a_control.client_dataset_schedule",
        "INSERT INTO workflow_a_control.client_schedule_run_history",
        "INSERT INTO workflow_a_control.client_dataset_recovery_run",
        "UPDATE workflow_a_control.client_dataset_recovery_run",
        "DELETE FROM workflow_a_control.client_dataset_recovery_run",
        "UPDATE workflow_a_control.client_dataset_coverage",
        "DELETE FROM workflow_a_control.client_dataset_coverage",
        "ON CONFLICT",
    ):
        assert forbidden not in text, forbidden


# ---------------------------------------------------------------------------
# Disposable PostgreSQL — dry run
# ---------------------------------------------------------------------------

def test_dry_run_is_byte_inert(conn, dsn: str) -> None:
    build_fixture(conn, history=HISTORY)
    with tempfile.TemporaryDirectory() as tmp:
        bundle, path = _generate_bundle(dsn, Path(tmp))
        assert bundle["audit_classification"] == \
            audit.CLASSIFICATION_UNRESOLVED_GAPS
        before = _snapshot(conn)
        code, plan = writer.run(
            _writer_args(dsn, path, bundle["bundle_sha256"])
        )
    conn.rollback()
    assert code == writer.EXIT_OK
    assert plan["mode"] == "DRY_RUN"
    assert plan["rows_to_insert"] == 1
    assert plan["rows_to_update"] == 0
    assert plan["rows_to_delete"] == 0
    assert plan["database_writes_performed"] == 0
    assert plan["client_mode_change"] is None
    assert plan["schedule_change"] is None
    assert plan["history_rows_created"] == 0
    assert plan["provider_requests"] == 0
    assert plan["subprocesses_launched"] == 0
    assert plan["bootstrap_status"] == "READY"
    assert plan["covered_through_source"] == "bootstrap"
    assert plan["last_gap_detected_ts"] is None
    assert plan["coverage_start_ts"] == GOOD_A
    assert plan["covered_through_ts"] == GOOD_W
    assert plan["schedule_id"] == SCHEDULE_TRIPS
    assert plan["client_id"] == CLIENT_ID
    assert _snapshot(conn) == before, "dry-run must leave the database identical"


def test_dry_run_accepts_gap_outside_the_selection(conn, dsn: str) -> None:
    build_fixture(conn, history=HISTORY)
    with tempfile.TemporaryDirectory() as tmp:
        bundle, path = _generate_bundle(dsn, Path(tmp))
        digest = bundle["bundle_sha256"]
        # Entirely after the hole.
        code, _ = writer.run(_writer_args(dsn, path, digest))
        assert code == writer.EXIT_OK
        # Entirely before the hole.
        code, _ = writer.run(_writer_args(
            dsn, path, digest,
            coverage_start_ts="2026-07-20T02:00:00Z",
            covered_through_ts="2026-07-27T01:59:59Z",
        ))
        assert code == writer.EXIT_OK
    conn.rollback()


def test_dry_run_refuses_intersecting_gap(conn, dsn: str) -> None:
    build_fixture(conn, history=HISTORY)
    with tempfile.TemporaryDirectory() as tmp:
        bundle, path = _generate_bundle(dsn, Path(tmp))
        digest = bundle["bundle_sha256"]
        try:
            writer.run(_writer_args(
                dsn, path, digest,
                coverage_start_ts="2026-07-20T02:00:00Z",
                covered_through_ts=GOOD_W,
            ))
        except writer.BootstrapRefused as exc:
            assert exc.code == "BOOTSTRAP_REFUSED_INTERVAL"
            # The refusal names the exact blocking interval rather than a count.
            assert "MISSING_FIRE" in str(exc)
            assert GAP_START in str(exc)
            assert "never moves A or W" in str(exc)
        else:
            raise AssertionError("an intersecting hole was accepted")
    conn.rollback()


def test_evidence_verification_matrix(conn, dsn: str) -> None:
    build_fixture(conn, history=HISTORY)
    with tempfile.TemporaryDirectory() as tmp:
        bundle, path = _generate_bundle(dsn, Path(tmp))
        digest = bundle["bundle_sha256"]

        # Declared hash mismatch.
        _expect_refusal(dsn, path, "b" * 64, "BOOTSTRAP_REFUSED_EVIDENCE")

        # Tampered content with a stale self-hash.
        tampered = {**bundle, "missing_or_unproven_intervals": []}
        path.write_text(audit.canonical_json(tampered) + "\n", encoding="utf-8")
        _expect_refusal(dsn, path, digest, "BOOTSTRAP_REFUSED_EVIDENCE")

        # Even a re-hashed identity change is refused.
        for changes, label in (
            ({"client_code": "OTHER0001"}, "client"),
            ({"dataset_name": "fuel_daily_aggregation"}, "dataset"),
            ({"environment_name": "local_dev"}, "environment"),
            ({"platform_uuid": "db8055e0-e030-4d5a-816b-ec4dc338d698"}, "uuid"),
            ({"schedule_id": "f25c8a6c-7ca5-4899-8a16-2490d9e5e241"}, "schedule"),
            ({"bundle_version": "other/9"}, "version"),
            ({"bootstrap_semantics_version": "other/9"}, "semantics"),
            ({"migration_ceiling": "056_workflow_a_trips_stabilization_config.sql"},
             "ceiling"),
            ({"audit_classification": audit.CLASSIFICATION_INSUFFICIENT_HISTORY},
             "classification"),
            ({"generated_at_utc": "2026-01-01T00:00:00Z"}, "stale"),
            ({"generated_at_utc": "2099-01-01T00:00:00Z"}, "future"),
        ):
            new_digest = _rewrite_bundle(path, bundle, **changes)
            expected = (
                "BOOTSTRAP_REFUSED_PREFLIGHT"
                if label in {"schedule"} else "BOOTSTRAP_REFUSED_EVIDENCE"
            )
            _expect_refusal(dsn, path, new_digest, expected)
    conn.rollback()


def test_live_state_preflight_matrix(conn, dsn: str) -> None:
    build_fixture(conn, history=HISTORY)
    with tempfile.TemporaryDirectory() as tmp:
        bundle, path = _generate_bundle(dsn, Path(tmp))
        digest = bundle["bundle_sha256"]

        _expect_refusal(
            dsn, path, digest, "BOOTSTRAP_REFUSED_PARAMETER",
            evidence_sha256="not-a-hash",
        )

        # Client mode drift.
        conn.execute(
            "UPDATE workflow_a_control.client_account "
            "SET trips_pagination_mode = 'data_invariants_v1'"
        )
        conn.commit()
        _expect_refusal(dsn, path, digest, "BOOTSTRAP_REFUSED_PREFLIGHT")
        conn.execute(
            "UPDATE workflow_a_control.client_account "
            "SET trips_pagination_mode = 'strict_meta'"
        )
        conn.commit()

        # Schedule configuration drift since the audit.
        conn.execute(
            "UPDATE workflow_a_control.client_dataset_schedule "
            "SET lookback_days = 9 WHERE schedule_id = %s", (SCHEDULE_TRIPS,)
        )
        conn.commit()
        _expect_refusal(dsn, path, digest, "BOOTSTRAP_REFUSED_PREFLIGHT")
        conn.execute(
            "UPDATE workflow_a_control.client_dataset_schedule "
            "SET lookback_days = 1 WHERE schedule_id = %s", (SCHEDULE_TRIPS,)
        )
        conn.commit()

        # A RUNNING history row blocks the bootstrap.
        conn.execute(
            """
            INSERT INTO workflow_a_control.client_schedule_run_history
              (schedule_id, client_id, client_code, dataset_name,
               window_start_ts, window_end_ts, scheduled_fire_ts, status)
            VALUES (%s, %s, %s, 'trips_sync', '2026-08-02T02:00:00Z',
                    '2026-08-02T02:00:00Z', '2026-08-02T02:00:00Z', 'RUNNING')
            """,
            (SCHEDULE_TRIPS, CLIENT_ID, CLIENT_CODE),
        )
        conn.commit()
        _expect_refusal(dsn, path, digest, "BOOTSTRAP_REFUSED_PREFLIGHT")
        conn.execute(
            "DELETE FROM workflow_a_control.client_schedule_run_history "
            "WHERE status = 'RUNNING'"
        )
        conn.commit()

        # An existing coverage row is never overwritten.
        conn.execute(
            """
            INSERT INTO workflow_a_control.client_dataset_coverage
              (schedule_id, client_id, client_code, dataset_name,
               bootstrap_status, covered_through_source)
            VALUES (%s, %s, %s, 'trips_sync', 'UNINITIALIZED', 'bootstrap')
            """,
            (SCHEDULE_TRIPS, CLIENT_ID, CLIENT_CODE),
        )
        conn.commit()
        _expect_refusal(dsn, path, digest, "BOOTSTRAP_REFUSED_EXISTING_ROW")
        conn.execute("DELETE FROM workflow_a_control.client_dataset_coverage")
        conn.commit()
    conn.rollback()


def test_identity_gate(conn, dsn: str) -> None:
    build_fixture(conn, history=HISTORY)
    with tempfile.TemporaryDirectory() as tmp:
        bundle, path = _generate_bundle(dsn, Path(tmp))
        digest = bundle["bundle_sha256"]
        # The bundle identity check fires before the live marker check, so a
        # mismatched expectation is refused as evidence drift either way.
        try:
            writer.run(_writer_args(
                dsn, path, digest, expected_environment="local_dev"
            ))
        except writer.BootstrapRefused as exc:
            assert exc.code in {
                "BOOTSTRAP_REFUSED_EVIDENCE", "IDENTITY_ENVIRONMENT_MISMATCH",
            }
        else:
            raise AssertionError("a mismatched environment was accepted")
    conn.rollback()


def test_execute_requires_matching_confirmation(conn, dsn: str) -> None:
    build_fixture(conn, history=HISTORY)
    with tempfile.TemporaryDirectory() as tmp:
        bundle, path = _generate_bundle(dsn, Path(tmp))
        digest = bundle["bundle_sha256"]
        before = _snapshot(conn)
        for confirm in (None, "", "OTHER0001"):
            _expect_refusal(
                dsn, path, digest, "BOOTSTRAP_REFUSED_CONFIRMATION",
                execute=True, confirm_client_code=confirm,
            )
        conn.rollback()
        assert _snapshot(conn) == before
    conn.rollback()


# ---------------------------------------------------------------------------
# Disposable PostgreSQL — execute
# ---------------------------------------------------------------------------

def test_execute_inserts_exactly_one_approved_row(conn, dsn: str) -> None:
    build_fixture(conn, history=HISTORY)
    with tempfile.TemporaryDirectory() as tmp:
        bundle, path = _generate_bundle(dsn, Path(tmp))
        digest = bundle["bundle_sha256"]
        accounts_before = conn.execute(
            "SELECT * FROM workflow_a_control.client_account"
        ).fetchall()
        schedules_before = conn.execute(
            "SELECT * FROM workflow_a_control.client_dataset_schedule"
        ).fetchall()
        history_before = conn.execute(
            "SELECT * FROM workflow_a_control.client_schedule_run_history"
        ).fetchall()

        code, plan = writer.run(_writer_args(
            dsn, path, digest, execute=True, confirm_client_code=CLIENT_CODE,
        ))
        assert code == writer.EXIT_OK
        assert plan["mode"] == "EXECUTE"
        assert plan["affected_row_count"] == 1
        assert plan["transaction_result"] == "COMMITTED"

        conn.rollback()
        rows = conn.execute(
            "SELECT * FROM workflow_a_control.client_dataset_coverage"
        ).fetchall()
        assert len(rows) == 1
        row = dict(rows[0])
        assert str(row["schedule_id"]) == SCHEDULE_TRIPS
        assert str(row["client_id"]) == CLIENT_ID
        assert row["client_code"] == CLIENT_CODE
        assert row["dataset_name"] == "trips_sync"
        assert audit.iso_utc(row["coverage_start_ts"]) == GOOD_A
        assert audit.iso_utc(row["covered_through_ts"]) == GOOD_W
        assert row["bootstrap_status"] == "READY"
        assert row["covered_through_source"] == "bootstrap"
        assert row["last_gap_detected_ts"] is None
        assert row["seeded_by"] == SEEDED_BY
        assert row["seeded_at"] is not None
        assert row["bootstrap_evidence_ref"] == (
            f"telematics-coverage-bootstrap/1:sha256={digest}:approval={APPROVAL_REF}"
        )
        # The stored reference identifies the bundle; it never embeds it.
        assert len(row["bootstrap_evidence_ref"]) < 200
        assert "missing_or_unproven_intervals" not in row["bootstrap_evidence_ref"]
        assert plan["stored_row"]["bootstrap_evidence_ref"] == \
            row["bootstrap_evidence_ref"]

        # Nothing else moved.
        assert conn.execute(
            "SELECT * FROM workflow_a_control.client_account"
        ).fetchall() == accounts_before
        assert conn.execute(
            "SELECT * FROM workflow_a_control.client_dataset_schedule"
        ).fetchall() == schedules_before
        assert conn.execute(
            "SELECT * FROM workflow_a_control.client_schedule_run_history"
        ).fetchall() == history_before
        assert conn.execute(
            "SELECT count(*) AS n FROM workflow_a_control.client_account "
            "WHERE trips_pagination_mode <> 'strict_meta'"
        ).fetchone()["n"] == 0

        # A second execution is refused; the row is never overwritten.
        _expect_refusal(
            dsn, path, digest, "BOOTSTRAP_REFUSED_EXISTING_ROW",
            execute=True, confirm_client_code=CLIENT_CODE,
        )
        conn.rollback()
        assert conn.execute(
            "SELECT * FROM workflow_a_control.client_dataset_coverage"
        ).fetchall() == rows
    conn.rollback()


def test_execute_row_survives_the_runtime_gate(conn, dsn: str) -> None:
    """The seeded row must be exactly what C5 accepts as a verified claim."""
    from jobs.api.telematics.coverage_windows import (
        COVERAGE_GATE_ALLOWED, CoverageState, evaluate_coverage_gate,
    )

    rows = conn.execute(
        "SELECT * FROM workflow_a_control.client_dataset_coverage"
    ).fetchall()
    assert len(rows) == 1
    row = dict(rows[0])
    state = CoverageState(
        schedule_id=str(row["schedule_id"]),
        client_id=str(row["client_id"]),
        client_code=row["client_code"],
        dataset_name=row["dataset_name"],
        coverage_start_ts=row["coverage_start_ts"],
        covered_through_ts=row["covered_through_ts"],
        bootstrap_status=row["bootstrap_status"],
        bootstrap_evidence_ref=row["bootstrap_evidence_ref"],
        seeded_at=row["seeded_at"],
        seeded_by=row["seeded_by"],
        covered_through_source=row["covered_through_source"],
        last_gap_detected_ts=row["last_gap_detected_ts"],
    )
    result = evaluate_coverage_gate(
        schedule_id=str(row["schedule_id"]),
        client_id=str(row["client_id"]),
        client_code=row["client_code"],
        dataset_name=row["dataset_name"],
        scheduled_fire_ts=datetime(2026, 8, 2, 2, 0, tzinfo=UTC),
        lookback_days=1,
        stabilization_delay_seconds=10800,
        overlap_seconds=3600,
        max_recovery_span_seconds=2678400,
        coverage_state=state,
        now_utc=datetime(2026, 8, 2, 12, 0, tzinfo=UTC),
    )
    assert result.classification == COVERAGE_GATE_ALLOWED, result.reason


def test_concurrent_insert_conflict_is_not_retried(conn, dsn: str) -> None:
    import psycopg
    from psycopg.rows import dict_row

    build_fixture(conn, history=HISTORY)
    with tempfile.TemporaryDirectory() as tmp:
        bundle, path = _generate_bundle(dsn, Path(tmp))
        digest = bundle["bundle_sha256"]
        params = {
            "schedule_id": SCHEDULE_TRIPS,
            "client_id": CLIENT_ID,
            "client_code": CLIENT_CODE,
            "dataset_name": "trips_sync",
            "coverage_start_ts": _instant(GOOD_A),
            "covered_through_ts": _instant(GOOD_W),
            "bootstrap_status": "READY",
            "bootstrap_evidence_ref": writer.build_evidence_ref(
                evidence_sha256=digest, approval_ref=APPROVAL_REF
            ),
            "seeded_at": datetime(2026, 8, 2, 12, 0, tzinfo=UTC),
            "seeded_by": SEEDED_BY,
            "covered_through_source": "bootstrap",
            "updated_at": datetime(2026, 8, 2, 12, 0, tzinfo=UTC),
        }
        # A competing transaction wins the primary key first.
        other = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
        try:
            with other.cursor() as cur:
                writer._insert_initial_coverage_row(cur, params)
        finally:
            other.close()

        write_conn = psycopg.connect(dsn, autocommit=False, row_factory=dict_row)
        try:
            writer.execute_bootstrap(
                write_conn, schedule_id=SCHEDULE_TRIPS, params=params
            )
        except writer.BootstrapRefused as exc:
            assert exc.code == "BOOTSTRAP_WRITE_CONFLICT"
            assert exc.exit_code == writer.EXIT_WRITE_CONFLICT
        else:
            raise AssertionError("a duplicate coverage insert was accepted")
        finally:
            write_conn.close()

    conn.rollback()
    assert conn.execute(
        "SELECT count(*) AS n FROM workflow_a_control.client_dataset_coverage"
    ).fetchone()["n"] == 1, "the conflict must leave exactly the winning row"


def test_mode_race_inside_the_transaction_rolls_back(conn, dsn: str) -> None:
    import psycopg
    from psycopg.rows import dict_row

    build_fixture(conn, history=HISTORY)
    params = {
        "schedule_id": SCHEDULE_TRIPS,
        "client_id": CLIENT_ID,
        "client_code": CLIENT_CODE,
        "dataset_name": "trips_sync",
        "coverage_start_ts": _instant(GOOD_A),
        "covered_through_ts": _instant(GOOD_W),
        "bootstrap_status": "READY",
        "bootstrap_evidence_ref": writer.build_evidence_ref(
            evidence_sha256="a" * 64, approval_ref=APPROVAL_REF
        ),
        "seeded_at": datetime(2026, 8, 2, 12, 0, tzinfo=UTC),
        "seeded_by": SEEDED_BY,
        "covered_through_source": "bootstrap",
        "updated_at": datetime(2026, 8, 2, 12, 0, tzinfo=UTC),
    }

    # Mode flipped after the read-only preflight but before the write.
    conn.execute(
        "UPDATE workflow_a_control.client_account "
        "SET trips_pagination_mode = 'data_invariants_v1'"
    )
    conn.commit()
    write_conn = psycopg.connect(dsn, autocommit=False, row_factory=dict_row)
    try:
        writer.execute_bootstrap(
            write_conn, schedule_id=SCHEDULE_TRIPS, params=params
        )
    except writer.BootstrapRefused as exc:
        assert exc.code == "BOOTSTRAP_WRITE_CONFLICT"
    else:
        raise AssertionError("a mode race was accepted")
    finally:
        write_conn.close()
    conn.rollback()
    assert conn.execute(
        "SELECT count(*) AS n FROM workflow_a_control.client_dataset_coverage"
    ).fetchone()["n"] == 0, "a refused write must leave no row"

    # A schedule identity race is equally fatal.
    conn.execute(
        "UPDATE workflow_a_control.client_account "
        "SET trips_pagination_mode = 'strict_meta'"
    )
    conn.commit()
    write_conn = psycopg.connect(dsn, autocommit=False, row_factory=dict_row)
    try:
        writer.execute_bootstrap(
            write_conn, schedule_id=SCHEDULE_TRIPS,
            params={**params, "dataset_name": "fuel_daily_aggregation"},
        )
    except writer.BootstrapRefused as exc:
        assert exc.code == "BOOTSTRAP_WRITE_CONFLICT"
    else:
        raise AssertionError("a schedule identity race was accepted")
    finally:
        write_conn.close()
    conn.rollback()
    assert conn.execute(
        "SELECT count(*) AS n FROM workflow_a_control.client_dataset_coverage"
    ).fetchone()["n"] == 0


def test_postwrite_verification_failure_rolls_back(conn, dsn: str) -> None:
    """A stored value that differs from the approved input must not commit."""
    import psycopg
    from psycopg.rows import dict_row

    build_fixture(conn, history=HISTORY)
    params = {
        "schedule_id": SCHEDULE_TRIPS,
        "client_id": CLIENT_ID,
        "client_code": CLIENT_CODE,
        "dataset_name": "trips_sync",
        "coverage_start_ts": _instant(GOOD_A),
        "covered_through_ts": _instant(GOOD_W),
        "bootstrap_status": "READY",
        "bootstrap_evidence_ref": writer.build_evidence_ref(
            evidence_sha256="a" * 64, approval_ref=APPROVAL_REF
        ),
        "seeded_at": datetime(2026, 8, 2, 12, 0, tzinfo=UTC),
        "seeded_by": SEEDED_BY,
        "covered_through_source": "bootstrap",
        "updated_at": datetime(2026, 8, 2, 12, 0, tzinfo=UTC),
    }
    original = writer._read_back

    def _corrupted(cur, *, client_id, dataset_name):
        rows = original(cur, client_id=client_id, dataset_name=dataset_name)
        if rows:
            rows[0]["covered_through_ts"] = rows[0][
                "covered_through_ts"
            ] + timedelta(seconds=1)
        return rows

    writer._read_back = _corrupted
    write_conn = psycopg.connect(dsn, autocommit=False, row_factory=dict_row)
    try:
        writer.execute_bootstrap(
            write_conn, schedule_id=SCHEDULE_TRIPS, params=params
        )
    except writer.BootstrapRefused as exc:
        assert exc.code == "BOOTSTRAP_POSTWRITE_VERIFICATION_FAILED"
        assert exc.exit_code == writer.EXIT_POSTWRITE_VERIFICATION_FAILED
        assert "covered_through_ts" in str(exc)
    else:
        raise AssertionError("a corrupted read-back was committed")
    finally:
        writer._read_back = original
        write_conn.close()

    conn.rollback()
    assert conn.execute(
        "SELECT count(*) AS n FROM workflow_a_control.client_dataset_coverage"
    ).fetchone()["n"] == 0, "post-write verification failure must roll back"


def test_on_disposable_postgres(dsn: str) -> None:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row, autocommit=False) as conn:
        test_dry_run_is_byte_inert(conn, dsn)
        test_dry_run_accepts_gap_outside_the_selection(conn, dsn)
        test_dry_run_refuses_intersecting_gap(conn, dsn)
        test_evidence_verification_matrix(conn, dsn)
        test_live_state_preflight_matrix(conn, dsn)
        test_identity_gate(conn, dsn)
        test_execute_requires_matching_confirmation(conn, dsn)
        test_execute_inserts_exactly_one_approved_row(conn, dsn)
        test_execute_row_survives_the_runtime_gate(conn, dsn)
        test_concurrent_insert_conflict_is_not_retried(conn, dsn)
        test_mode_race_inside_the_transaction_rolls_back(conn, dsn)
        test_postwrite_verification_failure_rolls_back(conn, dsn)
        conn.rollback()


def main() -> None:
    test_gap_before_a_does_not_block()
    test_gap_after_w_does_not_block()
    test_gap_intersecting_the_selection_blocks()
    test_unbounded_unresolved_entry_blocks()
    test_bounds_ordering_and_future_w()
    test_timezone_equivalent_instants_are_equal()
    test_evidence_ref_is_safe_and_carries_no_content()
    test_writer_module_launches_nothing()
    dsn = os.getenv("TELEMATICS_BOOTSTRAP_WRITER_TEST_DSN")
    if dsn:
        require_loopback_dsn_or_exit(
            dsn, label="TELEMATICS_BOOTSTRAP_WRITER_TEST_DSN",
        )
        test_on_disposable_postgres(dsn)
        print("PASS: disposable PostgreSQL coverage-bootstrap writer checks")
    else:
        print("SKIP: set TELEMATICS_BOOTSTRAP_WRITER_TEST_DSN for PostgreSQL checks")
    print("OK - Telematics coverage bootstrap writer checks passed")


if __name__ == "__main__":
    main()
