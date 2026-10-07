#!/usr/bin/env python3
"""C11 cold-start extension — disabled-schedule recovery on disposable PostgreSQL 16.

Proves the three halves of the isolation contract:

* **unchanged**: without ``--allow-disabled-schedule-for-cold-start`` a disabled
  schedule is still refused, and every historical gate behaves exactly as
  before;
* **narrow (first window)**: with the flag the tool *requires* a single disabled
  schedule plus a zero-width cold-start baseline, no history, no prior recovery,
  no platform run and an explicitly approved boundary — and refuses each of
  those individually;
* **narrow (continuation)**: a second or later window of one named chain is
  permitted only when the target's whole execution state is exactly that chain's
  complete, contiguous, successful prefix, with one SUCCESS business run per
  window and nothing unrelated. Failed, active, foreign, ambiguous and
  discontinuous state each refuse individually.

No production database, no provider request and no real subprocess is used. Set
``TELEMATICS_COLD_START_RECOVERY_TEST_DSN`` to a disposable PostgreSQL 16 DSN.
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
from ops import telematics_cold_start_chain as ccsc  # noqa: E402
from ops import recover_telematics_trips_window as rc  # noqa: E402
from ops.tests_manual.telematics_execution_outcome_fixtures import (  # noqa: E402
    committed_outcome,
)
from ops.tests_manual.postgres_dsn_safety import (  # noqa: E402
    require_loopback_dsn_or_exit,
)
from ops.tests_manual.test_telematics_cold_start_audit import (  # noqa: E402
    install_network_guard,
)

ENV = "TELEMATICS_COLD_START_RECOVERY_TEST_DSN"

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
DELAY = 10800

# The chain the cold-start windows of this suite belong to, and the narrower
# recovery horizon used by the multi-window cases: 7 days over a 20-day cold
# range forces a real three-window chain without touching the migration-058
# 31-day ceiling.
CHAIN = "TELEMATICS-COLD-START-ECHO00001-2026-08"
OTHER_CHAIN = "TELEMATICS-COLD-START-ECHO00001-2026-09"
CHAIN_SPAN = 604800
DEFAULT_SPAN = 2678400

# The zero-width baseline: A == W == the approved first managed instant.
# Both are re-derived from the database clock in `bootstrap()` so the recovery
# span stays inside the client's `trips_max_recovery_span_seconds` (31 days)
# whenever the suite runs.
BASELINE = datetime(2026, 7, 1, tzinfo=UTC)
SEEDED = datetime(2026, 7, 1, tzinfo=UTC)
COLD_EVIDENCE = (
    "telematics-cold-start-bootstrap/1:sha256=" + ("ab" * 32)
    + ":approval=TELEMATICS-COLD-START-ECHO00001-1"
    + ":managed-start=2026-07-01T00:00:00Z"
)
HISTORICAL_EVIDENCE = (
    "telematics-coverage-bootstrap/1:sha256=" + ("f3" * 32) + ":approval=T-1"
)

HEAD = "0" * 39 + "1"


def _dict_row():
    from psycopg.rows import dict_row
    return dict_row


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

def bootstrap(conn) -> None:
    global BASELINE, SEEDED, COLD_EVIDENCE

    with conn.cursor(row_factory=_dict_row()) as cur:
        cur.execute("SELECT date_trunc('hour', now()) AS now")
        db_now = dict(cur.fetchone())["now"].astimezone(UTC)
    conn.rollback()
    # Twenty days back keeps [baseline, now - D] comfortably inside the 31-day
    # `trips_max_recovery_span_seconds` cap on any day the suite runs.
    BASELINE = db_now - timedelta(days=20)
    SEEDED = db_now - timedelta(hours=1)
    COLD_EVIDENCE = (
        "telematics-cold-start-bootstrap/1:sha256=" + ("ab" * 32)
        + ":approval=TELEMATICS-COLD-START-ECHO00001-1"
        + ":managed-start=" + rc._iso(BASELINE)
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


def reset(
    conn,
    *,
    mode=MODE,
    schedule_enabled=False,
    competing_schedule=False,
    evidence=None,
    covered_through=None,
    coverage_start=None,
    zero_bounds=False,
    source="bootstrap",
    status="READY",
    history=(),
    platform_runs=(),
    prior_recovery=False,
    max_span=DEFAULT_SPAN,
) -> None:
    # Resolved at call time: `bootstrap()` rebinds the module-level baseline
    # from the database clock, so default arguments must not capture it.
    if evidence is None:
        evidence = COLD_EVIDENCE
    if not zero_bounds:
        covered_through = BASELINE if covered_through is None else covered_through
        coverage_start = BASELINE if coverage_start is None else coverage_start

    with conn.cursor() as cur:
        cur.execute("DELETE FROM workflow_a_control.client_dataset_recovery_run")
        cur.execute("DELETE FROM workflow_a_control.client_schedule_run_history")
        cur.execute("DELETE FROM workflow_a_control.client_dataset_coverage")
        cur.execute("DELETE FROM workflow_a_control.client_dataset_schedule")
        cur.execute("DELETE FROM workflow_a_control.client_account")
        cur.execute("DELETE FROM public.runs")
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
                    %s,3600,%s)
            """,
            (CID, CODE, mode, DELAY, max_span),
        )
        cur.execute(
            """
            INSERT INTO workflow_a_control.client_dataset_schedule
              (schedule_id, client_id, client_code, dataset_name, enabled,
               frequency, run_time, timezone, lookback_days, overwrite_existing)
            VALUES (%s,%s,%s,'trips_sync',%s,'daily','02:00','UTC',1,true)
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
        if status is not None:
            cur.execute(
                """
                INSERT INTO workflow_a_control.client_dataset_coverage
                  (schedule_id, client_id, client_code, dataset_name,
                   coverage_start_ts, covered_through_ts, bootstrap_status,
                   bootstrap_evidence_ref, seeded_at, seeded_by,
                   covered_through_source, last_gap_detected_ts, updated_at)
                VALUES (%s,%s,%s,'trips_sync',%s,%s,%s,%s,%s,'operator',%s,NULL,%s)
                """,
                (SID, CID, CODE, coverage_start, covered_through, status,
                 evidence, SEEDED, source, SEEDED),
            )
        for fire, fire_status in history:
            cur.execute(
                """
                INSERT INTO workflow_a_control.client_schedule_run_history
                  (schedule_id, client_id, client_code, dataset_name,
                   window_start_ts, window_end_ts, scheduled_fire_ts, status)
                VALUES (%s,%s,%s,'trips_sync',
                        (%s::timestamptz - interval '1 day'),%s,%s,%s)
                """,
                (SID, CID, CODE, fire, fire, fire, fire_status),
            )
        for run_id, run_status in platform_runs:
            cur.execute(
                "INSERT INTO public.runs (run_id, status, params)"
                " VALUES (%s,%s,%s::jsonb)",
                (run_id, run_status, json.dumps({"client_code": CODE})),
            )
        if prior_recovery:
            cur.execute(
                """
                INSERT INTO workflow_a_control.client_dataset_recovery_run
                  (client_id, client_code, schedule_id, dataset_name,
                   window_start_ts, window_end_ts,
                   expected_old_covered_through_ts, status, reason,
                   approval_ref, repository_head, pagination_mode,
                   stabilization_delay_seconds, overlap_seconds,
                   max_recovery_span_seconds, initial_coverage_snapshot,
                   initial_coverage_fingerprint, started_at, finished_at,
                   error_classification, error_summary)
                VALUES (%s,%s,%s,'trips_sync',%s,%s,%s,'FAILED','earlier',
                        'T-0',%s,'data_invariants_v1',%s,3600,2678400,
                        '{}'::jsonb,%s,now(),now(),'RECOVERY_BUSINESS_FAILED',
                        'earlier attempt')
                """,
                (CID, CODE, SID, BASELINE, BASELINE + timedelta(hours=1),
                 BASELINE, HEAD, DELAY, "a" * 64),
            )
    conn.commit()


def coverage(conn) -> dict:
    with conn.cursor(row_factory=_dict_row()) as cur:
        cur.execute(
            "SELECT schedule_id::text AS schedule_id,"
            " client_id::text AS client_id, client_code, dataset_name,"
            " coverage_start_ts, covered_through_ts, bootstrap_status,"
            " bootstrap_evidence_ref, covered_through_source, seeded_at,"
            " seeded_by, last_gap_detected_ts, updated_at"
            " FROM workflow_a_control.client_dataset_coverage"
            " WHERE schedule_id=%s",
            (SID,),
        )
        row = dict(cur.fetchone())
    conn.rollback()
    return row


def fingerprint(conn) -> str:
    return cf.coverage_fingerprint(coverage(conn))


def recoveries(conn) -> list:
    with conn.cursor(row_factory=_dict_row()) as cur:
        cur.execute(
            "SELECT * FROM workflow_a_control.client_dataset_recovery_run"
            " ORDER BY created_at"
        )
        rows = [dict(r) for r in cur.fetchall()]
    conn.rollback()
    return rows


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


def window_end(conn) -> datetime:
    """A boundary inside the past and outside the stabilization delay."""
    with conn.cursor(row_factory=_dict_row()) as cur:
        cur.execute("SELECT date_trunc('second', now()) AS now")
        now = dict(cur.fetchone())["now"].astimezone(UTC)
    conn.rollback()
    return now - timedelta(seconds=DELAY + 60)


def args(conn, dsn: str, *, execute=False, cold_start=True, **overrides):
    end = overrides.pop("window_end_value", None) or window_end(conn)
    final = overrides.pop("chain_final_value", None) or end
    base = {
        "client-code": CODE,
        "dataset": "trips_sync",
        "window-start": rc._iso(BASELINE),
        "window-end": rc._iso(end),
        "expected-old-covered-through": rc._iso(BASELINE),
        "reason": "first controlled cold-start recovery",
        "approval-ref": (
            CHAIN + "-W01" if cold_start
            else "TELEMATICS-C11-ECHO00001-COLD-START-1"
        ),
        "expected-environment": "production",
        "expected-platform-uuid": PLATFORM_UUID,
        "dsn": dsn,
    }
    flags = []
    if cold_start:
        base["expected-schedule-id"] = SID
        base["expected-coverage-fingerprint"] = fingerprint(conn)
        base["approved-shifted-cutoff-boundary"] = rc._iso(end)
        base["cold-start-chain-ref"] = CHAIN
        base["approved-final-chain-boundary"] = rc._iso(final)
        flags += [
            "--allow-disabled-schedule-for-cold-start",
            "--confirm-schedule-disabled",
        ]
    base.update({k.replace("_", "-"): v for k, v in overrides.items()})
    argv = []
    for key, value in base.items():
        if value is None:
            continue
        argv += [f"--{key}", str(value)]
    argv += flags
    if execute:
        argv += ["--execute", "--confirm-client-code", CODE]
    return rc.build_parser().parse_args(argv)


class FakeLaunch:
    """Deterministic stand-in for the one business subprocess."""

    def __init__(self, *, returncode=0, raises=None, outcome_builder=None,
                 schedule_id=SID):
        self.returncode = returncode
        self.raises = raises
        # The structured terminal record is now part of what a business
        # subprocess produces; the gate reads it rather than the return code.
        self.outcome_builder = outcome_builder or committed_outcome
        self.schedule_id = schedule_id
        self.calls = []
        self.authorities = []
        self.platform_run_ids = []

    def __call__(self, *, job_params, authority=None):
        self.calls.append(job_params)
        self.authorities.append(authority)
        if self.raises is not None:
            raise self.raises
        now = datetime.now(UTC)
        # A real `ops/runner.py` always writes the run-id file and the real job
        # always binds that id into its terminal record, so the double produces
        # one platform-run identity and reports it on both sides. Coverage-
        # eligible proof is required to carry an exact match; a double that left
        # it None would be asserting behavior the real path never produces.
        platform_run_id = str(uuid.uuid4())
        self.platform_run_ids.append(platform_run_id)
        return {
            "returncode": self.returncode,
            "platform_run_id": platform_run_id,
            "started_at": now,
            "finished_at": now,
            "duration_seconds": 1,
            "stderr_tail": "" if self.returncode == 0 else "PAGINATION_MISMATCH",
            "sanitized_command": "python ops/runner.py <module> <params>",
            "execution_outcome": (
                None if self.returncode != 0
                else self.outcome_builder(
                    job_params, schedule_id=self.schedule_id,
                    platform_run_id=platform_run_id,
                )
            ),
            "execution_outcome_error": None,
        }


def with_mocked_execution(fn):
    def wrapper(conn, dsn, launcher=None):
        original_launch, original_repo = rc.launch_sync, rc.repository_state
        if launcher is not None:
            rc.launch_sync = launcher
        rc.repository_state = lambda *, require_clean: {
            "repository_head": HEAD, "worktree_clean": True,
        }
        try:
            return fn(conn, dsn, launcher)
        finally:
            rc.launch_sync = original_launch
            rc.repository_state = original_repo
    return wrapper


def _expect(conn, dsn, code, **overrides) -> None:
    try:
        rc.run(args(conn, dsn, **overrides))
    except rc.RecoveryRefused as exc:
        assert exc.code == code, f"expected {code}, got {exc.code}"
        return
    raise AssertionError(f"expected refusal {code}")


# ---------------------------------------------------------------------------
# Chain fixtures
# ---------------------------------------------------------------------------

class ChainLaunch(FakeLaunch):
    """A stand-in execution that also leaves the platform run row behind.

    The real business subprocess registers itself in `public.runs` through the
    runner's `LOG_PLATFORM_RUN_ID_FILE` handoff. The chain contract checks that
    correspondence in both directions, so the stand-in has to produce it too.
    """

    def __init__(self, dsn, *, returncode=0, register_run=True,
                 run_status="SUCCESS"):
        super().__init__(returncode=returncode)
        self.dsn = dsn
        self.register_run = register_run
        self.run_status = run_status
        self.run_ids = []

    def __call__(self, *, job_params, authority=None):
        result = super().__call__(job_params=job_params, authority=authority)
        if not self.register_run:
            return result
        import psycopg

        run_id = str(uuid.uuid4())
        with psycopg.connect(self.dsn, autocommit=True) as other:
            other.execute(
                "INSERT INTO public.runs (run_id, status, params)"
                " VALUES (%s,%s,%s::jsonb)",
                (run_id, self.run_status, json.dumps(
                    {"client_id": CID, "client_code": CODE}
                )),
            )
        self.run_ids.append(run_id)
        result["platform_run_id"] = run_id
        # The record the real job writes carries the platform run id it was
        # given, so the stand-in has to rebuild it once that id exists.
        if result.get("execution_outcome") is not None:
            result["execution_outcome"] = self.outcome_builder(
                job_params, schedule_id=self.schedule_id,
                platform_run_id=run_id,
            )
        return result


def current_w(conn) -> datetime:
    return coverage(conn)["covered_through_ts"].astimezone(UTC)


def next_window(conn, *, final: datetime, span=CHAIN_SPAN):
    """The one deterministic next window the tool will accept."""
    return ccsc.plan_recovery_windows(
        start=current_w(conn), final_end=final, max_span_seconds=span,
    )[0]


def chain_args(conn, dsn, *, ordinal, final, span=CHAIN_SPAN, execute=False,
               **overrides):
    start, end = next_window(conn, final=final, span=span)
    return args(
        conn, dsn, execute=execute,
        window_start=rc._iso(start),
        window_end_value=end,
        chain_final_value=final,
        expected_old_covered_through=rc._iso(start),
        approval_ref=ccsc.window_approval_ref(CHAIN, ordinal),
        reason=f"cold-start chain window {ordinal}",
        **overrides,
    )


def seed_chain(conn, specs, *, covered_through, source="manual_recovery",
               coverage_start=None) -> list:
    """Insert an already-executed chain prefix plus its platform runs.

    Building the post-window state directly, rather than executing every window
    for every refusal case, keeps each refusal test about exactly one deviation.
    """
    run_ids = []
    with conn.cursor() as cur:
        for spec in specs:
            run_id = spec.get("run_id")
            if spec.get("register_run", True) and run_id is None:
                run_id = str(uuid.uuid4())
            if spec.get("register_run", True):
                cur.execute(
                    "INSERT INTO public.runs (run_id, status, params)"
                    " VALUES (%s,%s,%s::jsonb)"
                    " ON CONFLICT (run_id) DO NOTHING",
                    (run_id, spec.get("run_status", "SUCCESS"),
                     json.dumps({"client_id": CID, "client_code": CODE})),
                )
            run_ids.append(run_id)
            status = spec.get("status", "SUCCESS")
            terminal = status in ("SUCCESS", "FAILED", "FINALIZATION_CONFLICT")
            approval = spec.get(
                "approval", ccsc.window_approval_ref(CHAIN, spec["ordinal"])
            )
            cur.execute(
                """
                INSERT INTO workflow_a_control.client_dataset_recovery_run
                  (client_id, client_code, schedule_id, dataset_name,
                   window_start_ts, window_end_ts,
                   expected_old_covered_through_ts, status, reason,
                   approval_ref, repository_head, pagination_mode,
                   stabilization_delay_seconds, overlap_seconds,
                   max_recovery_span_seconds, initial_coverage_snapshot,
                   initial_coverage_fingerprint, platform_run_id,
                   created_at, started_at, finished_at,
                   error_classification, error_summary)
                VALUES (%s,%s,%s,'trips_sync',%s,%s,%s,%s,'seeded chain window',
                        %s,%s,'data_invariants_v1',%s,3600,%s,'{}'::jsonb,%s,%s,
                        %s,%s,%s,%s,%s)
                """,
                (
                    spec.get("client_id", CID), CODE,
                    spec.get("schedule_id", SID),
                    spec["start"], spec["end"], spec["start"], status,
                    approval, HEAD, DELAY, CHAIN_SPAN, "a" * 64,
                    None if not spec.get("register_run", True) else run_id,
                    SEEDED + timedelta(seconds=spec["ordinal"]),
                    SEEDED + timedelta(seconds=spec["ordinal"]),
                    (SEEDED + timedelta(seconds=spec["ordinal"]))
                    if terminal else None,
                    None if status in ("SUCCESS", "PLANNED", "RUNNING")
                    else "RECOVERY_BUSINESS_FAILED",
                    None if status in ("SUCCESS", "PLANNED", "RUNNING")
                    else "seeded failure",
                ),
            )
        cur.execute(
            "UPDATE workflow_a_control.client_dataset_coverage"
            "   SET covered_through_ts = %s, covered_through_source = %s,"
            "       coverage_start_ts = COALESCE(%s, coverage_start_ts)"
            " WHERE schedule_id = %s",
            (covered_through, source, coverage_start, SID),
        )
    conn.commit()
    return run_ids


def two_window_prefix(conn, *, final):
    """Reset to a state where window 1 of the chain has already succeeded."""
    reset(conn, max_span=CHAIN_SPAN)
    start, end = next_window(conn, final=final)
    run_ids = seed_chain(
        conn,
        [{"ordinal": 1, "start": start, "end": end}],
        covered_through=end,
    )
    return end, run_ids


# ---------------------------------------------------------------------------
# 1. The historical contract is unchanged
# ---------------------------------------------------------------------------

@with_mocked_execution
def test_normal_recovery_still_refuses_a_disabled_schedule(conn, dsn, _launcher) -> None:
    reset(conn, schedule_enabled=False, evidence=HISTORICAL_EVIDENCE)
    _expect(conn, dsn, "RECOVERY_REFUSED_TARGET", cold_start=False)


@with_mocked_execution
def test_cold_start_options_require_the_explicit_flag(conn, dsn, _launcher) -> None:
    reset(conn, schedule_enabled=False)
    end = window_end(conn)
    argv = [
        "--client-code", CODE, "--dataset", "trips_sync",
        "--window-start", rc._iso(BASELINE), "--window-end", rc._iso(end),
        "--expected-old-covered-through", rc._iso(BASELINE),
        "--reason", "half typed", "--approval-ref", "T-1",
        "--expected-environment", "production",
        "--expected-platform-uuid", PLATFORM_UUID, "--dsn", dsn,
        "--expected-schedule-id", SID,
    ]
    try:
        rc.run(rc.build_parser().parse_args(argv))
    except rc.RecoveryRefused as exc:
        assert exc.code == "RECOVERY_REFUSED_PARAMETER"
        assert exc.exit_code == rc.EXIT_INVALID_PARAMETERS
    else:
        raise AssertionError("a cold-start option was accepted without the flag")


@with_mocked_execution
def test_normal_enabled_schedule_recovery_is_unaffected(conn, dsn, _launcher) -> None:
    """The historical path still plans a recovery for an enabled schedule."""
    reset(
        conn, schedule_enabled=True, evidence=HISTORICAL_EVIDENCE,
        history=(("2026-07-02T02:00:00Z", "FAILED"),),
    )
    exit_code, plan = rc.run(args(conn, dsn, cold_start=False))
    assert exit_code == rc.EXIT_OK
    assert plan["mode"] == "DRY_RUN"
    assert plan["cold_start_path"] is False
    assert plan["cold_start_evidence"] == {}
    assert plan["schedule_enabled"] is True
    assert plan["would_claim_recovery"] is True
    assert plan["database_writes_performed"] == 0
    assert plan["business_subprocesses_launched"] == 0


# ---------------------------------------------------------------------------
# 2. The cold-start path
# ---------------------------------------------------------------------------

@with_mocked_execution
def test_cold_start_dry_run_passes_for_the_valid_state(conn, dsn, _launcher) -> None:
    reset(conn)
    before = snapshot(conn)
    exit_code, plan = rc.run(args(conn, dsn))
    assert exit_code == rc.EXIT_OK
    assert plan["mode"] == "DRY_RUN"
    assert plan["cold_start_path"] is True
    assert plan["schedule_enabled"] is False
    assert plan["schedule_enabled_changes"] == 0
    assert plan["cold_start_evidence"]["cold_start_history_rows"] == 0
    assert plan["cold_start_evidence"]["cold_start_recovery_rows"] == 0
    assert plan["cold_start_evidence"]["cold_start_platform_runs"] == 0
    assert plan["cold_start_evidence"]["cold_start_baseline_instant"] == \
        rc._iso(BASELINE)
    assert plan["database_writes_performed"] == 0
    assert plan["provider_requests"] == 0
    assert plan["business_subprocesses_launched"] == 0
    assert plan["schedule_history_rows_created"] == 0
    assert plan["automatic_retries"] == 0
    conn.rollback()
    assert snapshot(conn) == before, "a dry run must write nothing"


@with_mocked_execution
def test_cold_start_refuses_an_enabled_schedule(conn, dsn, _launcher) -> None:
    reset(conn, schedule_enabled=True)
    _expect(conn, dsn, "RECOVERY_REFUSED_COLD_START_SCHEDULE")


@with_mocked_execution
def test_cold_start_refuses_a_competing_schedule(conn, dsn, _launcher) -> None:
    reset(conn, competing_schedule=True)
    _expect(conn, dsn, "RECOVERY_REFUSED_COLD_START_SCHEDULE")


@with_mocked_execution
def test_cold_start_refuses_a_wrong_schedule_id(conn, dsn, _launcher) -> None:
    reset(conn)
    _expect(
        conn, dsn, "RECOVERY_REFUSED_COLD_START_SCHEDULE",
        expected_schedule_id="db8055e0-e030-4d5a-816b-ec4dc338d698",
    )


@with_mocked_execution
def test_cold_start_refuses_a_wrong_fingerprint(conn, dsn, _launcher) -> None:
    reset(conn)
    _expect(
        conn, dsn, "RECOVERY_REFUSED_COLD_START_COVERAGE",
        expected_coverage_fingerprint="0" * 64,
    )


@with_mocked_execution
def test_cold_start_refuses_historical_bootstrap_evidence(conn, dsn, _launcher) -> None:
    reset(conn, evidence=HISTORICAL_EVIDENCE)
    _expect(conn, dsn, "RECOVERY_REFUSED_COLD_START_COVERAGE")


@with_mocked_execution
def test_cold_start_refuses_a_positive_width_baseline(conn, dsn, _launcher) -> None:
    reset(conn, coverage_start=BASELINE - timedelta(days=1))
    _expect(conn, dsn, "RECOVERY_REFUSED_COLD_START_COVERAGE")


@with_mocked_execution
def test_cold_start_refuses_an_already_advanced_watermark(conn, dsn, _launcher) -> None:
    reset(conn, source="manual_recovery")
    _expect(conn, dsn, "RECOVERY_REFUSED_COLD_START_COVERAGE")


@with_mocked_execution
def test_cold_start_refuses_a_window_not_starting_at_w(conn, dsn, _launcher) -> None:
    reset(conn)
    # The shared watermark gate fires first: the window must anchor at W.
    _expect(
        conn, dsn, "RECOVERY_REFUSED_WINDOW",
        window_start=rc._iso(BASELINE + timedelta(seconds=1)),
    )


@with_mocked_execution
def test_cold_start_refuses_an_unapproved_boundary(conn, dsn, _launcher) -> None:
    reset(conn)
    end = window_end(conn)
    _expect(
        conn, dsn, "RECOVERY_REFUSED_PARAMETER",
        window_end_value=end,
        approved_shifted_cutoff_boundary=rc._iso(end - timedelta(seconds=60)),
    )


@with_mocked_execution
def test_cold_start_refuses_existing_history(conn, dsn, _launcher) -> None:
    reset(conn, history=(("2026-07-02T02:00:00Z", "SUCCESS"),))
    _expect(conn, dsn, "RECOVERY_REFUSED_COLD_START_HISTORY")


@with_mocked_execution
def test_cold_start_refuses_existing_platform_runs(conn, dsn, _launcher) -> None:
    reset(conn, platform_runs=(("cf4c4732-fd3b-4f8a-85b6-0871950a2f22", "SUCCESS"),))
    _expect(conn, dsn, "RECOVERY_REFUSED_COLD_START_RUNS")


@with_mocked_execution
def test_cold_start_refuses_a_prior_recovery(conn, dsn, _launcher) -> None:
    reset(conn, prior_recovery=True)
    _expect(conn, dsn, "RECOVERY_REFUSED_COLD_START_RECOVERY")


@with_mocked_execution
def test_cold_start_refuses_strict_meta(conn, dsn, _launcher) -> None:
    reset(conn, mode="strict_meta")
    _expect(conn, dsn, "RECOVERY_REFUSED_MODE")


@with_mocked_execution
def test_cold_start_refuses_a_non_ready_baseline(conn, dsn, _launcher) -> None:
    reset(conn, status="UNINITIALIZED", zero_bounds=True)
    _expect(conn, dsn, "RECOVERY_REFUSED_COVERAGE")


@with_mocked_execution
def test_cold_start_execute_success(conn, dsn, launcher) -> None:
    """The whole cold-start recovery, with a mocked business execution."""
    reset(conn)
    end = window_end(conn)
    before_schedule = snapshot(conn)["client_dataset_schedule"]
    exit_code, plan = rc.run(
        args(conn, dsn, execute=True, window_end_value=end)
    )
    assert exit_code == rc.EXIT_OK, plan
    assert plan["recovery_status"] == "SUCCESS"
    assert plan["cold_start_path"] is True
    assert plan["coverage_advanced"] is True
    assert plan["business_subprocesses_launched"] == 1
    assert len(launcher.calls) == 1, "exactly one business execution"
    assert plan["automatic_retries"] == 0

    after = coverage(conn)
    assert after["coverage_start_ts"].astimezone(UTC) == BASELINE, \
        "A is never moved by a recovery"
    assert after["covered_through_ts"].astimezone(UTC) == end
    assert after["covered_through_source"] == "manual_recovery"
    assert after["bootstrap_status"] == "READY"
    assert after["last_gap_detected_ts"] is None
    # The baseline and the recovered interval join with no hole: the recovery
    # started exactly at the old W.
    assert plan["window_start_ts"] == rc._iso(BASELINE)

    runs = recoveries(conn)
    assert len(runs) == 1 and runs[0]["status"] == "SUCCESS"
    assert runs[0]["window_start_ts"].astimezone(UTC) == BASELINE
    assert runs[0]["window_end_ts"].astimezone(UTC) == end

    # The schedule is untouched and still disabled.
    assert snapshot(conn)["client_dataset_schedule"] == before_schedule
    with conn.cursor(row_factory=_dict_row()) as cur:
        cur.execute(
            "SELECT enabled FROM workflow_a_control.client_dataset_schedule"
            " WHERE schedule_id=%s", (SID,),
        )
        assert dict(cur.fetchone())["enabled"] is False
        cur.execute(
            "SELECT count(*) AS n FROM"
            " workflow_a_control.client_schedule_run_history"
        )
        assert dict(cur.fetchone())["n"] == 0, "no synthetic history was created"
    conn.rollback()


@with_mocked_execution
def test_finalizer_advances_monotonically_from_the_baseline(conn, dsn, _launcher) -> None:
    """A second advancement from the recovered W is still strictly monotone."""
    row = coverage(conn)
    snap = cf.CoverageClaimSnapshot.from_row(row)
    current_w = row["covered_through_ts"].astimezone(UTC)
    with conn.cursor(row_factory=_dict_row()) as cur:
        cf.lock_coverage_row_for_update(
            cur, client_id=CID, dataset_name="trips_sync",
        )
        result = cf.advance_covered_through_cas(
            cur,
            snapshot=snap,
            candidate_covered_through_ts=current_w + timedelta(hours=1),
            source=cf.COVERAGE_SOURCE_SCHEDULED_RUN,
            mutation_ts=datetime.now(UTC).replace(microsecond=0),
        )
    conn.rollback()
    assert result.moved is True
    assert result.coverage_start_ts_unchanged is True


@with_mocked_execution
def test_failed_business_leaves_coverage_and_schedule_untouched(
    conn, dsn, launcher
) -> None:
    reset(conn)
    before_coverage = coverage(conn)
    before_schedule = snapshot(conn)["client_dataset_schedule"]
    exit_code, plan = rc.run(args(conn, dsn, execute=True))
    assert exit_code == rc.EXIT_BUSINESS_FAILED
    assert plan["recovery_status"] == "FAILED"
    assert plan["error_classification"] == rc.RECOVERY_BUSINESS_FAILED
    assert plan["coverage_unchanged"] is True
    assert plan["coverage_advanced"] is False
    assert len(launcher.calls) == 1, "no automatic retry"

    assert coverage(conn) == before_coverage
    assert snapshot(conn)["client_dataset_schedule"] == before_schedule
    runs = recoveries(conn)
    assert len(runs) == 1 and runs[0]["status"] == "FAILED"
    assert runs[0]["error_classification"] == rc.RECOVERY_BUSINESS_FAILED


@with_mocked_execution
def test_disabled_schedule_is_never_dispatcher_claimable(conn, dsn, _launcher) -> None:
    """The dispatcher's own selection predicate never sees a disabled schedule."""
    reset(conn)
    with conn.cursor(row_factory=_dict_row()) as cur:
        cur.execute(
            """
            SELECT count(*) AS n
              FROM workflow_a_control.client_dataset_schedule AS cds
              JOIN workflow_a_control.client_account AS ca
                ON ca.client_id = cds.client_id
             WHERE cds.enabled = true AND ca.enabled = true
            """
        )
        assert dict(cur.fetchone())["n"] == 0
    conn.rollback()
    # And the dispatcher loader itself returns nothing for this state.
    from jobs.api.telematics import dispatcher

    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row, autocommit=True) as probe:
        assert dispatcher._load_enabled_schedules(probe) == []


# ---------------------------------------------------------------------------
# 3. Multi-window chains
# ---------------------------------------------------------------------------

@with_mocked_execution
def test_first_window_still_requires_zero_prior_state(conn, dsn, _launcher) -> None:
    """The chain split does not soften the first window by one condition."""
    final = window_end(conn)
    for kwargs, code in (
        ({"history": (("2026-07-02T02:00:00Z", "SUCCESS"),)},
         "RECOVERY_REFUSED_COLD_START_HISTORY"),
        ({"platform_runs": (("cf4c4732-fd3b-4f8a-85b6-0871950a2f22", "SUCCESS"),)},
         "RECOVERY_REFUSED_COLD_START_RUNS"),
        ({"prior_recovery": True}, "RECOVERY_REFUSED_COLD_START_RECOVERY"),
        ({"source": "manual_recovery"}, "RECOVERY_REFUSED_COLD_START_COVERAGE"),
    ):
        reset(conn, max_span=CHAIN_SPAN, **kwargs)
        _expect(
            conn, dsn, code,
            **{k: v for k, v in chain_kwargs(conn, final=final).items()},
        )


def chain_kwargs(conn, *, final, ordinal=1):
    start, end = next_window(conn, final=final)
    return {
        "window_start": rc._iso(start),
        "window_end_value": end,
        "chain_final_value": final,
        "expected_old_covered_through": rc._iso(start),
        "approval_ref": ccsc.window_approval_ref(CHAIN, ordinal),
    }


@with_mocked_execution
def test_first_window_must_be_w01(conn, dsn, _launcher) -> None:
    final = window_end(conn)
    reset(conn, max_span=CHAIN_SPAN)
    _expect(
        conn, dsn, "RECOVERY_REFUSED_COLD_START_CHAIN",
        **chain_kwargs(conn, final=final, ordinal=2),
    )


@with_mocked_execution
def test_approval_ref_must_be_a_window_of_the_declared_chain(
    conn, dsn, _launcher
) -> None:
    final = window_end(conn)
    reset(conn, max_span=CHAIN_SPAN)
    kwargs = chain_kwargs(conn, final=final)
    # A bare approval reference is no longer a chain window.
    _expect(
        conn, dsn, "RECOVERY_REFUSED_PARAMETER",
        **dict(kwargs, approval_ref="TELEMATICS-C11-ECHO00001-COLD-START-1"),
    )
    # A window of a *different* chain.
    _expect(
        conn, dsn, "RECOVERY_REFUSED_PARAMETER",
        **dict(kwargs, approval_ref=ccsc.window_approval_ref(OTHER_CHAIN, 1)),
    )
    # A chain reference that is itself shaped like a window.
    _expect(
        conn, dsn, "RECOVERY_REFUSED_PARAMETER",
        **dict(kwargs, cold_start_chain_ref="X-W01", approval_ref="X-W01-W01"),
    )


@with_mocked_execution
def test_three_windows_execute_sequentially(conn, dsn, launcher) -> None:
    """The whole chain: three separately approved windows, one execution each."""
    final = window_end(conn)
    reset(conn, max_span=CHAIN_SPAN)
    assert current_w(conn) == BASELINE

    planned = ccsc.plan_recovery_windows(
        start=BASELINE, final_end=final, max_span_seconds=CHAIN_SPAN,
    )
    assert len(planned) == 3, planned

    before_schedule = snapshot(conn)["client_dataset_schedule"]
    for ordinal, (expected_start, expected_end) in enumerate(planned, start=1):
        # A dry run first — and it writes nothing.
        before = snapshot(conn)
        exit_code, plan = rc.run(
            chain_args(conn, dsn, ordinal=ordinal, final=final)
        )
        assert exit_code == rc.EXIT_OK, plan
        assert plan["mode"] == "DRY_RUN"
        assert plan["cold_start_chain_ref"] == CHAIN
        assert plan["cold_start_window_ordinal"] == ordinal
        assert plan["window_start_ts"] == rc._iso(expected_start)
        assert plan["window_end_ts"] == rc._iso(expected_end)
        assert plan["database_writes_performed"] == 0
        assert plan["provider_requests"] == 0
        assert plan["business_subprocesses_launched"] == 0
        evidence = plan["cold_start_evidence"]
        assert evidence["cold_start_chain_successful_window_count"] == ordinal - 1
        assert evidence["cold_start_chain_total_windows"] == 3
        assert evidence["cold_start_is_final_window"] is (ordinal == 3)
        assert evidence["cold_start_baseline_instant"] == rc._iso(BASELINE)
        conn.rollback()
        assert snapshot(conn) == before, "a dry run must write nothing"

        # Then the one approved execution.
        calls_before = len(launcher.calls)
        exit_code, plan = rc.run(
            chain_args(conn, dsn, ordinal=ordinal, final=final, execute=True)
        )
        assert exit_code == rc.EXIT_OK, plan
        assert plan["recovery_status"] == "SUCCESS"
        assert plan["coverage_advanced"] is True
        assert plan["cold_start_window_ordinal"] == ordinal
        assert plan["business_subprocesses_launched"] == 1
        assert len(launcher.calls) - calls_before == 1, "one execution per window"
        assert plan["automatic_retries"] == 0
        assert current_w(conn) == expected_end
        row = coverage(conn)
        assert row["coverage_start_ts"].astimezone(UTC) == BASELINE, \
            "A is never moved by any window of the chain"
        assert row["covered_through_source"] == "manual_recovery"

    assert current_w(conn) == final
    runs = recoveries(conn)
    assert len(runs) == 3
    assert [r["status"] for r in runs] == ["SUCCESS"] * 3
    assert [r["approval_ref"] for r in runs] == [
        ccsc.window_approval_ref(CHAIN, n) for n in (1, 2, 3)
    ]
    # Contiguity, with no one-second hole anywhere in the chain.
    assert runs[0]["window_start_ts"].astimezone(UTC) == BASELINE
    for previous, following in zip(runs, runs[1:]):
        assert previous["window_end_ts"] == following["window_start_ts"]
    assert runs[-1]["window_end_ts"].astimezone(UTC) == final

    # The schedule never moved and no fire was ever synthesized.
    assert snapshot(conn)["client_dataset_schedule"] == before_schedule
    with conn.cursor(row_factory=_dict_row()) as cur:
        cur.execute(
            "SELECT count(*) AS n FROM"
            " workflow_a_control.client_schedule_run_history"
        )
        assert dict(cur.fetchone())["n"] == 0
    conn.rollback()


@with_mocked_execution
def test_second_window_dry_run_is_permitted_after_the_first_succeeds(
    conn, dsn, _launcher
) -> None:
    final = window_end(conn)
    w1_end, _runs = two_window_prefix(conn, final=final)
    exit_code, plan = rc.run(chain_args(conn, dsn, ordinal=2, final=final))
    assert exit_code == rc.EXIT_OK, plan
    assert plan["cold_start_window_ordinal"] == 2
    assert plan["window_start_ts"] == rc._iso(w1_end)
    assert plan["cold_start_evidence"][
        "cold_start_chain_successful_window_count"
    ] == 1


@with_mocked_execution
def test_wrong_chain_ref_is_refused(conn, dsn, _launcher) -> None:
    final = window_end(conn)
    two_window_prefix(conn, final=final)
    # Declaring another chain makes window 1 an unrelated recovery row.
    _expect(
        conn, dsn, "RECOVERY_REFUSED_COLD_START_RECOVERY",
        **dict(
            chain_kwargs(conn, final=final, ordinal=1),
            cold_start_chain_ref=OTHER_CHAIN,
            approval_ref=ccsc.window_approval_ref(OTHER_CHAIN, 1),
        ),
    )


@with_mocked_execution
def test_wrong_window_ordinal_is_refused(conn, dsn, _launcher) -> None:
    final = window_end(conn)
    two_window_prefix(conn, final=final)
    for ordinal in (1, 3):
        try:
            rc.run(chain_args(conn, dsn, ordinal=ordinal, final=final))
        except rc.RecoveryRefused as exc:
            # W01 collides with the already-approved window; W03 skips W02.
            assert exc.code in (
                "RECOVERY_REFUSED_DUPLICATE",
                "RECOVERY_REFUSED_COLD_START_CHAIN",
            ), exc.code
        else:
            raise AssertionError(f"ordinal {ordinal} was accepted")


@with_mocked_execution
def test_next_start_must_equal_the_current_watermark(conn, dsn, _launcher) -> None:
    final = window_end(conn)
    w1_end, _runs = two_window_prefix(conn, final=final)
    kwargs = chain_kwargs(conn, final=final, ordinal=2)
    drifted = rc._iso(w1_end + timedelta(seconds=1))
    _expect(
        conn, dsn, "RECOVERY_REFUSED_WATERMARK",
        **dict(kwargs, window_start=drifted,
               expected_old_covered_through=drifted),
    )


@with_mocked_execution
def test_next_end_beyond_r_is_refused(conn, dsn, _launcher) -> None:
    final = window_end(conn)
    two_window_prefix(conn, final=final)
    kwargs = chain_kwargs(conn, final=final, ordinal=2)
    over = kwargs["window_end_value"] + timedelta(seconds=1)
    # The shared span gate fires first and is the stronger statement: the
    # interval simply does not fit `trips_max_recovery_span_seconds`.
    _expect(
        conn, dsn, "RECOVERY_REFUSED_WINDOW",
        **dict(kwargs, window_end_value=over),
    )
    # And a boundary short of the deterministic split is refused too: the
    # planner, not the operator, decides where a non-final window ends.
    _expect(
        conn, dsn, "RECOVERY_REFUSED_COLD_START_BOUNDARY",
        **dict(kwargs, window_end_value=kwargs["window_end_value"]
               - timedelta(seconds=1)),
    )


@with_mocked_execution
def test_stale_fingerprint_is_refused_mid_chain(conn, dsn, _launcher) -> None:
    final = window_end(conn)
    two_window_prefix(conn, final=final)
    _expect(
        conn, dsn, "RECOVERY_REFUSED_COLD_START_COVERAGE",
        **dict(chain_kwargs(conn, final=final, ordinal=2),
               expected_coverage_fingerprint="0" * 64),
    )


@with_mocked_execution
def test_prior_failed_chain_row_blocks_continuation(conn, dsn, _launcher) -> None:
    """A failed window is preserved and is never skipped by asking for the next."""
    final = window_end(conn)
    reset(conn, max_span=CHAIN_SPAN)
    first = next_window(conn, final=final)
    second_end = ccsc.plan_recovery_windows(
        start=first[1], final_end=final, max_span_seconds=CHAIN_SPAN,
    )[0][1]
    seed_chain(
        conn,
        [
            {"ordinal": 1, "start": first[0], "end": first[1]},
            {"ordinal": 2, "start": first[1], "end": second_end,
             "status": "FAILED"},
        ],
        covered_through=first[1],
    )
    _expect(
        conn, dsn, "RECOVERY_REFUSED_COLD_START_CHAIN",
        **chain_kwargs(conn, final=final, ordinal=3),
    )


@with_mocked_execution
def test_prior_active_chain_row_blocks_continuation(conn, dsn, _launcher) -> None:
    final = window_end(conn)
    reset(conn, max_span=CHAIN_SPAN)
    first = next_window(conn, final=final)
    second_end = ccsc.plan_recovery_windows(
        start=first[1], final_end=final, max_span_seconds=CHAIN_SPAN,
    )[0][1]
    seed_chain(
        conn,
        [
            {"ordinal": 1, "start": first[0], "end": first[1]},
            {"ordinal": 2, "start": first[1], "end": second_end,
             "status": "RUNNING", "register_run": False},
        ],
        covered_through=first[1],
    )
    _expect(
        conn, dsn, "RECOVERY_REFUSED_CONCURRENCY",
        **chain_kwargs(conn, final=final, ordinal=3),
    )


@with_mocked_execution
def test_unrelated_recovery_row_blocks_continuation(conn, dsn, _launcher) -> None:
    final = window_end(conn)
    w1_end, _runs = two_window_prefix(conn, final=final)
    seed_chain(
        conn,
        [{"ordinal": 9, "start": BASELINE, "end": w1_end,
          "approval": "TELEMATICS-C11-ECHO00001-UNRELATED",
          "register_run": False}],
        covered_through=w1_end,
    )
    _expect(
        conn, dsn, "RECOVERY_REFUSED_COLD_START_RECOVERY",
        **chain_kwargs(conn, final=final, ordinal=2),
    )


@with_mocked_execution
def test_unrelated_platform_run_blocks_continuation(conn, dsn, _launcher) -> None:
    final = window_end(conn)
    two_window_prefix(conn, final=final)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO public.runs (run_id, status, params)"
            " VALUES (%s,'SUCCESS',%s::jsonb)",
            (str(uuid.uuid4()), json.dumps({"client_code": CODE})),
        )
    conn.commit()
    _expect(
        conn, dsn, "RECOVERY_REFUSED_COLD_START_RUNS",
        **chain_kwargs(conn, final=final, ordinal=2),
    )


@with_mocked_execution
def test_missing_business_run_blocks_continuation(conn, dsn, _launcher) -> None:
    final = window_end(conn)
    reset(conn, max_span=CHAIN_SPAN)
    first = next_window(conn, final=final)
    seed_chain(
        conn,
        [{"ordinal": 1, "start": first[0], "end": first[1],
          "register_run": False}],
        covered_through=first[1],
    )
    _expect(
        conn, dsn, "RECOVERY_REFUSED_COLD_START_RUNS",
        **chain_kwargs(conn, final=final, ordinal=2),
    )


@with_mocked_execution
def test_duplicate_business_run_association_blocks_continuation(
    conn, dsn, _launcher
) -> None:
    final = window_end(conn)
    reset(conn, max_span=CHAIN_SPAN)
    first = next_window(conn, final=final)
    second_end = ccsc.plan_recovery_windows(
        start=first[1], final_end=final, max_span_seconds=CHAIN_SPAN,
    )[0][1]
    shared = str(uuid.uuid4())
    seed_chain(
        conn,
        [
            {"ordinal": 1, "start": first[0], "end": first[1],
             "run_id": shared},
            {"ordinal": 2, "start": first[1], "end": second_end,
             "run_id": shared},
        ],
        covered_through=second_end,
    )
    _expect(
        conn, dsn, "RECOVERY_REFUSED_COLD_START_RUNS",
        **chain_kwargs(conn, final=final, ordinal=3),
    )


@with_mocked_execution
def test_non_success_business_run_blocks_continuation(conn, dsn, _launcher) -> None:
    final = window_end(conn)
    reset(conn, max_span=CHAIN_SPAN)
    first = next_window(conn, final=final)
    seed_chain(
        conn,
        [{"ordinal": 1, "start": first[0], "end": first[1],
          "run_status": "FAILED"}],
        covered_through=first[1],
    )
    _expect(
        conn, dsn, "RECOVERY_REFUSED_COLD_START_RUNS",
        **chain_kwargs(conn, final=final, ordinal=2),
    )


@with_mocked_execution
def test_schedule_enabled_between_windows_is_refused(conn, dsn, _launcher) -> None:
    final = window_end(conn)
    two_window_prefix(conn, final=final)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE workflow_a_control.client_dataset_schedule"
            "   SET enabled = true WHERE schedule_id = %s", (SID,),
        )
    conn.commit()
    _expect(
        conn, dsn, "RECOVERY_REFUSED_COLD_START_SCHEDULE",
        **chain_kwargs(conn, final=final, ordinal=2),
    )


@with_mocked_execution
def test_schedule_history_between_windows_is_refused(conn, dsn, _launcher) -> None:
    final = window_end(conn)
    w1_end, _runs = two_window_prefix(conn, final=final)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO workflow_a_control.client_schedule_run_history
              (schedule_id, client_id, client_code, dataset_name,
               window_start_ts, window_end_ts, scheduled_fire_ts, status)
            VALUES (%s,%s,%s,'trips_sync',%s,%s,%s,'SUCCESS')
            """,
            (SID, CID, CODE, BASELINE, w1_end, w1_end),
        )
    conn.commit()
    _expect(
        conn, dsn, "RECOVERY_REFUSED_COLD_START_HISTORY",
        **chain_kwargs(conn, final=final, ordinal=2),
    )


@with_mocked_execution
def test_coverage_source_or_a_drift_is_refused(conn, dsn, _launcher) -> None:
    final = window_end(conn)

    # `covered_through_source` reverted to a bare baseline while chain rows exist.
    w1_end, _runs = two_window_prefix(conn, final=final)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE workflow_a_control.client_dataset_coverage"
            "   SET covered_through_source = 'bootstrap' WHERE schedule_id = %s",
            (SID,),
        )
    conn.commit()
    _expect(
        conn, dsn, "RECOVERY_REFUSED_COLD_START_COVERAGE",
        **chain_kwargs(conn, final=final, ordinal=2),
    )

    # `A` moved away from the instant window 1 anchored at.
    two_window_prefix(conn, final=final)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE workflow_a_control.client_dataset_coverage"
            "   SET coverage_start_ts = coverage_start_ts - interval '1 second'"
            " WHERE schedule_id = %s",
            (SID,),
        )
    conn.commit()
    _expect(
        conn, dsn, "RECOVERY_REFUSED_COLD_START_CHAIN",
        **chain_kwargs(conn, final=final, ordinal=2),
    )


@with_mocked_execution
def test_failed_second_window_preserves_the_first(conn, dsn, launcher) -> None:
    """Failure semantics mid-chain: prior evidence and W both survive intact."""
    final = window_end(conn)
    w1_end, _runs = two_window_prefix(conn, final=final)
    before_coverage = coverage(conn)
    exit_code, plan = rc.run(
        chain_args(conn, dsn, ordinal=2, final=final, execute=True)
    )
    assert exit_code == rc.EXIT_BUSINESS_FAILED
    assert plan["recovery_status"] == "FAILED"
    assert plan["coverage_unchanged"] is True
    assert plan["coverage_advanced"] is False
    assert len(launcher.calls) == 1, "no automatic retry"

    assert coverage(conn) == before_coverage
    assert current_w(conn) == w1_end, "W stays at the last finalized window"
    runs = recoveries(conn)
    assert [r["status"] for r in runs] == ["SUCCESS", "FAILED"]
    assert runs[0]["approval_ref"] == ccsc.window_approval_ref(CHAIN, 1)
    with conn.cursor(row_factory=_dict_row()) as cur:
        cur.execute(
            "SELECT enabled FROM workflow_a_control.client_dataset_schedule"
            " WHERE schedule_id=%s", (SID,),
        )
        assert dict(cur.fetchone())["enabled"] is False
    conn.rollback()

    # And the chain cannot simply be continued past the failure.
    _expect(
        conn, dsn, "RECOVERY_REFUSED_COLD_START_CHAIN",
        **chain_kwargs(conn, final=final, ordinal=3),
    )


# ---------------------------------------------------------------------------

def test_on_disposable_postgres(dsn: str) -> None:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row, autocommit=False) as conn:
        bootstrap(conn)
        test_normal_recovery_still_refuses_a_disabled_schedule(conn, dsn)
        test_cold_start_options_require_the_explicit_flag(conn, dsn)
        test_normal_enabled_schedule_recovery_is_unaffected(conn, dsn)
        test_cold_start_dry_run_passes_for_the_valid_state(conn, dsn)
        test_cold_start_refuses_an_enabled_schedule(conn, dsn)
        test_cold_start_refuses_a_competing_schedule(conn, dsn)
        test_cold_start_refuses_a_wrong_schedule_id(conn, dsn)
        test_cold_start_refuses_a_wrong_fingerprint(conn, dsn)
        test_cold_start_refuses_historical_bootstrap_evidence(conn, dsn)
        test_cold_start_refuses_a_positive_width_baseline(conn, dsn)
        test_cold_start_refuses_an_already_advanced_watermark(conn, dsn)
        test_cold_start_refuses_a_window_not_starting_at_w(conn, dsn)
        test_cold_start_refuses_an_unapproved_boundary(conn, dsn)
        test_cold_start_refuses_existing_history(conn, dsn)
        test_cold_start_refuses_existing_platform_runs(conn, dsn)
        test_cold_start_refuses_a_prior_recovery(conn, dsn)
        test_cold_start_refuses_strict_meta(conn, dsn)
        test_cold_start_refuses_a_non_ready_baseline(conn, dsn)
        test_cold_start_execute_success(conn, dsn, FakeLaunch(returncode=0))
        test_finalizer_advances_monotonically_from_the_baseline(conn, dsn)
        test_failed_business_leaves_coverage_and_schedule_untouched(
            conn, dsn, FakeLaunch(returncode=1),
        )
        test_disabled_schedule_is_never_dispatcher_claimable(conn, dsn)

        # --- multi-window chains ---
        test_first_window_still_requires_zero_prior_state(conn, dsn)
        test_first_window_must_be_w01(conn, dsn)
        test_approval_ref_must_be_a_window_of_the_declared_chain(conn, dsn)
        test_three_windows_execute_sequentially(
            conn, dsn, ChainLaunch(dsn, returncode=0),
        )
        test_second_window_dry_run_is_permitted_after_the_first_succeeds(conn, dsn)
        test_wrong_chain_ref_is_refused(conn, dsn)
        test_wrong_window_ordinal_is_refused(conn, dsn)
        test_next_start_must_equal_the_current_watermark(conn, dsn)
        test_next_end_beyond_r_is_refused(conn, dsn)
        test_stale_fingerprint_is_refused_mid_chain(conn, dsn)
        test_prior_failed_chain_row_blocks_continuation(conn, dsn)
        test_prior_active_chain_row_blocks_continuation(conn, dsn)
        test_unrelated_recovery_row_blocks_continuation(conn, dsn)
        test_unrelated_platform_run_blocks_continuation(conn, dsn)
        test_missing_business_run_blocks_continuation(conn, dsn)
        test_duplicate_business_run_association_blocks_continuation(conn, dsn)
        test_non_success_business_run_blocks_continuation(conn, dsn)
        test_schedule_enabled_between_windows_is_refused(conn, dsn)
        test_schedule_history_between_windows_is_refused(conn, dsn)
        test_coverage_source_or_a_drift_is_refused(conn, dsn)
        test_failed_second_window_preserves_the_first(
            conn, dsn, ChainLaunch(dsn, returncode=1, register_run=False),
        )
        conn.rollback()


def main() -> None:
    install_network_guard()
    dsn = os.getenv(ENV)
    if not dsn:
        print(f"SKIP: set {ENV} to a disposable PostgreSQL 16 DSN")
        return
    # Destructive: drops schemas and applies migrations. Prove the target
    # is loopback-only before opening a connection.
    require_loopback_dsn_or_exit(dsn, label=ENV)
    test_on_disposable_postgres(dsn)
    print("OK - Telematics cold-start recovery PostgreSQL checks passed")


if __name__ == "__main__":
    main()
