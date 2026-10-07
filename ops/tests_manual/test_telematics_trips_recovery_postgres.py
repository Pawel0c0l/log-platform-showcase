#!/usr/bin/env python3
"""C11 — manual compatibility recovery on disposable PostgreSQL 16.

Migration `058`, every refusal gate, the claim state machine, and the success,
failure and finalization-conflict paths with a mocked business execution.

No production database, no provider request and no real subprocess is used.
Set `TELEMATICS_C11_RECOVERY_TEST_DSN` to a disposable PostgreSQL 16 DSN.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from jobs.api.telematics import coverage_finalization as cf  # noqa: E402
from ops import recover_telematics_trips_window as rc  # noqa: E402
from ops.tests_manual.telematics_execution_outcome_fixtures import (  # noqa: E402
    committed_outcome,
)
from ops.tests_manual.postgres_dsn_safety import (  # noqa: E402
    require_loopback_dsn_or_exit,
)

ENV = "TELEMATICS_C11_RECOVERY_TEST_DSN"

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

CID = "6018be20-5faa-41b6-89c9-fe2b54a8283e"
SID = "1a3102bf-62d3-415c-8697-d5ee87bc415b"
CODE = "BRAVO00016"
PLATFORM_UUID = "52517750-7438-4558-8490-2736ae4cc629"
MODE = cf.TRIPS_PAGINATION_MODE_DATA_INVARIANTS_V1

A = datetime(2026, 7, 1, tzinfo=timezone.utc)
W = datetime(2026, 7, 27, tzinfo=timezone.utc)
E = datetime(2026, 8, 3, tzinfo=timezone.utc)
SEEDED = datetime(2026, 8, 3, 8, 7, 48, tzinfo=timezone.utc)
FAILED_FIRE = datetime(2026, 8, 3, tzinfo=timezone.utc)
EVIDENCE = "telematics-coverage-bootstrap/1:sha256=" + ("f3" * 32) + ":approval=T-1"

HEAD = "0" * 39 + "1"


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

def bootstrap(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS workflow_a_control CASCADE")
        cur.execute("DROP SCHEMA IF EXISTS ops_control CASCADE")
        cur.execute("DROP TABLE IF EXISTS public.schema_migrations")
        cur.execute("DROP TABLE IF EXISTS public.runs")
        for name in MIGRATIONS:
            cur.execute((ROOT / "db/migrations" / name).read_text(encoding="utf-8"))
        cur.execute(
            "CREATE TABLE public.schema_migrations ("
            " filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        cur.executemany(
            "INSERT INTO public.schema_migrations(filename) VALUES (%s)",
            [(name,) for name in MIGRATIONS],
        )
        cur.execute(
            "CREATE TABLE public.runs (run_id UUID PRIMARY KEY, status TEXT NOT NULL)"
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
    reset(conn)


def reset(conn, *, mode=MODE, w=W, status="READY", source="bootstrap") -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM workflow_a_control.client_dataset_recovery_run")
        cur.execute("DELETE FROM workflow_a_control.client_schedule_run_history")
        cur.execute("DELETE FROM workflow_a_control.client_dataset_coverage")
        cur.execute("DELETE FROM workflow_a_control.client_dataset_schedule")
        cur.execute("DELETE FROM workflow_a_control.client_account")
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
            VALUES (%s,%s,'Test','telematics','https://example.invalid','u','REF',
                    '127.0.0.1',5432,'db','u','REF','public','speeding',true,%s,
                    10800,3600,2678400)
            """,
            (CID, CODE, mode),
        )
        cur.execute(
            """
            INSERT INTO workflow_a_control.client_dataset_schedule
              (schedule_id, client_id, client_code, dataset_name, enabled,
               frequency, day_of_week, run_time, timezone, lookback_days,
               overwrite_existing)
            VALUES (%s,%s,%s,'trips_sync',true,'weekly',0,'02:00',
                    'Europe/Warsaw',7,true)
            """,
            (SID, CID, CODE),
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
                (SID, CID, CODE, A, w, status, EVIDENCE, SEEDED, source, SEEDED),
            )
        # The immutable historical evidence: the failed compatibility fire.
        cur.execute(
            """
            INSERT INTO workflow_a_control.client_schedule_run_history
              (schedule_id, client_id, client_code, dataset_name,
               window_start_ts, window_end_ts, scheduled_fire_ts, status,
               started_at, finished_at, error_summary)
            VALUES (%s,%s,%s,'trips_sync',%s,%s,%s,'FAILED',%s,%s,
                    'PAGINATION_MISMATCH')
            """,
            (SID, CID, CODE, W, E, FAILED_FIRE, FAILED_FIRE, FAILED_FIRE),
        )
    conn.commit()


def snapshot(conn) -> dict:
    """A comparable image of every surface this tool could possibly touch."""
    out = {}
    with conn.cursor() as cur:
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


def coverage(conn) -> dict:
    with conn.cursor(row_factory=_dict_row()) as cur:
        cur.execute(
            "SELECT schedule_id::text AS schedule_id, client_id::text AS client_id,"
            " client_code, dataset_name, coverage_start_ts, covered_through_ts,"
            " bootstrap_status, bootstrap_evidence_ref, covered_through_source,"
            " seeded_at, seeded_by, last_gap_detected_ts, updated_at"
            " FROM workflow_a_control.client_dataset_coverage WHERE schedule_id=%s",
            (SID,),
        )
        row = dict(cur.fetchone())
    conn.rollback()
    return row


def recoveries(conn) -> list:
    with conn.cursor(row_factory=_dict_row()) as cur:
        cur.execute(
            "SELECT * FROM workflow_a_control.client_dataset_recovery_run"
            " ORDER BY created_at"
        )
        rows = [dict(r) for r in cur.fetchall()]
    conn.rollback()
    return rows


def _dict_row():
    from psycopg.rows import dict_row
    return dict_row


def args(dsn: str, *, execute=False, **overrides):
    base = {
        "client-code": CODE,
        "dataset": "trips_sync",
        "window-start": "2026-07-27T00:00:00Z",
        "window-end": "2026-08-03T00:00:00Z",
        "expected-old-covered-through": "2026-07-27T00:00:00Z",
        "reason": "recover the failed compatibility interval",
        "approval-ref": "TELEMATICS-C11-1",
        "expected-environment": "production",
        "expected-platform-uuid": PLATFORM_UUID,
        "dsn": dsn,
    }
    base.update({k.replace("_", "-"): v for k, v in overrides.items()})
    argv = []
    for key, value in base.items():
        argv += [f"--{key}", str(value)]
    if execute:
        argv += ["--execute", "--confirm-client-code", CODE]
    return rc.build_parser().parse_args(argv)


class FakeLaunch:
    """Deterministic stand-in for the one business subprocess."""

    def __init__(self, *, returncode=0, raises=None, side_effect=None,
                 platform_run_id=None, outcome_builder=None,
                 schedule_id=SID):
        self.returncode = returncode
        self.raises = raises
        self.side_effect = side_effect
        # A real `ops/runner.py` always writes the run-id file and the real job
        # binds that same id into its terminal record, so the default double
        # reports one identity on both sides. Coverage-eligible proof must carry
        # an exact match, and a double defaulting to None would assert behavior
        # the real path never produces. Explicit values still override.
        self.platform_run_id = platform_run_id or str(uuid.uuid4())
        # A stand-in must now also produce the structured terminal record; the
        # return code alone no longer advances coverage.
        self.outcome_builder = outcome_builder or committed_outcome
        self.schedule_id = schedule_id
        self.calls = []
        self.authorities = []

    def __call__(self, *, job_params, authority=None):
        self.calls.append(job_params)
        self.authorities.append(authority)
        if self.side_effect is not None:
            self.side_effect()
        if self.raises is not None:
            raise self.raises
        now = datetime.now(timezone.utc)
        outcome = (
            None if self.returncode != 0
            else self.outcome_builder(
                job_params,
                schedule_id=self.schedule_id,
                platform_run_id=self.platform_run_id,
            )
        )
        return {
            "returncode": self.returncode,
            "platform_run_id": self.platform_run_id,
            "started_at": now,
            "finished_at": now,
            "duration_seconds": 1,
            "stderr_tail": "" if self.returncode == 0 else "PAGINATION_MISMATCH",
            "sanitized_command": "python ops/runner.py <module> <params>",
            "execution_outcome": outcome,
            "execution_outcome_error": None,
        }


def with_mocked_execution(fn):
    """Patch the subprocess launch and the repository-clean requirement."""
    def wrapper(conn, dsn, launcher):
        original_launch, original_repo = rc.launch_sync, rc.repository_state
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


# ---------------------------------------------------------------------------
# 1. Migration 058 shape
# ---------------------------------------------------------------------------

def test_migration_shape(conn) -> None:
    with conn.cursor(row_factory=_dict_row()) as cur:
        cur.execute(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns"
            " WHERE table_schema='workflow_a_control'"
            "   AND table_name='client_dataset_recovery_run'"
        )
        cols = {r["column_name"]: r for r in cur.fetchall()}
    required_not_null = {
        "recovery_run_id", "client_id", "schedule_id", "dataset_name",
        "window_start_ts", "window_end_ts", "expected_old_covered_through_ts",
        "status", "reason", "approval_ref", "repository_head",
        "pagination_mode", "stabilization_delay_seconds", "overlap_seconds",
        "max_recovery_span_seconds", "initial_coverage_snapshot",
        "initial_coverage_fingerprint", "created_at", "updated_at",
    }
    nullable = {
        "client_code", "platform_run_id", "provider_summary", "job_summary",
        "finalizer_result", "final_coverage_fingerprint", "error_classification",
        "error_summary", "started_at", "finished_at",
    }
    assert required_not_null | nullable == set(cols), set(cols)
    for name in required_not_null:
        assert cols[name]["is_nullable"] == "NO", name
    for name in nullable:
        assert cols[name]["is_nullable"] == "YES", name

    with conn.cursor(row_factory=_dict_row()) as cur:
        cur.execute(
            "SELECT conname, confdeltype FROM pg_constraint"
            " WHERE conrelid='workflow_a_control.client_dataset_recovery_run'::regclass"
            "   AND contype='f'"
        )
        fks = {r["conname"]: r["confdeltype"] for r in cur.fetchall()}
        # 'r' = RESTRICT. Operational evidence is never erased by a cascade.
        assert set(fks) == {
            "fk_client_dataset_recovery_run_client",
            "fk_client_dataset_recovery_run_schedule",
        }, fks
        assert set(fks.values()) == {"r"}, fks

        # 058 creates no trigger on either table it touches. In particular
        # nothing may mutate coverage as a side effect of writing evidence.
        cur.execute(
            "SELECT count(*) AS n FROM pg_trigger t"
            " JOIN pg_class c ON c.oid=t.tgrelid"
            " JOIN pg_namespace n ON n.oid=c.relnamespace"
            " WHERE n.nspname='workflow_a_control' AND NOT t.tgisinternal"
            "   AND c.relname IN ('client_dataset_recovery_run',"
            "                     'client_dataset_coverage')"
        )
        assert cur.fetchone()["n"] == 0, "058 must create no trigger"

        cur.execute(
            "SELECT indexname FROM pg_indexes"
            " WHERE schemaname='workflow_a_control'"
            "   AND tablename='client_dataset_recovery_run'"
        )
        names = {r["indexname"] for r in cur.fetchall()}
        assert "uq_client_dataset_recovery_run_approved_window" in names
        assert "uq_client_dataset_recovery_run_active" in names
    conn.rollback()


def _insert_recovery(conn, **overrides) -> None:
    row = {
        "client_id": CID, "client_code": CODE, "schedule_id": SID,
        "dataset_name": "trips_sync", "window_start_ts": W, "window_end_ts": E,
        "expected_old_covered_through_ts": W, "status": "RUNNING",
        "reason": "r", "approval_ref": "T-1", "repository_head": HEAD,
        "pagination_mode": MODE, "stabilization_delay_seconds": 10800,
        "overlap_seconds": 3600, "max_recovery_span_seconds": 2678400,
        "initial_coverage_snapshot": json.dumps({"k": "v"}),
        "initial_coverage_fingerprint": "a" * 64,
        "started_at": SEEDED, "finished_at": None,
        "error_classification": None,
    }
    row.update(overrides)
    cols = ", ".join(row)
    vals = ", ".join(f"%({k})s" for k in row)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO workflow_a_control.client_dataset_recovery_run "
            f"({cols}) VALUES ({vals})",
            row,
        )


def test_migration_constraints(conn) -> None:
    import psycopg

    reset(conn)
    # The widened source vocabulary accepts `manual_recovery` and nothing else.
    for source, ok in (
        ("manual_recovery", True), ("scheduled_run", True),
        ("bootstrap", True), ("operator", True), ("guessed", False),
    ):
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE workflow_a_control.client_dataset_coverage"
                    " SET covered_through_source=%s WHERE schedule_id=%s",
                    (source, SID),
                )
            assert ok, source
            conn.rollback()
        except psycopg.errors.CheckViolation:
            assert not ok, source
            conn.rollback()

    rejected = (
        ({"dataset_name": "fuel_daily_aggregation"}, "dataset"),
        ({"pagination_mode": "strict_meta"}, "mode"),
        ({"window_end_ts": W - timedelta(days=1)}, "inverted window"),
        ({"expected_old_covered_through_ts": A}, "unanchored window"),
        ({"status": "PARTIAL"}, "unknown status"),
        ({"status": "SUCCESS", "finished_at": None}, "terminal without finish"),
        ({"status": "FAILED", "finished_at": SEEDED}, "terminal without reason"),
        ({"reason": "   "}, "blank reason"),
        ({"repository_head": "abc"}, "short head"),
        ({"initial_coverage_fingerprint": "XYZ"}, "bad fingerprint"),
        ({"max_recovery_span_seconds": 2678401}, "span above the provider limit"),
        (
            {"status": "RUNNING", "final_coverage_fingerprint": "b" * 64},
            "non-success carrying success evidence",
        ),
    )
    for overrides, label in rejected:
        try:
            _insert_recovery(conn, **overrides)
            conn.rollback()
            raise AssertionError(f"expected a constraint violation: {label}")
        except (psycopg.errors.CheckViolation, psycopg.errors.NotNullViolation):
            conn.rollback()

    # Only one non-terminal recovery per schedule, enforced by the database.
    _insert_recovery(conn)
    try:
        _insert_recovery(conn, approval_ref="T-2")
        conn.rollback()
        raise AssertionError("a second active recovery must be impossible")
    except psycopg.errors.UniqueViolation:
        conn.rollback()

    # The same approved window is executed exactly once.
    _insert_recovery(
        conn, status="FAILED", finished_at=SEEDED,
        error_classification=rc.RECOVERY_BUSINESS_FAILED,
    )
    try:
        _insert_recovery(
            conn, status="FAILED", finished_at=SEEDED,
            error_classification=rc.RECOVERY_BUSINESS_FAILED,
        )
        conn.rollback()
        raise AssertionError("a duplicate approved window must be impossible")
    except psycopg.errors.UniqueViolation:
        conn.rollback()

    # Evidence survives an attempt to delete the parent schedule.
    _insert_recovery(
        conn, status="SUCCESS", finished_at=SEEDED,
        final_coverage_fingerprint="b" * 64,
    )
    conn.commit()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM workflow_a_control.client_dataset_schedule"
                " WHERE schedule_id=%s", (SID,),
            )
        raise AssertionError("recovery evidence must block a schedule delete")
    except psycopg.errors.ForeignKeyViolation:
        conn.rollback()
    reset(conn)


# ---------------------------------------------------------------------------
# 2. Dry-run
# ---------------------------------------------------------------------------

def test_dry_run_is_byte_identical(conn, dsn) -> None:
    reset(conn)
    before = snapshot(conn)
    code, plan = rc.run(args(dsn))
    after = snapshot(conn)

    assert code == rc.EXIT_OK
    assert before == after, "a dry-run must leave the database byte-identical"
    assert plan["mode"] == "DRY_RUN"
    assert plan["database_writes_performed"] == 0
    assert plan["provider_requests"] == 0
    assert plan["business_subprocesses_launched"] == 0
    assert plan["planned_recovery_executions"] == 1
    assert plan["schedule_history_rows_created"] == 0
    assert plan["schedule_history_rows_modified"] == 0
    assert plan["automatic_retries"] == 0
    assert plan["would_advance_covered_through_to"] == "2026-08-03T00:00:00Z"
    assert plan["would_set_covered_through_source"] == "manual_recovery"
    assert plan["coverage_start_ts_written"] is False
    assert plan["bootstrap_status_written"] is False
    assert plan["client_id"] == CID and plan["schedule_id"] == SID
    assert plan["sole_compatibility_client"] is True
    assert plan["initial_coverage_fingerprint"] == cf.coverage_fingerprint(
        coverage(conn)
    )

    # Safe logs: no credential, DSN or provider payload reaches the plan.
    serialized = json.dumps(plan, sort_keys=True, default=str).lower()
    for leaked in ("password", "secret", "dbname=", "host=", "provider_basic_auth"):
        assert leaked not in serialized, leaked


def test_dry_run_gates(conn, dsn) -> None:
    cases = (
        ({"window_start": "2026-07-26T00:00:00Z"}, "RECOVERY_REFUSED_WINDOW"),
        ({"window_end": "2026-07-27T00:00:00Z"}, "RECOVERY_REFUSED_WINDOW"),
        ({"window_end": "2026-07-20T00:00:00Z"}, "RECOVERY_REFUSED_WINDOW"),
        ({"window_end": "2099-01-01T00:00:00Z"}, "RECOVERY_REFUSED_WINDOW"),
        (
            {"expected_old_covered_through": "2026-07-26T00:00:00Z"},
            "RECOVERY_REFUSED_WATERMARK",
        ),
        ({"dataset": "fuel_daily_aggregation"}, "RECOVERY_REFUSED_TARGET"),
        ({"client_code": "NOPE00001"}, "RECOVERY_REFUSED_TARGET"),
        (
            {"expected_platform_uuid": "db8055e0-e030-4d5a-816b-ec4dc338d698"},
            "RECOVERY_REFUSED_IDENTITY",
        ),
        ({"expected_environment": "local_dev"}, "RECOVERY_REFUSED_IDENTITY"),
    )
    for overrides, expected in cases:
        reset(conn)
        before = snapshot(conn)
        try:
            rc.run(args(dsn, **overrides))
            raise AssertionError(f"expected {expected} for {overrides}")
        except rc.RecoveryRefused as exc:
            assert exc.code == expected, (overrides, exc.code)
        assert snapshot(conn) == before

    # A window that exceeds the client's recovery span cap.
    reset(conn)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE workflow_a_control.client_account"
            " SET trips_max_recovery_span_seconds=3600 WHERE client_id=%s", (CID,),
        )
    conn.commit()
    try:
        rc.run(args(dsn))
        raise AssertionError("expected a span refusal")
    except rc.RecoveryRefused as exc:
        assert exc.code == "RECOVERY_REFUSED_WINDOW"

    # A window ending inside the stabilization delay is refused before the
    # provider would ever be asked.
    reset(conn)
    now_ts = datetime.now(timezone.utc).replace(microsecond=0)
    recent = now_ts - timedelta(seconds=60)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE workflow_a_control.client_dataset_coverage"
            " SET covered_through_ts=%s WHERE schedule_id=%s",
            (recent - timedelta(days=1), SID),
        )
    conn.commit()
    try:
        rc.run(args(
            dsn,
            window_start=_iso(recent - timedelta(days=1)),
            expected_old_covered_through=_iso(recent - timedelta(days=1)),
            window_end=_iso(recent),
        ))
        raise AssertionError("expected a stabilization refusal")
    except rc.RecoveryRefused as exc:
        assert exc.code == "RECOVERY_REFUSED_WINDOW"

    # Strict client, non-READY coverage and missing coverage.
    for kwargs, expected in (
        ({"mode": "strict_meta"}, "RECOVERY_REFUSED_MODE"),
        ({"status": "GAP_DETECTED"}, "RECOVERY_REFUSED_COVERAGE"),
        ({"status": "RESEED_REQUIRED"}, "RECOVERY_REFUSED_COVERAGE"),
        ({"status": None}, "RECOVERY_REFUSED_COVERAGE"),
    ):
        reset(conn, **kwargs)
        before = snapshot(conn)
        try:
            rc.run(args(dsn))
            raise AssertionError(f"expected {expected}")
        except rc.RecoveryRefused as exc:
            assert exc.code == expected, (kwargs, exc.code)
        assert snapshot(conn) == before

    # Concurrency: a RUNNING scheduled fire and a non-terminal recovery.
    reset(conn)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO workflow_a_control.client_schedule_run_history"
            " (schedule_id, client_id, client_code, dataset_name,"
            "  window_start_ts, window_end_ts, scheduled_fire_ts, status, started_at)"
            " VALUES (%s,%s,%s,'trips_sync',%s,%s,%s,'RUNNING',%s)",
            (SID, CID, CODE, W, E, FAILED_FIRE + timedelta(days=7), SEEDED),
        )
    conn.commit()
    try:
        rc.run(args(dsn))
        raise AssertionError("expected a concurrency refusal")
    except rc.RecoveryRefused as exc:
        assert exc.code == "RECOVERY_REFUSED_CONCURRENCY"

    reset(conn)
    _insert_recovery(conn)
    conn.commit()
    try:
        rc.run(args(dsn))
        raise AssertionError("expected a concurrency refusal")
    except rc.RecoveryRefused as exc:
        assert exc.code == "RECOVERY_REFUSED_CONCURRENCY"

    # A terminal recovery for the same approved window is never repeated.
    reset(conn)
    _insert_recovery(
        conn, approval_ref="TELEMATICS-C11-1", status="FAILED",
        finished_at=SEEDED, error_classification=rc.RECOVERY_BUSINESS_FAILED,
    )
    conn.commit()
    try:
        rc.run(args(dsn))
        raise AssertionError("expected a duplicate refusal")
    except rc.RecoveryRefused as exc:
        assert exc.code == "RECOVERY_REFUSED_DUPLICATE"
    reset(conn)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def test_execute_requires_confirmation(conn, dsn) -> None:
    reset(conn)
    before = snapshot(conn)
    parsed = rc.build_parser().parse_args(
        [a for a in _argv(dsn)] + ["--execute"]
    )
    try:
        rc.run(parsed)
        raise AssertionError("expected a confirmation refusal")
    except rc.RecoveryRefused as exc:
        assert exc.code == "RECOVERY_REFUSED_CONFIRMATION"
    assert snapshot(conn) == before


def _argv(dsn: str) -> list:
    return [
        "--client-code", CODE, "--dataset", "trips_sync",
        "--window-start", "2026-07-27T00:00:00Z",
        "--window-end", "2026-08-03T00:00:00Z",
        "--expected-old-covered-through", "2026-07-27T00:00:00Z",
        "--reason", "recover", "--approval-ref", "TELEMATICS-C11-1",
        "--expected-environment", "production",
        "--expected-platform-uuid", PLATFORM_UUID, "--dsn", dsn,
    ]


# ---------------------------------------------------------------------------
# 3. Execution paths
# ---------------------------------------------------------------------------

@with_mocked_execution
def test_execute_success(conn, dsn, launcher) -> None:
    reset(conn)
    history_before = snapshot(conn)["client_schedule_run_history"]
    client_before = snapshot(conn)["client_account"]
    schedule_before = snapshot(conn)["client_dataset_schedule"]

    code, plan = rc.run(args(dsn, execute=True))

    assert code == rc.EXIT_OK
    assert plan["recovery_status"] == "SUCCESS"
    assert plan["coverage_advanced"] is True

    # Exactly one business execution, with the literal window and the mode.
    assert len(launcher.calls) == 1
    params = launcher.calls[0]
    assert params["window_start_ts"] == "2026-07-27T00:00:00Z"
    assert params["window_end_ts"] == "2026-08-03T00:00:00Z"
    assert params["trips_pagination_mode"] == MODE
    assert params["trigger"] == "MANUAL_RECOVERY"

    # W advanced exactly once; A, status and gap history untouched.
    cov = coverage(conn)
    assert cov["covered_through_ts"] == E
    assert cov["covered_through_source"] == "manual_recovery"
    assert cov["coverage_start_ts"] == A
    assert cov["bootstrap_status"] == "READY"
    assert cov["last_gap_detected_ts"] is None
    assert cov["bootstrap_evidence_ref"] == EVIDENCE
    assert cov["seeded_at"] == SEEDED and cov["seeded_by"] == "operator"
    assert cov["updated_at"] != SEEDED
    assert plan["final_coverage_fingerprint"] == cf.coverage_fingerprint(cov)
    assert plan["finalizer_result"]["moved"] is True
    assert plan["finalizer_result"]["rows_updated"] == 1

    # Separate, durable recovery identity — and no schedule-history row.
    rows = recoveries(conn)
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "SUCCESS"
    assert str(row["client_id"]) == CID and str(row["schedule_id"]) == SID
    assert row["window_start_ts"] == W and row["window_end_ts"] == E
    assert row["expected_old_covered_through_ts"] == W
    assert row["repository_head"] == HEAD
    assert row["approval_ref"] == "TELEMATICS-C11-1"
    assert row["initial_coverage_fingerprint"] != row["final_coverage_fingerprint"]
    assert row["error_classification"] is None
    assert row["job_summary"]["automatic_retries"] == 0
    assert str(row["recovery_run_id"]) == plan["recovery_run_id"]

    after = snapshot(conn)
    assert after["client_schedule_run_history"] == history_before, (
        "the failed scheduled fire is immutable historical evidence"
    )
    assert after["client_account"] == client_before
    assert after["client_dataset_schedule"] == schedule_before

    # No evidence is stored that could carry personal or provider payloads.
    stored = json.dumps(row, sort_keys=True, default=str).lower()
    for leaked in ("password", "secret", "registration", "driver"):
        assert leaked not in stored, leaked


@with_mocked_execution
def test_execute_business_failure(conn, dsn, launcher) -> None:
    reset(conn)
    before = coverage(conn)
    history_before = snapshot(conn)["client_schedule_run_history"]

    code, plan = rc.run(args(dsn, execute=True))

    assert code == rc.EXIT_BUSINESS_FAILED
    assert plan["recovery_status"] == "FAILED"
    assert plan["coverage_advanced"] is False
    assert plan["coverage_unchanged"] is True
    assert plan["error_classification"] == rc.RECOVERY_BUSINESS_FAILED
    assert coverage(conn) == before, "a failure never advances W"
    assert snapshot(conn)["client_schedule_run_history"] == history_before

    rows = recoveries(conn)
    assert len(rows) == 1 and rows[0]["status"] == "FAILED"
    assert rows[0]["final_coverage_fingerprint"] is None
    assert rows[0]["finalizer_result"] is None
    assert "PAGINATION_MISMATCH" in rows[0]["error_summary"]
    assert len(launcher.calls) == 1, "no automatic retry"


@with_mocked_execution
def test_execute_orchestration_failure(conn, dsn, launcher) -> None:
    reset(conn)
    before = coverage(conn)
    code, plan = rc.run(args(dsn, execute=True))
    assert code == rc.EXIT_BUSINESS_FAILED
    assert plan["error_classification"] == rc.RECOVERY_ORCHESTRATION_FAILED
    assert coverage(conn) == before
    rows = recoveries(conn)
    assert len(rows) == 1 and rows[0]["status"] == "FAILED"


@with_mocked_execution
def test_execute_finalization_conflict(conn, dsn, launcher) -> None:
    """The business work succeeded, but an operator moved W underneath it."""
    reset(conn)
    code, plan = rc.run(args(dsn, execute=True))

    assert code == rc.EXIT_FINALIZATION_CONFLICT
    assert plan["recovery_status"] == "FINALIZATION_CONFLICT"
    assert plan["error_classification"] == cf.TRIPS_COVERAGE_ADVANCE_CONFLICT
    assert plan["coverage_advanced"] is False

    cov = coverage(conn)
    assert cov["covered_through_source"] == "operator", (
        "the concurrent operator value must not be overwritten"
    )
    assert cov["covered_through_ts"] == W
    assert cov["coverage_start_ts"] == A

    rows = recoveries(conn)
    assert len(rows) == 1
    assert rows[0]["status"] == "FINALIZATION_CONFLICT"
    assert rows[0]["error_classification"] == cf.TRIPS_COVERAGE_ADVANCE_CONFLICT
    assert rows[0]["final_coverage_fingerprint"] is None
    assert len(launcher.calls) == 1, "no automatic retry after a conflict"


@with_mocked_execution
def test_duplicate_execute_is_rejected(conn, dsn, launcher) -> None:
    reset(conn)
    code, _ = rc.run(args(dsn, execute=True))
    assert code == rc.EXIT_OK
    coverage_after_success = coverage(conn)

    # A second run of the same approved window: the watermark gate fires first,
    # and even after restoring W the duplicate-approval gate refuses.
    try:
        rc.run(args(dsn, execute=True))
        raise AssertionError("expected a refusal")
    except rc.RecoveryRefused as exc:
        assert exc.code == "RECOVERY_REFUSED_WATERMARK"
    assert coverage(conn) == coverage_after_success

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE workflow_a_control.client_dataset_coverage"
            " SET covered_through_ts=%s, covered_through_source='bootstrap'"
            " WHERE schedule_id=%s", (W, SID),
        )
    conn.commit()
    try:
        rc.run(args(dsn, execute=True))
        raise AssertionError("expected a duplicate refusal")
    except rc.RecoveryRefused as exc:
        assert exc.code == "RECOVERY_REFUSED_DUPLICATE"
    assert len(recoveries(conn)) == 1
    assert len(launcher.calls) == 1, "the business job runs at most once"


# ---------------------------------------------------------------------------

def test_on_disposable_postgres(dsn: str) -> None:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row, autocommit=False) as conn:
        bootstrap(conn)
        test_migration_shape(conn)
        test_migration_constraints(conn)
        test_dry_run_is_byte_identical(conn, dsn)
        test_dry_run_gates(conn, dsn)
        test_execute_requires_confirmation(conn, dsn)
        test_execute_success(conn, dsn, FakeLaunch(returncode=0))
        test_execute_business_failure(conn, dsn, FakeLaunch(returncode=1))
        test_execute_orchestration_failure(
            conn, dsn, FakeLaunch(raises=OSError("cannot spawn")),
        )

        def steal_watermark():
            with psycopg.connect(dsn, autocommit=True) as other:
                other.execute(
                    "UPDATE workflow_a_control.client_dataset_coverage"
                    " SET covered_through_source='operator' WHERE schedule_id=%s",
                    (SID,),
                )

        test_execute_finalization_conflict(
            conn, dsn, FakeLaunch(returncode=0, side_effect=steal_watermark),
        )
        test_duplicate_execute_is_rejected(conn, dsn, FakeLaunch(returncode=0))
        conn.rollback()


def main() -> None:
    dsn = os.getenv(ENV)
    if not dsn:
        print(f"SKIP: set {ENV} to a disposable PostgreSQL 16 DSN")
        return
    # Destructive: this suite drops schemas and applies migrations. Prove the
    # target is loopback-only before opening a connection.
    require_loopback_dsn_or_exit(dsn, label=ENV)
    test_on_disposable_postgres(dsn)
    print("OK - Telematics manual recovery PostgreSQL checks passed")


if __name__ == "__main__":
    main()
