#!/usr/bin/env python3
"""Focused tests for the zero-state cold-start coverage baseline writer.

Every check needs the same two *disposable* PostgreSQL 16 databases as the
cold-start audit, because the writer's dry-run path is itself a live preflight:

  TELEMATICS_COLD_START_TEST_DSN='postgresql://loguser:...@127.0.0.1:55731/coldstart_test' \\
  TELEMATICS_COLD_START_BUSINESS_DSN='postgresql://loguser:...@127.0.0.1:55731/echo_business_test' \\
      .venv/bin/python ops/tests_manual/test_telematics_cold_start_bootstrap_postgres.py

The suite proves the writer inserts exactly one zero-width baseline row, refuses
every non-zero state, leaves the mode and the disabled schedule alone, and
cannot exchange evidence with the historical C10 path in either direction.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from argparse import Namespace
from datetime import timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ops import audit_telematics_cold_start as cold  # noqa: E402
from ops import bootstrap_telematics_cold_start_coverage as writer  # noqa: E402
from ops import bootstrap_telematics_trips_coverage as historical_writer  # noqa: E402
from ops.tests_manual.postgres_dsn_safety import (  # noqa: E402
    require_loopback_dsn_or_exit,
)
from ops.tests_manual.test_telematics_cold_start_audit import (  # noqa: E402
    BUSINESS_ENV,
    install_network_guard,
    CLIENT_CODE,
    CLIENT_ID,
    ENVIRONMENT,
    MANAGED_START,
    PLATFORM_ENV,
    PLATFORM_UUID,
    SCHEDULE_TRIPS,
    audit_args,
    build_fixture,
    snapshot,
)

UTC = timezone.utc
SEEDED_BY = "operator@example.invalid"
APPROVAL_REF = "TELEMATICS-COLD-START-ECHO00001-1"


def _generate_bundle(dsn: str, tmp: Path, **overrides) -> tuple:
    bundle, path = cold.run_audit(
        audit_args(dsn, tmp / "cold.json", **overrides)
    )
    return bundle, path


def writer_args(dsn: str, bundle: dict, path: Path, **overrides) -> Namespace:
    values = {
        "client_code": CLIENT_CODE,
        "dataset": "trips_sync",
        "evidence_file": str(path),
        "evidence_sha256": bundle["bundle_sha256"],
        "expected_schedule_id": SCHEDULE_TRIPS,
        "confirm_schedule_disabled": True,
        "coverage_start_ts": MANAGED_START,
        "initial_covered_through_ts": MANAGED_START,
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


def coverage_rows(conn) -> list:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT to_jsonb(t) AS row"
            "  FROM workflow_a_control.client_dataset_coverage AS t"
        )
        rows = [dict(r["row"]) for r in cur.fetchall()]
    conn.rollback()
    return rows


def _expect(dsn: str, bundle: dict, path: Path, code: str, **overrides) -> None:
    try:
        writer.run(writer_args(dsn, bundle, path, **overrides))
    except writer.ColdStartBootstrapRefused as exc:
        assert exc.code == code, f"expected {code}, got {exc.code}"
        return
    raise AssertionError(f"expected refusal {code}")


# ---------------------------------------------------------------------------

def test_dry_run_writes_nothing(conn, business_conn, dsn, business_dsn) -> None:
    build_fixture(conn, business_conn, business_dsn)
    with tempfile.TemporaryDirectory() as tmp:
        bundle, path = _generate_bundle(dsn, Path(tmp))
        before = snapshot(conn)
        exit_code, plan = writer.run(writer_args(dsn, bundle, path))
        assert exit_code == writer.EXIT_OK
        assert plan["mode"] == "DRY_RUN"
        assert plan["rows_to_insert"] == 1
        assert plan["database_writes_performed"] == 0
        assert plan["covered_interval_seconds"] == 0
        assert plan["baseline_kind"] == "ZERO_WIDTH_INITIALIZATION"
        assert plan["reporting_ready"] is False
        assert plan["client_mode_changes"] == 0
        assert plan["schedule_changes"] == 0
        assert plan["schedule_enabled_changes"] == 0
        assert plan["history_rows_created"] == 0
        assert plan["recovery_rows_created"] == 0
        assert plan["provider_requests"] == 0
        assert plan["subprocesses_launched"] == 0
        assert plan["schedule_enabled"] is False
    conn.rollback()
    assert snapshot(conn) == before, "a dry run must write nothing"


def test_execute_inserts_exactly_one_zero_width_row(
    conn, business_conn, dsn, business_dsn
) -> None:
    build_fixture(conn, business_conn, business_dsn)
    with tempfile.TemporaryDirectory() as tmp:
        bundle, path = _generate_bundle(dsn, Path(tmp))
        before = snapshot(conn)
        exit_code, plan = writer.run(
            writer_args(
                dsn, bundle, path, execute=True, confirm_client_code=CLIENT_CODE
            )
        )
    assert exit_code == writer.EXIT_OK
    assert plan["mode"] == "EXECUTE"
    assert plan["affected_row_count"] == 1
    assert plan["database_writes_performed"] == 1
    assert plan["transaction_result"] == "COMMITTED"

    rows = coverage_rows(conn)
    assert len(rows) == 1
    row = rows[0]
    assert row["schedule_id"] == SCHEDULE_TRIPS
    assert row["client_id"] == CLIENT_ID
    assert row["client_code"] == CLIENT_CODE
    assert row["dataset_name"] == "trips_sync"
    assert row["bootstrap_status"] == "READY"
    assert row["covered_through_source"] == "bootstrap"
    assert row["last_gap_detected_ts"] is None
    assert row["coverage_start_ts"] == row["covered_through_ts"]
    assert row["bootstrap_evidence_ref"].startswith(
        writer.COLD_START_EVIDENCE_REF_PREFIX
    )
    assert bundle["bundle_sha256"] in row["bootstrap_evidence_ref"]
    assert APPROVAL_REF in row["bootstrap_evidence_ref"]

    # The fingerprint the operator carries forward is reproducible.
    from jobs.api.telematics.coverage_finalization import coverage_fingerprint

    with conn.cursor() as cur:
        cur.execute(
            "SELECT schedule_id::text AS schedule_id,"
            " client_id::text AS client_id, client_code, dataset_name,"
            " coverage_start_ts, covered_through_ts, bootstrap_status,"
            " bootstrap_evidence_ref, covered_through_source, seeded_at,"
            " seeded_by, last_gap_detected_ts, updated_at"
            " FROM workflow_a_control.client_dataset_coverage"
        )
        stored = dict(cur.fetchone())
    conn.rollback()
    assert coverage_fingerprint(stored) == plan["baseline_coverage_fingerprint"]

    # Nothing outside the coverage table moved.
    after = snapshot(conn)
    for table in (
        "workflow_a_control.client_account",
        "workflow_a_control.client_dataset_schedule",
        "workflow_a_control.client_schedule_run_history",
        "workflow_a_control.client_dataset_recovery_run",
        "public.runs",
    ):
        assert after[table] == before[table], table
    assert before["workflow_a_control.client_dataset_coverage"] == []

    # The mode is still strict and the schedule is still disabled.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT trips_pagination_mode FROM"
            " workflow_a_control.client_account WHERE client_id = %s",
            (CLIENT_ID,),
        )
        assert dict(cur.fetchone())["trips_pagination_mode"] == "strict_meta"
        cur.execute(
            "SELECT enabled FROM workflow_a_control.client_dataset_schedule"
            " WHERE schedule_id = %s",
            (SCHEDULE_TRIPS,),
        )
        assert dict(cur.fetchone())["enabled"] is False
    conn.rollback()


def test_second_execution_refuses_without_overwrite(
    conn, business_conn, dsn, business_dsn
) -> None:
    """The insert already committed above; a repeat must refuse, not overwrite."""
    with tempfile.TemporaryDirectory() as tmp:
        # A fresh bundle cannot even be generated now — the zero state is gone.
        try:
            _generate_bundle(dsn, Path(tmp))
        except cold.ColdStartAuditError as exc:
            assert exc.code == "COLD_START_REFUSED_COVERAGE_PRESENT"
        else:
            raise AssertionError("evidence was generated for a bootstrapped client")

    before = coverage_rows(conn)
    build_fixture(conn, business_conn, business_dsn)
    with tempfile.TemporaryDirectory() as tmp:
        bundle, path = _generate_bundle(dsn, Path(tmp))
        writer.run(
            writer_args(
                dsn, bundle, path, execute=True, confirm_client_code=CLIENT_CODE
            )
        )
        # Now the same reviewed evidence is replayed against a written state.
        # The read-only preflight refuses before any write transaction opens,
        # which is the fail-closed direction; the in-transaction re-check that
        # would catch a race is exercised separately below.
        _expect(
            dsn, bundle, path, "COLD_START_REFUSED_COVERAGE_PRESENT",
            execute=True, confirm_client_code=CLIENT_CODE,
        )
    after = coverage_rows(conn)
    assert len(after) == 1
    assert before != []  # the earlier case really did insert


def test_confirmation_gates(conn, business_conn, dsn, business_dsn) -> None:
    build_fixture(conn, business_conn, business_dsn)
    with tempfile.TemporaryDirectory() as tmp:
        bundle, path = _generate_bundle(dsn, Path(tmp))
        _expect(
            dsn, bundle, path, "COLD_START_BOOTSTRAP_REFUSED_CONFIRMATION",
            execute=True,
        )
        _expect(
            dsn, bundle, path, "COLD_START_BOOTSTRAP_REFUSED_CONFIRMATION",
            execute=True, confirm_client_code="WRONG0001",
        )
        _expect(
            dsn, bundle, path, "COLD_START_BOOTSTRAP_REFUSED_CONFIRMATION",
            confirm_schedule_disabled=False,
        )
    assert coverage_rows(conn) == []


def test_baseline_must_be_zero_width_and_bound_to_evidence(
    conn, business_conn, dsn, business_dsn
) -> None:
    build_fixture(conn, business_conn, business_dsn)
    with tempfile.TemporaryDirectory() as tmp:
        bundle, path = _generate_bundle(dsn, Path(tmp))
        _expect(
            dsn, bundle, path, "COLD_START_BOOTSTRAP_REFUSED_BASELINE",
            initial_covered_through_ts="2026-07-02T00:00:00Z",
        )
        _expect(
            dsn, bundle, path, "COLD_START_BOOTSTRAP_REFUSED_BASELINE",
            coverage_start_ts="2026-06-30T00:00:00Z",
            initial_covered_through_ts="2026-06-30T00:00:00Z",
        )
        _expect(
            dsn, bundle, path, "COLD_START_BOOTSTRAP_REFUSED_PARAMETER",
            coverage_start_ts="2026-07-01T00:00:00.5Z",
        )
    assert coverage_rows(conn) == []


def test_evidence_gates(conn, business_conn, dsn, business_dsn) -> None:
    build_fixture(conn, business_conn, business_dsn)
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        bundle, path = _generate_bundle(dsn, tmpdir)

        _expect(
            dsn, bundle, path, "COLD_START_BOOTSTRAP_REFUSED_EVIDENCE",
            evidence_sha256="f" * 64,
        )

        tampered = dict(bundle)
        tampered["client_code"] = "OTHR0001"
        tampered_path = tmpdir / "tampered.json"
        tampered_path.write_text(
            cold.canonical_json(tampered), encoding="utf-8"
        )
        _expect(
            dsn, tampered, tampered_path,
            "COLD_START_BOOTSTRAP_REFUSED_EVIDENCE",
            evidence_sha256=tampered["bundle_sha256"],
        )

        # A bundle whose counts were edited to hide non-zero state still fails
        # the hash, and a re-hashed one fails the count gate.
        lying = dict(bundle)
        lying["zero_state_counts"] = dict(bundle["zero_state_counts"])
        lying["zero_state_counts"]["schedule_history_rows"] = 4
        lying["bundle_sha256"] = cold.bundle_sha256(lying)
        lying_path = tmpdir / "lying.json"
        lying_path.write_text(cold.canonical_json(lying), encoding="utf-8")
        _expect(
            dsn, lying, lying_path, "COLD_START_BOOTSTRAP_REFUSED_EVIDENCE",
            evidence_sha256=lying["bundle_sha256"],
        )
    assert coverage_rows(conn) == []


def test_historical_and_cold_start_evidence_cannot_be_exchanged(
    conn, business_conn, dsn, business_dsn
) -> None:
    """Neither writer accepts the other's bundle, in either direction."""
    from ops import audit_telematics_coverage_bootstrap as historical_audit

    build_fixture(conn, business_conn, business_dsn)
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        cold_bundle, cold_path = _generate_bundle(dsn, tmpdir)

        # 1. The cold-start bundle offered to the historical C10 writer.
        historical_args = Namespace(
            client_code=CLIENT_CODE,
            dataset="trips_sync",
            evidence_file=str(cold_path),
            evidence_sha256=cold_bundle["bundle_sha256"],
            coverage_start_ts=MANAGED_START,
            covered_through_ts=MANAGED_START,
            seeded_by=SEEDED_BY,
            approval_ref=APPROVAL_REF,
            expected_environment=ENVIRONMENT,
            expected_platform_uuid=PLATFORM_UUID,
            execute=False,
            confirm_client_code=None,
            dsn=dsn,
        )
        try:
            historical_writer.run(historical_args)
        except historical_writer.BootstrapRefused as exc:
            assert exc.code == "BOOTSTRAP_REFUSED_EVIDENCE"
        else:
            raise AssertionError(
                "the historical writer accepted cold-start evidence"
            )

        # 2. A historical C10 bundle offered to the cold-start writer. The
        #    historical audit needs an enabled schedule and history, so a
        #    separate fixture shape is used to produce one.
        build_fixture(
            conn, business_conn, business_dsn, trips_enabled=True,
            history=(("2026-07-02T02:00:00Z", "SUCCESS"),),
        )
        hist_bundle, hist_path = historical_audit.run_audit(
            Namespace(
                client_code=CLIENT_CODE,
                dataset="trips_sync",
                output=str(tmpdir / "historical.json"),
                expected_environment=ENVIRONMENT,
                expected_platform_uuid=PLATFORM_UUID,
                range_start="2026-07-02T00:00:00Z",
                range_end="2026-07-02T12:00:00Z",
                dsn=dsn,
            )
        )
        assert hist_bundle["bundle_version"] == historical_audit.BUNDLE_VERSION
        _expect(
            dsn, hist_bundle, hist_path,
            "COLD_START_BOOTSTRAP_REFUSED_EVIDENCE",
            evidence_sha256=hist_bundle["bundle_sha256"],
        )
    assert coverage_rows(conn) == []


def test_live_state_change_between_evidence_and_write_is_refused(
    conn, business_conn, dsn, business_dsn
) -> None:
    build_fixture(conn, business_conn, business_dsn)
    with tempfile.TemporaryDirectory() as tmp:
        bundle, path = _generate_bundle(dsn, Path(tmp))

        # The schedule is enabled after the evidence was produced.
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE workflow_a_control.client_dataset_schedule"
                " SET enabled = true WHERE schedule_id = %s",
                (SCHEDULE_TRIPS,),
            )
        conn.commit()
        _expect(
            dsn, bundle, path, "COLD_START_REFUSED_SCHEDULE",
            execute=True, confirm_client_code=CLIENT_CODE,
        )
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE workflow_a_control.client_dataset_schedule"
                " SET enabled = false WHERE schedule_id = %s",
                (SCHEDULE_TRIPS,),
            )
        conn.commit()

        # The mode is flipped after the evidence was produced.
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE workflow_a_control.client_account"
                " SET trips_pagination_mode = 'data_invariants_v1'"
                " WHERE client_id = %s",
                (CLIENT_ID,),
            )
        conn.commit()
        _expect(
            dsn, bundle, path, "COLD_START_REFUSED_MODE",
            execute=True, confirm_client_code=CLIENT_CODE,
        )
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE workflow_a_control.client_account"
                " SET trips_pagination_mode = 'strict_meta'"
                " WHERE client_id = %s",
                (CLIENT_ID,),
            )
        conn.commit()

        # A history row appears after the evidence was produced.
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO workflow_a_control.client_schedule_run_history
                  (schedule_id, client_id, client_code, dataset_name,
                   window_start_ts, window_end_ts, scheduled_fire_ts, status)
                VALUES (%s,%s,%s,'trips_sync','2026-07-01T00:00:00Z',
                        '2026-07-02T00:00:00Z','2026-07-02T02:00:00Z','FAILED')
                """,
                (SCHEDULE_TRIPS, CLIENT_ID, CLIENT_CODE),
            )
        conn.commit()
        _expect(
            dsn, bundle, path, "COLD_START_REFUSED_HISTORY_PRESENT",
            execute=True, confirm_client_code=CLIENT_CODE,
        )
    assert coverage_rows(conn) == []


def _insert_racing_row(dsn: str) -> None:
    """Commit a competing coverage row from an independent connection."""
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as other:
        other.execute(
            """
            INSERT INTO workflow_a_control.client_dataset_coverage
              (schedule_id, client_id, client_code, dataset_name,
               coverage_start_ts, covered_through_ts, bootstrap_status,
               bootstrap_evidence_ref, seeded_at, seeded_by,
               covered_through_source, updated_at)
            VALUES (%s,%s,%s,'trips_sync',%s,%s,'READY','other-ref',
                    now(),'other-operator','bootstrap',now())
            """,
            (SCHEDULE_TRIPS, CLIENT_ID, CLIENT_CODE, MANAGED_START,
             MANAGED_START),
        )


def test_race_after_preflight_is_refused_under_the_lock(
    conn, business_conn, dsn, business_dsn
) -> None:
    """A row committed between the preflight and the write transaction refuses.

    The competing row is committed *after* the read-only preflight passed, so
    the only thing that can catch it is the zero-state re-evaluation performed
    inside the write transaction under the target row locks.
    """
    build_fixture(conn, business_conn, business_dsn)
    with tempfile.TemporaryDirectory() as tmp:
        bundle, path = _generate_bundle(dsn, Path(tmp))

        original_preflight = writer.preflight
        calls = {"n": 0}

        def preflight_then_race(cur, **kwargs):
            state = original_preflight(cur, **kwargs)
            calls["n"] += 1
            _insert_racing_row(dsn)
            return state

        writer.preflight = preflight_then_race
        try:
            _expect(
                dsn, bundle, path, "COLD_START_BOOTSTRAP_WRITE_CONFLICT",
                execute=True, confirm_client_code=CLIENT_CODE,
            )
        finally:
            writer.preflight = original_preflight

    assert calls["n"] == 1, "the preflight runs once and the write is not retried"
    rows = coverage_rows(conn)
    assert len(rows) == 1
    assert rows[0]["seeded_by"] == "other-operator", "the racer was not overwritten"


def test_primary_key_backstop_refuses_without_overwrite(
    conn, business_conn, dsn, business_dsn
) -> None:
    """Even with every logical gate bypassed, the PK refuses a second row.

    The in-transaction zero-state re-check is deliberately neutralized here so
    that the database-level backstop is exercised on its own: a cold start must
    never be able to overwrite an existing coverage row, whatever the tooling
    believes.
    """
    build_fixture(conn, business_conn, business_dsn)
    with tempfile.TemporaryDirectory() as tmp:
        bundle, path = _generate_bundle(dsn, Path(tmp))
        original_preflight = writer.preflight
        original_evaluate = writer.evaluate_zero_state
        attempts = {"n": 0}

        def blind_evaluate(cur, **kwargs):
            return {}

        def preflight_then_race(cur, **kwargs):
            state = original_preflight(cur, **kwargs)
            _insert_racing_row(dsn)
            # Neutralized only *after* the honest preflight ran, so the write
            # transaction reaches the INSERT with no logical gate left.
            writer.evaluate_zero_state = blind_evaluate
            return state

        original_insert = writer._insert_baseline_row

        def counting_insert(cur, params):
            attempts["n"] += 1
            return original_insert(cur, params)

        writer.preflight = preflight_then_race
        writer._insert_baseline_row = counting_insert
        try:
            _expect(
                dsn, bundle, path, "COLD_START_BOOTSTRAP_WRITE_CONFLICT",
                execute=True, confirm_client_code=CLIENT_CODE,
            )
        finally:
            writer.preflight = original_preflight
            writer.evaluate_zero_state = original_evaluate
            writer._insert_baseline_row = original_insert

    assert attempts["n"] == 1, "the insert is attempted once and never retried"
    rows = coverage_rows(conn)
    assert len(rows) == 1
    assert rows[0]["seeded_by"] == "other-operator"


def test_writer_module_makes_no_provider_or_job_call() -> None:
    import inspect

    text = (
        REPO_ROOT / "ops" / "bootstrap_telematics_cold_start_coverage.py"
    ).read_text(encoding="utf-8")
    for forbidden in (
        "import requests", "provider_client", "runner.py", "Popen",
        "import subprocess", "subprocess.", "os.system",
    ):
        assert forbidden not in text, forbidden
    # Exactly one INSERT statement, and it is a plain VALUES insert.
    assert text.count("INSERT INTO") == 1
    assert "UPDATE workflow_a_control" not in text
    assert "DELETE FROM workflow_a_control" not in text
    statement = inspect.getsource(writer._insert_baseline_row)
    for forbidden in ("ON CONFLICT", "DO UPDATE", "DO NOTHING", "RETURNING"):
        assert forbidden not in statement, forbidden


# ---------------------------------------------------------------------------

def test_on_disposable_postgres(dsn: str, business_dsn: str) -> None:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row, autocommit=False) as conn, \
            psycopg.connect(
                business_dsn, row_factory=dict_row, autocommit=False
            ) as business_conn:
        test_dry_run_writes_nothing(conn, business_conn, dsn, business_dsn)
        test_execute_inserts_exactly_one_zero_width_row(
            conn, business_conn, dsn, business_dsn
        )
        test_second_execution_refuses_without_overwrite(
            conn, business_conn, dsn, business_dsn
        )
        test_confirmation_gates(conn, business_conn, dsn, business_dsn)
        test_baseline_must_be_zero_width_and_bound_to_evidence(
            conn, business_conn, dsn, business_dsn
        )
        test_evidence_gates(conn, business_conn, dsn, business_dsn)
        test_historical_and_cold_start_evidence_cannot_be_exchanged(
            conn, business_conn, dsn, business_dsn
        )
        test_live_state_change_between_evidence_and_write_is_refused(
            conn, business_conn, dsn, business_dsn
        )
        test_race_after_preflight_is_refused_under_the_lock(
            conn, business_conn, dsn, business_dsn
        )
        test_primary_key_backstop_refuses_without_overwrite(
            conn, business_conn, dsn, business_dsn
        )
        conn.rollback()


def main() -> None:
    install_network_guard()
    test_writer_module_makes_no_provider_or_job_call()
    dsn = os.getenv(PLATFORM_ENV)
    business_dsn = os.getenv(BUSINESS_ENV)
    if not (dsn and business_dsn):
        print(f"SKIP: set {PLATFORM_ENV} and {BUSINESS_ENV}")
        return
    # Destructive on both databases: prove each is loopback-only first.
    require_loopback_dsn_or_exit(dsn, label=PLATFORM_ENV)
    require_loopback_dsn_or_exit(business_dsn, label=BUSINESS_ENV)
    test_on_disposable_postgres(dsn, business_dsn)
    print("OK - Telematics cold-start bootstrap PostgreSQL checks passed")


if __name__ == "__main__":
    main()
