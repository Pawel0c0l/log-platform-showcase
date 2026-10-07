#!/usr/bin/env python3
"""Schedule activation contract on disposable PostgreSQL 16.

The last transition of the cold-start onboarding path: `enabled` false → true,
one row, one field, only after a verified recovery **chain** of one or more
contiguous successful windows. Set ``TELEMATICS_SCHEDULE_ACTIVATION_TEST_DSN`` to
a disposable PostgreSQL 16 DSN.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jobs.api.telematics import coverage_finalization as cf  # noqa: E402
from ops import activate_telematics_trips_schedule as act  # noqa: E402
from ops import telematics_cold_start_chain as ccsc  # noqa: E402
from ops.tests_manual.telematics_execution_outcome_fixtures import (  # noqa: E402
    outcome_for_window,
    recovery_job_summary,
    skipped_disabled_schedule_outcome,
)
from ops.tests_manual.postgres_dsn_safety import (  # noqa: E402
    require_loopback_dsn_or_exit,
)
from ops.tests_manual.test_telematics_cold_start_audit import (  # noqa: E402
    install_network_guard,
)

ENV = "TELEMATICS_SCHEDULE_ACTIVATION_TEST_DSN"

MIGRATIONS = (
    "008_workflow_a_control_plane.sql",
    "010_add_client_code.sql",
    "011_workflow_a_dataset_registry.sql",
    "012_workflow_a_client_dataset_schedule.sql",
    "013_workflow_a_client_table_retention.sql",
    "014_workflow_a_dispatcher_v1.sql",
    "017_workflow_a_add_client_code_to_control_tables.sql",
    "018_workflow_a_schedule_event_enrichment_mode.sql",
    "042_platform_environment_identity.sql",
    "055_workflow_a_trips_pagination_mode.sql",
    "056_workflow_a_trips_stabilization_config.sql",
    "057_workflow_a_trips_coverage_state.sql",
    "058_telematics_trips_manual_recovery.sql",
    "062_workflow_a_multi_cadence_schedule_identity.sql",
)

CID = "598bc0e3-99b6-4e38-88ea-c1c63e519b2f"
SID = "60c80b85-f294-4a00-8e09-b6a3688af443"
OTHER_SID = "f25c8a6c-7ca5-4899-8a16-2490d9e5e241"
CODE = "ECHO00001"
PLATFORM_UUID = "52517750-7438-4558-8490-2736ae4cc629"
MODE = cf.TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1

UTC = timezone.utc
BASELINE = datetime(2026, 7, 1, tzinfo=UTC)
RECOVERED_W = datetime(2026, 8, 4, 9, 0, tzinfo=UTC)
SEEDED = datetime(2026, 8, 4, 8, 0, tzinfo=UTC)
COLD_EVIDENCE = (
    "telematics-cold-start-bootstrap/1:sha256=" + ("ab" * 32)
    + ":approval=TELEMATICS-COLD-START-ECHO00001-1"
    + ":managed-start=2026-07-01T00:00:00Z"
)
HEAD = "0" * 39 + "1"
APPROVAL = "TELEMATICS-ACTIVATE-ECHO00001-1"
CHAIN = "TELEMATICS-COLD-START-ECHO00001-2026-08"
OTHER_CHAIN = "TELEMATICS-COLD-START-ECHO00001-2026-09"


def _dict_row():
    from psycopg.rows import dict_row
    return dict_row


def bootstrap(conn) -> None:
    global BASELINE, RECOVERED_W, SEEDED, COLD_EVIDENCE

    with conn.cursor(row_factory=_dict_row()) as cur:
        cur.execute("SELECT date_trunc('hour', now()) AS now")
        db_now = dict(cur.fetchone())["now"].astimezone(UTC)
    conn.rollback()
    BASELINE = db_now - timedelta(days=20)
    RECOVERED_W = db_now - timedelta(hours=4)
    SEEDED = db_now - timedelta(hours=3)
    COLD_EVIDENCE = (
        "telematics-cold-start-bootstrap/1:sha256=" + ("ab" * 32)
        + ":approval=TELEMATICS-COLD-START-ECHO00001-1"
        + ":managed-start=" + act.iso_utc(BASELINE)
    )

    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS workflow_a_control CASCADE")
        cur.execute("DROP SCHEMA IF EXISTS ops_control CASCADE")
        cur.execute("DROP TABLE IF EXISTS public.schema_migrations")
        cur.execute("DROP TABLE IF EXISTS public.runs")
        for name in MIGRATIONS:
            cur.execute((ROOT / "db/migrations" / name).read_text(encoding="utf-8"))
        cur.execute(
            "CREATE TABLE public.schema_migrations ("
            " filename TEXT PRIMARY KEY,"
            " applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        cur.executemany(
            "INSERT INTO public.schema_migrations(filename) VALUES (%s)",
            [(name,) for name in MIGRATIONS],
        )
        cur.execute(
            "CREATE TABLE public.runs (run_id UUID PRIMARY KEY,"
            " status TEXT NOT NULL,"
            " params JSONB NOT NULL DEFAULT '{}'::jsonb)"
        )
        cur.execute(
            """
            INSERT INTO ops_control.environment_identity
              (identity_key, environment, database_identity_id, database_role,
               database_name, provisioned_by)
            VALUES ('primary','production',%s,'platform','logdb','test')
            """,
            (PLATFORM_UUID,),
        )
    conn.commit()


def chain_windows(count, *, start=None, end=None):
    """`count` contiguous windows spanning [start, end], evenly and exactly."""
    start = BASELINE if start is None else start
    end = RECOVERED_W if end is None else end
    total = int((end - start).total_seconds())
    step = total // count
    bounds = [start + timedelta(seconds=step * n) for n in range(count)] + [end]
    return [
        {"ordinal": n + 1, "start": bounds[n], "end": bounds[n + 1]}
        for n in range(count)
    ]


def reset(
    conn,
    *,
    mode=MODE,
    schedule_enabled=False,
    competing_schedule=False,
    covered_through=None,
    source="manual_recovery",
    status="READY",
    recovery_status="SUCCESS",
    recovery_present=True,
    recovery_window_end=None,
    extra_recovery=False,
    running_history=False,
    chain=None,
    extra_runs=(),
    total_history=False,
) -> None:
    # Resolved at call time: `bootstrap()` rebinds these from the database clock.
    covered_through = RECOVERED_W if covered_through is None else covered_through
    recovery_window_end = recovery_window_end or covered_through
    if chain is None and recovery_present:
        chain = [{
            "ordinal": 1, "start": BASELINE, "end": recovery_window_end,
            "status": recovery_status,
        }]
    elif chain is None:
        chain = []
    with conn.cursor() as cur:
        cur.execute("DELETE FROM public.runs")
        cur.execute("DELETE FROM workflow_a_control.client_dataset_recovery_run")
        cur.execute("DELETE FROM workflow_a_control.client_schedule_run_history")
        cur.execute("DELETE FROM workflow_a_control.client_dataset_coverage")
        cur.execute("DELETE FROM workflow_a_control.client_dataset_schedule")
        cur.execute("DELETE FROM workflow_a_control.client_account")
        cur.execute(
            "ALTER TABLE workflow_a_control.client_dataset_schedule"
            " DROP CONSTRAINT IF EXISTS uq_client_dataset_schedule"
        )
        # M5 added a partial unique index enforcing one BASE schedule
        # per (client, dataset). Modelling an ambiguous schedule now
        # means defeating both structures, not just the constraint.
        cur.execute(
            "DROP INDEX IF EXISTS"
            " workflow_a_control.uq_client_dataset_schedule_base"
        )
        cur.execute(
            """
            INSERT INTO workflow_a_control.client_account
              (client_id, client_code, client_name, provider_type,
               provider_base_url, provider_basic_auth_username,
               provider_basic_auth_password_secret_ref, client_db_host,
               client_db_port, client_db_name, client_db_user,
               client_db_password_secret_ref, client_db_schema,
               speed_trigger_filter_text, enabled, trips_pagination_mode,
               trips_stabilization_delay_seconds, trips_overlap_seconds,
               trips_max_recovery_span_seconds)
            VALUES (%s,%s,'Echo','telematics','https://example.invalid','u','REF',
                    '127.0.0.1',5432,'db','u','REF','public','speeding',true,%s,
                    10800,3600,2678400)
            """,
            (CID, CODE, mode),
        )
        cur.execute(
            """
            INSERT INTO workflow_a_control.client_dataset_schedule
              (schedule_id, client_id, client_code, dataset_name, enabled,
               frequency, run_time, timezone, lookback_days, overwrite_existing,
               event_enrichment_mode)
            VALUES (%s,%s,%s,'trips_sync',%s,'daily','02:00','UTC',1,true,
                    'enabled')
            """,
            (SID, CID, CODE, schedule_enabled),
        )
        if competing_schedule:
            cur.execute(
                """
                INSERT INTO workflow_a_control.client_dataset_schedule
                  (schedule_id, client_id, client_code, dataset_name, enabled,
                   frequency, run_time, timezone, lookback_days,
                   overwrite_existing)
                VALUES (%s,%s,%s,'trips_sync',false,'daily','03:00','UTC',1,true)
                """,
                (OTHER_SID, CID, CODE),
            )
        cur.execute(
            """
            INSERT INTO workflow_a_control.client_dataset_coverage
              (schedule_id, client_id, client_code, dataset_name,
               coverage_start_ts, covered_through_ts, bootstrap_status,
               bootstrap_evidence_ref, seeded_at, seeded_by,
               covered_through_source, last_gap_detected_ts, updated_at)
            VALUES (%s,%s,%s,'trips_sync',%s,%s,%s,%s,%s,'operator',%s,NULL,%s)
            """,
            (SID, CID, CODE, BASELINE, covered_through, status, COLD_EVIDENCE,
             SEEDED, source, SEEDED),
        )
        for spec in chain:
            _insert_recovery(cur, **spec)
        if extra_recovery:
            _insert_recovery(
                cur, ordinal=9, start=BASELINE,
                end=recovery_window_end + timedelta(hours=1), status="FAILED",
                approval="TELEMATICS-C11-ECHO00001-UNRELATED",
            )
        for run_status in extra_runs:
            cur.execute(
                "INSERT INTO public.runs (run_id, status, params)"
                " VALUES (%s,%s,%s::jsonb)",
                (str(uuid.uuid4()), run_status,
                 json.dumps({"client_id": CID, "client_code": CODE})),
            )
        if total_history:
            cur.execute(
                """
                INSERT INTO workflow_a_control.client_schedule_run_history
                  (schedule_id, client_id, client_code, dataset_name,
                   window_start_ts, window_end_ts, scheduled_fire_ts, status)
                VALUES (%s,%s,%s,'trips_sync',%s,%s,%s,'SUCCESS')
                """,
                (SID, CID, CODE, BASELINE, RECOVERED_W,
                 RECOVERED_W - timedelta(hours=1)),
            )
        if running_history:
            cur.execute(
                """
                INSERT INTO workflow_a_control.client_schedule_run_history
                  (schedule_id, client_id, client_code, dataset_name,
                   window_start_ts, window_end_ts, scheduled_fire_ts, status)
                VALUES (%s,%s,%s,'trips_sync',%s,%s,%s,'RUNNING')
                """,
                (SID, CID, CODE, BASELINE, RECOVERED_W, RECOVERED_W),
            )
    conn.commit()


#: Sentinel meaning "build the correct committed proof for this window".
GENUINE_PROOF = object()


def _insert_recovery(
    cur, *, ordinal, start, end, status="SUCCESS", approval=None,
    register_run=True, run_status="SUCCESS", run_id=None, chain_ref=CHAIN,
    client_id=CID, schedule_id=SID, dataset_name="trips_sync",
    recovery_run_id=None, proof=GENUINE_PROOF, job_summary=GENUINE_PROOF,
) -> str:
    """One recovery row plus, by default, the platform run it corresponds to.

    `proof` controls the structured execution record activation now requires:
    the default builds the genuine committed one, `None` reproduces a
    pre-hardening row that carries no proof, and an explicit dict injects a
    skipped or mismatched proof. `job_summary=None` reproduces a row with no
    `job_summary` column value at all.
    """
    terminal = status in ("SUCCESS", "FAILED", "FINALIZATION_CONFLICT")
    if register_run and run_id is None:
        run_id = str(uuid.uuid4())
    if register_run:
        cur.execute(
            "INSERT INTO public.runs (run_id, status, params)"
            " VALUES (%s,%s,%s::jsonb) ON CONFLICT (run_id) DO NOTHING",
            (run_id, run_status,
             json.dumps({"client_id": CID, "client_code": CODE})),
        )
    recovery_run_id = recovery_run_id or str(uuid.uuid4())
    if proof is GENUINE_PROOF:
        proof = outcome_for_window(
            client_id=client_id, client_code=CODE, schedule_id=schedule_id,
            recovery_run_id=recovery_run_id,
            window_start_ts=start, window_end_ts=end,
            platform_run_id=run_id if register_run else None,
            dataset_name=dataset_name,
        )
    if job_summary is GENUINE_PROOF:
        job_summary = recovery_job_summary(proof)
    cur.execute(
        """
        INSERT INTO workflow_a_control.client_dataset_recovery_run
          (recovery_run_id, client_id, client_code, schedule_id, dataset_name,
           window_start_ts, window_end_ts, expected_old_covered_through_ts,
           status, reason, approval_ref, repository_head, pagination_mode,
           stabilization_delay_seconds, overlap_seconds,
           max_recovery_span_seconds, initial_coverage_snapshot,
           initial_coverage_fingerprint, platform_run_id, job_summary,
           created_at, started_at, finished_at,
           error_classification, error_summary)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'cold start',%s,%s,
                'data_invariants_v1',10800,3600,2678400,'{}'::jsonb,%s,%s,%s,
                %s,%s,%s,%s,%s)
        """,
        (
            recovery_run_id,
            client_id, CODE, schedule_id, dataset_name, start, end, start,
            status,
            approval or ccsc.window_approval_ref(chain_ref, ordinal),
            HEAD, "a" * 64, run_id if register_run else None,
            None if job_summary is None else json.dumps(job_summary),
            SEEDED + timedelta(seconds=ordinal),
            SEEDED + timedelta(seconds=ordinal),
            (SEEDED + timedelta(seconds=ordinal)) if terminal else None,
            None if status in ("SUCCESS", "PLANNED", "RUNNING")
            else "RECOVERY_BUSINESS_FAILED",
            None if status in ("SUCCESS", "PLANNED", "RUNNING") else "earlier",
        ),
    )
    return run_id


def coverage(conn) -> dict:
    with conn.cursor(row_factory=_dict_row()) as cur:
        cur.execute(
            "SELECT schedule_id::text AS schedule_id,"
            " client_id::text AS client_id, client_code, dataset_name,"
            " coverage_start_ts, covered_through_ts, bootstrap_status,"
            " bootstrap_evidence_ref, covered_through_source, seeded_at,"
            " seeded_by, last_gap_detected_ts, updated_at"
            " FROM workflow_a_control.client_dataset_coverage WHERE schedule_id=%s",
            (SID,),
        )
        row = dict(cur.fetchone())
    conn.rollback()
    return row


def fingerprint(conn) -> str:
    return cf.coverage_fingerprint(coverage(conn))


def schedule_row(conn) -> dict:
    with conn.cursor(row_factory=_dict_row()) as cur:
        cur.execute(
            "SELECT to_jsonb(t) AS row"
            "  FROM workflow_a_control.client_dataset_schedule AS t"
            " WHERE schedule_id = %s",
            (SID,),
        )
        row = dict(dict(cur.fetchone())["row"])
    conn.rollback()
    return row


def snapshot(conn) -> dict:
    out = {}
    with conn.cursor(row_factory=_dict_row()) as cur:
        for table in (
            "client_account", "client_dataset_schedule",
            "client_dataset_coverage", "client_schedule_run_history",
            "client_dataset_recovery_run",
        ):
            cur.execute(
                f"SELECT to_jsonb(t) AS row FROM workflow_a_control.{table} AS t"
            )
            out[table] = sorted(
                json.dumps(r["row"], sort_keys=True) for r in cur.fetchall()
            )
    conn.rollback()
    return out


def args(conn, dsn: str, *, execute=False, **overrides):
    base = {
        "client-code": CODE,
        "dataset": "trips_sync",
        "expected-schedule-id": SID,
        "expected-covered-through": act.iso_utc(RECOVERED_W),
        "expected-coverage-fingerprint": fingerprint(conn),
        "cold-start-chain-ref": CHAIN,
        "expected-successful-window-count": 1,
        "approved-final-chain-boundary": act.iso_utc(RECOVERED_W),
        "approval-ref": APPROVAL,
        "expected-environment": "production",
        "expected-platform-uuid": PLATFORM_UUID,
        "dsn": dsn,
    }
    base.update({k.replace("_", "-"): v for k, v in overrides.items()})
    argv = []
    for key, value in base.items():
        if value is None:
            continue
        argv += [f"--{key}", str(value)]
    if execute:
        argv += ["--execute", "--confirm-client-code", CODE]
    return act.build_parser().parse_args(argv)


def _expect(conn, dsn, code, **overrides) -> None:
    try:
        act.run(args(conn, dsn, **overrides))
    except act.ActivationRefused as exc:
        assert exc.code == code, f"expected {code}, got {exc.code}"
        return
    raise AssertionError(f"expected refusal {code}")


# ---------------------------------------------------------------------------

def test_dry_run_writes_nothing(conn, dsn) -> None:
    reset(conn)
    before = snapshot(conn)
    exit_code, plan = act.run(args(conn, dsn))
    assert exit_code == act.EXIT_OK
    assert plan["mode"] == "DRY_RUN"
    assert plan["would_activate"] is True
    assert plan["rows_to_update"] == 1
    assert plan["fields_to_change"] == ["enabled"]
    assert plan["timing_fields_changed"] == 0
    assert plan["client_mode_changes"] == 0
    assert plan["coverage_mutations"] == 0
    assert plan["recovery_mutations"] == 0
    assert plan["history_rows_created"] == 0
    assert plan["provider_requests"] == 0
    assert plan["subprocesses_launched"] == 0
    assert plan["database_writes_performed"] == 0
    assert plan["historical_scheduled_fires"] == 0
    conn.rollback()
    assert snapshot(conn) == before, "a dry run must write nothing"


def test_execute_requires_confirmation(conn, dsn) -> None:
    reset(conn)
    argv_args = args(conn, dsn)
    argv_args.execute = True
    argv_args.confirm_client_code = None
    try:
        act.run(argv_args)
    except act.ActivationRefused as exc:
        assert exc.code == "ACTIVATION_REFUSED_CONFIRMATION"
    else:
        raise AssertionError("--execute without confirmation was accepted")
    argv_args.confirm_client_code = "WRONG0001"
    try:
        act.run(argv_args)
    except act.ActivationRefused as exc:
        assert exc.code == "ACTIVATION_REFUSED_CONFIRMATION"
    else:
        raise AssertionError("a mismatched confirmation was accepted")
    assert schedule_row(conn)["enabled"] is False


def test_activation_changes_exactly_one_field(conn, dsn) -> None:
    reset(conn)
    before_row = schedule_row(conn)
    before = snapshot(conn)
    exit_code, plan = act.run(args(conn, dsn, execute=True))
    assert exit_code == act.EXIT_OK
    assert plan["activation_result"] == "ACTIVATED"
    assert plan["affected_row_count"] == 1
    assert plan["database_writes_performed"] == 1
    assert plan["transaction_result"] == "COMMITTED"
    assert plan["changed_fields"] == ["enabled"]
    assert plan["schedule_enabled_after"] is True
    assert plan["dispatcher_visible_after"] == 1

    after_row = schedule_row(conn)
    assert after_row["enabled"] is True
    for key in sorted(before_row):
        if key == "enabled":
            continue
        assert after_row[key] == before_row[key], key
    # Nothing outside the schedule table moved.
    after = snapshot(conn)
    for table in (
        "client_account", "client_dataset_coverage",
        "client_schedule_run_history", "client_dataset_recovery_run",
    ):
        assert after[table] == before[table], table


def test_second_activation_reports_already_enabled(conn, dsn) -> None:
    """The schedule is enabled from the previous case; no second write occurs."""
    before = snapshot(conn)
    exit_code, plan = act.run(args(conn, dsn, execute=True))
    assert exit_code == act.EXIT_OK
    assert plan["activation_result"] == "ALREADY_ENABLED"
    assert plan["database_writes_performed"] == 0
    assert plan["rows_to_update"] == 0
    conn.rollback()
    assert snapshot(conn) == before


def test_concurrent_activation_is_safe(conn, dsn) -> None:
    """A racing activation between the plan and the write writes nothing twice."""
    import psycopg

    reset(conn)
    original = act._schedule_row
    raced = {"n": 0}

    def race_then_read(cur, *, schedule_id):
        raced["n"] += 1
        return original(cur, schedule_id=schedule_id)

    # The racer commits before this tool takes its lock, by racing inside the
    # read-only planning phase.
    original_gates = act.evaluate_gates
    fired = {"n": 0}

    def gates_then_race(cur, **kwargs):
        result = original_gates(cur, **kwargs)
        if fired["n"] == 0:
            fired["n"] = 1
            with psycopg.connect(dsn, autocommit=True) as other:
                other.execute(
                    "UPDATE workflow_a_control.client_dataset_schedule"
                    " SET enabled = true WHERE schedule_id = %s",
                    (SID,),
                )
        return result

    act._schedule_row = race_then_read
    act.evaluate_gates = gates_then_race
    try:
        exit_code, plan = act.run(args(conn, dsn, execute=True))
    finally:
        act._schedule_row = original
        act.evaluate_gates = original_gates
    assert exit_code == act.EXIT_OK
    assert plan["activation_result"] == "ALREADY_ENABLED"
    assert plan["database_writes_performed"] == 0
    assert schedule_row(conn)["enabled"] is True


def test_activation_before_a_successful_recovery_is_refused(conn, dsn) -> None:
    reset(conn, recovery_present=False)
    _expect(conn, dsn, "ACTIVATION_REFUSED_RECOVERY")

    # Every non-SUCCESS terminal or in-flight state is refused by the recovery
    # gate, which is evaluated before the concurrency gate: "not SUCCESS" is the
    # stronger and more informative statement about a PLANNED/RUNNING row.
    for status in ("FAILED", "FINALIZATION_CONFLICT", "PLANNED", "RUNNING"):
        reset(conn, recovery_status=status)
        _expect(conn, dsn, "ACTIVATION_REFUSED_RECOVERY")

    # A successful chain accompanied by a second, active window is refused too.
    reset(conn)
    with conn.cursor() as cur:
        _insert_recovery(
            cur, ordinal=2, start=RECOVERED_W,
            end=RECOVERED_W + timedelta(hours=1), status="PLANNED",
            register_run=False,
        )
    conn.commit()
    _expect(conn, dsn, "ACTIVATION_REFUSED_RECOVERY")
    assert schedule_row(conn)["enabled"] is False


def test_activation_on_a_bare_baseline_is_refused(conn, dsn) -> None:
    """A cold-start baseline that no recovery has advanced is not activatable."""
    reset(conn, covered_through=BASELINE, source="bootstrap",
          recovery_present=False)
    _expect(
        conn, dsn, "ACTIVATION_REFUSED_COVERAGE",
        expected_covered_through=act.iso_utc(BASELINE),
        approved_final_chain_boundary=act.iso_utc(BASELINE),
    )
    assert schedule_row(conn)["enabled"] is False


def test_wrong_watermark_or_fingerprint_is_refused(conn, dsn) -> None:
    reset(conn)
    _expect(
        conn, dsn, "ACTIVATION_REFUSED_COVERAGE",
        expected_covered_through="2026-08-04T10:00:00Z",
        approved_final_chain_boundary="2026-08-04T10:00:00Z",
    )
    _expect(
        conn, dsn, "ACTIVATION_REFUSED_COVERAGE",
        expected_coverage_fingerprint="0" * 64,
    )
    _expect(
        conn, dsn, "ACTIVATION_REFUSED_SCHEDULE",
        expected_schedule_id="db8055e0-e030-4d5a-816b-ec4dc338d698",
    )
    assert schedule_row(conn)["enabled"] is False


def test_other_refusals(conn, dsn) -> None:
    reset(conn, mode="strict_meta")
    _expect(conn, dsn, "ACTIVATION_REFUSED_MODE")

    reset(conn, competing_schedule=True)
    _expect(conn, dsn, "ACTIVATION_REFUSED_SCHEDULE")

    reset(conn, running_history=True)
    _expect(conn, dsn, "ACTIVATION_REFUSED_CONCURRENCY")

    reset(conn, extra_recovery=True)
    _expect(conn, dsn, "ACTIVATION_REFUSED_RECOVERY")

    reset(conn, status="GAP_DETECTED")
    _expect(conn, dsn, "ACTIVATION_REFUSED_COVERAGE")

    reset(conn)
    _expect(
        conn, dsn, "ACTIVATION_REFUSED_IDENTITY",
        expected_environment="local_dev",
    )
    _expect(
        conn, dsn, "ACTIVATION_REFUSED_IDENTITY",
        expected_platform_uuid="db8055e0-e030-4d5a-816b-ec4dc338d698",
    )
    assert schedule_row(conn)["enabled"] is False


def test_dispatcher_sees_the_schedule_only_after_activation(conn, dsn) -> None:
    from jobs.api.telematics import dispatcher

    import psycopg
    from psycopg.rows import dict_row

    reset(conn)
    with psycopg.connect(dsn, row_factory=dict_row, autocommit=True) as probe:
        assert dispatcher._load_enabled_schedules(probe) == []
    act.run(args(conn, dsn, execute=True))
    with psycopg.connect(dsn, row_factory=dict_row, autocommit=True) as probe:
        loaded = dispatcher._load_enabled_schedules(probe)
    assert [row.schedule_id for row in loaded] == [SID]


# ---------------------------------------------------------------------------
# Recovery chains of N windows
# ---------------------------------------------------------------------------

def _activate_chain(conn, dsn, count) -> dict:
    reset(conn, chain=chain_windows(count))
    exit_code, plan = act.run(
        args(
            conn, dsn, execute=True,
            expected_successful_window_count=count,
        )
    )
    assert exit_code == act.EXIT_OK, plan
    return plan


def test_one_two_and_three_window_chains_all_activate(conn, dsn) -> None:
    """A one-window cold start is a chain of length one and still activates."""
    for count in (1, 2, 3):
        plan = _activate_chain(conn, dsn, count)
        assert plan["activation_result"] == "ACTIVATED", count
        assert plan["chain_successful_window_count"] == count
        assert plan["expected_successful_window_count"] == count
        assert plan["cold_start_chain_ref"] == CHAIN
        assert plan["changed_fields"] == ["enabled"]
        assert plan["affected_row_count"] == 1
        assert plan["database_writes_performed"] == 1
        assert plan["schedule_enabled_after"] is True
        windows = plan["chain_windows"]
        assert [w["window_ordinal"] for w in windows] == list(range(1, count + 1))
        assert windows[0]["window_start_ts"] == act.iso_utc(BASELINE)
        assert windows[-1]["window_end_ts"] == act.iso_utc(RECOVERED_W)
        for previous, following in zip(windows, windows[1:]):
            assert previous["window_end_ts"] == following["window_start_ts"]
        assert plan["chain_business_run_correspondence"] == {
            "chain_business_runs": count,
            "target_business_runs": count,
            "unrelated_business_runs": 0,
        }


def test_activation_verifies_the_expected_window_count(conn, dsn) -> None:
    reset(conn, chain=chain_windows(2))
    # The chain is genuinely complete, but the operator declared a different N.
    for declared in (1, 3):
        _expect(
            conn, dsn, "ACTIVATION_REFUSED_RECOVERY",
            expected_successful_window_count=declared,
        )
    # And zero is refused before any state is read at all.
    _expect(
        conn, dsn, "ACTIVATION_REFUSED_PARAMETER",
        expected_successful_window_count=0,
    )
    assert schedule_row(conn)["enabled"] is False


def test_activation_refuses_an_incomplete_chain(conn, dsn) -> None:
    """The last successful window must end exactly at the stored watermark."""
    windows = chain_windows(2)
    reset(conn, chain=windows[:1])
    _expect(conn, dsn, "ACTIVATION_REFUSED_RECOVERY")
    assert schedule_row(conn)["enabled"] is False


def test_activation_refuses_a_gapped_chain(conn, dsn) -> None:
    windows = chain_windows(2)
    windows[1]["start"] = windows[1]["start"] + timedelta(seconds=1)
    reset(conn, chain=windows)
    _expect(
        conn, dsn, "ACTIVATION_REFUSED_RECOVERY",
        expected_successful_window_count=2,
    )
    # A chain that does not start at the original baseline A is a gap too.
    windows = chain_windows(2)
    windows[0]["start"] = windows[0]["start"] + timedelta(seconds=1)
    reset(conn, chain=windows)
    _expect(
        conn, dsn, "ACTIVATION_REFUSED_RECOVERY",
        expected_successful_window_count=2,
    )
    assert schedule_row(conn)["enabled"] is False


def test_activation_refuses_an_overlapping_chain(conn, dsn) -> None:
    windows = chain_windows(2)
    windows[1]["start"] = windows[1]["start"] - timedelta(seconds=1)
    reset(conn, chain=windows)
    _expect(
        conn, dsn, "ACTIVATION_REFUSED_RECOVERY",
        expected_successful_window_count=2,
    )
    # A missing ordinal is refused before any geometry is considered.
    windows = chain_windows(2)
    windows[1]["ordinal"] = 3
    reset(conn, chain=windows)
    _expect(
        conn, dsn, "ACTIVATION_REFUSED_RECOVERY",
        expected_successful_window_count=2,
    )
    assert schedule_row(conn)["enabled"] is False


def test_activation_refuses_a_failed_chain_row(conn, dsn) -> None:
    for status in ("FAILED", "FINALIZATION_CONFLICT"):
        windows = chain_windows(2)
        windows[0]["status"] = status
        reset(conn, chain=windows)
        _expect(
            conn, dsn, "ACTIVATION_REFUSED_RECOVERY",
            expected_successful_window_count=2,
        )
    assert schedule_row(conn)["enabled"] is False


def test_activation_refuses_unrelated_recovery_and_run_state(conn, dsn) -> None:
    # A recovery row of another chain.
    windows = chain_windows(2)
    windows.append({
        "ordinal": 1, "start": BASELINE, "end": RECOVERED_W,
        "chain_ref": OTHER_CHAIN, "register_run": False,
    })
    reset(conn, chain=windows)
    _expect(
        conn, dsn, "ACTIVATION_REFUSED_RECOVERY",
        expected_successful_window_count=2,
    )

    # A platform business run that belongs to no chain window.
    reset(conn, chain=chain_windows(2), extra_runs=("SUCCESS",))
    _expect(
        conn, dsn, "ACTIVATION_REFUSED_RECOVERY",
        expected_successful_window_count=2,
    )

    # A chain window whose business run never succeeded.
    windows = chain_windows(2)
    windows[0]["run_status"] = "FAILED"
    reset(conn, chain=windows)
    _expect(
        conn, dsn, "ACTIVATION_REFUSED_RECOVERY",
        expected_successful_window_count=2,
    )

    # A chain window with no business run at all.
    windows = chain_windows(2)
    windows[1]["register_run"] = False
    reset(conn, chain=windows)
    _expect(
        conn, dsn, "ACTIVATION_REFUSED_RECOVERY",
        expected_successful_window_count=2,
    )
    assert schedule_row(conn)["enabled"] is False


def test_activation_refuses_a_final_w_mismatch(conn, dsn) -> None:
    reset(conn, chain=chain_windows(2))
    # The approved final chain boundary must be the watermark being activated on.
    _expect(
        conn, dsn, "ACTIVATION_REFUSED_PARAMETER",
        expected_successful_window_count=2,
        approved_final_chain_boundary=act.iso_utc(
            RECOVERED_W - timedelta(hours=1)
        ),
    )
    # And a chain whose last window ends elsewhere than W is refused.
    windows = chain_windows(2)
    windows[-1]["end"] = RECOVERED_W - timedelta(seconds=1)
    reset(conn, chain=windows)
    _expect(
        conn, dsn, "ACTIVATION_REFUSED_RECOVERY",
        expected_successful_window_count=2,
    )
    assert schedule_row(conn)["enabled"] is False


def test_activation_refuses_any_schedule_history(conn, dsn) -> None:
    reset(conn, chain=chain_windows(2), total_history=True)
    _expect(
        conn, dsn, "ACTIVATION_REFUSED_HISTORY",
        expected_successful_window_count=2,
    )
    assert schedule_row(conn)["enabled"] is False


def test_two_window_chain_dry_run_writes_nothing(conn, dsn) -> None:
    reset(conn, chain=chain_windows(2))
    before = snapshot(conn)
    exit_code, plan = act.run(
        args(conn, dsn, expected_successful_window_count=2)
    )
    assert exit_code == act.EXIT_OK
    assert plan["mode"] == "DRY_RUN"
    assert plan["would_activate"] is True
    assert plan["chain_successful_window_count"] == 2
    assert plan["database_writes_performed"] == 0
    assert plan["coverage_mutations"] == 0
    assert plan["recovery_mutations"] == 0
    assert plan["history_rows_created"] == 0
    assert plan["provider_requests"] == 0
    assert plan["subprocesses_launched"] == 0
    conn.rollback()
    assert snapshot(conn) == before, "a dry run must write nothing"


def test_module_launches_nothing() -> None:
    import inspect

    text = (
        ROOT / "ops" / "activate_telematics_trips_schedule.py"
    ).read_text(encoding="utf-8")
    for forbidden in (
        "import requests", "provider_client", "runner.py", "Popen",
        "import subprocess", "subprocess.", "os.system",
        "INSERT INTO", "DELETE FROM",
    ):
        assert forbidden not in text, forbidden
    # Exactly one UPDATE in the whole module, and it touches only `enabled`.
    assert text.count("UPDATE workflow_a_control") == 1
    assert text.count("UPDATE workflow_a_control.client_dataset_schedule") == 1
    statement = inspect.getsource(act.execute_activation)
    assert statement.count("SET enabled = true") == 1
    for forbidden in (
        "run_time", "timezone", "lookback_days", "frequency",
        "overwrite_existing", "event_enrichment_mode", "updated_at",
        "trips_pagination_mode = ", "client_dataset_coverage SET",
    ):
        assert f"SET {forbidden}" not in statement, forbidden
    assert "client_dataset_coverage" not in statement
    assert "client_dataset_recovery_run" not in statement


# ---------------------------------------------------------------------------
# Structured execution proof (the hardening contract)
# ---------------------------------------------------------------------------

def test_activation_accepts_genuine_committed_recovery_evidence(conn, dsn) -> None:
    """The happy path, stated explicitly: a real committed proof activates."""
    reset(conn)
    exit_code, plan = act.run(args(conn, dsn))
    assert exit_code == act.EXIT_OK, plan
    proofs = plan["chain_execution_proofs"]
    assert len(proofs) == 1, proofs
    assert proofs[0]["outcome"] == "EXECUTED_COMMITTED", proofs
    assert proofs[0]["transaction_status"] == "COMMITTED", proofs
    assert proofs[0]["provider_execution_entered"] is True, proofs


def test_activation_refuses_a_recovery_without_execution_proof(conn, dsn) -> None:
    """A SUCCESS row from before structured proof existed is not evidence."""
    reset(conn, chain=[{
        "ordinal": 1, "start": BASELINE, "end": RECOVERED_W,
        "status": "SUCCESS", "job_summary": None,
    }])
    _expect(conn, dsn, "ACTIVATION_REFUSED_EXECUTION_PROOF")
    assert schedule_row(conn)["enabled"] is False

    reset(conn, chain=[{
        "ordinal": 1, "start": BASELINE, "end": RECOVERED_W,
        "status": "SUCCESS", "proof": None,
    }])
    _expect(conn, dsn, "ACTIVATION_REFUSED_EXECUTION_PROOF")
    assert schedule_row(conn)["enabled"] is False


def test_activation_refuses_skipped_recovery_evidence(conn, dsn) -> None:
    """Exactly the ECHO00001 shape: SUCCESS row, rc=0, zero work performed."""
    recovery_run_id = str(uuid.uuid4())
    reset(conn, chain=[{
        "ordinal": 1, "start": BASELINE, "end": RECOVERED_W,
        "status": "SUCCESS",
        "recovery_run_id": recovery_run_id,
        "proof": skipped_disabled_schedule_outcome(
            {
                "client_id": CID, "client_code": CODE,
                "manual_recovery_run_id": recovery_run_id,
                "window_start_ts": BASELINE.isoformat(),
                "window_end_ts": RECOVERED_W.isoformat(),
            },
            schedule_id=SID,
        ),
    }])
    _expect(conn, dsn, "ACTIVATION_REFUSED_EXECUTION_PROOF")
    assert schedule_row(conn)["enabled"] is False


def test_activation_refuses_a_mismatched_execution_proof(conn, dsn) -> None:
    """A well-formed proof describing another window or client is refused."""
    for field, value in (
        ("client_id", "bd7662a5-eeb4-4614-8720-d477abfcb227"),
        ("schedule_id", "b454f82c-5857-4bab-8342-b7258e5cf7de"),
        ("dataset_name", "fuel_daily_aggregation"),
        ("recovery_run_id", str(uuid.uuid4())),
    ):
        recovery_run_id = str(uuid.uuid4())
        proof = outcome_for_window(
            client_id=CID, client_code=CODE, schedule_id=SID,
            recovery_run_id=recovery_run_id,
            window_start_ts=BASELINE, window_end_ts=RECOVERED_W,
        )
        proof[field] = value
        reset(conn, chain=[{
            "ordinal": 1, "start": BASELINE, "end": RECOVERED_W,
            "status": "SUCCESS", "recovery_run_id": recovery_run_id,
            "proof": proof,
        }])
        _expect(conn, dsn, "ACTIVATION_REFUSED_EXECUTION_PROOF")
        assert schedule_row(conn)["enabled"] is False, field

    # A proof whose window does not match the recovery row's window.
    recovery_run_id = str(uuid.uuid4())
    reset(conn, chain=[{
        "ordinal": 1, "start": BASELINE, "end": RECOVERED_W,
        "status": "SUCCESS", "recovery_run_id": recovery_run_id,
        "proof": outcome_for_window(
            client_id=CID, client_code=CODE, schedule_id=SID,
            recovery_run_id=recovery_run_id,
            window_start_ts=BASELINE,
            window_end_ts=RECOVERED_W - timedelta(hours=2),
        ),
    }])
    _expect(conn, dsn, "ACTIVATION_REFUSED_EXECUTION_PROOF")
    assert schedule_row(conn)["enabled"] is False


def test_activation_refuses_a_proof_without_a_valid_platform_run_id(
    conn, dsn,
) -> None:
    """Every accepted chain member must name the platform run its row recorded.

    A proof with no platform-run identity cannot be tied back to a real platform
    run, so it is not evidence of committed business work however well-formed the
    rest of it is. A proof naming a *different* valid run is equally refused.
    """
    for label, value in (
        ("absent", None),
        ("empty", ""),
        ("blank", "   "),
        ("malformed", "not-a-uuid"),
        ("foreign", "f6222a11-06ee-4e4f-8b25-302a9d963cfa"),
    ):
        recovery_run_id = str(uuid.uuid4())
        run_id = str(uuid.uuid4())
        proof = outcome_for_window(
            client_id=CID, client_code=CODE, schedule_id=SID,
            recovery_run_id=recovery_run_id,
            window_start_ts=BASELINE, window_end_ts=RECOVERED_W,
            platform_run_id=run_id,
        )
        proof["platform_run_id"] = value
        reset(conn, chain=[{
            "ordinal": 1, "start": BASELINE, "end": RECOVERED_W,
            "status": "SUCCESS", "recovery_run_id": recovery_run_id,
            "run_id": run_id, "proof": proof,
        }])
        _expect(conn, dsn, "ACTIVATION_REFUSED_EXECUTION_PROOF")
        assert schedule_row(conn)["enabled"] is False, label

    # The permitted case, so the requirement is not vacuously strict: the proof
    # names exactly the platform run the recovery row recorded.
    recovery_run_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    reset(conn, chain=[{
        "ordinal": 1, "start": BASELINE, "end": RECOVERED_W,
        "status": "SUCCESS", "recovery_run_id": recovery_run_id,
        "run_id": run_id,
        "proof": outcome_for_window(
            client_id=CID, client_code=CODE, schedule_id=SID,
            recovery_run_id=recovery_run_id,
            window_start_ts=BASELINE, window_end_ts=RECOVERED_W,
            platform_run_id=run_id,
        ),
    }])
    exit_code, plan = act.run(args(conn, dsn, execute=True))
    assert exit_code == act.EXIT_OK, plan
    assert schedule_row(conn)["enabled"] is True, plan
    assert plan["chain_execution_proofs"][0]["platform_run_id"] == run_id, plan


def test_every_chain_window_needs_its_own_proof(conn, dsn) -> None:
    """A two-window chain where only the first window carries proof refuses."""
    mid = (BASELINE + (RECOVERED_W - BASELINE) / 2).replace(microsecond=0)
    reset(conn, chain=[
        {"ordinal": 1, "start": BASELINE, "end": mid, "status": "SUCCESS"},
        {"ordinal": 2, "start": mid, "end": RECOVERED_W, "status": "SUCCESS",
         "proof": None},
    ])
    _expect(
        conn, dsn, "ACTIVATION_REFUSED_EXECUTION_PROOF",
        expected_successful_window_count=2,
    )
    assert schedule_row(conn)["enabled"] is False


# ---------------------------------------------------------------------------
# Deny-by-default schedule-mutation surfaces
# ---------------------------------------------------------------------------

def test_an_alternate_activation_path_cannot_bypass_the_strict_guard() -> None:
    """A newly written activation path gets no exemption.

    Two independent failures are proven: an unregistered surface is refused
    outright, and even the registered surface is refused for a strict_meta
    client. A hypothetical new tool therefore cannot enable a strict_meta
    `trips_sync` schedule either by being new or by copying the registered name.
    """
    from jobs.api.telematics import schedule_mutation_surfaces as sms

    try:
        sms.assert_activation_permitted(
            surface="ops/some_new_activation_tool.py",
            dataset_name="trips_sync",
            pagination_mode="data_invariants_v1",
        )
    except sms.ScheduleMutationRefused as exc:
        assert exc.code == "SCHEDULE_MUTATION_SURFACE_UNREGISTERED", exc.code
    else:
        raise AssertionError("an unregistered activation surface was permitted")

    try:
        sms.assert_activation_permitted(
            surface=sms.SURFACE_ACTIVATE_TRIPS_SCHEDULE,
            dataset_name="trips_sync",
            pagination_mode="strict_meta",
        )
    except sms.ScheduleMutationRefused as exc:
        assert exc.code == "SCHEDULE_ACTIVATION_REFUSED_STRICT_META", exc.code
    else:
        raise AssertionError("strict_meta activation was permitted")

    # The onboarding surface is registered, but not for activation.
    try:
        sms.assert_activation_permitted(
            surface=sms.SURFACE_ONBOARDING,
            dataset_name="trips_sync",
            pagination_mode="data_invariants_v1",
        )
    except sms.ScheduleMutationRefused as exc:
        assert exc.code == "SCHEDULE_MUTATION_SURFACE_NOT_PERMITTED", exc.code
    else:
        raise AssertionError("the creation surface was permitted to activate")


def test_unregistered_schedule_mutation_surface_fails_deny_by_default() -> None:
    """Creation is deny-by-default too, and trips_sync must be created disabled."""
    from jobs.api.telematics import schedule_mutation_surfaces as sms

    try:
        sms.assert_creation_permitted(
            surface="scripts/some_new_onboarding_tool.py",
            dataset_name="trips_sync", enabled=False,
        )
    except sms.ScheduleMutationRefused as exc:
        assert exc.code == "SCHEDULE_MUTATION_SURFACE_UNREGISTERED", exc.code
    else:
        raise AssertionError("an unregistered creation surface was permitted")

    try:
        sms.assert_creation_permitted(
            surface=sms.SURFACE_ONBOARDING,
            dataset_name="trips_sync", enabled=True,
        )
    except sms.ScheduleMutationRefused as exc:
        assert exc.code == "SCHEDULE_CREATION_REFUSED_ENABLED", exc.code
    else:
        raise AssertionError("onboarding was permitted to create it enabled")

    # The permitted case, so the guard is not vacuously strict.
    sms.assert_creation_permitted(
        surface=sms.SURFACE_ONBOARDING,
        dataset_name="trips_sync", enabled=False,
    )


def test_the_documented_scanning_test_location_is_real() -> None:
    """The registry's docstring must point at a test that actually exists.

    `schedule_mutation_surfaces` tells a future maintainer where the repository
    scan lives. A stale path there is worse than none: it advertises coverage
    nobody can find. Both the file and the named function are checked.
    """
    import inspect

    import jobs.api.telematics.schedule_mutation_surfaces as sms

    doc = inspect.getdoc(sms) or ""
    referenced = [
        token.strip("`")
        for token in doc.split()
        if token.strip("`").startswith("ops/tests_manual/")
    ]
    assert referenced, "the registry names no scanning test at all"
    root = Path(__file__).resolve().parents[2]
    for relative in referenced:
        assert (root / relative).is_file(), (
            f"{relative} is referenced by schedule_mutation_surfaces but does "
            "not exist"
        )
    assert "test_no_unregistered_module_mutates_schedule_enablement" in doc, doc
    assert str(Path(__file__).resolve().relative_to(root)) in referenced, (
        "the scan lives in this file; the registry should name it"
    )


def test_no_unregistered_module_mutates_schedule_enablement() -> None:
    """The enumeration in the registry must stay true of the repository.

    Scans every non-test Python module for an INSERT into, or an `enabled`
    UPDATE of, `client_dataset_schedule`. Only the two registered surfaces may
    contain one; anything else is a mutation path that would silently inherit
    no enforcement.
    """
    import re as _re

    root = Path(__file__).resolve().parents[2]
    allowed = {
        root / "scripts" / "onboard_workflow_a_client.py",
        root / "ops" / "activate_telematics_trips_schedule.py",
        # M6: the reconciliation lifecycle surface. Registered in both
        # CREATION_SURFACES and ACTIVATION_SURFACES because it creates the row
        # (always disabled) and enables it through two separate subcommands.
        root / "ops" / "manage_telematics_reconciliation_schedule.py",
    }
    # The allowlist must stay a subset of what the registry itself declares,
    # otherwise this scan could quietly bless a module the policy oracle would
    # refuse at runtime.
    from jobs.api.telematics import schedule_mutation_surfaces as _sms

    declared = {root / relative for relative in _sms.REGISTERED_SURFACES}
    assert allowed == declared, (
        "the scan allowlist and schedule_mutation_surfaces.REGISTERED_SURFACES "
        f"disagree; scan={sorted(str(p) for p in allowed)} "
        f"registry={sorted(str(p) for p in declared)}"
    )
    insert_re = _re.compile(
        r"INSERT\s+INTO\s+workflow_a_control\.client_dataset_schedule",
        _re.IGNORECASE,
    )
    update_re = _re.compile(
        r"UPDATE\s+workflow_a_control\.client_dataset_schedule",
        _re.IGNORECASE,
    )
    offenders = []
    for path in sorted(root.rglob("*.py")):
        parts = set(path.parts)
        if "tests_manual" in parts or ".venv" in parts:
            continue
        if path in allowed:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        # Collapse newlines so a statement split across lines is still seen.
        flat = " ".join(text.split())
        if insert_re.search(flat) or update_re.search(flat):
            offenders.append(str(path.relative_to(root)))
    assert not offenders, (
        "unregistered schedule-mutation surface(s) found: "
        f"{offenders}; register them in "
        "jobs.api.telematics.schedule_mutation_surfaces or remove the statement"
    )


# ---------------------------------------------------------------------------

def test_on_disposable_postgres(dsn: str) -> None:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row, autocommit=False) as conn:
        bootstrap(conn)
        test_dry_run_writes_nothing(conn, dsn)
        test_execute_requires_confirmation(conn, dsn)
        test_activation_changes_exactly_one_field(conn, dsn)
        test_second_activation_reports_already_enabled(conn, dsn)
        test_concurrent_activation_is_safe(conn, dsn)
        test_activation_before_a_successful_recovery_is_refused(conn, dsn)
        test_activation_on_a_bare_baseline_is_refused(conn, dsn)
        test_wrong_watermark_or_fingerprint_is_refused(conn, dsn)
        test_other_refusals(conn, dsn)
        test_dispatcher_sees_the_schedule_only_after_activation(conn, dsn)

        # --- recovery chains of N windows ---
        test_two_window_chain_dry_run_writes_nothing(conn, dsn)
        test_one_two_and_three_window_chains_all_activate(conn, dsn)
        test_activation_verifies_the_expected_window_count(conn, dsn)
        test_activation_refuses_an_incomplete_chain(conn, dsn)
        test_activation_refuses_a_gapped_chain(conn, dsn)
        test_activation_refuses_an_overlapping_chain(conn, dsn)
        test_activation_refuses_a_failed_chain_row(conn, dsn)
        test_activation_refuses_unrelated_recovery_and_run_state(conn, dsn)
        test_activation_refuses_a_final_w_mismatch(conn, dsn)
        test_activation_refuses_any_schedule_history(conn, dsn)

        # --- structured execution proof ---
        test_activation_accepts_genuine_committed_recovery_evidence(conn, dsn)
        test_activation_refuses_a_recovery_without_execution_proof(conn, dsn)
        test_activation_refuses_skipped_recovery_evidence(conn, dsn)
        test_activation_refuses_a_mismatched_execution_proof(conn, dsn)
        test_activation_refuses_a_proof_without_a_valid_platform_run_id(conn, dsn)
        test_every_chain_window_needs_its_own_proof(conn, dsn)
        conn.rollback()


def main() -> None:
    install_network_guard()
    test_module_launches_nothing()
    test_an_alternate_activation_path_cannot_bypass_the_strict_guard()
    test_unregistered_schedule_mutation_surface_fails_deny_by_default()
    test_the_documented_scanning_test_location_is_real()
    test_no_unregistered_module_mutates_schedule_enablement()
    dsn = os.getenv(ENV)
    if not dsn:
        print(f"SKIP: set {ENV} to a disposable PostgreSQL 16 DSN")
        return
    # Destructive: drops schemas and applies migrations. Prove the target
    # is loopback-only before opening a connection.
    require_loopback_dsn_or_exit(dsn, label=ENV)
    test_on_disposable_postgres(dsn)
    print("OK - Telematics schedule activation PostgreSQL checks passed")


if __name__ == "__main__":
    main()
